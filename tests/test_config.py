import json
from pathlib import Path
from mcp_hub.config import Config, HubConfig, ServerConfig, load_config, save_config, redact


def test_redact_masks_secret_like_keys():
    env = {"GITLAB_PERSONAL_ACCESS_TOKEN": "glpat-xxx", "MARIADB_HOST": "db.local", "MARIADB_PASS": "hunter2"}
    out = redact(env)
    assert out["GITLAB_PERSONAL_ACCESS_TOKEN"] == "***"
    assert out["MARIADB_PASS"] == "***"
    assert out["MARIADB_HOST"] == "db.local"


def test_load_config_missing_file_returns_default(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.json")
    assert cfg.hub.host == "127.0.0.1"
    assert cfg.servers == {}


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config(
        hub=HubConfig(host="127.0.0.1", port=37450, authToken=None, autostart=True),
        servers={"mariadb": ServerConfig(enabled=True, command="npx",
                                          args=["-y", "@oleander/mcp-server-mariadb"],
                                          env={"MARIADB_PASS": "x"}, concurrency="parallel")},
    )
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded == cfg


def test_save_is_atomic_leaves_no_temp_file_on_success(tmp_path):
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(), servers={}), path)
    assert path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_default_concurrency_is_exclusive():
    sc = ServerConfig(enabled=True, command="npx", args=[], env={})
    assert sc.concurrency == "exclusive"
