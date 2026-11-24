"""hub-proxy-hardening R5: automatic restart with backoff and a circuit breaker."""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from mcp_hub import manager as manager_module
from mcp_hub.config import Config, HubConfig, ServerConfig, load_config, save_config
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager, ManagedServer

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(manager_module, "RESTART_BASE_DELAY", 0.05)
    monkeypatch.setattr(manager_module, "RESTART_MAX_DELAY", 0.2)


def _cfg(**kw) -> ServerConfig:
    base = dict(enabled=True, command=sys.executable, args=[FAKE_SERVER], env={}, concurrency="parallel")
    base.update(kw)
    return ServerConfig(**base)


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


def _kill(server: ManagedServer) -> None:
    server.process.kill()


@pytest.mark.asyncio
async def test_unexpected_exit_is_followed_by_a_restart():
    server = ManagedServer("fake", _cfg())
    await server.start()
    first_pid = server.process.pid
    try:
        _kill(server)
        await _wait_for(lambda: server.status == "restarting" or server.process.pid != first_pid)
        await _wait_for(lambda: server.status == "running" and server.process.pid != first_pid)
        assert server.restart_count == 1
        assert any("restarting fake" in line for line in server.logs)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_restart_never_leaves_the_server_crashed():
    server = ManagedServer("fake", _cfg(restart="never"))
    await server.start()
    try:
        _kill(server)
        await _wait_for(lambda: server.status == "crashed")
        await asyncio.sleep(0.4)  # plenty of time for a restart that must not happen
        assert server.status == "crashed" and server.restart_count == 0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_deliberate_stop_is_never_restarted():
    server = ManagedServer("fake", _cfg())
    await server.start()
    await server.stop()
    await asyncio.sleep(0.4)
    assert server.status == "stopped" and server.restart_count == 0
    assert server.process.returncode is not None


@pytest.mark.asyncio
async def test_circuit_opens_after_max_attempts_in_the_window():
    # Dies the instant it starts: every respawn fails too.
    server = ManagedServer("dies", _cfg(args=["-c", "import sys; sys.exit(3)"], restartMaxAttempts=2))
    await server.start()
    try:
        await _wait_for(lambda: server.status == "crashed" and any("giving up" in l for l in server.logs))
        exits = [l for l in server.logs if "exited with code 3" in l]
        assert len(exits) == 3, "the first run plus exactly restartMaxAttempts restarts"
        assert server.restart_count == 2
        await asyncio.sleep(0.4)
        assert len([l for l in server.logs if "exited with code 3" in l]) == 3, "circuit open: no more attempts"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_manual_start_overrides_a_pending_restart_and_rearms_the_circuit(monkeypatch):
    monkeypatch.setattr(manager_module, "RESTART_BASE_DELAY", 30.0)  # the backoff would outlast the test
    monkeypatch.setattr(manager_module, "RESTART_MAX_DELAY", 30.0)
    server = ManagedServer("fake", _cfg())
    await server.start()
    try:
        _kill(server)
        await _wait_for(lambda: server.status == "restarting")
        pending = server._restart_task
        await server.start()
        assert server.status == "running"
        assert pending.cancelled() or pending.done()
        assert len(server._restart_times) == 0, "a manual start re-arms the circuit breaker"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_stop_during_backoff_cancels_the_restart(monkeypatch):
    monkeypatch.setattr(manager_module, "RESTART_BASE_DELAY", 30.0)
    monkeypatch.setattr(manager_module, "RESTART_MAX_DELAY", 30.0)
    server = ManagedServer("fake", _cfg())
    await server.start()
    _kill(server)
    await _wait_for(lambda: server.status == "restarting")
    await server.stop()
    assert server.status == "stopped"
    assert server._restart_task is None


def test_backoff_doubles_up_to_the_cap(monkeypatch):
    monkeypatch.setattr(manager_module, "RESTART_BASE_DELAY", 1.0)
    monkeypatch.setattr(manager_module, "RESTART_MAX_DELAY", 60.0)
    monkeypatch.setattr(manager_module.random, "uniform", lambda a, b: 1.0)
    server = ManagedServer("fake", _cfg())
    assert [server._restart_delay(n) for n in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]


def test_backoff_has_jitter_within_twenty_percent(monkeypatch):
    monkeypatch.setattr(manager_module, "RESTART_BASE_DELAY", 10.0)
    monkeypatch.setattr(manager_module, "RESTART_MAX_DELAY", 60.0)  # the autouse fixture lowers the cap
    server = ManagedServer("fake", _cfg())
    delays = {server._restart_delay(0) for _ in range(50)}
    assert len(delays) > 1 and all(8.0 <= d <= 12.0 for d in delays)


def test_attempts_older_than_the_window_are_forgotten(monkeypatch):
    server = ManagedServer("fake", _cfg(restartMaxAttempts=2, restartWindow=100.0))
    now = 1000.0
    monkeypatch.setattr(manager_module.time, "monotonic", lambda: now)
    server._restart_times.extend([now - 500.0, now - 450.0])  # both outside the window
    # With stale entries dropped there is budget again, so it schedules a restart
    # (the task needs a running loop; use a stub to keep this a pure unit test).
    created = []
    monkeypatch.setattr(manager_module.asyncio, "create_task", lambda coro: (coro.close(), created.append(1))[1])
    server._schedule_restart()
    assert server.status == "restarting" and len(server._restart_times) == 1 and created


@pytest.mark.asyncio
async def test_a_restarted_upstream_serves_the_same_connection_again():
    """End to end through the proxy: after an upstream crash the client keeps
    its connection, gets an error for the lost request, and is served by the
    respawned process (which the hub re-initialises) without reconnecting."""
    import anyio
    from mcp.shared.message import SessionMessage
    from mcp_types import jsonrpc_message_adapter

    from mcp_hub.hub_app import _proxy

    server = ManagedServer("fake", _cfg())
    await server.start()
    rs, rr = anyio.create_memory_object_stream(16)
    ws, wr = anyio.create_memory_object_stream(16)
    task = asyncio.create_task(_proxy(rr, ws, server))

    async def call(req_id, method, params=None):
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        await rs.send(SessionMessage(jsonrpc_message_adapter.validate_python(msg)))
        with anyio.fail_after(5.0):
            got = await wr.receive()
        return got.message.model_dump(by_alias=True, exclude_unset=True)

    try:
        assert (await call(1, "test/echo", {"value": "a"}))["result"] == {"echo": "a"}
        lost = await call(2, "test/die")
        assert lost["error"]["code"] == -32000
        await _wait_for(lambda: server.status == "running" and server.restart_count == 1)
        assert (await call(3, "test/echo", {"value": "b"}))["result"] == {"echo": "b"}
        stats = (await call(4, "test/handshake_stats"))["result"]
        assert stats["initializes"] == 1, "the new process was initialised by the hub"
    finally:
        await rs.aclose()
        await task
        await server.stop()


def test_restart_settings_keep_the_old_config_shape_and_round_trip(tmp_path):
    path = tmp_path / "config.json"
    plain = _cfg()
    tuned = _cfg(restart="never", restartMaxAttempts=3, restartWindow=60.0)
    save_config(Config(hub=HubConfig(), servers={"plain": plain, "tuned": tuned}), path)
    raw = json.loads(path.read_text(encoding="utf-8"))["servers"]
    assert "restart" not in raw["plain"] and "restartMaxAttempts" not in raw["plain"]
    assert raw["tuned"]["restart"] == "never" and raw["tuned"]["restartMaxAttempts"] == 3
    loaded = load_config(path).servers
    assert loaded["plain"].restart == "on-failure" and loaded["tuned"].restartWindow == 60.0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"restart": "always"},
    {"restartMaxAttempts": 0},
    {"restartMaxAttempts": True},
    {"restartWindow": -1},
])
async def test_api_rejects_invalid_restart_settings(bad):
    hub = HubManager(Config(hub=HubConfig(), servers={}))
    app = build_app(hub)
    body = {"enabled": False, "command": "x", "args": [], **bad}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:37450") as c:
        assert (await c.post("/api/servers/a", json=body)).status_code == 400
        ok = await c.post("/api/servers/a", json={"enabled": False, "command": "x", "args": [], "restart": "never"})
        assert ok.status_code == 200
