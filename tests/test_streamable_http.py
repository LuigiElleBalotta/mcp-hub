"""hub-proxy-efficiency R4: the Streamable HTTP endpoint (`/<name>/mcp`), driven
by the REAL SDK client against the real hub and a real SDK MCP server."""
import asyncio

import httpx
import pytest
from mcp.shared.exceptions import MCPError

from mcp_hub import manager as manager_module
from mcp_hub.config import HubConfig

from hub_harness import client, mcp_client, running_hub


def _text(result) -> str:
    return result.content[0].text


def _sessions(manager) -> int:
    return len(manager.app.state.endpoints["real"].streamable)


@pytest.mark.asyncio
async def test_real_client_lists_and_calls_tools_over_streamable_http():
    async with running_hub() as (manager, base):
        async with mcp_client(base) as session:
            names = {t.name for t in (await session.list_tools()).tools}
            assert {"add", "slow", "whoami", "die"} <= names
            assert _text(await session.call_tool("add", {"a": 20, "b": 22})) == "42"
            assert _sessions(manager) == 1
        # the client's DELETE ended the session: nothing leaks
        for _ in range(100):
            if _sessions(manager) == 0:
                break
            await asyncio.sleep(0.05)
        assert _sessions(manager) == 0
        assert manager.get("real").initialize_result["serverInfo"]["name"] == "real-fixture"


@pytest.mark.asyncio
async def test_two_streamable_clients_share_one_upstream_with_separate_progress():
    async with running_hub() as (manager, base):
        async with mcp_client(base) as a, mcp_client(base) as b:
            assert _text(await a.call_tool("whoami", {})) == _text(await b.call_tool("whoami", {}))
            got_a, got_b = [], []

            async def on_a(progress, total, message):
                got_a.append((progress, total))

            async def on_b(progress, total, message):
                got_b.append((progress, total))

            ra, rb = await asyncio.gather(
                a.call_tool("slow", {"steps": 3}, progress_callback=on_a),
                b.call_tool("slow", {"steps": 4}, progress_callback=on_b),
            )
            assert _text(ra) == "done" and _text(rb) == "done"
            assert got_a == [(1, 3), (2, 3), (3, 3)]
            assert got_b == [(1, 4), (2, 4), (3, 4), (4, 4)]


@pytest.mark.asyncio
async def test_sse_and_streamable_clients_can_use_the_same_upstream_at_once():
    async with running_hub() as (manager, base):
        async with client(base) as sse, mcp_client(base) as http:
            assert _text(await sse.call_tool("whoami", {})) == _text(await http.call_tool("whoami", {}))
            assert _text(await http.call_tool("add", {"a": 1, "b": 2})) == "3"
            assert _text(await sse.call_tool("add", {"a": 3, "b": 4})) == "7"
        # the second list is served from the one shared cache
        assert manager.get("real").cache.get("tools/list") is not None


@pytest.mark.asyncio
async def test_token_and_origin_apply_to_the_streamable_endpoint_too():
    async with running_hub(HubConfig(authToken="s3cret")) as (_manager, base):
        async with mcp_client(base, headers={"Authorization": "Bearer s3cret"}) as session:
            assert _text(await session.call_tool("add", {"a": 1, "b": 1})) == "2"
        async with httpx.AsyncClient() as http:
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}}
            h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
            assert (await http.post(f"{base}/real/mcp", json=init, headers=h)).status_code == 401
            evil = {**h, "Authorization": "Bearer s3cret", "Origin": "https://evil.example"}
            assert (await http.post(f"{base}/real/mcp", json=init, headers=evil)).status_code == 403


@pytest.mark.asyncio
async def test_an_unknown_session_id_gets_404_so_the_client_reinitialises():
    async with running_hub() as (manager, base):
        async with httpx.AsyncClient() as http:
            h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                 "Mcp-Session-Id": "bogus-or-from-before-a-restart"}
            body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            r = await http.post(f"{base}/real/mcp", json=body, headers=h)
            assert r.status_code == 404
            assert r.json()["error"]["message"] == "Session not found"
        assert _sessions(manager) == 0


@pytest.mark.asyncio
async def test_junk_requests_without_a_session_do_not_leak_sessions():
    async with running_hub() as (manager, base):
        async with httpx.AsyncClient() as http:
            for method in ("GET", "DELETE"):
                r = await http.request(method, f"{base}/real/mcp", headers={"Accept": "text/event-stream"})
                assert r.status_code < 500
            r = await http.post(f"{base}/real/mcp", content="not json",
                                headers={"Accept": "application/json, text/event-stream",
                                         "Content-Type": "application/json"})
            assert 400 <= r.status_code < 500
        await asyncio.sleep(0.2)
        assert _sessions(manager) == 0


@pytest.mark.asyncio
async def test_streamable_sessions_survive_a_config_change_to_another_server():
    async with running_hub() as (manager, base):
        async with mcp_client(base) as session:
            assert _text(await session.call_tool("add", {"a": 1, "b": 1})) == "2"
            async with httpx.AsyncClient() as http:
                r = await http.post(f"{base}/api/servers/other", json={"enabled": False, "command": "x", "args": []})
                assert r.status_code == 200
            assert _text(await session.call_tool("add", {"a": 2, "b": 2})) == "4"
            assert _sessions(manager) == 1


@pytest.mark.asyncio
async def test_replacing_the_server_entry_ends_its_streamable_sessions():
    async with running_hub() as (manager, base):
        async with mcp_client(base) as session:
            assert _text(await session.call_tool("add", {"a": 1, "b": 1})) == "2"
            async with httpx.AsyncClient() as http:
                r = await http.post(f"{base}/api/servers/real",
                                    json={"enabled": False, "command": "x", "args": []})  # replaces the entry
                assert r.status_code == 200
            for _ in range(100):
                if "real" not in manager.app.state.endpoints:
                    break
                await asyncio.sleep(0.05)
            assert "real" not in manager.app.state.endpoints


@pytest.mark.asyncio
async def test_upstream_crash_over_streamable_is_answered_then_recovers(monkeypatch):
    monkeypatch.setattr(manager_module, "RESTART_BASE_DELAY", 0.1)
    monkeypatch.setattr(manager_module, "RESTART_MAX_DELAY", 0.3)
    async with running_hub() as (manager, base):
        server = manager.get("real")
        async with mcp_client(base) as session:
            before = _text(await session.call_tool("whoami", {}))
            with pytest.raises(MCPError) as lost:
                await session.call_tool("die", {})
            assert lost.value.code == -32000
            for _ in range(100):
                if server.status == "running" and server.restart_count >= 1:
                    break
                await asyncio.sleep(0.05)
            assert _text(await session.call_tool("whoami", {})) != before  # same session, new process
            assert _sessions(manager) == 1


@pytest.mark.asyncio
async def test_isolated_mode_over_streamable_gives_each_client_its_own_process_and_cleans_up():
    async with running_hub(concurrency="isolated") as (manager, base):
        server = manager.get("real")
        async with mcp_client(base) as a, mcp_client(base) as b:
            pid_a = _text(await a.call_tool("whoami", {}))
            pid_b = _text(await b.call_tool("whoami", {}))
            assert pid_a != pid_b
            procs = [c.process for c in server.children]
            assert len(procs) == 2
        for _ in range(100):
            if not server.children and all(p.returncode is not None for p in procs):
                break
            await asyncio.sleep(0.05)
        assert server.children == set() and all(p.returncode is not None for p in procs)
