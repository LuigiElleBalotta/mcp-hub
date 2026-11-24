"""`apply --client ...` and `rollback` on the command line."""
import json
import sys

import pytest

from mcp_hub import __main__ as cli
from mcp_hub import config as config_module
from mcp_hub.config import Config, HubConfig, ServerConfig, save_config


@pytest.fixture
def hub_config(tmp_path, monkeypatch):
    path = tmp_path / "hub" / "config.json"
    save_config(Config(hub=HubConfig(authToken="hub-token-xyz"), servers={
        "gitlab": ServerConfig(enabled=True, command="npx", args=[]),
        "mariadb": ServerConfig(enabled=True, command="npx", args=[]),
        "svc": ServerConfig(enabled=True, command="x", args=[], type="service"),
    }), path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    return path


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["mcp_hub", *argv])
    cli.main()


def _target(tmp_path, data=None):
    f = tmp_path / "client" / "mcp.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps(data if data is not None else {"mcpServers": {}}, indent=2), encoding="utf-8")
    return f


def test_dry_run_shows_the_diff_and_writes_nothing(tmp_path, hub_config, monkeypatch, capsys):
    f = _target(tmp_path)
    before = f.read_bytes()
    run(monkeypatch, "apply", "--client", "cursor", "--to", str(f), "--dry-run")
    out = capsys.readouterr().out
    assert "2 nuovi" in out and "/gitlab/sse" in out and "dry run" in out
    assert f.read_bytes() == before and list(f.parent.glob("*.bak-*")) == []
    assert "hub-token-xyz" not in out, "the token is masked in the preview"


def test_apply_writes_after_confirmation_and_a_second_run_changes_nothing(tmp_path, hub_config, monkeypatch, capsys):
    f = _target(tmp_path, {"mcpServers": {"gitlab": {"command": "npx", "env": {"TOKEN": "keepme"}}}})
    run(monkeypatch, "apply", "--client", "cursor", "--to", str(f), "--yes")
    out = capsys.readouterr().out
    assert "Written. Backup:" in out and "1 nuovi" in out and "1 voci non-hub sostituite" in out
    written = json.loads(f.read_text(encoding="utf-8"))["mcpServers"]
    assert set(written) == {"gitlab", "mariadb"} and "svc" not in written, "a service is not an MCP server"
    assert written["gitlab"]["headers"] == {"Authorization": "Bearer hub-token-xyz"}
    assert len(list(f.parent.glob("mcp.json.bak-*"))) == 1
    run(monkeypatch, "apply", "--client", "cursor", "--to", str(f), "--yes")
    assert "Nothing to change." in capsys.readouterr().out
    assert len(list(f.parent.glob("mcp.json.bak-*"))) == 1, "no change, no new backup"


def test_declining_the_prompt_writes_nothing(tmp_path, hub_config, monkeypatch, capsys):
    f = _target(tmp_path)
    before = f.read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    run(monkeypatch, "apply", "--client", "claude", "--to", str(f))
    assert "Cancelled." in capsys.readouterr().out and f.read_bytes() == before


def test_only_narrows_the_servers(tmp_path, hub_config, monkeypatch):
    f = _target(tmp_path)
    run(monkeypatch, "apply", "--client", "claude", "--to", str(f), "--only", "mariadb", "--yes")
    assert set(json.loads(f.read_text(encoding="utf-8"))["mcpServers"]) == {"mariadb"}


def test_project_scope_uses_the_clients_project_file_and_key(tmp_path, hub_config, monkeypatch):
    run(monkeypatch, "apply", "--client", "vscode", "--scope", "project", "--project-dir", str(tmp_path), "--yes")
    written = json.loads((tmp_path / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    assert set(written["servers"]) == {"gitlab", "mariadb"} and written["servers"]["gitlab"]["type"] == "sse"
    run(monkeypatch, "apply", "--client", "claude", "--scope", "project", "--project-dir", str(tmp_path), "--yes")
    assert set(json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]) == {"gitlab", "mariadb"}


def test_a_file_with_comments_is_refused_with_exit_1_and_left_alone(tmp_path, hub_config, monkeypatch, capsys):
    f = tmp_path / "mcp.json"
    f.write_text('{ // mine\n "servers": {} }', encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        run(monkeypatch, "apply", "--client", "vscode", "--to", str(f), "--yes")
    assert stop.value.code == 1 and "JSON" in capsys.readouterr().out
    assert f.read_text(encoding="utf-8") == '{ // mine\n "servers": {} }'


def test_rollback_restores_the_newest_backup_and_reports_when_there_is_none(tmp_path, hub_config, monkeypatch, capsys):
    f = _target(tmp_path, {"mcpServers": {"gitlab": {"command": "orig"}}})
    original = f.read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        run(monkeypatch, "rollback", "--client", "cursor", "--to", str(f))
    assert "no backup" in str(stop.value)
    run(monkeypatch, "apply", "--client", "cursor", "--to", str(f), "--yes")
    assert f.read_text(encoding="utf-8") != original
    capsys.readouterr()
    run(monkeypatch, "rollback", "--client", "cursor", "--to", str(f))
    assert "Restored" in capsys.readouterr().out and f.read_text(encoding="utf-8") == original


def test_legacy_apply_to_still_works_and_apply_alone_explains_itself(tmp_path, hub_config, monkeypatch, capsys):
    f = _target(tmp_path, {"mcpServers": {"gitlab": {"command": "npx"}}})
    run(monkeypatch, "apply", "--to", str(f))
    assert "Applied 2 server(s)" in capsys.readouterr().out
    assert json.loads(f.read_text(encoding="utf-8"))["mcpServers"]["gitlab"]["type"] == "http"
    with pytest.raises(SystemExit) as stop:
        run(monkeypatch, "apply")
    assert "--client" in str(stop.value)


def test_claude_legacy_project_flag_is_rejected_in_the_new_form(tmp_path, hub_config, monkeypatch):
    with pytest.raises(SystemExit) as stop:
        run(monkeypatch, "apply", "--client", "claude", "--to", str(_target(tmp_path)), "--project", "C:/x")
    assert "--scope project" in str(stop.value)
