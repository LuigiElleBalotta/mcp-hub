# tests/test_claude_config.py
import json
from pathlib import Path
import pytest
from mcp_hub.config import Config, HubConfig, ServerConfig
from mcp_hub.claude_config import backup_file, import_servers, apply_servers


def _write_claude_json(path: Path, mcp_servers: dict) -> None:
    path.write_text(json.dumps({"mcpServers": mcp_servers}), encoding="utf-8")


def test_backup_file_creates_timestamped_copy(tmp_path):
    original = tmp_path / ".claude.json"
    original.write_text('{"a": 1}', encoding="utf-8")
    backup = backup_file(original)
    assert backup.exists()
    assert backup.read_text() == '{"a": 1}'
    assert backup.name.startswith(".claude.json.bak-")


def test_import_adds_new_servers_disabled_by_default(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {
        "mariadb": {"command": "npx", "args": ["-y", "@oleander/mcp-server-mariadb"], "env": {"MARIADB_PASS": "x"}}
    })
    hub_config = Config(hub=HubConfig(), servers={})
    imported = import_servers(claude_json, hub_config)
    assert imported == ["mariadb"]
    assert hub_config.servers["mariadb"].enabled is False
    assert hub_config.servers["mariadb"].concurrency == "exclusive"
    assert hub_config.servers["mariadb"].command == "npx"


def test_import_skips_servers_already_present(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {"mariadb": {"command": "npx", "args": [], "env": {}}})
    hub_config = Config(hub=HubConfig(), servers={
        "mariadb": ServerConfig(enabled=True, command="npx", args=[], env={})
    })
    imported = import_servers(claude_json, hub_config)
    assert imported == []


def test_apply_rewrites_enabled_servers_to_remote_type(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {
        "mariadb": {"command": "npx", "args": ["-y", "@oleander/mcp-server-mariadb"], "env": {}}
    })
    hub_config = Config(
        hub=HubConfig(host="127.0.0.1", port=37450, authToken=None),
        servers={"mariadb": ServerConfig(enabled=True, command="npx", args=[], env={})},
    )
    migrated = apply_servers(claude_json, hub_config)
    assert migrated == ["mariadb"]
    written = json.loads(claude_json.read_text())
    assert written["mcpServers"]["mariadb"] == {
        "type": "http", "url": "http://127.0.0.1:37450/mariadb/sse"
    }


def test_apply_adds_auth_header_when_token_set(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {"mariadb": {"command": "npx", "args": [], "env": {}}})
    hub_config = Config(
        hub=HubConfig(host="0.0.0.0", port=37450, authToken="secret-token"),
        servers={"mariadb": ServerConfig(enabled=True, command="npx", args=[], env={})},
    )
    apply_servers(claude_json, hub_config)
    written = json.loads(claude_json.read_text())
    assert written["mcpServers"]["mariadb"]["headers"]["Authorization"] == "Bearer secret-token"


def test_apply_creates_a_backup_before_writing(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {"mariadb": {"command": "npx", "args": [], "env": {}}})
    hub_config = Config(hub=HubConfig(), servers={"mariadb": ServerConfig(enabled=True, command="npx", args=[], env={})})
    apply_servers(claude_json, hub_config)
    backups = list(tmp_path.glob(".claude.json.bak-*"))
    assert len(backups) == 1


def test_apply_only_touches_servers_in_the_only_list(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {
        "mariadb": {"command": "npx", "args": [], "env": {}},
        "headroom": {"command": "headroom.exe", "args": [], "env": {}},
    })
    hub_config = Config(hub=HubConfig(), servers={
        "mariadb": ServerConfig(enabled=True, command="npx", args=[], env={}),
        "headroom": ServerConfig(enabled=True, command="headroom.exe", args=[], env={}),
    })
    migrated = apply_servers(claude_json, hub_config, only=["mariadb"])
    assert migrated == ["mariadb"]
    written = json.loads(claude_json.read_text())
    assert written["mcpServers"]["headroom"]["command"] == "headroom.exe"  # untouched
