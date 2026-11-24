from __future__ import annotations

import asyncio
import contextlib
import json
import time

from mcp.server.sse import SseServerTransport
from mcp.shared.message import ServerMessageMetadata, SessionMessage
from mcp_types import jsonrpc_message_adapter
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import Response
from starlette.routing import Mount, Route

from mcp_hub.cache import CACHEABLE_LISTS, has_cursor
from mcp_hub.filters import filter_tools_result, resolve_call
from mcp_hub.management_api import management_routes
from mcp_hub.manager import HubManager, ManagedServer
from mcp_hub.security import HubSecurityMiddleware
from mcp_hub.session import SESSION_META_KEY, HubSession
from mcp_hub.streamable import StreamableSessions


# JSON-RPC "server error" range (-32000..-32099): the managed server is down.
SERVER_UNAVAILABLE_CODE = -32000


class _Endpoints:
    """The long-lived transports of one managed server.

    They hold the connected sessions (an SSE session is registered in its
    `SseServerTransport`, a Streamable one in `StreamableSessions`), so they must
    outlive route rebuilds: `build_app` keeps one per server and only replaces it
    when the server entry itself is replaced. Rebuilding them on every config
    change used to leave every already-connected client without a session (its
    next POST hit a transport that had never heard of it) and hung it forever.
    """

    def __init__(self, managed: ManagedServer) -> None:
        self.managed = managed
        # Endpoint is relative to THIS mount's root_path (Starlette sets it from
        # the outer Mount(f"/{name}", ...)) — do NOT repeat the server name
        # here. Confirmed in Task 2's spike (scripts/spike_multi_mount.py):
        # f"/{name}/messages" doubles the prefix into "/{name}/{name}/messages"
        # and breaks every client POST. Trailing slash matches Mount("/messages/", ...)
        # and avoids an extra 307 redirect.
        self.sse = SseServerTransport("/messages/")
        self.streamable = StreamableSessions(managed)


def _mount_for(name: str, endpoints: _Endpoints) -> Mount:
    managed = endpoints.managed
    transport = endpoints.sse

    async def handle_sse(request):
        # No `process` assertion: a lazy or idle server has none until a request
        # needs it (`ensure_running` in `handle_message`).
        # NOTE: no `managed.guard.run(...)` around this whole SSE lifetime.
        # Task 6 review finding: an `exclusive` server's guard must gate one
        # JSON-RPC request's turn talking to the subprocess, not an entire
        # SSE session (sessions are opened once and kept alive for a whole
        # client run). The guard is applied per-request inside `_proxy`
        # instead, so two SSE clients can both connect to an `exclusive`
        # server at once; what serializes is each individual request.
        # An `isolated` server gives each session its own process.
        upstream = managed.session_upstream()
        try:
            async with transport.connect_sse(request.scope, request.receive, request._send) as (read, write):
                await _proxy(read, write, upstream)
        finally:
            await managed.release_session_upstream(upstream)
        return Response()  # avoids a TypeError on client disconnect (Task 2 spike finding)

    return Mount(f"/{name}", routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=transport.handle_post_message),
        # Streamable HTTP, the transport that replaces SSE in the MCP spec.
        Route("/mcp", endpoint=endpoints.streamable, methods=["GET", "POST", "DELETE"]),
    ])


async def _proxy(read, write, managed: ManagedServer) -> None:
    """Pump JSON-RPC frames between one SSE client (read/write) and the
    managed subprocess's shared stdio (managed.process.stdin/stdout).

    `read`/`write` are the streams `SseServerTransport.connect_sse` yields
    (installed mcp SDK, see mcp.server.sse). They do NOT carry raw JSON or
    plain `JSONRPCMessage` objects: `read` yields `SessionMessage | Exception`
    (an `Exception` when the client sent something that failed to parse —
    those are dropped here rather than forwarded to the subprocess), and
    `write` accepts `SessionMessage` sends.

    Concurrency (Task 6 review, finding 1): `managed.process.stdin`/`stdout`
    is a single pipe shared by every connection currently proxying to this
    server (there is one subprocess per server, not one per client). Reading
    stdout is centralized in `ManagedServer._read_stdout` (one task per
    subprocess, started lazily via `ensure_stdout_reader`) so there is never
    more than one consumer racing to read lines off the shared stream, no
    matter how many SSE connections are open concurrently. That reader
    correlates a JSON-RPC response back to whichever `_proxy` call sent the
    matching request, keyed by a hub-generated id (see `next_request_id` /
    `handle_message` below) rather than the client-supplied one, via
    `managed.pending`, and broadcasts anything else (notifications, or a
    response with no matching in-flight request) to every currently-connected
    client via `managed.subscribers`.

    Each incoming client message is handled by its own task (`handle_message`,
    spawned per message into this connection's task group) rather than
    awaited inline in the read loop, so a client that pipelines multiple
    requests within one SSE session isn't serialized against itself. The
    actual write-to-stdin-and-await-the-response step for each message is
    what goes through `managed.guard.run(...)`: for an `exclusive` server
    that's the one thing that must serialize — one request's turn on the
    subprocess at a time, across ALL connections — not this connection's
    whole lifetime. For a `parallel` server `guard.run` is a no-op passthrough
    (see ConcurrencyGuard), so this reduces to full concurrency as before.
    """
    import anyio

    async def deliver(obj: dict, related_request_id=None) -> None:
        message = jsonrpc_message_adapter.validate_python(obj)
        metadata = None
        if related_request_id is not None:
            # Streamable HTTP puts a server message on the response stream of
            # the request it belongs to; without this it would land on the
            # standalone GET stream. The SSE transport ignores the metadata.
            metadata = ServerMessageMetadata(related_request_id=related_request_id)
        await write.send(SessionMessage(message, metadata=metadata))

    session = HubSession(deliver)

    def shaped(method: str, result: dict) -> dict:
        """What this client may see of a list answer (R6: tool filter + prefix)."""
        cfg = managed.config
        if method == "tools/list" and cfg.has_tool_filter:
            return filter_tools_result(result, cfg.toolsAllow, cfg.toolsDeny, cfg.toolPrefix)
        return result

    async def handle_message(session_message: SessionMessage) -> None:
        payload_text = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
        obj = json.loads(payload_text)
        client_id = obj.get("id")
        # A JSON-RPC request (expects a response) has both an id and a
        # method; a notification has no id; a client's response to a
        # server-initiated request has an id but no method — none of the
        # latter two get a future registered, since nothing will resolve it.
        is_request = client_id is not None and "method" in obj

        if obj.get("method") == "notifications/cancelled":
            # hub-proxy-hardening R3: the client names ITS request id, which
            # the upstream never saw (it knows only the hub id). Translate it;
            # an unknown, finished or foreign id is ignored, which the spec
            # explicitly allows, and must never cancel another session's call.
            cancel_params = obj.get("params")
            cancel_params = cancel_params if isinstance(cancel_params, dict) else {}
            hub_request_id = managed.table.translate_cancel(session, cancel_params.get("requestId"))
            if hub_request_id is None:
                return
            obj["params"] = {**cancel_params, "requestId": hub_request_id}
            payload_text = json.dumps(obj)

        # The client's own progress token (if any), swapped for a hub token at
        # dispatch time so two sessions that both picked "tok" cannot collide.
        # A list request the cache may answer or fill (R1): page requests never.
        cache_method = obj.get("method") if (is_request and obj.get("method") in CACHEABLE_LISTS
                                             and not has_cursor(obj.get("params"))) else None

        cfg = managed.config
        if is_request and obj.get("method") == "tools/call" and cfg.has_tool_filter:
            # R6: the client names a tool as IT sees it (prefixed, filtered). Map it
            # back to the upstream's own name, or refuse a tool that does not exist
            # for this client without bothering the upstream.
            call_params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
            original = resolve_call(call_params.get("name"), cfg.toolsAllow, cfg.toolsDeny, cfg.toolPrefix)
            if original is None:
                await deliver({"jsonrpc": "2.0", "id": client_id, "error": {
                    "code": -32602, "message": f"Unknown tool: {call_params.get('name')}"}})
                return
            obj["params"] = {**call_params, "name": original}
            payload_text = json.dumps(obj)

        client_progress_token = None
        req_params = obj.get("params")
        if is_request and isinstance(req_params, dict) and isinstance(req_params.get("_meta"), dict):
            client_progress_token = req_params["_meta"].get("progressToken")

        # Round-2 review fix (Important finding): the client-supplied `id` is
        # only unique within that one client's own session, not across the
        # multiple SSE connections a `parallel` server allows to share this
        # one subprocess (and `managed.pending`) concurrently. Two
        # independent clients commonly both start numbering at 1, which would
        # otherwise let one client's registration in `managed.pending`
        # silently clobber another's in-flight request. Rewrite to a
        # hub-generated id (unique for this ManagedServer's whole lifetime,
        # see `next_request_id`) before writing to the subprocess, and
        # restore the client's own id on the response before delivering it
        # back — the client expects its own id echoed, per JSON-RPC, and has
        # no knowledge of the hub-generated one.
        hub_id = managed.next_request_id() if is_request else None
        if hub_id is not None:
            obj["id"] = hub_id
            payload_text = json.dumps(obj)

        async def write_and_maybe_wait() -> None:
            # Fail fast if the subprocess is already gone *before* registering
            # anything in `managed.pending`. Without this check there is a gap
            # the Critical-finding fix doesn't otherwise close: if the
            # subprocess died and its stdout reader already ran to completion
            # (rejecting whatever was pending at the time and exiting), a
            # request registered AFTER that point has no live reader left to
            # ever reject its future -- `ensure_stdout_reader()` would spawn a
            # new reader against the same dead process, which hits EOF
            # immediately and exits again having rejected nothing (pending was
            # still empty at that instant), and a write to a dead process's
            # stdin pipe does not reliably raise synchronously. That future
            # would then hang forever, deadlocking an `exclusive` guard again.
            # Checking `returncode` here closes that window: no future is ever
            # registered unless the process was observably still running at
            # registration time.
            if managed.process is None or managed.process.returncode is not None:
                raise ConnectionError(
                    f"managed server {managed.name!r} subprocess is not running"
                )
            # Round-3 review fix: `ensure_stdout_reader()` is otherwise only
            # called once per connection, at connection-open, before this
            # per-message loop even starts. If the subprocess dies mid
            # connection, that ONE shared reader observes EOF, rejects
            # whatever was pending at that instant, and exits -- no new
            # reader is spawned for this still-open connection. A second
            # message pipelined on the SAME connection after that point then
            # depended entirely on the `returncode` check above to avoid
            # registering an orphaned future, but `returncode` is only
            # updated once asyncio's subprocess transport notices the child
            # exited -- not guaranteed to have happened yet at the exact
            # instant stdout EOF was observed by the reader. Calling
            # `ensure_stdout_reader()` again here, immediately before
            # registering anything in `pending`, closes that window
            # regardless of `returncode` timing: it is idempotent (a no-op
            # while a reader is already running), so this is cheap on the
            # hot path, but if the previous reader already ran to completion
            # it spawns a fresh one *before* the future below exists. That
            # new reader, whether it finds the process still alive or
            # already dead (immediate EOF), always ends in `_reject_pending()`
            # once it exits -- and since spawning it and registering the
            # future are both synchronous statements with no `await` between
            # them, the new reader cannot get a turn to run (and reject
            # `pending`) until this coroutine's next `await`, by which time
            # the future is already registered. So the future is always
            # either resolved normally or rejected -- never orphaned.
            managed.ensure_stdout_reader()
            fut: asyncio.Future | None = None
            cache_generation = managed.cache.generation(cache_method) if cache_method else 0
            out_text = payload_text
            if hub_id is not None:
                fut = asyncio.get_running_loop().create_future()
                managed.pending[hub_id] = fut
                hub_progress = managed.table.register(session, client_id, hub_id, client_progress_token)
                meta_updates: dict = {}
                if hub_progress is not None:
                    meta_updates["progressToken"] = hub_progress
                if managed.config.concurrency == "session-aware":
                    meta_updates[SESSION_META_KEY] = session.id  # R3: who is asking
                if meta_updates and isinstance(obj.get("params", {}), dict):
                    out_params = dict(obj.get("params") or {})
                    out_params["_meta"] = {**(out_params.get("_meta") or {}), **meta_updates}
                    out_text = json.dumps({**obj, "params": out_params})
                # Permanent hub-side dispatch timing (Task 13 review finding:
                # scripts/concurrency_check.py's client-observed completion
                # timestamps can appear near-simultaneous for two calls to an
                # `exclusive` server even when dispatch itself was cleanly
                # serialized -- that gap is real SSE response-delivery
                # latency downstream of this point, in the `mcp` SDK's
                # transport, not a guard failure). Logged here, which already
                # runs under `managed.guard`'s lock for an `exclusive` server
                # (`guard.run` wraps this whole coroutine -- see `_proxy`'s
                # docstring above), this line and its matching "done"/"error"
                # line below give a rerunnable, hub-side proof of
                # serialization that is immune to that downstream latency.
                # `id`/`method` let a reader (the script, or a human) pair a
                # start with its outcome and isolate the specific request(s)
                # it cares about from other traffic (initialize,
                # notifications, etc.) that also passes through this same
                # guarded path. `time.monotonic()`, not wall clock: the proof
                # this backs is entirely intra-hub (one window's end vs the
                # next window's start), so cross-process comparability is
                # irrelevant and monotonic is immune to clock steps.
                managed.append_log(
                    f"[hub-dispatch] phase=start id={hub_id} method={obj.get('method')} "
                    f"t={time.monotonic():.6f}"
                )
            # Tracks whether "done" was already logged, so the broad
            # `except Exception` below (covering the stdin write/drain AND
            # the awaited response, not just the latter) can't double-log an
            # "error" after a successful dispatch that only failed later,
            # during `deliver()`'s SSE send -- that's a delivery problem, not
            # a dispatch one, and this timing is specifically NOT about the
            # delivery leg (see comment above `deliver` below).
            dispatch_done_logged = False
            try:
                try:
                    managed.process.stdin.write((out_text + "\n").encode("utf-8"))
                    await managed.process.stdin.drain()
                except (ConnectionError, OSError) as exc:
                    # asyncio's own text here ("Connection lost") names no
                    # server; the client gets this answer, so make it useful.
                    raise ConnectionError(
                        f"managed server {managed.name!r} subprocess is not accepting requests ({exc})"
                    ) from exc
                if fut is not None:
                    response_obj = await fut
                    # Logged BEFORE `deliver(...)`: `deliver` hands the
                    # response to the SSE transport, which is exactly the
                    # downstream leg this timing is meant to exclude from the
                    # measured dispatch window (see comment above).
                    managed.append_log(
                        f"[hub-dispatch] phase=done id={hub_id} method={obj.get('method')} "
                        f"t={time.monotonic():.6f}"
                    )
                    dispatch_done_logged = True
                    if cache_method and isinstance(response_obj.get("result"), dict):
                        if managed.cache.put(cache_method, response_obj["result"], cache_generation):
                            managed.persist_cache()
                    response_obj = {**response_obj, "id": client_id}
                    # Every tools/list answer, pages included (they never reach the cache).
                    if obj.get("method") == "tools/list" and isinstance(response_obj.get("result"), dict):
                        response_obj["result"] = shaped("tools/list", response_obj["result"])
                    await deliver(response_obj)
            except Exception:
                # Log the "error" outcome too (not just the happy path): a
                # reader waiting to pair this id's start/outcome lines must
                # not hang or mis-parse if the stdin write/drain itself
                # raises, or the subprocess died, or the guard's wait was
                # otherwise rejected mid-flight (see `_reject_pending`) --
                # any of those leaves `fut` without a "done".
                if fut is not None and not dispatch_done_logged:
                    managed.append_log(
                        f"[hub-dispatch] phase=error id={hub_id} method={obj.get('method')} "
                        f"t={time.monotonic():.6f}"
                    )
                raise
            finally:
                if fut is not None:
                    managed.pending.pop(hub_id, None)
                    managed.table.complete(hub_id)
                    managed.touch()
                    # `_reject_pending` may have failed this future while the
                    # stdin write above was already raising, so nothing ever
                    # awaits it: mark its exception as retrieved, otherwise
                    # asyncio logs "Future exception was never retrieved".
                    if fut.done() and not fut.cancelled():
                        fut.exception()

        try:
            method = obj.get("method")
            if method == "notifications/initialized":
                return  # the hub sent its own when it initialised the upstream
            if cache_method is not None:
                cached = managed.cache.get(cache_method)
                if cached is not None:
                    # Answered without touching the upstream (and even while it
                    # is restarting): the same list every client would get.
                    await deliver({"jsonrpc": "2.0", "id": client_id, "result": shaped(cache_method, cached)})
                    return
            managed.touch()
            if method is not None:
                if method == "initialize" and is_request and managed.status == "idle" \
                        and managed.initialize_result is not None:
                    # Lazy/idle server: its last handshake answers the client
                    # without spawning anything; the process starts when a
                    # request actually needs it.
                    await deliver({"jsonrpc": "2.0", "id": client_id, "result": managed.initialize_result})
                    return
                await managed.ensure_running()
                await managed.ensure_ready()
                if method == "initialize" and is_request:
                    # hub-proxy-hardening R4: never forwarded. Every client of
                    # this shared upstream gets the hub's cached handshake, so
                    # a respawned upstream is initialised exactly once, by the
                    # hub, whatever the clients believe.
                    await deliver({"jsonrpc": "2.0", "id": client_id, "result": managed.initialize_result})
                    return
            if is_request:
                await managed.guard.run(write_and_maybe_wait)
            else:
                # A notification or a client's answer to a server request takes
                # no "turn" on the upstream. Behind an exclusive guard it would
                # queue behind the very request it concerns: the answer to the
                # server's question could never arrive (deadlock) and a
                # cancellation would wait for the call it wants to cancel.
                await write_and_maybe_wait()
        except ConnectionError as exc:
            # hub-proxy-hardening R2: the upstream is gone (exited with this
            # request in flight, a stdin write hit a dead pipe, or it was
            # already down on arrival). Answer THIS request with a JSON-RPC
            # error carrying the client's own id instead of letting the
            # exception escape into the connection's task group, which would
            # tear down the whole SSE session and every other request on it.
            # Never replayed: a tool call may have had side effects.
            managed.append_log(f"[hub] request {client_id!r} failed: {exc}")
            if is_request:
                try:
                    await deliver({
                        "jsonrpc": "2.0",
                        "id": client_id,
                        "error": {"code": SERVER_UNAVAILABLE_CODE, "message": str(exc)},
                    })
                except Exception:
                    pass  # the client is gone too; nobody left to tell

    managed.subscribers.add(deliver)
    managed.ensure_stdout_reader()
    try:
        async with anyio.create_task_group() as tg:
            async for session_message in read:
                if isinstance(session_message, Exception):
                    continue
                tg.start_soon(handle_message, session_message)
    finally:
        managed.subscribers.discard(deliver)


class DrainGate:
    """While the hub drains (shutting down, or told to via /api/drain) it takes
    no new MCP traffic: 503 + Retry-After so a client tries again after the
    restart. The management API stays open, and so does DELETE so a Streamable
    client can still end its session cleanly."""

    def __init__(self, app, manager: HubManager) -> None:
        self.app = app
        self.manager = manager

    async def __call__(self, scope, receive, send) -> None:
        if (scope["type"] == "http" and self.manager.draining
                and not scope["path"].startswith("/api/") and scope["method"] != "DELETE"):
            from starlette.responses import JSONResponse

            await JSONResponse({"error": "hub is shutting down, try again shortly"}, status_code=503,
                               headers={"Retry-After": "5"})(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _shutdown_later(endpoints: "_Endpoints") -> None:
    """Ends a replaced/removed server's Streamable sessions in the background
    (route rebuilds are synchronous). Without a running loop nothing can be
    connected, so there is nothing to end."""
    try:
        asyncio.get_running_loop().create_task(endpoints.streamable.shutdown())
    except RuntimeError:
        pass


def _server_mounts(manager: HubManager, registry: dict[str, _Endpoints]) -> list[Mount]:
    mounts: list[Mount] = []
    for name, sc in manager.config.servers.items():
        if not sc.enabled or sc.is_service:
            continue
        managed = manager.get(name)
        endpoints = registry.get(name)
        if endpoints is None or endpoints.managed is not managed:
            if endpoints is not None:  # the entry was replaced: its sessions are gone with it
                _shutdown_later(endpoints)
            endpoints = registry[name] = _Endpoints(managed)
        mounts.append(_mount_for(name, endpoints))
    for name in [n for n in registry if n not in {m.path[1:] for m in mounts}]:
        _shutdown_later(registry.pop(name))
    return mounts


def build_app(manager: HubManager, shutdown_event: asyncio.Event | None = None) -> Starlette:
    # Each enabled server's SSE/messages routes used to be computed ONCE here
    # and baked into the Starlette app forever. That is the root cause of the
    # "enable a server / flip enabled in config.json -> its route 404s until
    # the whole hub process is restarted" bug: `upsert`/`remove` (management_
    # api.py) always correctly started/stopped the SUBPROCESS, but never
    # touched this route list, so a freshly-enabled server ran fine yet was
    # unreachable over HTTP. Starlette's router just iterates `router.routes`
    # per request (no compile/cache step), so reassigning that list live is
    # enough to make a route change take effect on the very next request --
    # no ASGI-level restart needed. `on_change` is threaded into
    # `management_routes` and invoked at the end of `upsert`/`remove`, which
    # is what actually closes the gap for the two ways servers change
    # (GUI Add/Edit/Remove, and `HubManager.reload_from_disk` for a
    # hand-edited config.json -- see `manager.py`).
    registry: dict[str, _Endpoints] = {}

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        # Hub stopping: end every Streamable session (their pump tasks).
        await asyncio.gather(*(e.streamable.shutdown() for e in registry.values()), return_exceptions=True)

    app = Starlette(
        routes=[],
        middleware=[
            Middleware(HubSecurityMiddleware, hub=manager.config.hub),
            Middleware(DrainGate, manager=manager),
        ],
        lifespan=lifespan,
    )

    def rebuild_routes() -> None:
        app.router.routes = _server_mounts(manager, registry) + mgmt_routes

    app.state.endpoints = registry  # name -> _Endpoints (diagnostics, tests)
    mgmt_routes = management_routes(manager, shutdown_event, on_change=rebuild_routes)
    rebuild_routes()  # populates the initial route list (see docstring above)
    return app
