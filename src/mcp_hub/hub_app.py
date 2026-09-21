from __future__ import annotations

import asyncio
import json

from mcp.server.sse import SseServerTransport
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

from mcp_hub.manager import HubManager, ManagedServer


def _mount_for(name: str, manager: HubManager) -> Mount:
    managed = manager.get(name)
    # Endpoint is relative to THIS mount's root_path (Starlette sets it from
    # the outer Mount(f"/{name}", ...) below) — do NOT repeat the server name
    # here. Confirmed in Task 2's spike (scripts/spike_multi_mount.py):
    # f"/{name}/messages" doubles the prefix into "/{name}/{name}/messages"
    # and breaks every client POST. Trailing slash matches Mount("/messages/", ...)
    # below and avoids an extra 307 redirect.
    transport = SseServerTransport("/messages/")

    async def handle_sse(request):
        assert managed.process is not None
        # NOTE: no `managed.guard.run(...)` around this whole SSE lifetime.
        # Task 6 review finding: an `exclusive` server's guard must gate one
        # JSON-RPC request's turn talking to the subprocess, not an entire
        # SSE session (sessions are opened once and kept alive for a whole
        # client run). The guard is applied per-request inside `_proxy`
        # instead, so two SSE clients can both connect to an `exclusive`
        # server at once; what serializes is each individual request.
        async with transport.connect_sse(request.scope, request.receive, request._send) as (read, write):
            await _proxy(read, write, managed)
        return Response()  # avoids a TypeError on client disconnect (Task 2 spike finding)

    return Mount(f"/{name}", routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=transport.handle_post_message),
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
    matching request (by `id`, via `managed.pending`), and broadcasts
    anything else (notifications, or a response with no matching in-flight
    request) to every currently-connected client via `managed.subscribers`.

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

    async def deliver(obj: dict) -> None:
        message = jsonrpc_message_adapter.validate_python(obj)
        await write.send(SessionMessage(message))

    async def handle_message(session_message: SessionMessage) -> None:
        assert managed.process is not None and managed.process.stdin is not None
        payload_text = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
        obj = json.loads(payload_text)
        msg_id = obj.get("id")
        # A JSON-RPC request (expects a response) has both an id and a
        # method; a notification has no id; a client's response to a
        # server-initiated request has an id but no method — none of the
        # latter two get a future registered, since nothing will resolve it.
        is_request = msg_id is not None and "method" in obj

        async def write_and_maybe_wait() -> None:
            fut: asyncio.Future | None = None
            if is_request:
                fut = asyncio.get_running_loop().create_future()
                managed.pending[msg_id] = fut
            try:
                managed.process.stdin.write((payload_text + "\n").encode("utf-8"))
                await managed.process.stdin.drain()
                if fut is not None:
                    response_obj = await fut
                    await deliver(response_obj)
            finally:
                if fut is not None:
                    managed.pending.pop(msg_id, None)

        await managed.guard.run(write_and_maybe_wait)

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


def build_app(manager: HubManager) -> Starlette:
    routes = [_mount_for(name, manager) for name, sc in manager.config.servers.items() if sc.enabled]
    return Starlette(routes=routes)
