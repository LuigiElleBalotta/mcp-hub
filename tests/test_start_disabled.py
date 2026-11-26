"""Starting a DISABLED server: its endpoints are not served, so the hub refuses instead of
running an unreachable process (the GUI's Avvia used to do exactly that and every client got 404)."""
import sys

import httpx
import pytest

from mcp_hub.config import Config, HubConfig, ServerConfig
from mcp_hub.gui.api_client import HubApiClient
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager

BASE = "http://127.0.0.1:37450"


def _sleeper(enabled):
    return ServerConfig(enabled=enabled, command=sys.executable, args=["-c", "import time; time.sleep(30)"],
                        env={}, restart="never")


def _hub():
    return HubManager(Config(hub=HubConfig(), servers={"on": _sleeper(True), "off": _sleeper(False)}))


@pytest.mark.asyncio
async def test_a_disabled_server_is_not_started_and_says_why():
    hub = _hub()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(hub)), base_url=BASE) as api:
            r = await api.post("/api/servers/off/start")
            assert r.status_code == 409
            assert "disabled" in r.json()["error"] and "enable it first" in r.json()["error"]
            assert hub.get("off").process is None, "no process was spawned"
            # the same server, once enabled, starts normally; an enabled one never needed that
            assert (await api.post("/api/servers/on/start")).json()["status"] == "running"
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_stopping_a_disabled_server_is_still_allowed():
    hub = _hub()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(hub)), base_url=BASE) as api:
        assert (await api.post("/api/servers/off/stop")).status_code == 200


def _client_answering(status, body):
    client = HubApiClient()
    client._client = httpx.Client(
        base_url=BASE, auth=client._auth,
        transport=httpx.MockTransport(lambda req: httpx.Response(status, json=body)))
    return client


def test_the_gui_client_turns_a_refusal_into_a_readable_error_not_a_keyerror():
    with pytest.raises(RuntimeError, match="is disabled: enable it first"):
        _client_answering(409, {"error": "server 'x' is disabled: enable it first", "status": "stopped"}).start("x")
    assert _client_answering(200, {"status": "running"}).start("x") == "running"
