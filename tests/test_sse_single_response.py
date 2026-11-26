"""The SSE endpoint must send exactly ONE HTTP response.

`SseServerTransport.connect_sse` writes the whole response (headers, events, final empty body)
through the raw ASGI `send`. The handler used to `return Response()` afterwards, which made
Starlette send a SECOND `http.response.start`. uvicorn ignores that once the client is gone, but
when the HUB ends the stream while the connection is still open (a graceful shutdown, a restart,
a replaced server) it raised, and the log filled with
`RuntimeError: Unexpected ASGI message 'http.response.start' sent, after response already completed.`
"""
import contextlib
import sys

import pytest
from starlette.routing import Router

from mcp_hub import hub_app
from mcp_hub.config import ServerConfig
from mcp_hub.hub_app import _Endpoints, _mount_for
from mcp_hub.manager import ManagedServer


class StrictSend:
    """uvicorn's h11 rule: nothing may follow the final body of a response."""

    def __init__(self):
        self.messages = []
        self.complete = False

    async def __call__(self, message):
        if message["type"] == "http.response.start" and (self.complete or any(
                m["type"] == "http.response.start" for m in self.messages)):
            raise RuntimeError("Unexpected ASGI message 'http.response.start' sent, after response already completed.")
        self.messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            self.complete = True


@pytest.mark.asyncio
async def test_a_stream_the_hub_ends_itself_does_not_get_a_second_response(monkeypatch):
    managed = ManagedServer("x", ServerConfig(enabled=True, command=sys.executable, args=["-c", "pass"], env={}))
    endpoints = _Endpoints(managed)

    @contextlib.asynccontextmanager
    async def connect_sse(scope, receive, send):
        # what the SDK transport does: the complete response goes out through the raw send
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        yield None, None
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def no_proxy(read, write, upstream):
        return None

    monkeypatch.setattr(endpoints.sse, "connect_sse", connect_sse)
    monkeypatch.setattr(hub_app, "_proxy", no_proxy)

    sent = StrictSend()

    async def receive():
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": "GET", "path": "/x/sse", "raw_path": b"/x/sse", "root_path": "",
             "query_string": b"", "headers": [], "scheme": "http", "server": ("127.0.0.1", 37450),
             "client": ("127.0.0.1", 1)}
    await Router(routes=[_mount_for("x", endpoints)])(scope, receive, sent)  # raises on a second response

    starts = [m for m in sent.messages if m["type"] == "http.response.start"]
    assert len(starts) == 1 and sent.complete
