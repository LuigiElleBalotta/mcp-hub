"""hub-proxy-hardening R6: the hub pings its upstreams and restarts one that is
alive but no longer answering."""
import asyncio
import json
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from mcp_hub import manager as manager_module
from mcp_hub.config import Config, HubConfig, ServerConfig, load_config, save_config
from mcp_hub.hub_app import _proxy, build_app
from mcp_hub.manager import HubManager, ManagedServer

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch):
    monkeypatch.setattr(manager_module, "RESTART_BASE_DELAY", 0.05)
    monkeypatch.setattr(manager_module, "RESTART_MAX_DELAY", 0.2)
    monkeypatch.setattr(manager_module, "HEALTH_PING_TIMEOUT", 0.15)


def _cfg(**kw) -> ServerConfig:
    base = dict(enabled=True, command=sys.executable, args=[FAKE_SERVER], env={}, concurrency="parallel",
                healthInterval=0.05, healthFailures=2)
    base.update(kw)
    return ServerConfig(**base)


async def _wait_for(predicate, timeout: float = 6.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


async def _ready(server: ManagedServer) -> None:
    await server.start()
    await server.ensure_ready()  # the hub's handshake starts the ping loop


@pytest.mark.asyncio
async def test_a_healthy_server_is_pinged_and_left_alone():
    server = ManagedServer("fake", _cfg())
    await _ready(server)
    try:
        await asyncio.sleep(0.6)
        resp = await server.hub_request("test/ping_count", {}, timeout=2.0)
        assert resp["result"]["pings"] >= 3
        assert server.status == "running" and server.restart_count == 0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_pings_are_off_when_the_interval_is_zero():
    server = ManagedServer("fake", _cfg(healthInterval=0))
    await _ready(server)
    try:
        await asyncio.sleep(0.4)
        assert (await server.hub_request("test/ping_count", {}, timeout=2.0))["result"]["pings"] == 0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_an_unresponsive_server_is_terminated_and_restarted():
    server = ManagedServer("fake", _cfg(env={"FAKE_IGNORE_PING": "1"}, restartMaxAttempts=1))
    await _ready(server)
    first_pid = server.process.pid
    try:
        await _wait_for(lambda: any("is unresponsive" in l for l in server.logs))
        await _wait_for(lambda: server.restart_count == 1)  # the policy of R5 took over
        assert any("did not answer ping (2/2)" in l for l in server.logs)
        await _wait_for(lambda: server.process.pid != first_pid or server.status == "crashed")
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_unresponsive_with_restart_never_ends_crashed_not_running():
    server = ManagedServer("fake", _cfg(env={"FAKE_IGNORE_PING": "1"}, restart="never"))
    await _ready(server)
    try:
        await _wait_for(lambda: any("is unresponsive" in l for l in server.logs))
        await _wait_for(lambda: server.status == "crashed")
        await asyncio.sleep(0.3)
        assert server.restart_count == 0 and server.status == "crashed"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_server_busy_with_a_client_request_is_not_judged_by_a_late_ping():
    """The fake is single-threaded: during `test/sleep` it cannot answer a ping.
    That is busy, not hung: pings are skipped while a client request is in flight."""
    server = ManagedServer("fake", _cfg(healthFailures=1))
    await _ready(server)
    rs, rr = anyio.create_memory_object_stream(16)
    ws, wr = anyio.create_memory_object_stream(16)
    task = asyncio.create_task(_proxy(rr, ws, server))
    try:
        msg = {"jsonrpc": "2.0", "id": 1, "method": "test/sleep", "params": {"seconds": 1.0}}
        await rs.send(SessionMessage(jsonrpc_message_adapter.validate_python(msg)))
        with anyio.fail_after(5.0):
            reply = await wr.receive()
        assert reply.message.model_dump(by_alias=True, exclude_unset=True)["result"] == {"slept": 1.0}
        assert not any("unresponsive" in l for l in server.logs)
        assert server.restart_count == 0 and server.status == "running"
    finally:
        await rs.aclose()
        await task
        await server.stop()


@pytest.mark.asyncio
async def test_stop_cancels_the_ping_loop():
    server = ManagedServer("fake", _cfg())
    await _ready(server)
    assert server._ping_task is not None
    await server.stop()
    assert server._ping_task is None
    await asyncio.sleep(0.2)
    assert not any("did not answer ping" in l for l in server.logs)


def test_health_settings_keep_the_old_config_shape_and_round_trip(tmp_path):
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(), servers={
        "plain": ServerConfig(enabled=True, command="x", args=[]),
        "tuned": ServerConfig(enabled=True, command="x", args=[], healthInterval=5.0, healthFailures=2),
    }), path)
    raw = json.loads(path.read_text(encoding="utf-8"))["servers"]
    assert "healthInterval" not in raw["plain"] and "healthFailures" not in raw["plain"]
    assert raw["tuned"]["healthInterval"] == 5.0 and raw["tuned"]["healthFailures"] == 2
    loaded = load_config(path).servers
    assert loaded["plain"].healthInterval == 30.0 and loaded["tuned"].healthFailures == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"healthInterval": -1}, {"healthInterval": True}, {"healthFailures": 0}])
async def test_api_rejects_invalid_health_settings(bad):
    app = build_app(HubManager(Config(hub=HubConfig(), servers={})))
    body = {"enabled": False, "command": "x", "args": [], **bad}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:37450") as c:
        assert (await c.post("/api/servers/a", json=body)).status_code == 400
        ok = await c.post("/api/servers/a", json={"enabled": False, "command": "x", "args": [], "healthInterval": 0})
        assert ok.status_code == 200
