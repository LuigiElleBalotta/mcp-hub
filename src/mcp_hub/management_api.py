from __future__ import annotations

import asyncio
import json
import os
import time
from fnmatch import fnmatchcase
from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from dataclasses import asdict

from mcp_hub.config import BUILTIN_RIZZO, CONCURRENCY_MODES, CONFIG_PATH, ServerConfig, valid_group_name, rizzo_settings_from_dict, save_config
from mcp_hub.filters import PREFIX_RE, is_glob, tool_visible
from mcp_hub.manager import HubManager


def management_routes(
    manager: HubManager,
    shutdown_event: asyncio.Event | None = None,
    on_change: Callable[[], None] | None = None,
) -> list[Route]:
    # `on_change` is `hub_app.build_app`'s `rebuild_routes`: called after
    # anything that can change WHICH servers have a mounted SSE route
    # (upsert/remove/reload), so the hub's HTTP-level routing stays in sync
    # with `manager.config.servers` without a process restart. A no-op
    # default keeps every existing caller (tests included) working
    # unchanged.
    def _notify_change() -> None:
        if on_change is not None:
            on_change()

    async def status(request: Request) -> JSONResponse:
        body: dict = {"servers": manager.status_snapshot()}
        if manager.draining:
            body["draining"] = True  # additive: older clients only read "servers"
        return JSONResponse(body)

    async def settings(request: Request) -> JSONResponse:
        return JSONResponse({
            "checkForUpdates": manager.config.hub.checkForUpdates,
            "includeBetaUpdates": manager.config.hub.includeBetaUpdates,
        })

    async def update_settings(request: Request) -> JSONResponse:
        # Only the fields the running hub actually reads live are accepted
        # here -- host/port/authToken take effect only on the next hub
        # start (the listening socket is already bound) and are edited by
        # the GUI writing config.json directly instead (see settings_dialog.py).
        body = await request.json()
        if "checkForUpdates" in body:
            manager.config.hub.checkForUpdates = bool(body["checkForUpdates"])
        if "includeBetaUpdates" in body:
            manager.config.hub.includeBetaUpdates = bool(body["includeBetaUpdates"])
        save_config(manager.config)
        return JSONResponse({
            "checkForUpdates": manager.config.hub.checkForUpdates,
            "includeBetaUpdates": manager.config.hub.includeBetaUpdates,
        })

    async def pid(request: Request) -> JSONResponse:
        return JSONResponse({"pid": os.getpid()})

    async def _optional_json(request: Request) -> dict:
        """An optional JSON object body: absent or empty means {}."""
        raw = await request.body()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _timeout_from(data: dict, key: str, default: float) -> float:
        value = data.get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 3600:
            raise ValueError(f"{key} must be a number of seconds between 0 and 3600")
        return float(value)

    async def drain(request: Request) -> JSONResponse:
        """Stop accepting new MCP traffic and wait (up to `timeout` seconds) for
        requests in flight. The hub keeps running; DELETE resumes."""
        try:
            timeout = _timeout_from(await _optional_json(request), "timeout", manager.config.hub.drainTimeout)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        drained = await manager.drain(timeout)
        return JSONResponse({"drained": drained, "inFlight": manager.in_flight()})

    async def resume(request: Request) -> JSONResponse:
        manager.resume()
        return JSONResponse({"draining": False})

    background: set[asyncio.Task] = set()

    async def shutdown(request: Request) -> JSONResponse:
        # Graceful: lets `serve_with_managed_shutdown`'s `finally` stop every
        # managed subprocess before the process exits, instead of a caller
        # killing the hub's OS process directly and orphaning them. Used by
        # the GUI's self-update flow, which needs the hub's exe file
        # unlocked (process exited) before it can be replaced on disk.
        # Replies at once and does the draining in the background, so a caller with
        # a short HTTP timeout (the GUI's) is not left waiting: new MCP traffic is
        # refused from now, requests in flight get `drainTimeout` seconds to
        # finish, then the hub exits.
        try:
            timeout = _timeout_from(await _optional_json(request), "drainTimeout", manager.config.hub.drainTimeout)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if shutdown_event is not None:
            async def finish() -> None:
                try:
                    await manager.drain(timeout)
                finally:
                    shutdown_event.set()

            task = asyncio.create_task(finish())
            background.add(task)
            task.add_done_callback(background.discard)
        return JSONResponse({"status": "shutting down", "drainTimeout": timeout})

    def _unknown(name: str) -> JSONResponse:
        return JSONResponse({"error": f"unknown server: {name}"}, status_code=404)

    async def start(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            server = manager.get(name)
        except KeyError:
            return _unknown(name)
        if not server.config.enabled and not server.config.is_service:
            # An MCP server's endpoints are only served while it is enabled, so a process
            # started anyway would run but answer 404 to every client. (A group's start
            # already refuses it the same way.)
            return JSONResponse({
                "error": f"server '{name}' is disabled: enable it first (its endpoints are not served while it is disabled)",
                "status": server.status,
            }, status_code=409)
        await server.start()
        return JSONResponse({"status": server.status})

    async def stop(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            server = manager.get(name)
        except KeyError:
            return _unknown(name)
        await server.stop()
        return JSONResponse({"status": server.status})

    async def logs(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            server = manager.get(name)
        except KeyError:
            return _unknown(name)
        return JSONResponse({"lines": list(server.logs)})

    async def server_metrics(request: Request) -> JSONResponse:
        """Dashboard detail of one server: counters, latency, recent calls.
        Method names and durations only, never arguments or results."""
        name = request.path_params["name"]
        try:
            server = manager.get(name)
        except KeyError:
            return _unknown(name)
        return JSONResponse({
            **server.metrics_detail(),
            "enabled": server.metrics.enabled,
            "recent": server.metrics.recent(),
        })

    async def server_tools(request: Request) -> JSONResponse:
        """The server's RAW tools with what the client-facing filters make of each
        (`visible`, and the `clientName` a client would call it by)."""
        name = request.path_params["name"]
        try:
            server = manager.get(name)
        except KeyError:
            return _unknown(name)
        try:
            tools, source = await server.fetch_tools()
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ConnectionError, asyncio.TimeoutError, OSError) as exc:
            return JSONResponse({"error": str(exc) or "the server did not answer"}, status_code=502)
        cfg = server.config
        shaped = []
        for tool in tools:
            tool_name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(tool_name, str):
                continue
            visible = tool_visible(tool_name, cfg.toolsAllow, cfg.toolsDeny)
            shaped.append({
                "name": tool_name,
                "clientName": f"{cfg.toolPrefix}{tool_name}",
                "description": tool.get("description") or "",
                "inputSchema": tool.get("inputSchema") or {},
                "readOnly": bool((tool.get("annotations") or {}).get("readOnlyHint")),
                "visible": visible,
                # hidden by a deny GLOB: deny wins, so an exact-name edit cannot undo it
                # (an exact name in toolsDeny, by contrast, is simply removed).
                "lockedByPattern": not visible and any(is_glob(p) and fnmatchcase(tool_name, p) for p in cfg.toolsDeny),
            })
        return JSONResponse({
            "tools": shaped, "source": source, "filtered": cfg.has_tool_filter,
            "toolsAllow": list(cfg.toolsAllow), "toolsDeny": list(cfg.toolsDeny), "toolPrefix": cfg.toolPrefix,
        })

    async def set_filters(request: Request) -> JSONResponse:
        """Changes a server's tool filters in place: no restart, so connected
        sessions are kept; clients are told the tool list changed."""
        name = request.path_params["name"]
        try:
            server = manager.get(name)
        except KeyError:
            return _unknown(name)
        if name == BUILTIN_RIZZO or server.config.is_service:
            return JSONResponse({"error": "only MCP servers have tool filters"}, status_code=409)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        new = {}
        for key in ("toolsAllow", "toolsDeny"):
            value = body.get(key, getattr(server.config, key))
            if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
                return JSONResponse({"error": f"{key} must be a list of non-empty strings"}, status_code=400)
            new[key] = list(value)
        prefix = body.get("toolPrefix", server.config.toolPrefix)
        if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix):
            return JSONResponse({"error": "invalid toolPrefix"}, status_code=400)
        server.config.toolsAllow, server.config.toolsDeny, server.config.toolPrefix = (
            new["toolsAllow"], new["toolsDeny"], prefix)
        save_config(manager.config)
        await server.notify_tools_changed()
        for child in list(server.children):  # an isolated session's process holds a copy of the config
            child.config.toolsAllow, child.config.toolsDeny, child.config.toolPrefix = (
                new["toolsAllow"], new["toolsDeny"], prefix)
            await child.notify_tools_changed()
        return JSONResponse({"toolsAllow": new["toolsAllow"], "toolsDeny": new["toolsDeny"], "toolPrefix": prefix})

    async def call_tool(request: Request) -> JSONResponse:
        """Calls a tool from the GUI's tester. Goes through the server's guard and
        metrics like a client call. A tool that is not declared read-only needs
        `confirm: true`, so a stray request cannot run something with side effects."""
        name = request.path_params["name"]
        try:
            server = manager.get(name)
        except KeyError:
            return _unknown(name)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        tool = body.get("tool") if isinstance(body, dict) else None
        arguments = body.get("arguments", {}) if isinstance(body, dict) else None
        if not isinstance(tool, str) or not tool or not isinstance(arguments, dict):
            return JSONResponse({"error": "expected {tool: str, arguments?: object, confirm?: bool}"}, status_code=400)
        try:
            tools, _source = await server.fetch_tools()
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ConnectionError, asyncio.TimeoutError, OSError) as exc:
            return JSONResponse({"error": str(exc) or "the server did not answer"}, status_code=502)
        declared = next((t for t in tools if isinstance(t, dict) and t.get("name") == tool), None)
        if declared is None:
            return JSONResponse({"error": f"unknown tool: {tool}"}, status_code=404)
        read_only = bool((declared.get("annotations") or {}).get("readOnlyHint"))
        if not read_only and body.get("confirm") is not True:
            return JSONResponse(
                {"error": "this tool is not declared read-only: send confirm=true to run it", "readOnly": False},
                status_code=409,
            )
        started = time.monotonic()
        try:
            response = await server.call_tool(tool, arguments)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "the tool did not answer in time"}, status_code=504)
        except (ConnectionError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({
            "result": response.get("result"), "error": response.get("error"),
            "ms": round((time.monotonic() - started) * 1000, 1), "readOnly": read_only,
        })

    async def groups_list(request: Request) -> JSONResponse:
        return JSONResponse({"groups": {n: list(m) for n, m in manager.config.groups.items()}})

    async def group_put(request: Request) -> JSONResponse:
        """Creates or replaces a group. Members must be existing servers (the built-in
        Rizzo service is managed on its own tab and cannot be grouped)."""
        name = request.path_params["name"]
        if not valid_group_name(name):
            return JSONResponse({"error": "group name: 1-40 characters of letters, digits, space . _ -"}, status_code=400)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        members = body.get("servers") if isinstance(body, dict) else None
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            return JSONResponse({"error": "expected {servers: [name, ...]}"}, status_code=400)
        unknown = [m for m in members if m not in manager.config.servers]
        if unknown:
            return JSONResponse({"error": f"unknown server(s): {', '.join(unknown)}"}, status_code=400)
        manager.config.groups[name] = list(dict.fromkeys(members))  # no duplicates, order kept
        save_config(manager.config)
        return JSONResponse({"group": name, "servers": manager.config.groups[name]})

    async def group_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name not in manager.config.groups:
            return JSONResponse({"error": f"unknown group: {name}"}, status_code=404)
        del manager.config.groups[name]
        save_config(manager.config)
        return JSONResponse({"status": "removed"})

    async def group_action(request: Request) -> JSONResponse:
        """Starts or stops every server of a group, one after the other."""
        name = request.path_params["name"]
        action = request.path_params["action"]
        if name not in manager.config.groups:
            return JSONResponse({"error": f"unknown group: {name}"}, status_code=404)
        results: dict[str, str] = {}
        for member in manager.config.groups[name]:
            try:
                server = manager.get(member)
            except KeyError:
                results[member] = "missing"  # a server removed after it was grouped
                continue
            if action == "start":
                if not server.config.enabled:
                    results[member] = "disabled"
                    continue
                try:
                    await server.start()
                except OSError as exc:
                    results[member] = f"error: {exc}"
                    continue
            else:
                await server.stop()
            results[member] = server.status
        return JSONResponse({"results": results})

    def _rizzo_state() -> dict:
        snapshot = manager.status_snapshot().get(BUILTIN_RIZZO)
        return {"settings": asdict(manager.config.rizzo), "status": snapshot["status"] if snapshot else None}

    async def get_rizzo(request: Request) -> JSONResponse:
        return JSONResponse(_rizzo_state())

    async def put_rizzo(request: Request) -> JSONResponse:
        # The Rizzo Flow tab's settings: the hub rebuilds the built-in
        # service from them. A running server is stopped (new command line).
        try:
            settings = rizzo_settings_from_dict(await request.json())
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": f"invalid rizzo settings: {exc}"}, status_code=400)
        managed = await manager.apply_rizzo(settings)
        save_config(manager.config)
        if managed is not None and managed.config.starts_with_hub:
            await managed.start()
        return JSONResponse(_rizzo_state())

    async def upsert(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name == BUILTIN_RIZZO:
            return JSONResponse({"error": f"'{name}' is managed by the Rizzo Flow panel"}, status_code=409)
        body = await request.json()
        try:
            server_config = ServerConfig(**body)
        except TypeError as exc:
            return JSONResponse({"error": f"invalid server config: {exc}"}, status_code=400)
        if server_config.type not in ("mcp", "service"):
            return JSONResponse({"error": "type must be 'mcp' or 'service'"}, status_code=400)
        if server_config.is_service and not server_config.command:
            return JSONResponse({"error": "a service needs a command"}, status_code=400)
        if server_config.concurrency not in CONCURRENCY_MODES:
            return JSONResponse({"error": f"concurrency must be one of {', '.join(CONCURRENCY_MODES)}"}, status_code=400)
        if server_config.restart not in ("never", "on-failure"):
            return JSONResponse({"error": "restart must be 'never' or 'on-failure'"}, status_code=400)
        attempts = server_config.restartMaxAttempts
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            return JSONResponse({"error": "restartMaxAttempts must be an integer >= 1"}, status_code=400)
        window = server_config.restartWindow
        if not isinstance(window, (int, float)) or isinstance(window, bool) or window <= 0:
            return JSONResponse({"error": "restartWindow must be a number > 0"}, status_code=400)
        for key in ("toolsAllow", "toolsDeny"):
            patterns = getattr(server_config, key)
            if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
                return JSONResponse({"error": f"{key} must be a list of non-empty strings"}, status_code=400)
        prefix = server_config.toolPrefix
        if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix):
            return JSONResponse(
                {"error": "toolPrefix must be at most 64 characters of letters, digits, '_', '-' or '.'"},
                status_code=400,
            )
        if server_config.startMode not in ("eager", "lazy"):
            return JSONResponse({"error": "startMode must be 'eager' or 'lazy'"}, status_code=400)
        idle = server_config.idleTimeout
        if not isinstance(idle, (int, float)) or isinstance(idle, bool) or idle < 0:
            return JSONResponse({"error": "idleTimeout must be a number >= 0"}, status_code=400)
        if not isinstance(server_config.persistent, bool):
            return JSONResponse({"error": "persistent must be a boolean"}, status_code=400)
        interval = server_config.healthInterval
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval < 0:
            return JSONResponse({"error": "healthInterval must be a number >= 0"}, status_code=400)
        failures = server_config.healthFailures
        if not isinstance(failures, int) or isinstance(failures, bool) or failures < 1:
            return JSONResponse({"error": "healthFailures must be an integer >= 1"}, status_code=400)
        # HubManager.upsert stops any previously-running process for `name`
        # itself before replacing the entry (round-2 review fix), so the
        # freshly-returned `managed` here is a brand-new ManagedServer with
        # no old process to worry about -- just start it if enabled.
        managed = await manager.upsert(name, server_config)
        save_config(manager.config)
        if server_config.starts_with_hub:
            await managed.start()
        _notify_change()
        return JSONResponse({"status": managed.status})

    async def remove(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if name == BUILTIN_RIZZO:
            return JSONResponse({"error": f"'{name}' is managed by the Rizzo Flow panel"}, status_code=409)
        await manager.remove(name)
        save_config(manager.config)
        _notify_change()
        return JSONResponse({"status": "removed"})

    async def reload(request: Request) -> JSONResponse:
        # Picks up a config.json edited by hand (or by `mcp_hub apply`/
        # `import`, which both write the file directly without telling a
        # running hub) without requiring a hub restart -- the same gap that
        # `on_change`/`_notify_change` closes for GUI-driven upsert/remove,
        # but for changes the hub never saw happen in the first place.
        added, updated, removed = await manager.reload_from_disk(CONFIG_PATH)
        _notify_change()
        return JSONResponse({"added": added, "updated": updated, "removed": removed})

    return [
        Route("/api/status", status, methods=["GET"]),
        Route("/api/settings", settings, methods=["GET"]),
        Route("/api/settings", update_settings, methods=["PUT"]),
        Route("/api/rizzo", get_rizzo, methods=["GET"]),
        Route("/api/rizzo", put_rizzo, methods=["PUT"]),
        Route("/api/pid", pid, methods=["GET"]),
        Route("/api/shutdown", shutdown, methods=["POST"]),
        Route("/api/drain", drain, methods=["POST"]),
        Route("/api/drain", resume, methods=["DELETE"]),
        Route("/api/reload", reload, methods=["POST"]),
        Route("/api/servers/{name}/start", start, methods=["POST"]),
        Route("/api/servers/{name}/stop", stop, methods=["POST"]),
        Route("/api/servers/{name}/logs", logs, methods=["GET"]),
        Route("/api/servers/{name}/metrics", server_metrics, methods=["GET"]),
        Route("/api/servers/{name}/tools", server_tools, methods=["GET"]),
        Route("/api/servers/{name}/call", call_tool, methods=["POST"]),
        Route("/api/groups", groups_list, methods=["GET"]),
        Route("/api/groups/{name}", group_put, methods=["PUT"]),
        Route("/api/groups/{name}", group_delete, methods=["DELETE"]),
        Route("/api/groups/{name}/{action}", group_action, methods=["POST"]),
        Route("/api/servers/{name}/filters", set_filters, methods=["PUT"]),
        Route("/api/servers/{name}", upsert, methods=["POST"]),
        Route("/api/servers/{name}", remove, methods=["DELETE"]),
    ]
