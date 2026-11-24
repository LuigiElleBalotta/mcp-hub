"""hub-gui-and-clients R3: client config sync with preview, backup and rollback."""
import json
import os
import sys

import pytest

from mcp_hub import client_sync
from mcp_hub.client_sync import (
    ADAPTERS, apply_plan, backups, detect_clients, hub_base_url, mask_secrets, plan_sync, rollback, sync_names,
)
from mcp_hub.config import Config, HubConfig, ServerConfig


def _cfg(**hub):
    return Config(hub=HubConfig(**hub), servers={
        "gitlab": ServerConfig(enabled=True, command="npx", args=[]),
        "mariadb": ServerConfig(enabled=True, command="npx", args=[]),
        "off": ServerConfig(enabled=False, command="npx", args=[]),
        "svc": ServerConfig(enabled=True, command="x", args=[], type="service"),
    })


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


NAMES = ["gitlab", "mariadb"]


def test_sync_names_skip_disabled_servers_and_services():
    assert sync_names(_cfg()) == NAMES
    assert sync_names(_cfg(), only=["mariadb", "off", "svc"]) == ["mariadb"]


# -------------------------------------------------------------------- entry shapes

def test_entry_shapes_per_client_and_transport():
    claude, cursor, vscode = ADAPTERS["claude"], ADAPTERS["cursor"], ADAPTERS["vscode"]
    sse, http = HubConfig(), HubConfig(preferredTransport="http")
    base = "http://127.0.0.1:37450/gitlab"
    assert claude.entry(sse, "gitlab") == {"type": "http", "url": f"{base}/sse"}, "what apply always wrote"
    assert claude.entry(http, "gitlab") == {"type": "http", "url": f"{base}/mcp"}
    assert cursor.entry(sse, "gitlab") == {"url": f"{base}/sse"} and cursor.entry(http, "gitlab") == {"url": f"{base}/mcp"}
    assert vscode.entry(sse, "gitlab") == {"type": "sse", "url": f"{base}/sse"}
    assert vscode.entry(http, "gitlab") == {"type": "http", "url": f"{base}/mcp"}
    tokened = HubConfig(authToken="tk", preferredTransport="http")
    for adapter in (claude, cursor, vscode):
        assert adapter.entry(tokened, "gitlab")["headers"] == {"Authorization": "Bearer tk"}


def test_a_wildcard_bind_is_written_as_loopback():
    assert hub_base_url(HubConfig(host="0.0.0.0", port=40000)) == "http://127.0.0.1:40000"
    assert hub_base_url(HubConfig(host="::1", port=1)) == "http://[::1]:1"


def test_servers_key_differs_per_client():
    assert ADAPTERS["claude"].key == "mcpServers" and ADAPTERS["cursor"].key == "mcpServers"
    assert ADAPTERS["vscode"].key == "servers"


# ----------------------------------------------------------------------- planning

def test_plan_reads_only_and_classifies_every_server(tmp_path):
    path = tmp_path / "mcp.json"
    hub = HubConfig()
    _write(path, {"mcpServers": {
        "gitlab": {"type": "http", "url": "http://127.0.0.1:37450/gitlab/sse"},      # already right
        "mariadb": {"command": "npx", "args": ["-y", "x"], "env": {"A": "1"}},      # the original stdio entry
        "other": {"command": "keep-me"},                                               # not ours
    }})
    before = path.read_bytes()
    plan = plan_sync(ADAPTERS["claude"], path, _cfg(), NAMES)
    assert path.read_bytes() == before, "planning never writes"
    assert plan.unchanged == ["gitlab"] and plan.updated == ["mariadb"] and plan.replaced_foreign == ["mariadb"]
    assert plan.added == [] and plan.changed and plan.error is None
    after = json.loads(plan.after)["mcpServers"]
    assert after["other"] == {"command": "keep-me"}, "entries that are not ours are untouched"
    assert after["mariadb"]["url"].endswith("/mariadb/sse")
    assert "voci non-hub sostituite" in plan.summary()


def test_a_hub_entry_pointing_at_the_other_transport_is_updated_not_replaced(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, {"mcpServers": {"gitlab": {"type": "http", "url": "http://127.0.0.1:37450/gitlab/sse"}}})
    plan = plan_sync(ADAPTERS["claude"], path, _cfg(preferredTransport="http"), ["gitlab"])
    assert plan.updated == ["gitlab"] and plan.replaced_foreign == [] and json.loads(plan.after)["mcpServers"]["gitlab"]["url"].endswith("/mcp")


def test_plan_for_a_missing_file_creates_it_and_for_an_up_to_date_one_changes_nothing(tmp_path):
    path = tmp_path / "sub" / "mcp.json"
    plan = plan_sync(ADAPTERS["cursor"], path, _cfg(), NAMES)
    assert plan.exists is False and plan.added == NAMES and plan.changed
    apply_plan(plan)
    assert path.exists() and set(_read(path)["mcpServers"]) == set(NAMES)
    again = plan_sync(ADAPTERS["cursor"], path, _cfg(), NAMES)
    assert again.unchanged == NAMES and not again.changed and again.after == again.before
    assert apply_plan(again) is None


def test_vscode_uses_its_own_key_and_keeps_inputs(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, {"inputs": [{"id": "k", "type": "promptString"}], "servers": {"x": {"command": "y"}}})
    plan = plan_sync(ADAPTERS["vscode"], path, _cfg(), ["gitlab"])
    out = json.loads(plan.after)
    assert out["inputs"] == [{"id": "k", "type": "promptString"}] and out["servers"]["x"] == {"command": "y"}
    assert out["servers"]["gitlab"] == {"type": "sse", "url": "http://127.0.0.1:37450/gitlab/sse"}
    assert "mcpServers" not in out


@pytest.mark.parametrize("content,needle", [
    ('{ // a comment\n "servers": {} }', "JSON"),
    ('{"servers": {},}', "JSON"),
    ("[1, 2]", "oggetto"),
    ('{"mcpServers": []}', "non e' un oggetto"),
])
def test_a_file_that_is_not_plain_json_is_refused_untouched(tmp_path, content, needle):
    path = tmp_path / "mcp.json"
    path.write_text(content, encoding="utf-8")
    plan = plan_sync(ADAPTERS["claude"], path, _cfg(), NAMES)
    assert plan.error and needle in plan.error and not plan.changed
    with pytest.raises(ValueError):
        apply_plan(plan)
    assert path.read_text(encoding="utf-8") == content and backups(path) == []


# ------------------------------------------------------------- apply, backup, rollback

def test_apply_backs_up_first_and_rollback_restores_it(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, {"mcpServers": {"mariadb": {"command": "npx", "env": {"PASS": "s3cret"}}}})
    original = path.read_text(encoding="utf-8")
    backup = apply_plan(plan_sync(ADAPTERS["claude"], path, _cfg(), NAMES))
    assert backup is not None and backup.read_text(encoding="utf-8") == original
    assert _read(path)["mcpServers"]["mariadb"]["url"].endswith("/sse")

    restored = rollback(path)
    assert restored == backup and path.read_text(encoding="utf-8") == original
    # the rolled-back state was itself backed up: a second rollback undoes the first
    assert len(backups(path)) == 2
    rollback(path)
    assert "url" in _read(path)["mcpServers"]["mariadb"]


def test_rollback_without_a_backup_or_with_a_corrupt_one_fails_cleanly(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, {"mcpServers": {}})
    with pytest.raises(FileNotFoundError):
        rollback(path)
    (tmp_path / "mcp.json.bak-20260101-000000").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        rollback(path)
    assert _read(path) == {"mcpServers": {}}, "the good file was not replaced by a bad backup"


def test_backups_are_ordered_by_the_timestamp_in_their_name_not_the_mtime(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{}", encoding="utf-8")
    names = ["f.json.bak-20260101-000000", "f.json.bak-20260102-000000", "f.json.bak-20260102-000000-1",
             "f.json.bak-20260102-000000-10", "f.json.bak-20260102-000000-2"]
    for n in names:
        (tmp_path / n).write_text("{}", encoding="utf-8")
    os.utime(tmp_path / names[1], (1, 1))  # an old mtime must not matter
    assert [b.name for b in backups(path)] == [names[3], names[4], names[2], names[1], names[0]]


def test_a_failed_staged_write_leaves_the_live_file_alone(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    _write(path, {"mcpServers": {}})
    original = path.read_bytes()
    plan = plan_sync(ADAPTERS["claude"], path, _cfg(), NAMES)
    plan.after = "{broken"  # what a bug would stage
    with pytest.raises(ValueError, match="refusing"):
        apply_plan(plan)
    assert path.read_bytes() == original and not (tmp_path / "mcp.json.tmp").exists()


# ----------------------------------------------------------------------- preview

def test_the_diff_masks_secrets_of_every_entry_but_the_file_keeps_them(tmp_path):
    path = tmp_path / "mcp.json"
    # the neighbour's secret sits right before the inserted entry, so it lands in the diff's context lines
    _write(path, {"mcpServers": {"other": {"env": {"API_KEY": "sk-live-123"}}}})
    plan = plan_sync(ADAPTERS["claude"], path, _cfg(authToken="hub-token-xyz"), ["mariadb"])
    diff = plan.diff()
    assert '"API_KEY": "***"' in diff, "the secret IS in the context and is masked, not just absent"
    assert '"Authorization": "***"' in diff and "/mariadb/sse" in diff
    assert "hub-token-xyz" not in diff and "sk-live-123" not in diff
    apply_plan(plan)
    written = path.read_text(encoding="utf-8")
    assert "hub-token-xyz" in written and "sk-live-123" in written, "...but the file itself is exact"


def test_mask_secrets_keys():
    text = '{"password": "p", "Token": "t", "name": "n", "x_secret_y": "s", "auth": "a"}'
    masked = mask_secrets(text)
    assert masked == '{"password": "***", "Token": "***", "name": "n", "x_secret_y": "***", "auth": "***"}'


def test_the_diff_is_empty_when_nothing_changes(tmp_path):
    path = tmp_path / "mcp.json"
    plan = plan_sync(ADAPTERS["claude"], path, _cfg(), [])
    assert plan.diff() == "" and not plan.changed and "nessun server" in plan.summary()


# ------------------------------------------------------------------------ detection

def test_detect_finds_installed_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    assert detect_clients() == []
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".cursor").mkdir()  # installed, no mcp.json yet
    found = {d.adapter.id: d for d in detect_clients()}
    assert found["claude"].exists is True and found["cursor"].exists is False
    assert found["cursor"].path == tmp_path / ".cursor" / "mcp.json"
    if sys.platform == "win32":
        assert "vscode" not in found
        (tmp_path / "AppData" / "Code" / "User").mkdir(parents=True)
        assert "vscode" in {d.adapter.id for d in detect_clients()}


def test_vscode_user_file_is_not_offered_outside_windows(monkeypatch):
    monkeypatch.setattr(client_sync.sys, "platform", "darwin")
    assert ADAPTERS["vscode"].user_path() is None
    monkeypatch.setattr(client_sync.sys, "platform", "linux")
    assert ADAPTERS["vscode"].user_path() is None


# ------------------------------------------------------------ formatting is preserved

def test_detect_indent():
    from mcp_hub.client_sync import detect_indent

    assert detect_indent('{\n\t"a": 1\n}') == "\t"
    assert detect_indent('{\n    "a": 1\n}') == 4
    assert detect_indent('{\n  "a": {\n    "b": 1\n  }\n}') == 2
    assert detect_indent('{"a": 1}') == 2 and detect_indent("") == 2


@pytest.mark.parametrize("indent", ["\t", "    ", "  "], ids=["tabs", "4-spaces", "2-spaces"])
def test_the_files_own_indentation_is_kept(tmp_path, indent):
    path = tmp_path / "mcp.json"
    original = json.dumps({"inputs": [{"id": "a"}], "servers": {"keep": {"command": "x", "args": ["1", "2"]}}},
                          indent=indent) + "\n"
    path.write_text(original, encoding="utf-8")
    plan = plan_sync(ADAPTERS["vscode"], path, _cfg(), ["gitlab"])
    assert plan.after.startswith("{\n" + indent + '"inputs"') and plan.after.endswith("\n")
    assert json.loads(plan.after)["servers"]["keep"] == {"command": "x", "args": ["1", "2"]}
    removed = [ln for ln in plan.diff().splitlines() if ln.startswith("-") and not ln.startswith("---")]
    # only the closing brace/comma lines around the insertion may differ, never the whole file
    assert len(removed) <= 2, f"too many lines rewritten: {removed}"


def test_a_tab_indented_file_yields_a_small_additive_diff(tmp_path):
    path = tmp_path / "mcp.json"
    big = {"inputs": [{"id": f"i{n}", "type": "promptString", "description": "d" * 40} for n in range(8)],
           "servers": {"a": {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]}}}
    path.write_text(json.dumps(big, indent="\t") + "\n", encoding="utf-8")
    plan = plan_sync(ADAPTERS["vscode"], path, _cfg(), ["gitlab"])
    changed = [ln for ln in plan.diff().splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
    assert len(changed) < 15, f"a one-server insertion touched {len(changed)} lines"
