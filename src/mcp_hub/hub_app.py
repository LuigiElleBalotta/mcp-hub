from __future__ import annotations

from mcp.server.sse import SseServerTransport
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

from mcp_hub.manager import HubManager


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
        async def guarded_run():
            assert managed.process is not None
            async with transport.connect_sse(request.scope, request.receive, request._send) as (read, write):
                await _proxy(read, write, managed)
        await managed.guard.run(guarded_run)
        return Response()  # avoids a TypeError on client disconnect (Task 2 spike finding)

    return Mount(f"/{name}", routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=transport.handle_post_message),
    ])


async def _proxy(read, write, managed) -> None:
    """Pump JSON-RPC frames between the SSE client (read/write) and the
    managed subprocess's stdio (managed.process.stdin/stdout).

    `read`/`write` are the streams `SseServerTransport.connect_sse` yields
    (installed mcp==2.2.0, see mcp.server.sse). They do NOT carry raw JSON or
    plain `JSONRPCMessage` objects: `read` yields `SessionMessage | Exception`
    (an `Exception` when the client sent something that failed to parse —
    those are dropped here rather than forwarded to the subprocess), and
    `write` accepts `SessionMessage` sends. This matches
    `mcp.shared.message.SessionMessage` used throughout the SDK's own client
    and server transports.

    Framing to/from the subprocess matches `mcp.client.stdio`'s stdio client
    transport exactly (the reference implementation of the MCP stdio wire
    format the brief pointed at): newline-delimited JSON, one JSON-RPC
    envelope per line, encoded/decoded as UTF-8.
      - to_process: for each `SessionMessage` read from the SSE client,
        serialize with `.message.model_dump_json(by_alias=True,
        exclude_unset=True)` (the exact call `mcp.client.stdio.stdin_writer`
        uses) + "\n", encode, write to `managed.process.stdin`, then drain.
      - from_process: for each line read off `managed.process.stdin`'s sibling
        `managed.process.stdout` (an `asyncio.StreamReader`, so `async for`
        yields one line at a time via `readline()`), strip the trailing
        newline (headroom is a Windows binary and writes "\r\n"; blank lines
        from that stripping are skipped), parse with
        `jsonrpc_message_adapter.validate_json(...)` into a `JSONRPCMessage`,
        wrap in a fresh `SessionMessage`, and `write.send(...)` it back to the
        SSE client.

    The task group is NOT a plain "run both forever": when the SSE client
    disconnects, `read` closes and `to_process` returns, but the managed
    subprocess is long-lived and its stdout never ends on its own, so
    `from_process` would otherwise block forever, the task group would never
    exit, `handle_sse` would never return, and (for an "exclusive" guarded
    server) the concurrency lock would stay held forever. So `from_process`
    runs as a background task while `to_process` is awaited directly, and once
    `to_process` finishes (client gone), the task group's scope is cancelled
    to stop `from_process` too.
    """
    import anyio

    async def to_process():
        assert managed.process is not None and managed.process.stdin is not None
        async for session_message in read:
            if isinstance(session_message, Exception):
                continue
            data = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
            managed.process.stdin.write((data + "\n").encode("utf-8"))
            await managed.process.stdin.drain()

    async def from_process():
        assert managed.process is not None and managed.process.stdout is not None
        async for raw in managed.process.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            message = jsonrpc_message_adapter.validate_json(line, by_name=False)
            await write.send(SessionMessage(message))

    async with anyio.create_task_group() as tg:
        tg.start_soon(from_process)
        await to_process()
        tg.cancel_scope.cancel()


def build_app(manager: HubManager) -> Starlette:
    routes = [_mount_for(name, manager) for name, sc in manager.config.servers.items() if sc.enabled]
    return Starlette(routes=routes)
