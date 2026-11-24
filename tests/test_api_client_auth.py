"""GUI api client sends the hub token, and re-reads it on a 401 (hub restarted
with a token changed in Settings)."""
import json

import httpx

from mcp_hub.config import Config, HubConfig, hub_to_dict, load_config, save_config
from mcp_hub.gui.api_client import HubApiClient


def _client_with(handler, token=None, reloader=None) -> HubApiClient:
    client = HubApiClient(token=token, token_reloader=reloader)
    client._client = httpx.Client(
        base_url="http://127.0.0.1:37450", transport=httpx.MockTransport(handler), auth=client._auth
    )
    return client


def test_no_token_sends_no_authorization_header():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"servers": {}})

    _client_with(handler).status()
    assert seen == [None]


def test_token_is_sent_as_bearer():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"servers": {}})

    _client_with(handler, token="abc").status()
    assert seen == ["Bearer abc"]


def test_401_reloads_token_and_retries_once():
    calls = []

    def handler(request):
        calls.append(request.headers.get("authorization"))
        if request.headers.get("authorization") == "Bearer new":
            return httpx.Response(200, json={"servers": {"a": {}}})
        return httpx.Response(401, json={"error": "bad token"})

    client = _client_with(handler, token="old", reloader=lambda: "new")
    assert client.status() == {"a": {}}
    assert calls == ["Bearer old", "Bearer new"]
    client.status()  # the fresh token sticks: no second round trip through 401
    assert calls[2:] == ["Bearer new"]


def test_401_with_unchanged_token_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad token"})

    client = _client_with(handler, token="same", reloader=lambda: "same")
    resp = client._client.get("/api/status")
    assert resp.status_code == 401
    assert len(calls) == 1


def test_from_config_uses_host_port_and_token(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(host="127.0.0.1", port=40000, authToken="tk"), servers={}), path)
    monkeypatch.setattr("mcp_hub.config.CONFIG_PATH", path)
    client = HubApiClient.from_config()
    assert str(client._client.base_url).startswith("http://127.0.0.1:40000")
    assert client._auth.token == "tk"


def test_hub_config_keeps_old_shape_when_new_lists_are_empty(tmp_path):
    assert "allowedHosts" not in hub_to_dict(HubConfig())
    assert hub_to_dict(HubConfig(allowedHosts=["h"]))["allowedHosts"] == ["h"]
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(), servers={}), path)
    assert "allowedHosts" not in json.loads(path.read_text(encoding="utf-8"))["hub"]


def test_load_config_ignores_unknown_hub_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hub": {"port": 41000, "someFutureKey": 1}, "servers": {}}), encoding="utf-8")
    assert load_config(path).hub.port == 41000
