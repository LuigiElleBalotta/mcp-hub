# tests/test_service.py
"""`type: "service"` servers: plain long-running processes (e.g. an HTTP
server) the hub only starts/stops/monitors -- never proxied as MCP."""
import asyncio
import json
import socket
import sys

import httpx
import psutil
import pytest

from mcp_hub.claude_config import apply_servers
from mcp_hub.config import Config, HubConfig, ServerConfig, load_config, save_config
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager

# Serves GET /health on the given port, prints the pid of a spawned child
# (to prove the whole tree is stopped) and then serves forever.
_SERVER_SCRIPT = """
import subprocess, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
delay = float(sys.argv[2])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
print("child=%d" % child.pid, flush=True)
time.sleep(delay)
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _service(port: int, delay: float = 0.0, **overrides) -> ServerConfig:
    kwargs = dict(
        enabled=True, command=sys.executable,
        args=["-c", _SERVER_SCRIPT, str(port), str(delay)],
        type="service", healthUrl=f"http://127.0.0.1:{port}/health",
        healthTimeout=15,
    )
    kwargs.update(overrides)
    return ServerConfig(**kwargs)


async def _wait_status(server, wanted: str, timeout: float = 15) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while server.status != wanted:
        assert asyncio.get_running_loop().time() < deadline, f"status stuck at {server.status!r}"
        await asyncio.sleep(0.1)


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_mcp_entries_keep_their_original_shape_on_disk(tmp_path):
    path = tmp_path / "config.json"
    port = 18999
    cfg = Config(hub=HubConfig(), servers={
        "m": ServerConfig(enabled=True, command="npx", args=["x"]),
        "s": _service(port, cwd="C:/x", autostart=True),
    })
    save_config(cfg, path)
    raw = json.loads(path.read_text(encoding="utf-8"))["servers"]
    assert set(raw["m"]) == {"enabled", "command", "args", "env", "concurrency"}
    assert raw["s"]["type"] == "service" and raw["s"]["cwd"] == "C:/x"
    assert load_config(path) == cfg


def test_legacy_config_without_type_loads_as_mcp(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hub": {}, "servers": {"m": {"enabled": True, "command": "a", "args": []}}}))
    assert load_config(path).servers["m"].type == "mcp"


def test_starts_with_hub_needs_autostart_for_services():
    assert ServerConfig(enabled=True, command="a", args=[]).starts_with_hub
    assert not _service(1, autostart=False).starts_with_hub
    assert _service(1, autostart=True).starts_with_hub
    assert not _service(1, autostart=True, enabled=False).starts_with_hub


def test_port_defaults_to_health_url_port():
    assert _service(18017).effective_port == 18017
    assert ServerConfig(enabled=True, command="a", args=[], type="service", healthUrl="http://127.0.0.1:18017/h", port=9).effective_port == 9


@pytest.mark.asyncio
async def test_start_waits_for_health_then_stop_kills_tree_and_frees_port():
    port = _free_port()
    manager = HubManager(Config(hub=HubConfig(), servers={"svc": _service(port, delay=1.0)}))
    svc = manager.get("svc")
    await svc.start()
    assert svc.status == "starting"  # spawned, health not answering yet
    await _wait_status(svc, "running")
    assert _port_open(port)
    child_line = next(line for line in svc.logs if line.startswith("child="))
    child_pid = int(child_line.split("=")[1])
    assert psutil.pid_exists(child_pid)

    await svc.stop()
    assert svc.status == "stopped"
    assert not _port_open(port)
    await asyncio.sleep(0.3)
    assert not psutil.pid_exists(child_pid)


@pytest.mark.asyncio
async def test_start_is_idempotent_while_running():
    port = _free_port()
    manager = HubManager(Config(hub=HubConfig(), servers={"svc": _service(port)}))
    svc = manager.get("svc")
    await svc.start()
    pid = svc.process.pid
    await svc.start()
    assert svc.process.pid == pid
    await svc.stop()


@pytest.mark.asyncio
async def test_port_already_in_use_is_refused_and_foreign_process_untouched():
    port = _free_port()
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", port))
        blocker.listen()
        manager = HubManager(Config(hub=HubConfig(), servers={"svc": _service(port)}))
        svc = manager.get("svc")
        await svc.start()
        assert svc.status == "crashed"
        assert svc.process is None
        assert any("already in use" in line for line in svc.logs)


@pytest.mark.asyncio
async def test_unexpected_exit_is_reported_as_crashed():
    cfg = ServerConfig(enabled=True, command=sys.executable, args=["-c", "import sys; print('boom'); sys.exit(3)"],
                       type="service")
    manager = HubManager(Config(hub=HubConfig(), servers={"svc": cfg}))
    svc = manager.get("svc")
    await svc.start()
    await _wait_status(svc, "crashed")
    assert "boom" in svc.logs


@pytest.mark.asyncio
async def test_health_timeout_stops_the_service_and_reports_crashed():
    port = _free_port()
    cfg = _service(port, delay=60, healthTimeout=1.5)
    manager = HubManager(Config(hub=HubConfig(), servers={"svc": cfg}))
    svc = manager.get("svc")
    await svc.start()
    await _wait_status(svc, "crashed", timeout=15)
    assert svc.process.returncode is not None


@pytest.mark.asyncio
async def test_start_all_skips_services_without_autostart():
    port = _free_port()
    manager = HubManager(Config(hub=HubConfig(), servers={"svc": _service(port)}))
    await manager.start_all()
    assert manager.get("svc").status == "stopped"


@pytest.mark.asyncio
async def test_api_start_stop_status_and_no_sse_route(tmp_path, monkeypatch):
    monkeypatch.setattr("mcp_hub.management_api.save_config", lambda *a, **k: None)
    port = _free_port()
    manager = HubManager(Config(hub=HubConfig(), servers={}))
    app = build_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://hub") as client:
        body = {k: v for k, v in _service(port).__dict__.items()}
        resp = await client.post("/api/servers/svc", json=body)
        assert resp.status_code == 200
        assert resp.json() == {"status": "stopped"}  # autostart is off: manual start
        status = (await client.get("/api/status")).json()["servers"]["svc"]
        assert status["type"] == "service"
        assert (await client.get("/svc/sse")).status_code == 404

        assert (await client.post("/api/servers/svc/start")).json()["status"] in ("starting", "running")
        await _wait_status(manager.get("svc"), "running")
        assert any("healthy" in line for line in (await client.get("/api/servers/svc/logs")).json()["lines"])
        assert (await client.post("/api/servers/svc/stop")).json() == {"status": "stopped"}
        assert not _port_open(port)


@pytest.mark.asyncio
async def test_api_rejects_invalid_type_and_commandless_service(monkeypatch):
    monkeypatch.setattr("mcp_hub.management_api.save_config", lambda *a, **k: None)
    manager = HubManager(Config(hub=HubConfig(), servers={}))
    transport = httpx.ASGITransport(app=build_app(manager))
    async with httpx.AsyncClient(transport=transport, base_url="http://hub") as client:
        bad_type = {"enabled": False, "command": "x", "args": [], "type": "daemon"}
        assert (await client.post("/api/servers/a", json=bad_type)).status_code == 400
        no_cmd = {"enabled": False, "command": "", "args": [], "type": "service"}
        assert (await client.post("/api/servers/b", json=no_cmd)).status_code == 400
        assert (await client.post("/api/servers/c", json={"bogus": 1})).status_code == 400


def test_apply_skips_services(tmp_path):
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{}", encoding="utf-8")
    cfg = Config(hub=HubConfig(), servers={
        "m": ServerConfig(enabled=True, command="npx", args=[]),
        "s": _service(18017),
    })
    assert apply_servers(claude_json, cfg) == ["m"]
    assert "s" not in json.loads(claude_json.read_text(encoding="utf-8"))["mcpServers"]
