"""Streamable HTTP endpoint (`/<name>/mcp`) of one managed server
(hub-proxy-efficiency R4).

MCP's current HTTP transport (SSE is deprecated). The SDK's own
`StreamableHTTPSessionManager` needs an `mcp.server.Server` to drive; the hub
has none, because what it serves is a proxy. So this is a small session
registry around the SDK's `StreamableHTTPServerTransport`, which already does
the protocol (POST/GET/DELETE, `Mcp-Session-Id`, SSE or JSON responses): every
session's `connect()` streams are handed to the same `_proxy` pump the SSE
endpoint uses, so both transports share request ids, caching, guard and
routing.

Sessions live in memory only. After a hub restart an old session id answers
404, which per the spec makes the client initialise a new one.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import uuid4

import anyio
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER, StreamableHTTPServerTransport
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from mcp_hub.manager import ManagedServer

logger = logging.getLogger(__name__)

# Seconds a session may go with no request in flight before it is ended (its
# id then answers 404 and the client initialises a new one).
SESSION_IDLE_TIMEOUT = 1800.0


@dataclass(eq=False)
class _Session:
    transport: StreamableHTTPServerTransport
    upstream: ManagedServer


def _not_found() -> Response:
    body = '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Session not found"}}'
    return Response(body, status_code=404, media_type="application/json")


class StreamableSessions:
    """ASGI app for one server's `/mcp` route."""

    def __init__(self, managed: ManagedServer, idle_timeout: float | None = SESSION_IDLE_TIMEOUT) -> None:
        self.managed = managed
        self.idle_timeout = idle_timeout
        self._sessions: dict[str, _Session] = {}
        self._tasks: set[asyncio.Task] = set()

    def __len__(self) -> int:
        return len(self._sessions)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        session_id = Request(scope, receive).headers.get(MCP_SESSION_ID_HEADER)
        if session_id is None:
            await self._open(scope, receive, send)
            return
        session = self._sessions.get(session_id)
        if session is None:
            await _not_found()(scope, receive, send)  # unknown or expired: the client re-initialises
            return
        await session.transport.handle_request(scope, receive, send)
        if session.transport.is_terminated:  # the client ended it (DELETE)
            await self._discard(session_id)

    async def _open(self, scope: Scope, receive: Receive, send: Send) -> None:
        """A request without a session id: only an `initialize` POST can succeed,
        so a session that was not established is dropped again."""
        from mcp_hub.hub_app import _proxy  # late: hub_app imports this module

        session_id = uuid4().hex
        transport = StreamableHTTPServerTransport(mcp_session_id=session_id, idle_timeout=self.idle_timeout)
        session = _Session(transport, self.managed.session_upstream())
        self._sessions[session_id] = session
        ready = asyncio.Event()

        async def run() -> None:
            try:
                async with transport.connect() as (read, write):
                    ready.set()
                    with transport.idle_scope or anyio.CancelScope():
                        await _proxy(read, write, session.upstream)
                    if transport.idle_scope is not None and transport.idle_scope.cancelled_caught:
                        logger.info("Streamable session %s idle: ended", session_id)
            except Exception:  # noqa: BLE001 - one session's failure must not reach the others
                logger.exception("Streamable session %s crashed", session_id)
            finally:
                ready.set()
                await self._discard(session_id)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await ready.wait()

        established = False
        try:
            status = await _status_of(transport.handle_request, scope, receive, send)
            established = status is not None and status < 400
        finally:
            if not established:
                await self._discard(session_id)

    async def _discard(self, session_id: str) -> None:
        """Forgets the session (its id answers 404 from this instant), ends its
        transport and releases its upstream. Safe to call twice."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        try:
            if not session.transport.is_terminated:
                with anyio.CancelScope(shield=True):
                    await session.transport.terminate()
        finally:
            await self.managed.release_session_upstream(session.upstream)

    async def shutdown(self) -> None:
        """Ends every session (hub stopping, or the server entry replaced)."""
        for session_id in list(self._sessions):
            await self._discard(session_id)
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


async def _status_of(app, scope: Scope, receive: Receive, send: Send) -> int | None:
    """Runs `app` and returns the HTTP status it answered with (None: none)."""
    status: int | None = None

    async def watch(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        await send(message)

    await app(scope, receive, watch)
    return status
