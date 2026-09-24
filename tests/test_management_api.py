# tests/test_management_api.py
"""Coverage for the management API routes added for the GUI's Edit/Remove
and Settings dialog features:
- `DELETE /api/servers/{name}` (manager.remove wired to save_config).
- `PUT /api/settings` (updates the two live-reloadable HubConfig fields and
  persists them, distinct from the read-only `GET /api/settings`).
"""
import httpx
import pytest

from mcp_hub.config import Config, HubConfig, ServerConfig
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager


def _disabled_server_config() -> ServerConfig:
    return ServerConfig(enabled=False, command="does-not-matter", args=[], env={})


async def _client_for(manager: HubManager) -> httpx.AsyncClient:
    app = build_app(manager)
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
