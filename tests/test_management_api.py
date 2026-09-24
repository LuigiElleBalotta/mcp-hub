# tests/test_management_api.py
"""Coverage for the management API routes added for the GUI's Edit/Remove
and Settings dialog features:
- `DELETE /api/servers/{name}` (manager.remove wired to save_config).
- `PUT /api/settings` (updates the two live-reloadable HubConfig fields and
  persists them, distinct from the read-only `GET /api/settings`).

Also covers the live-reported bug this file's `test_upsert_*`/`test_reload_*`
tests below regression-test: `build_app` used to compute each enabled
server's SSE `Mount` ONCE, at hub startup. `upsert`/`remove` always correctly
started/stopped the underlying SUBPROCESS, but never touched that route
list -- so a server enabled (or added) after hub startup ran fine yet was
unreachable over HTTP (404) until the whole hub process was restarted.
`on_change` (`hub_app.build_app`'s `rebuild_routes`, threaded through
`management_routes`) is what makes the route show up on the very next
request instead.
"""
import sys

import httpx
import pytest

from mcp_hub.config import Config, HubConfig, ServerConfig
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager


def _disabled_server_config() -> ServerConfig:
    return ServerConfig(enabled=False, command="does-not-matter", args=[], env={})


def _sleep_server_config(seconds: float = 2.0) -> ServerConfig:
    return ServerConfig(enabled=True, command=sys.executable,
                         args=["-c", f"import time; time.sleep({seconds})"], env={})


def _has_mount(app, name: str) -> bool:
    return any(getattr(route, "path", None) == f"/{name}" for route in app.router.routes)


async def _client_for(manager: HubManager) -> httpx.AsyncClient:
    return await _client_for_app(build_app(manager))


async def _client_for_app(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_delete_server_removes_it_from_status_and_config(tmp_path, monkeypatch):
    monkeypatch.setattr("mcp_hub.management_api.save_config", lambda config: None)
    cfg = Config(hub=HubConfig(), servers={"a": _disabled_server_config()})
    manager = HubManager(cfg)
    async with await _client_for(manager) as client:
        resp = await client.delete("/api/servers/a")
        assert resp.status_code == 200
        assert resp.json() == {"status": "removed"}

        status_resp = await client.get("/api/status")
        assert "a" not in status_resp.json()["servers"]
    assert "a" not in cfg.servers


@pytest.mark.asyncio
async def test_delete_unknown_server_does_not_error():
    cfg = Config(hub=HubConfig(), servers={})
    manager = HubManager(cfg)
    async with await _client_for(manager) as client:
        resp = await client.delete("/api/servers/does-not-exist")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_put_settings_updates_live_config_and_is_reflected_by_get(monkeypatch):
    saved = []
    monkeypatch.setattr("mcp_hub.management_api.save_config", lambda config: saved.append(config))
    cfg = Config(hub=HubConfig(checkForUpdates=True, includeBetaUpdates=False), servers={})
    manager = HubManager(cfg)
    async with await _client_for(manager) as client:
        resp = await client.put(
            "/api/settings",
            json={"checkForUpdates": False, "includeBetaUpdates": True},
        )
        assert resp.status_code == 200
        assert resp.json() == {"checkForUpdates": False, "includeBetaUpdates": True}

        get_resp = await client.get("/api/settings")
        assert get_resp.json() == {"checkForUpdates": False, "includeBetaUpdates": True}
    assert cfg.hub.checkForUpdates is False
    assert cfg.hub.includeBetaUpdates is True
    assert saved  # save_config was called


@pytest.mark.asyncio
async def test_upsert_enabling_a_new_server_mounts_its_sse_route_without_restart(monkeypatch):
    monkeypatch.setattr("mcp_hub.management_api.save_config", lambda config: None)
    cfg = Config(hub=HubConfig(), servers={})
    manager = HubManager(cfg)
    app = build_app(manager)
    try:
        assert not _has_mount(app, "a")
        async with await _client_for_app(app) as client:
            resp = await client.post("/api/servers/a", json={
                "enabled": True, "command": sys.executable,
                "args": ["-c", "import time; time.sleep(2)"], "env": {},
            })
            assert resp.status_code == 200
        assert _has_mount(app, "a")  # route exists on THIS same, already-built app
    finally:
        await manager.get("a").stop()


@pytest.mark.asyncio
async def test_upsert_disabling_a_server_unmounts_its_sse_route(monkeypatch):
    monkeypatch.setattr("mcp_hub.management_api.save_config", lambda config: None)
    cfg = Config(hub=HubConfig(), servers={"a": _sleep_server_config()})
    manager = HubManager(cfg)
    app = build_app(manager)
    assert _has_mount(app, "a")
    async with await _client_for_app(app) as client:
        resp = await client.post("/api/servers/a", json={
            "enabled": False, "command": sys.executable, "args": [], "env": {},
        })
        assert resp.status_code == 200
    assert not _has_mount(app, "a")


@pytest.mark.asyncio
async def test_delete_server_unmounts_its_sse_route(monkeypatch):
    monkeypatch.setattr("mcp_hub.management_api.save_config", lambda config: None)
    cfg = Config(hub=HubConfig(), servers={"a": _sleep_server_config()})
    manager = HubManager(cfg)
    app = build_app(manager)
    assert _has_mount(app, "a")
    async with await _client_for_app(app) as client:
        resp = await client.delete("/api/servers/a")
        assert resp.status_code == 200
    assert not _has_mount(app, "a")


@pytest.mark.asyncio
async def test_reload_endpoint_picks_up_a_hand_edited_config_without_restart(monkeypatch, tmp_path):
    """The other half of the regression: config.json changed by something
    other than this hub's own API (a hand edit -- exactly today's incident --
    or `mcp_hub apply`/`import`, which both write the file directly). `POST
    /api/reload` must make an added-on-disk, enabled server's route appear
    on this SAME running app, with no restart."""
    from mcp_hub.config import save_config

    monkeypatch.setattr("mcp_hub.management_api.save_config", lambda config: None)
    disk_path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(), servers={}), disk_path)
    monkeypatch.setattr("mcp_hub.management_api.CONFIG_PATH", disk_path)

    cfg = Config(hub=HubConfig(), servers={})
    manager = HubManager(cfg)
    app = build_app(manager)
    try:
        assert not _has_mount(app, "a")

        save_config(Config(hub=HubConfig(), servers={"a": _sleep_server_config()}), disk_path)

        async with await _client_for_app(app) as client:
            resp = await client.post("/api/reload")
            assert resp.status_code == 200
            assert resp.json() == {"added": ["a"], "updated": [], "removed": []}

        assert _has_mount(app, "a")
    finally:
        if "a" in manager.config.servers:
            await manager.get("a").stop()


@pytest.mark.asyncio
async def test_status_includes_concurrency_per_server():
    cfg = Config(hub=HubConfig(), servers={
        "a": ServerConfig(enabled=False, command="x", args=[], env={}, concurrency="parallel"),
    })
    manager = HubManager(cfg)
    async with await _client_for(manager) as client:
        resp = await client.get("/api/status")
        assert resp.json() == {"servers": {"a": {"status": "stopped", "concurrency": "parallel"}}}


@pytest.mark.asyncio
async def test_put_settings_ignores_host_port_token_fields(monkeypatch):
    """host/port/authToken aren't live-reloadable (the socket is already
    bound) -- the GUI writes those straight to config.json itself instead of
    through this endpoint. Sending them here must not raise or silently
    change hub.host/port/authToken via this path."""
    monkeypatch.setattr("mcp_hub.management_api.save_config", lambda config: None)
    cfg = Config(hub=HubConfig(host="127.0.0.1", port=37450), servers={})
    manager = HubManager(cfg)
    async with await _client_for(manager) as client:
        resp = await client.put(
            "/api/settings",
            json={"host": "0.0.0.0", "port": 1, "checkForUpdates": False},
        )
        assert resp.status_code == 200
    assert cfg.hub.host == "127.0.0.1"
    assert cfg.hub.port == 37450
    assert cfg.hub.checkForUpdates is False
