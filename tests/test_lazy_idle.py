"""hub-proxy-efficiency R2: lazy start, idle shutdown, persistent."""
import asyncio
import json
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from mcp_hub.config import Config, HubConfig, ServerConfig, load_config, save_config
from mcp_hub.hub_app import _proxy, build_app
from mcp_hub.manager import HubManager, ManagedServer

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")


def _cfg(**kw) -> ServerConfig:
    base = dict(enabled=True, command=sys.executable, args=[FAKE_SERVER], env={}, concurrency="parallel",
                restart="never", startMode="lazy")
    base.update(kw)
    return ServerConfig(**base)


def _msg(obj):
    return SessionMessage(jsonrpc_message_adapter.validate_python(obj))


class Conn:
    def __init__(self, managed):
        self._rs, self._rr = anyio.create_memory_object_stream(32)
        self._ws, self._wr = anyio.create_memory_object_stream(32)
        self.task = asyncio.create_task(_proxy(self._rr, self._ws, managed))

    async def call(self, req_id, method, params=None, timeout=5.0):
        await self._rs.send(_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}))
        while True:
            with anyio.fail_after(timeout):
                m = (await self._wr.receive()).message.model_dump(by_alias=True, exclude_unset=True)
            if m.get("id") == req_id:
                return m

    async def close(self):
        await self._rs.aclose()
        with anyio.fail_after(5.0):
            await self.task


async def _wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}}


def test_a_lazy_server_does_not_start_with_the_hub_and_is_idle(tmp_path):
    assert _cfg().starts_with_hub is False
    assert ServerConfig(enabled=True, command="x", args=[]).starts_with_hub is True
    server = ManagedServer("fake", _cfg(), cache_dir=tmp_path)
    assert server.status == "idle" and server.process is None
    hub = HubManager(Config(hub=HubConfig(), servers={"fake": _cfg()}))
    assert hub.status_snapshot()["fake"]["status"] == "idle"


@pytest.mark.asyncio
async def test_the_first_request_spawns_it_and_the_next_list_comes_from_the_cache(tmp_path):
    server = ManagedServer("fake", _cfg(), cache_dir=tmp_path)
    c = Conn(server)
    try:
        assert server.process is None
        first = await c.call(1, "tools/list")
        assert server.status == "running" and server.process is not None
        assert first["result"]["tools"][0]["name"] == "t0"
        pid = server.process.pid
        await c.call(2, "tools/list")
        assert (await c.call(3, "test/list_calls"))["result"]["calls"]["tools/list"] == 1
        assert server.process.pid == pid
        await c.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_many_simultaneous_first_requests_spawn_exactly_once(tmp_path, monkeypatch):
    server = ManagedServer("fake", _cfg(), cache_dir=tmp_path)
    spawns = []
    real_spawn = server._spawn

    async def counting_spawn():
        spawns.append(1)
        await real_spawn()

    monkeypatch.setattr(server, "_spawn", counting_spawn)
    conns = [Conn(server) for _ in range(5)]
    try:
        replies = await asyncio.gather(*(c.call(1, "test/echo", {"value": i}) for i, c in enumerate(conns)))
        assert [r["result"]["echo"] for r in replies] == [0, 1, 2, 3, 4]
        assert len(spawns) == 1
        for c in conns:
            await c.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_disk_cache_answers_initialize_and_lists_of_a_never_spawned_server(tmp_path):
    first = ManagedServer("fake", _cfg(), cache_dir=tmp_path)
    c = Conn(first)
    try:
        init = await c.call(1, "initialize", INIT)
        await c.call(2, "tools/list")
        await c.close()
    finally:
        await first.stop()
    assert list(tmp_path.glob("fake.json")), "the first run left a cache file"

    second = ManagedServer("fake", _cfg(), cache_dir=tmp_path)  # a fresh hub, same config
    c2 = Conn(second)
    try:
        assert second.process is None and second.status == "idle"
        again = await c2.call(1, "initialize", INIT)
        tools = await c2.call(2, "tools/list")
        assert again["result"] == init["result"]
        assert tools["result"]["tools"][0]["name"] == "t0"
        assert second.process is None, "answered from disk: nothing was spawned"
        assert (await c2.call(3, "test/echo", {"value": "now"}))["result"] == {"echo": "now"}
        assert second.process is not None and second.status == "running"
        await c2.close()
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_a_changed_command_line_ignores_the_old_disk_cache(tmp_path):
    first = ManagedServer("fake", _cfg(), cache_dir=tmp_path)
    c = Conn(first)
    try:
        await c.call(1, "tools/list")
        await c.close()
    finally:
        await first.stop()
    changed = ManagedServer("fake", _cfg(args=[FAKE_SERVER, "--other"]), cache_dir=tmp_path)
    assert changed.initialize_result is None and changed.cache.get("tools/list") is None


@pytest.mark.asyncio
async def test_idle_server_is_stopped_then_respawned_on_demand(tmp_path):
    server = ManagedServer("fake", _cfg(idleTimeout=0.3, startMode="eager"), cache_dir=tmp_path)
    await server.start()
    c = Conn(server)
    try:
        assert (await c.call(1, "test/echo", {"value": "a"}))["result"] == {"echo": "a"}
        first = server.process
        await _wait_for(lambda: server.status == "idle" and first.returncode is not None, timeout=4.0)
        # (status flips to idle first on purpose: a request arriving now waits for the stop, then respawns)
        assert any("idle for" in line for line in server.logs)
        # the next request brings it back, with a fresh handshake
        assert (await c.call(2, "test/echo", {"value": "b"}))["result"] == {"echo": "b"}
        assert server.status == "running" and server.process is not first
        assert (await c.call(3, "test/handshake_stats"))["result"]["initializes"] == 1
        await c.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_request_in_flight_is_never_cut_off_by_the_idle_timeout(tmp_path):
    server = ManagedServer("fake", _cfg(idleTimeout=0.2, startMode="eager"), cache_dir=tmp_path)
    await server.start()
    c = Conn(server)
    try:
        reply = await c.call(1, "test/sleep", {"seconds": 1.0}, timeout=6.0)  # 5x the idle timeout
        assert reply["result"] == {"slept": 1.0}
        assert server.status == "running"
        await c.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_persistent_server_is_never_stopped_for_idleness(tmp_path):
    server = ManagedServer("fake", _cfg(idleTimeout=0.2, persistent=True, startMode="eager"), cache_dir=tmp_path)
    await server.start()
    c = Conn(server)
    try:
        await c.call(1, "test/echo", {"value": 1})
        await asyncio.sleep(0.8)
        assert server.status == "running"
        await c.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_after_an_idle_stop_initialize_is_still_answered_without_a_spawn(tmp_path):
    server = ManagedServer("fake", _cfg(idleTimeout=0.2, startMode="eager"), cache_dir=tmp_path)
    await server.start()
    c = Conn(server)
    try:
        await c.call(1, "initialize", INIT)
        stopped = server.process
        await _wait_for(lambda: server.status == "idle" and stopped.returncode is not None, timeout=4.0)
        again = await c.call(2, "initialize", INIT)
        assert again["result"]["serverInfo"]["name"] == "fake"
        assert server.status == "idle" and server.process.returncode is not None
        await c.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_lazy_server_that_cannot_start_answers_with_an_error(tmp_path):
    server = ManagedServer("ghost", _cfg(command="definitely-not-a-real-command-xyz", args=[]), cache_dir=tmp_path)
    c = Conn(server)
    try:
        reply = await c.call(1, "test/echo", {"value": 1})
        assert reply["error"]["code"] == -32000 and "failed to start" in reply["error"]["message"]
        assert server.status == "crashed"
        await c.close()  # the connection survived
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_deliberately_stopped_lazy_server_is_not_resurrected_by_a_request(tmp_path):
    server = ManagedServer("fake", _cfg(), cache_dir=tmp_path)
    c = Conn(server)
    try:
        await c.call(1, "test/echo", {"value": 1})
        await server.stop()
        assert server.status == "stopped"
        reply = await c.call(2, "test/echo", {"value": 2})
        assert reply["error"]["code"] == -32000
        assert server.status == "stopped" and server.process.returncode is not None
        await c.close()
    finally:
        await server.stop()


def test_new_settings_keep_the_old_shape_and_round_trip(tmp_path):
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(), servers={
        "plain": ServerConfig(enabled=True, command="x", args=[]),
        "lazy": ServerConfig(enabled=True, command="x", args=[], startMode="lazy", idleTimeout=60.0, persistent=True),
    }), path)
    raw = json.loads(path.read_text(encoding="utf-8"))["servers"]
    assert not {"startMode", "idleTimeout", "persistent"} & set(raw["plain"])
    assert raw["lazy"]["startMode"] == "lazy" and raw["lazy"]["idleTimeout"] == 60.0 and raw["lazy"]["persistent"]
    loaded = load_config(path).servers
    assert loaded["lazy"].startMode == "lazy" and loaded["plain"].idleTimeout == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"startMode": "sometimes"}, {"idleTimeout": -1}, {"idleTimeout": True},
                                 {"persistent": "yes"}])
async def test_api_rejects_invalid_lazy_settings(bad):
    app = build_app(HubManager(Config(hub=HubConfig(), servers={})))
    body = {"enabled": False, "command": "x", "args": [], **bad}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:37450") as c:
        assert (await c.post("/api/servers/a", json=body)).status_code == 400
        ok = await c.post("/api/servers/a", json={"enabled": False, "command": "x", "args": [],
                                                   "startMode": "lazy", "idleTimeout": 30})
        assert ok.status_code == 200
