"""hub-gui-and-clients R5: groups of servers (config, API, CLI, apply --group)."""
import json
import sys

import httpx
import pytest

from mcp_hub import __main__ as cli
from mcp_hub import config as config_module
from mcp_hub.client_sync import sync_names
from mcp_hub.config import Config, HubConfig, ServerConfig, load_config, save_config, valid_group_name
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager

BASE = "http://127.0.0.1:37450"


def _sleeper(enabled=True):
    return ServerConfig(enabled=enabled, command=sys.executable, args=["-c", "import time; time.sleep(30)"],
                        env={}, restart="never")


def _cfg(groups=None):
    return Config(hub=HubConfig(), groups=groups or {}, servers={
        "a": _sleeper(), "b": _sleeper(), "c": _sleeper(enabled=False),
        "svc": ServerConfig(enabled=True, command="x", args=[], type="service"),
    })


# ----------------------------------------------------------------------------- config

def test_group_names():
    for ok in ("Lavoro", "Progetto X", "a", "team-1.beta_2"):
        assert valid_group_name(ok), ok
    for bad in ("", " x", "-x", "x" * 41, "a/b", "a\nb", None, 5):
        assert not valid_group_name(bad), bad


def test_groups_round_trip_and_are_omitted_while_empty(tmp_path):
    path = tmp_path / "config.json"
    save_config(_cfg(), path)
    assert "groups" not in json.loads(path.read_text(encoding="utf-8")), "an old config.json keeps its shape"
    save_config(_cfg({"Lavoro": ["a", "b"]}), path)
    assert json.loads(path.read_text(encoding="utf-8"))["groups"] == {"Lavoro": ["a", "b"]}
    assert load_config(path).groups == {"Lavoro": ["a", "b"]}


def test_load_ignores_malformed_groups(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hub": {}, "servers": {}, "groups": {
        "ok": ["a", 5, "b"], "bad name/": ["a"], "notalist": "a", "": []}}), encoding="utf-8")
    assert load_config(path).groups == {"ok": ["a", "b"]}


# ---------------------------------------------------------------------------- sync names

def test_sync_names_by_group_keeps_only_enabled_mcp_members():
    cfg = _cfg({"G": ["a", "c", "svc", "ghost"]})
    assert sync_names(cfg, group="G") == ["a"], "disabled, service and missing members are skipped"
    assert sync_names(cfg, only=["b"], group="G") == []
    assert sync_names(cfg) == ["a", "b"]
    with pytest.raises(ValueError, match="unknown group: nope"):
        sync_names(cfg, group="nope")


# -------------------------------------------------------------------------------- API

def _api(hub):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(hub)), base_url=BASE)


@pytest.mark.asyncio
async def test_create_list_replace_and_delete_a_group():
    hub = HubManager(_cfg())
    async with _api(hub) as api:
        assert (await api.get("/api/groups")).json() == {"groups": {}}
        r = await api.put("/api/groups/Lavoro", json={"servers": ["a", "b", "a"]})
        assert r.status_code == 200 and r.json()["servers"] == ["a", "b"], "duplicates dropped, order kept"
        assert (await api.get("/api/groups")).json() == {"groups": {"Lavoro": ["a", "b"]}}
        await api.put("/api/groups/Lavoro", json={"servers": ["c"]})
        assert hub.config.groups == {"Lavoro": ["c"]}
        assert load_config().groups == {"Lavoro": ["c"]}, "persisted"
        assert (await api.delete("/api/groups/Lavoro")).status_code == 200
        assert (await api.delete("/api/groups/Lavoro")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("name,body", [("bad!", {"servers": []}), ("G", {}), ("G", {"servers": "a"}),
                                       ("G", {"servers": [1]}), ("G", {"servers": ["ghost"]}), ("G", [1])])
async def test_invalid_groups_are_rejected_and_change_nothing(name, body):
    hub = HubManager(_cfg())
    async with _api(hub) as api:
        r = await api.put(f"/api/groups/{name}", json=body)
        assert r.status_code in (400, 404)
    assert hub.config.groups == {}


@pytest.mark.asyncio
async def test_start_and_stop_a_whole_group():
    hub = HubManager(_cfg({"G": ["a", "b", "c", "ghost"]}))
    try:
        async with _api(hub) as api:
            started = (await api.post("/api/groups/G/start")).json()["results"]
            assert started == {"a": "running", "b": "running", "c": "disabled", "ghost": "missing"}
            assert hub.get("a").process.returncode is None and hub.get("b").process.returncode is None
            stopped = (await api.post("/api/groups/G/stop")).json()["results"]
            assert stopped["a"] == "stopped" and stopped["b"] == "stopped"
            assert (await api.post("/api/groups/nope/start")).status_code == 404
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_removing_a_server_removes_it_from_its_groups():
    hub = HubManager(_cfg({"G": ["a", "b"], "H": ["a"]}))
    async with _api(hub) as api:
        assert (await api.delete("/api/servers/a")).status_code == 200
    assert hub.config.groups == {"G": ["b"], "H": []}
    assert load_config().groups == {"G": ["b"], "H": []}


@pytest.mark.asyncio
async def test_reload_picks_up_hand_edited_groups(tmp_path):
    hub = HubManager(_cfg())
    path = tmp_path / "config.json"
    save_config(_cfg({"Edited": ["a"]}), path)
    await hub.reload_from_disk(path)
    assert hub.config.groups == {"Edited": ["a"]}
    await hub.stop_all()


# --------------------------------------------------------------------------------- CLI

@pytest.fixture
def hub_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save_config(_cfg(), path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    return path


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["mcp_hub", *argv])
    cli.main()


def test_cli_groups_set_list_delete(hub_config, monkeypatch, capsys):
    run(monkeypatch, "groups", "list")
    assert "No groups." in capsys.readouterr().out
    run(monkeypatch, "groups", "set", "Lavoro", "a,b")
    assert "Group Lavoro: a, b" in capsys.readouterr().out
    run(monkeypatch, "groups", "list")
    assert "Lavoro: a, b" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="Unknown server"):
        run(monkeypatch, "groups", "set", "X", "a,ghost")
    with pytest.raises(SystemExit, match="Group name"):
        run(monkeypatch, "groups", "set", "bad/name", "a")
    run(monkeypatch, "groups", "delete", "Lavoro")
    with pytest.raises(SystemExit, match="Unknown group"):
        run(monkeypatch, "groups", "delete", "Lavoro")


def test_apply_group_writes_only_that_group_into_a_project_file(hub_config, monkeypatch, tmp_path, capsys):
    run(monkeypatch, "groups", "set", "ProgettoX", "b")
    target = tmp_path / ".mcp.json"
    run(monkeypatch, "apply", "--client", "claude", "--to", str(target), "--group", "ProgettoX", "--yes")
    assert set(json.loads(target.read_text(encoding="utf-8"))["mcpServers"]) == {"b"}
    assert "1 nuovi" in capsys.readouterr().out


def test_an_unknown_group_lists_the_known_ones_and_writes_nothing(hub_config, monkeypatch, tmp_path):
    run(monkeypatch, "groups", "set", "G1", "a")
    with pytest.raises(SystemExit) as stop:
        run(monkeypatch, "apply", "--to", str(tmp_path / "x.json"), "--group", "typo")
    assert "unknown group: typo" in str(stop.value) and "G1" in str(stop.value)
    assert not (tmp_path / "x.json").exists(), "a typo must not write an empty or a full set"
