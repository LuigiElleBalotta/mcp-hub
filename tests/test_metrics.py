"""hub-gui-and-clients R1: per-server metrics for the dashboard."""
import asyncio
import json
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from mcp_hub import metrics as metrics_module
from mcp_hub.config import Config, HubConfig, ServerConfig, hub_to_dict, load_config, save_config
from mcp_hub.hub_app import _proxy, build_app
from mcp_hub.manager import HubManager
from mcp_hub.metrics import RECENT, WINDOW, ServerMetrics, label_for

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")


# ------------------------------------------------------------------------- unit

def test_percentiles_use_nearest_rank_over_the_last_window():
    m = ServerMetrics()
    for ms in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
        m.record("x", ms / 1000, True)
    assert m.percentile(50) == 50.0 and m.percentile(95) == 100.0 and m.percentile(10) == 10.0
    assert ServerMetrics().percentile(50) is None


def test_windows_are_bounded():
    m = ServerMetrics()
    for i in range(WINDOW + 50):
        m.record(f"m{i}", 0.001, True)
    assert m.calls == WINDOW + 50 and len(m._durations) == WINDOW
    recent = m.recent()
    assert len(recent) == RECENT and recent[0]["method"] == f"m{WINDOW + 49}", "newest first"


def test_errors_and_last_error():
    m = ServerMetrics()
    m.record("a", 0.01, True)
    m.record("b", 0.01, False, error="boom " * 200)
    assert m.errors == 1 and m.calls == 2 and len(m.last_error) == 300
    assert m.summary()["lastError"] == m.last_error


def test_calls_per_minute_only_counts_the_last_sixty_seconds(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(metrics_module.time, "monotonic", lambda: now[0])
    m = ServerMetrics()
    m.record("old", 0.001, True)
    now[0] += 61
    m.record("new1", 0.001, True)
    m.record("new2", 0.001, True)
    assert m.calls_per_minute() == 2 and m.calls == 3


def test_disabled_metrics_record_nothing():
    m = ServerMetrics(enabled=False)
    m.record("a", 0.1, False, error="x")
    m.record_cache_hit()
    assert m.calls == m.errors == m.cache_hits == 0 and m.recent() == []


def test_label_names_the_tool_but_never_reads_its_arguments():
    assert label_for("tools/list", {}) == "tools/list"
    assert label_for("tools/call", {"name": "read_file", "arguments": {"password": "hunter2"}}) == "tools/call read_file"
    assert label_for("tools/call", {"arguments": {}}) == "tools/call"
    assert len(label_for("tools/call", {"name": "x" * 500})) == 80


def test_config_flag_keeps_the_old_shape(tmp_path):
    assert "metrics" not in hub_to_dict(HubConfig())
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(metrics=False), servers={}), path)
    assert json.loads(path.read_text(encoding="utf-8"))["hub"]["metrics"] is False
    assert load_config(path).hub.metrics is False


# ------------------------------------------------------------------ through the proxy

def _msg(obj):
    return SessionMessage(jsonrpc_message_adapter.validate_python(obj))


class Conn:
    def __init__(self, server):
        self.server = server
        self.upstream = server.session_upstream()
        self._rs, self._rr = anyio.create_memory_object_stream(32)
        self._ws, self._wr = anyio.create_memory_object_stream(32)
        self.task = asyncio.create_task(_proxy(self._rr, self._ws, self.upstream))

    async def call(self, req_id, method, params=None):
        await self._rs.send(_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}))
        while True:
            with anyio.fail_after(5.0):
                m = (await self._wr.receive()).message.model_dump(by_alias=True, exclude_unset=True)
            if m.get("id") == req_id:
                return m

    async def close(self):
        await self._rs.aclose()
        with anyio.fail_after(5.0):
            await self.task
        await self.server.release_session_upstream(self.upstream)


def _hub(mode="parallel", **hub_kw):
    cfg = ServerConfig(enabled=True, command=sys.executable, args=[FAKE_SERVER], env={},
                       concurrency=mode, restart="never")
    return HubManager(Config(hub=HubConfig(**hub_kw), servers={"fake": cfg}))


@pytest.mark.asyncio
async def test_calls_errors_latency_and_cache_hits_are_counted():
    hub = _hub()
    await hub.start_all()
    server = hub.get("fake")
    c = Conn(server)
    try:
        await c.call(1, "test/sleep", {"seconds": 0.15})
        await c.call(2, "tools/list")
        await c.call(3, "tools/list")  # served from the cache: a hit, not an upstream call
        assert server.metrics.cache_hits == 1
        slow = next(r for r in server.metrics.recent() if r["method"] == "test/sleep")
        assert slow["ms"] >= 140 and slow["ok"] is True
        before = server.metrics.calls
        await c.call(4, "test/die")  # the upstream dies mid-call
        assert server.metrics.errors == 1 and "subprocess" in server.metrics.last_error
        assert server.metrics.calls == before + 1
        assert server.metrics.percentile(95) is not None
        await c.close()
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_arguments_and_results_never_reach_the_metrics():
    hub = _hub()
    await hub.start_all()
    server = hub.get("fake")
    c = Conn(server)
    try:
        await c.call(1, "tools/call", {"name": "read_file", "arguments": {"password": "hunter2", "path": "C:/secret"}})
        dumped = json.dumps([server.metrics.recent(), server.metrics_detail(), hub.status_snapshot()])
        assert "hunter2" not in dumped and "C:/secret" not in dumped
        assert "tools/call read_file" in dumped, "the tool NAME is kept, nothing else"
        await c.close()
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_status_snapshot_and_detail_report_pid_uptime_sessions():
    hub = _hub()
    await hub.start_all()
    server = hub.get("fake")
    a, b = Conn(server), Conn(server)
    try:
        await a.call(1, "test/echo", {"value": 1})
        entry = hub.status_snapshot()["fake"]
        assert entry["pid"] == server.process.pid and entry["sessions"] == 2 and entry["inFlight"] == 0
        assert entry["calls"] >= 1 and entry["uptime"] >= 0 and "p95" in entry and "callsPerMin" in entry
        detail = server.metrics_detail()
        assert detail["restarts"] == 0 and detail["cacheHits"] == 0
        await a.close()
        assert hub.status_snapshot()["fake"]["sessions"] == 1
        await b.close()
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_an_isolated_server_aggregates_its_sessions_processes():
    hub = _hub("isolated")
    parent = hub.get("fake")
    a, b = Conn(parent), Conn(parent)
    try:
        await a.call(1, "test/echo", {"value": 1})
        await b.call(1, "test/echo", {"value": 2})
        await b.call(2, "test/echo", {"value": 3})
        assert parent.metrics.calls == 3, "the server's numbers cover all of its sessions"
        assert a.upstream.metrics.calls == 1 and b.upstream.metrics.calls == 2
        entry = hub.status_snapshot()["fake"]
        assert entry["sessions"] == 2 and entry["calls"] == 3
        await a.close()
        await b.close()
    finally:
        await hub.stop_all()


def test_metrics_can_be_switched_off_and_status_omits_the_fields():
    hub = _hub(metrics=False)
    assert hub.get("fake").metrics.enabled is False
    assert "calls" not in hub.status_snapshot()["fake"]


@pytest.mark.asyncio
async def test_metrics_route_shape_order_and_unknown_server():
    hub = _hub()
    await hub.start_all()
    server = hub.get("fake")
    c = Conn(server)
    app = build_app(hub)
    try:
        await c.call(1, "test/echo", {"value": 1})
        await c.call(2, "tools/list")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:37450") as api:
            body = (await api.get("/api/servers/fake/metrics")).json()
            assert body["pid"] == server.process.pid and body["status"] == "running" and body["enabled"] is True
            assert {"calls", "errors", "p50", "p95", "callsPerMin", "uptime", "sessions", "inFlight"} <= set(body)
            assert [r["method"] for r in body["recent"]][:2] == ["tools/list", "test/echo"], "newest first"
            assert set(body["recent"][0]) == {"at", "method", "ms", "ok"}
            assert (await api.get("/api/servers/nope/metrics")).status_code == 404
        await c.close()
    finally:
        await hub.stop_all()
