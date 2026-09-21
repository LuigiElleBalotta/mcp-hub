# tests/test_manager.py
import asyncio
import sys
import pytest
from mcp_hub.config import Config, HubConfig, ServerConfig
from mcp_hub.manager import HubManager


def _python_sleep_config(seconds: float = 0.2) -> ServerConfig:
    return ServerConfig(enabled=True, command=sys.executable,
                         args=["-c", f"import time; print('ready'); time.sleep({seconds})"],
                         env={}, concurrency="exclusive")


@pytest.mark.asyncio
async def test_start_all_marks_enabled_servers_running():
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config()})
    manager = HubManager(cfg)
    await manager.start_all()
    await asyncio.sleep(0.05)
    assert manager.get("a").status == "running"
    await manager.stop_all()


@pytest.mark.asyncio
async def test_disabled_server_is_never_started():
    disabled = _python_sleep_config()
    disabled.enabled = False
    cfg = Config(hub=HubConfig(), servers={"a": disabled})
    manager = HubManager(cfg)
    await manager.start_all()
    assert manager.get("a").status == "stopped"


@pytest.mark.asyncio
async def test_stop_sets_status_stopped():
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config(seconds=5)})
    manager = HubManager(cfg)
    await manager.start_all()
    await manager.get("a").stop()
    assert manager.get("a").status == "stopped"


@pytest.mark.asyncio
async def test_crash_is_detected():
    crashing = ServerConfig(enabled=True, command=sys.executable,
                             args=["-c", "import sys; sys.exit(1)"], env={})
    cfg = Config(hub=HubConfig(), servers={"a": crashing})
    manager = HubManager(cfg)
    await manager.start_all()
    await asyncio.sleep(0.3)
    assert manager.get("a").status == "crashed"


def test_status_snapshot_reflects_all_servers():
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config(), "b": _python_sleep_config()})
    manager = HubManager(cfg)
    assert manager.status_snapshot() == {"a": "stopped", "b": "stopped"}


def test_append_log_redacts_secret_like_lines():
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config()})
    manager = HubManager(cfg)
    server = manager.get("a")
    server.append_log("GITLAB_PERSONAL_ACCESS_TOKEN=glpat-realsecret")
    assert "glpat-realsecret" not in server.logs[-1]


def test_upsert_adds_a_new_server_to_both_config_and_manager():
    cfg = Config(hub=HubConfig(), servers={})
    manager = HubManager(cfg)
    new_server = _python_sleep_config()
    managed = manager.upsert("b", new_server)
    assert managed.name == "b"
    assert manager.get("b") is managed
    assert cfg.servers["b"] is new_server


def test_upsert_replaces_an_existing_server():
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config()})
    manager = HubManager(cfg)
    replacement = _python_sleep_config(seconds=1)
    manager.upsert("a", replacement)
    assert manager.get("a").config is replacement
