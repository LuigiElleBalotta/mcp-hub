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


@pytest.mark.asyncio
async def test_start_merges_custom_env_with_parent_environment():
    custom_env_config = ServerConfig(
        enabled=True, command=sys.executable,
        args=["-c", "import os, sys; print('CUSTOM=' + os.environ.get('MCP_HUB_TEST_VAR', 'MISSING'), file=sys.stderr); print('PATH_PRESENT=' + str(bool(os.environ.get('PATH'))), file=sys.stderr)"],
        env={"MCP_HUB_TEST_VAR": "hello"}, concurrency="exclusive",
    )
    cfg = Config(hub=HubConfig(), servers={"a": custom_env_config})
    manager = HubManager(cfg)
    await manager.start_all()
    await asyncio.sleep(0.3)
    server = manager.get("a")
    logs = "\n".join(server.logs)
    assert "CUSTOM=hello" in logs
    assert "PATH_PRESENT=True" in logs
    await manager.stop_all()


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


async def test_upsert_adds_a_new_server_to_both_config_and_manager():
    cfg = Config(hub=HubConfig(), servers={})
    manager = HubManager(cfg)
    new_server = _python_sleep_config()
    managed = await manager.upsert("b", new_server)
    assert managed.name == "b"
    assert manager.get("b") is managed
    assert cfg.servers["b"] is new_server


async def test_upsert_replaces_an_existing_server():
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config()})
    manager = HubManager(cfg)
    replacement = _python_sleep_config(seconds=1)
    await manager.upsert("a", replacement)
    assert manager.get("a").config is replacement


async def test_upsert_stops_the_old_process_before_replacing_a_running_server():
    """Round-2 review regression test (Important finding): re-upserting an
    already-running server must stop the OLD ManagedServer's process before
    it is dropped/replaced, not just swap in a new ManagedServer object and
    orphan the old subprocess."""
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config(seconds=5)})
    manager = HubManager(cfg)
    await manager.start_all()
    await asyncio.sleep(0.05)
    old_managed = manager.get("a")
    old_process = old_managed.process
    assert old_process is not None
    assert old_process.returncode is None  # confirmed running before upsert

    replacement = _python_sleep_config(seconds=5)
    new_managed = await manager.upsert("a", replacement)

    # The OLD ManagedServer object was actually stopped, not just discarded.
    assert old_managed.status == "stopped"
    assert old_process.returncode is not None  # the old OS process is gone

    # A different ManagedServer/process is now registered under the same name.
    assert new_managed is not old_managed
    assert manager.get("a") is new_managed
    assert new_managed.config is replacement
    assert new_managed.process is None  # not started yet -- upsert doesn't auto-start

    await manager.stop_all()
