"""End to end: the real MCP SDK client -> the hub (real uvicorn, real SSE) ->
a real MCP server (SDK MCPServer over stdio).

Hub-level behaviour that the hand-made fake cannot prove: the genuine
handshake, tool listing/calls, progress notifications routed per session,
the bearer token and Origin checks over real HTTP, and recovery after the
upstream crashes.
"""
import asyncio
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.exceptions import MCPError

from mcp_hub import manager as manager_module
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
    http = uvicorn.Server(uvicorn.Config(build_app(manager), host="127.0.0.1", port=port, log_level="warning",
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


@pytest.mark.asyncio
async def test_real_client_lists_and_calls_tools_through_the_hub():
    async with running_hub() as (manager, base):
        async with client(base) as session:
            names = {t.name for t in (await session.list_tools()).tools}
            assert {"add", "slow", "whoami", "die"} <= names
            assert _text(await session.call_tool("add", {"a": 2, "b": 3})) == "5"
        # the hub, not the client, initialised the one shared upstream
        assert manager.get("real").initialize_result["serverInfo"]["name"] == "real-fixture"


@pytest.mark.asyncio
async def test_two_real_clients_share_one_upstream_and_keep_their_own_progress():
    async with running_hub() as (manager, base):
        async with client(base) as a, client(base) as b:
            pid_a = _text(await a.call_tool("whoami", {}))
            pid_b = _text(await b.call_tool("whoami", {}))
            assert pid_a == pid_b, "both sessions are served by the SAME upstream process"

            got_a, got_b = [], []

            async def on_a(progress, total, message):
                got_a.append((progress, total))

            async def on_b(progress, total, message):
                got_b.append((progress, total))

            ra, rb = await asyncio.gather(
                a.call_tool("slow", {"steps": 3}, progress_callback=on_a),
                b.call_tool("slow", {"steps": 5}, progress_callback=on_b),
            )
            assert _text(ra) == "done" and _text(rb) == "done"
            # each client saw exactly ITS OWN progress sequence, nobody else's
            assert got_a == [(1, 3), (2, 3), (3, 3)]
            assert got_b == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]


@pytest.mark.asyncio
async def test_token_and_origin_are_enforced_over_real_http():
    async with running_hub(HubConfig(authToken="s3cret")) as (_manager, base):
        async with client(base, headers={"Authorization": "Bearer s3cret"}) as session:
            assert _text(await session.call_tool("add", {"a": 1, "b": 1})) == "2"
        async with httpx.AsyncClient() as http:
            assert (await http.get(f"{base}/api/status")).status_code == 401
            good = {"Authorization": "Bearer s3cret"}
            assert (await http.get(f"{base}/api/status", headers=good)).status_code == 200
            evil = {**good, "Origin": "https://evil.example"}
            assert (await http.get(f"{base}/api/status", headers=evil)).status_code == 403
            rebind = {**good, "Host": "evil.example"}
            assert (await http.get(f"{base}/api/status", headers=rebind)).status_code == 403
            as_text = await http.post(f"{base}/api/servers/x", headers=good, content='{"command":"x","args":[],"enabled":false}')
            assert as_text.status_code == 415


@pytest.mark.asyncio
async def test_real_upstream_crash_is_answered_then_the_hub_restarts_it(monkeypatch):
    monkeypatch.setattr(manager_module, "RESTART_BASE_DELAY", 0.1)
    monkeypatch.setattr(manager_module, "RESTART_MAX_DELAY", 0.3)
    async with running_hub() as (manager, base):
        server = manager.get("real")
        async with client(base) as session:
            before = _text(await session.call_tool("whoami", {}))
            with pytest.raises(MCPError) as lost:  # the upstream dies mid-call
                await session.call_tool("die", {})
            # answered with the hub's JSON-RPC error (code -32000), not left hanging
            assert lost.value.code == -32000 and "subprocess exited" in lost.value.message

            for _ in range(100):
                if server.status == "running" and server.restart_count >= 1:
                    break
                await asyncio.sleep(0.05)
            assert server.restart_count == 1

            # SAME client connection, no reconnect: served by a new, hub-initialised process
            after = _text(await session.call_tool("whoami", {}))
            assert after != before
            assert _text(await session.call_tool("add", {"a": 4, "b": 4})) == "8"


@pytest.mark.asyncio
async def test_isolated_mode_gives_each_real_client_its_own_upstream_and_cleans_up():
    async with running_hub(concurrency="isolated") as (manager, base):
        server = manager.get("real")
        async with client(base) as a, client(base) as b:
            pid_a = _text(await a.call_tool("whoami", {}))
            pid_b = _text(await b.call_tool("whoami", {}))
            assert pid_a != pid_b, "two sessions, two upstream processes"
            assert _text(await a.call_tool("whoami", {})) == pid_a
            assert len(server.children) == 2
            procs = [c.process for c in server.children]
        # both clients left: their private processes are stopped, nothing leaks
        for _ in range(100):
            if not server.children and all(p.returncode is not None for p in procs):
                break
            await asyncio.sleep(0.05)
        assert server.children == set()
        assert all(p.returncode is not None for p in procs)
