"""hub.preferredTransport: which endpoint `apply` writes into a client config."""
import json

from mcp_hub.claude_config import apply_servers
from mcp_hub.config import Config, HubConfig, ServerConfig, hub_to_dict, load_config, save_config


def _apply(tmp_path, transport):
    target = tmp_path / ".claude.json"
    target.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    cfg = Config(hub=HubConfig(preferredTransport=transport),
                 servers={"gitlab": ServerConfig(enabled=True, command="npx", args=[])})
    apply_servers(target, cfg)
    return json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["gitlab"]


def test_sse_is_the_default_and_writes_what_apply_always_wrote(tmp_path):
    assert _apply(tmp_path, "sse") == {"type": "http", "url": "http://127.0.0.1:37450/gitlab/sse"}


def test_http_writes_the_streamable_endpoint(tmp_path):
    assert _apply(tmp_path, "http") == {"type": "http", "url": "http://127.0.0.1:37450/gitlab/mcp"}


def test_the_token_header_is_written_for_both(tmp_path):
    target = tmp_path / ".claude.json"
    target.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    cfg = Config(hub=HubConfig(authToken="tk", preferredTransport="http"),
                 servers={"gitlab": ServerConfig(enabled=True, command="npx", args=[])})
    apply_servers(target, cfg)
    entry = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["gitlab"]
    assert entry["headers"] == {"Authorization": "Bearer tk"} and entry["url"].endswith("/gitlab/mcp")


def test_default_is_omitted_from_config_json_and_a_typo_falls_back(tmp_path):
    assert "preferredTransport" not in hub_to_dict(HubConfig())
    assert hub_to_dict(HubConfig(preferredTransport="http"))["preferredTransport"] == "http"
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(preferredTransport="http"), servers={}), path)
    assert load_config(path).hub.preferredTransport == "http"
    path.write_text(json.dumps({"hub": {"preferredTransport": "websocket"}, "servers": {}}), encoding="utf-8")
    assert load_config(path).hub.preferredTransport == "sse"
