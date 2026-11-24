"""Shared harness for the end-to-end tests: a real hub (uvicorn) in the test's
event loop, in front of a real SDK MCP server, and real SDK clients."""
import asyncio
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client

from mcp_hub.config import Config, HubConfig, ServerConfig
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager

REAL_SERVER = str(Path(__file__).parent / "fixtures" / "real_mcp_server.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def running_hub(hub: HubConfig | None = None, **server_kw):
    # sse-starlette keeps a PROCESS-global "the server is shutting down" flag
    # that it sets when any uvicorn server stops and never clears. One hub per
    # process (production) never notices; several hubs in one test process
    # would see every later SSE stream end at once.
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit = False
    port = _free_port()
    hub = hub or HubConfig()
    hub.port = port
    server_kw.setdefault("concurrency", "parallel")
    cfg = ServerConfig(enabled=True, command=sys.executable, args=[REAL_SERVER], env={}, **server_kw)
    manager = HubManager(Config(hub=hub, servers={"real": cfg}))
    await manager.start_all()
    app = build_app(manager)
    manager.app = app  # tests reach `app.state.endpoints` (live sessions) through it
    http = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                            timeout_graceful_shutdown=3))
    task = asyncio.create_task(http.serve())
    while not http.started:
        await asyncio.sleep(0.02)
    try:
        yield manager, f"http://127.0.0.1:{port}"
    finally:
        http.should_exit = True
        await task
        await manager.stop_all()


@asynccontextmanager
async def client(base: str, headers: dict | None = None):
    async with sse_client(f"{base}/real/sse", headers=headers) as streams:
        async with ClientSession(*streams[:2]) as session:
            await session.initialize()
            yield session


def _text(result) -> str:
    return result.content[0].text




@asynccontextmanager
async def mcp_client(base: str, headers: dict | None = None, name: str = "real"):
    """A real SDK client speaking Streamable HTTP to `/<name>/mcp`."""
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(f"{base}/{name}/mcp", http_client=http_client) as streams:
            async with ClientSession(*streams[:2]) as session:
                await session.initialize()
                yield session
