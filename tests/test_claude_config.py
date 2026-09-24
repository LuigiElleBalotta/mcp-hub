# tests/test_claude_config.py
import json
from datetime import datetime
from pathlib import Path
import pytest
import mcp_hub.claude_config as claude_config_module
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


# --- Regression tests for review findings (Task 15 fix pass) ---


def test_apply_never_replaces_live_file_when_staged_write_is_invalid(tmp_path, monkeypatch):
    """Finding 1: validation must happen BEFORE os.replace, not after.

    We force json.dumps (used inside apply_servers to serialize the staged
    content) to return unparseable text -- simulating a corrupted/truncated
    write. If validation genuinely happens before the atomic replace, then:
      - os.replace is never invoked at all,
      - the live claude_config_path is byte-for-byte unchanged,
      - no leftover .tmp file survives,
      - the pristine backup taken at the top of apply_servers is untouched.
    """
    claude_json = tmp_path / ".claude.json"
    original_content = json.dumps({
        "mcpServers": {"mariadb": {"command": "npx", "args": [], "env": {}}}
    })
    claude_json.write_text(original_content, encoding="utf-8")

    hub_config = Config(
        hub=HubConfig(),
        servers={"mariadb": ServerConfig(enabled=True, command="npx", args=[], env={})},
    )

    replace_calls = []
    real_os_replace = claude_config_module.os.replace

    def spy_replace(src, dst):
        replace_calls.append((src, dst))
        return real_os_replace(src, dst)

    monkeypatch.setattr(claude_config_module.os, "replace", spy_replace)
    monkeypatch.setattr(claude_config_module.json, "dumps", lambda *a, **k: "{not valid json")

    with pytest.raises(ValueError):
        apply_servers(claude_json, hub_config)

    # os.replace must never have been reached -- the corrupted content
    # could not possibly have become live.
    assert replace_calls == []

    # The live file is completely untouched.
    assert claude_json.read_text(encoding="utf-8") == original_content

    # No leftover staging file.
    assert list(tmp_path.glob("*.tmp")) == []

    # The pristine backup (taken before the failed write) is still there,
    # intact, as the recovery path.
    backups = list(tmp_path.glob(".claude.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original_content


def test_backup_file_does_not_clobber_existing_backup_on_same_second_collision(tmp_path, monkeypatch):
    """Finding 2: two backup_file calls with the same timestamp must not collide."""

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 12, 0, 0)

    monkeypatch.setattr(claude_config_module, "datetime", _FixedDateTime)

    original = tmp_path / ".claude.json"
    original.write_text('{"a": 1}', encoding="utf-8")

    backup1 = backup_file(original)

    # Mutate the "live" file between backups, as apply_servers would between
    # a retried invocation, so a clobber would be observable via content.
    original.write_text('{"a": 2}', encoding="utf-8")

    backup2 = backup_file(original)

    assert backup1 != backup2
    assert backup1.name.endswith(".bak-20260101-120000")
    assert backup2.name.endswith(".bak-20260101-120000-1")

    # Both backups exist, distinct, with their own correct content -- the
    # first backup was never overwritten by the second call.
    assert backup1.exists()
    assert backup2.exists()
    assert backup1.read_text(encoding="utf-8") == '{"a": 1}'
    assert backup2.read_text(encoding="utf-8") == '{"a": 2}'
