"""hub-proxy-hardening R1: token, Host/Origin allow-list, Content-Type.

Everything runs against the real ASGI app in memory; servers are never
started (enabled=False), so nothing is executed.
"""
import json

import httpx
import pytest

from mcp_hub.config import Config, HubConfig
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager

BODY = json.dumps({"enabled": False, "command": "NOT-RUN", "args": []})
JSON = {"Content-Type": "application/json"}


def _client(hub: HubConfig | None = None, host: str = "127.0.0.1:37450") -> httpx.AsyncClient:
    hub = hub or HubConfig()
    manager = HubManager(Config(hub=hub, servers={}))
    transport = httpx.ASGITransport(app=build_app(manager))
    return httpx.AsyncClient(transport=transport, base_url=f"http://{host}")


async def test_default_localhost_request_is_allowed():
    async with _client() as c:
        assert (await c.get("/api/status")).status_code == 200


@pytest.mark.parametrize("host", ["localhost:37450", "[::1]:37450", "127.0.0.1:37450"])
async def test_local_host_names_are_allowed(host):
    async with _client(host=host) as c:
        assert (await c.get("/api/status")).status_code == 200


async def test_foreign_host_is_rejected_dns_rebinding():
    async with _client() as c:
        r = await c.get("/api/status", headers={"Host": "evil.example:37450"})
    assert r.status_code == 403


async def test_foreign_origin_is_rejected():
    async with _client() as c:
        r = await c.post("/api/servers/x", content=BODY, headers={**JSON, "Origin": "https://evil.example"})
    assert r.status_code == 403


async def test_null_origin_is_rejected():
    async with _client() as c:
        r = await c.get("/api/status", headers={"Origin": "null"})
    assert r.status_code == 403


async def test_own_origin_and_configured_origin_are_allowed():
    hub = HubConfig(allowedOrigins=["https://tools.example"])
    async with _client(hub) as c:
        own = await c.get("/api/status", headers={"Origin": "http://127.0.0.1:37450"})
        extra = await c.get("/api/status", headers={"Origin": "https://tools.example"})
    assert own.status_code == 200 and extra.status_code == 200


async def test_configured_host_is_allowed():
    hub = HubConfig(allowedHosts=["hub.internal"])
    async with _client(hub) as c:
        r = await c.get("/api/status", headers={"Host": "hub.internal:37450"})
    assert r.status_code == 200


async def test_non_local_bind_skips_host_check_but_needs_token():
    hub = HubConfig(host="0.0.0.0", authToken="tok")
    async with _client(hub) as c:
        ok = await c.get("/api/status", headers={"Host": "10.1.2.3:37450", "Authorization": "Bearer tok"})
        no = await c.get("/api/status", headers={"Host": "10.1.2.3:37450"})
    assert ok.status_code == 200 and no.status_code == 401


async def test_token_required_on_management_and_mcp_routes():
    async with _client(HubConfig(authToken="tok")) as c:
        assert (await c.get("/api/status")).status_code == 401
        assert (await c.get("/anything/sse")).status_code == 401  # before routing
        wrong = await c.get("/api/status", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        assert "www-authenticate" in wrong.headers
        assert (await c.get("/api/status", headers={"Authorization": "Bearer tok"})).status_code == 200
        assert (await c.get("/api/status", headers={"Authorization": "bearer tok"})).status_code == 200
        assert (await c.get("/api/status", headers={"Authorization": "Basic tok"})).status_code == 401


async def test_no_token_configured_means_no_auth_header_needed():
    async with _client(HubConfig(authToken=None)) as c:
        assert (await c.get("/api/status")).status_code == 200


async def test_management_body_must_be_json():
    manager = HubManager(Config(hub=HubConfig(), servers={}))
    app = build_app(manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:37450") as c:
        bad = await c.post("/api/servers/x", content=BODY, headers={"Content-Type": "text/plain"})
        assert bad.status_code == 415
        assert "x" not in manager.config.servers  # nothing was applied
        none = await c.post("/api/servers/x", content=BODY)  # no Content-Type at all
        assert none.status_code == 415
        ok = await c.post("/api/servers/x", content=BODY, headers={"Content-Type": "application/json; charset=utf-8"})
        assert ok.status_code == 200
        assert "x" in manager.config.servers


async def test_bodiless_management_post_does_not_need_content_type():
    async with _client() as c:
        # No body: the Origin/Host checks are what protect these routes.
        assert (await c.post("/api/servers/unknown/stop")).status_code == 404  # reaches the route


async def test_bodiless_post_from_foreign_origin_is_rejected():
    async with _client() as c:
        r = await c.post("/api/shutdown", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
