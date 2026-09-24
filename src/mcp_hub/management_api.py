from __future__ import annotations

import asyncio
import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_hub.config import ServerConfig, save_config
from mcp_hub.manager import HubManager


def management_routes(manager: HubManager, shutdown_event: asyncio.Event | None = None) -> list[Route]:
    async def status(request: Request) -> JSONResponse:
        return JSONResponse({"servers": manager.status_snapshot()})

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

    async def shutdown(request: Request) -> JSONResponse:
        # Graceful: lets `serve_with_managed_shutdown`'s `finally` stop every
        # managed subprocess before the process exits, instead of a caller
        # killing the hub's OS process directly and orphaning them. Used by
        # the GUI's self-update flow, which needs the hub's exe file
        # unlocked (process exited) before it can be replaced on disk.
        if shutdown_event is not None:
            shutdown_event.set()
        return JSONResponse({"status": "shutting down"})

    async def start(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        await manager.get(name).start()
        return JSONResponse({"status": manager.get(name).status})

    async def stop(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        await manager.get(name).stop()
        return JSONResponse({"status": manager.get(name).status})

    async def logs(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        return JSONResponse({"lines": list(manager.get(name).logs)})

    async def upsert(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        body = await request.json()
        server_config = ServerConfig(**body)
        # HubManager.upsert stops any previously-running process for `name`
        # itself before replacing the entry (round-2 review fix), so the
        # freshly-returned `managed` here is a brand-new ManagedServer with
        # no old process to worry about -- just start it if enabled.
        managed = await manager.upsert(name, server_config)
        save_config(manager.config)
        if server_config.enabled:
            await managed.start()
        return JSONResponse({"status": managed.status})

    async def remove(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        await manager.remove(name)
        save_config(manager.config)
        return JSONResponse({"status": "removed"})

    return [
        Route("/api/status", status, methods=["GET"]),
        Route("/api/settings", settings, methods=["GET"]),
        Route("/api/settings", update_settings, methods=["PUT"]),
        Route("/api/pid", pid, methods=["GET"]),
        Route("/api/shutdown", shutdown, methods=["POST"]),
        Route("/api/servers/{name}/start", start, methods=["POST"]),
        Route("/api/servers/{name}/stop", stop, methods=["POST"]),
        Route("/api/servers/{name}/logs", logs, methods=["GET"]),
        Route("/api/servers/{name}", upsert, methods=["POST"]),
        Route("/api/servers/{name}", remove, methods=["DELETE"]),
    ]
