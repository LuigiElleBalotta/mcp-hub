"""hub-gui-and-clients R4: env secrets in the OS credential store.

Every test runs against an in-memory keyring: the real Credential Manager is never touched."""
import asyncio
import json
import sys
from pathlib import Path

import anyio
import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from mcp_hub import __main__ as cli
from mcp_hub import config as config_module
from mcp_hub import secrets
from mcp_hub.config import Config, HubConfig, ServerConfig, load_config, save_config
from mcp_hub.hub_app import _proxy
from mcp_hub.manager import HubManager, ManagedServer

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store = {}

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


@pytest.fixture
def store():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def _server(env, **kw):
    return ServerConfig(enabled=True, command=sys.executable, args=[FAKE_SERVER], env=env,
                        concurrency="parallel", restart="never", **kw)


# ----------------------------------------------------------------------- references

def test_reference_round_trip_and_what_is_not_a_reference():
    ref = secrets.make_ref("gitlab", "GITLAB_TOKEN")
    assert ref == "${secret:gitlab/GITLAB_TOKEN}" and secrets.parse_ref(ref) == ("gitlab", "GITLAB_TOKEN")
    for not_ref in ("plain", "${secret:nokey}", "${secret:/K}", "x${secret:a/B}", "${secret:a/B}x", None, 5, ""):
        assert secrets.parse_ref(not_ref) is None, not_ref


def test_which_keys_are_secrets():
    assert all(secrets.is_secret_key(k) for k in ("TOKEN", "api_key", "DB_PASSWORD", "AUTH_HEADER", "client_secret"))
    assert not any(secrets.is_secret_key(k) for k in ("REGION", "PATH", "LOG_LEVEL"))


# --------------------------------------------------------------------------- the store

def test_set_get_delete(store):
    assert secrets.available() and secrets.get_secret("s", "K") is None
    secrets.set_secret("s", "K", "v1")
    assert secrets.get_secret("s", "K") == "v1"
    assert store.store == {("mcp-hub", "s/K"): "v1"}, "service name and user are what the README documents"
    assert secrets.delete_secret("s", "K") is True and secrets.delete_secret("s", "K") is False


def test_without_a_usable_store_nothing_breaks_for_plain_env_but_secrets_say_so():
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        assert secrets.available() is False
        with pytest.raises(secrets.SecretStoreUnavailable):
            secrets.set_secret("s", "K", "v")
        assert secrets.resolve_env({"PLAIN": "x"}) == {"PLAIN": "x"}, "plaintext keeps working"
        with pytest.raises(secrets.SecretError):
            secrets.resolve_env({"K": "${secret:s/K}"})
    finally:
        keyring.set_keyring(previous)


def test_resolve_env_substitutes_references_and_never_leaks_a_value_in_errors(store):
    secrets.set_secret("gitlab", "TOKEN", "glpat-REAL")
    out = secrets.resolve_env({"TOKEN": "${secret:gitlab/TOKEN}", "REGION": "eu"})
    assert out == {"TOKEN": "glpat-REAL", "REGION": "eu"}
    with pytest.raises(secrets.SecretError) as err:
        secrets.resolve_env({"PASSWORD": "${secret:gitlab/MISSING}"})
    assert "${secret:gitlab/MISSING}" in str(err.value) and "PASSWORD" in str(err.value)
    assert "glpat-REAL" not in str(err.value)
    assert isinstance(err.value, OSError), "spawn paths treat it like any failure to start"


# --------------------------------------------------------------------------- migration

def test_migration_moves_only_plaintext_secrets_and_is_idempotent(store):
    servers = {
        "gitlab": _server({"GITLAB_TOKEN": "glpat-1", "REGION": "eu", "EMPTY_KEY": "", "DONE_KEY": "${secret:gitlab/DONE_KEY}"}),
        "db": _server({"DB_PASSWORD": "hunter2"}),
    }
    created = secrets.migrate_config_secrets(servers)
    assert created == ["${secret:gitlab/GITLAB_TOKEN}", "${secret:db/DB_PASSWORD}"]
    assert servers["gitlab"].env == {"GITLAB_TOKEN": "${secret:gitlab/GITLAB_TOKEN}", "REGION": "eu",
                                    "EMPTY_KEY": "", "DONE_KEY": "${secret:gitlab/DONE_KEY}"}
    assert secrets.get_secret("gitlab", "GITLAB_TOKEN") == "glpat-1" and secrets.get_secret("db", "DB_PASSWORD") == "hunter2"
    assert secrets.migrate_config_secrets(servers) == [], "a second run finds nothing to move"
    assert secrets.get_secret("gitlab", "DONE_KEY") is None, "an existing reference is not re-stored"


def test_migration_can_be_limited_to_one_server(store):
    servers = {"a": _server({"A_TOKEN": "1"}), "b": _server({"B_TOKEN": "2"})}
    assert secrets.migrate_config_secrets(servers, only="b") == ["${secret:b/B_TOKEN}"]
    assert servers["a"].env == {"A_TOKEN": "1"}


def test_stored_names_lists_references_and_whether_they_exist(store):
    servers = {"a": _server({"A_TOKEN": "${secret:a/A_TOKEN}", "X_KEY": "${secret:a/X_KEY}", "P": "plain"})}
    secrets.set_secret("a", "A_TOKEN", "v")
    assert secrets.stored_names(servers) == [("${secret:a/A_TOKEN}", "a:A_TOKEN", True),
                                             ("${secret:a/X_KEY}", "a:X_KEY", False)]


# ------------------------------------------------------------------------------ spawning

def _msg(obj):
    return SessionMessage(jsonrpc_message_adapter.validate_python(obj))


class Conn:
    def __init__(self, server):
        self._rs, self._rr = anyio.create_memory_object_stream(32)
        self._ws, self._wr = anyio.create_memory_object_stream(32)
        self.task = asyncio.create_task(_proxy(self._rr, self._ws, server))

    async def call(self, rid, method, params=None):
        await self._rs.send(_msg({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}))
        while True:
            with anyio.fail_after(5.0):
                m = (await self._wr.receive()).message.model_dump(by_alias=True, exclude_unset=True)
            if m.get("id") == rid:
                return m

    async def close(self):
        await self._rs.aclose()
        with anyio.fail_after(5.0):
            await self.task


@pytest.mark.asyncio
async def test_the_child_gets_the_real_value_and_it_appears_nowhere_else(store):
    secrets.set_secret("fake", "MY_TOKEN", "s3cr3t-value-123")
    cfg = _server({"MY_TOKEN": "${secret:fake/MY_TOKEN}", "PLAIN": "visible"})
    hub = HubManager(Config(hub=HubConfig(), servers={"fake": cfg}))
    await hub.start_all()
    server = hub.get("fake")
    c = Conn(server)
    try:
        assert (await c.call(1, "test/getenv", {"name": "MY_TOKEN"}))["result"]["value"] == "s3cr3t-value-123"
        assert (await c.call(2, "test/getenv", {"name": "PLAIN"}))["result"]["value"] == "visible"
        everything = json.dumps([list(server.logs), server.metrics_detail(), server.metrics.recent(),
                                 hub.status_snapshot(), cfg.env])
        assert "s3cr3t-value-123" not in everything, "the config, logs, status and metrics only ever hold the reference"
        assert "${secret:fake/MY_TOKEN}" in cfg.env.values()
        await c.close()
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_a_missing_secret_stops_the_start_with_a_clear_message_and_no_value(store):
    cfg = _server({"MY_TOKEN": "${secret:fake/MY_TOKEN}"})
    server = ManagedServer("fake", cfg)
    with pytest.raises(secrets.SecretError):
        await server.start()
    assert server.status == "crashed" and server.process is None
    assert any("${secret:fake/MY_TOKEN}" in line and "not in the credential store" in line for line in server.logs)


@pytest.mark.asyncio
async def test_a_client_of_a_lazy_server_with_a_missing_secret_gets_a_json_rpc_error(store):
    server = ManagedServer("fake", _server({"MY_TOKEN": "${secret:fake/MY_TOKEN}"}, startMode="lazy"))
    c = Conn(server)
    try:
        reply = await c.call(1, "test/echo", {"value": 1})
        assert reply["error"]["code"] == -32000 and "credential store" in reply["error"]["message"]
        await c.close()  # the connection survived
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_service_resolves_secrets_too_and_its_output_is_still_redacted(store):
    secrets.set_secret("svc", "SVC_TOKEN", "svc-secret-999")
    cfg = ServerConfig(enabled=True, command=sys.executable, type="service",
                       args=["-c", "import os,time; print('SVC_TOKEN=' + os.environ['SVC_TOKEN'], flush=True); time.sleep(5)"],
                       env={"SVC_TOKEN": "${secret:svc/SVC_TOKEN}"})
    server = ManagedServer("svc", cfg)
    await server.start()
    try:
        for _ in range(100):
            if any("SVC_TOKEN=" in line for line in server.logs):
                break
            await asyncio.sleep(0.05)
        text = "\n".join(server.logs)
        assert "SVC_TOKEN=***" in text and "svc-secret-999" not in text, "defence in depth: log redaction"
    finally:
        await server.stop()


# ------------------------------------------------------------------------------- CLI

@pytest.fixture
def hub_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(), servers={"gitlab": _server({"GITLAB_TOKEN": "glpat-1", "REGION": "eu"})}), path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    return path


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["mcp_hub", *argv])
    cli.main()


def test_cli_migrate_writes_references_to_config_and_values_to_the_store(store, hub_config, monkeypatch, capsys):
    run(monkeypatch, "secrets", "migrate")
    out = capsys.readouterr().out
    assert "Moved 1 secret(s)" in out and "glpat-1" not in out
    on_disk = hub_config.read_text(encoding="utf-8")
    assert "glpat-1" not in on_disk and "${secret:gitlab/GITLAB_TOKEN}" in on_disk
    assert store.store[("mcp-hub", "gitlab/GITLAB_TOKEN")] == "glpat-1"
    run(monkeypatch, "secrets", "migrate")
    assert "Moved 0 secret(s)" in capsys.readouterr().out


def test_cli_list_prints_names_and_state_never_values(store, hub_config, monkeypatch, capsys):
    run(monkeypatch, "secrets", "migrate")
    capsys.readouterr()
    run(monkeypatch, "secrets", "list")
    assert "ok" in capsys.readouterr().out
    secrets.delete_secret("gitlab", "GITLAB_TOKEN")
    run(monkeypatch, "secrets", "list")
    out = capsys.readouterr().out
    assert "MISSING" in out and "glpat-1" not in out


def test_cli_set_reads_the_value_hidden_and_delete_removes_it(store, hub_config, monkeypatch, capsys):
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "typed-secret")
    run(monkeypatch, "secrets", "set", "gitlab", "GITLAB_TOKEN")
    out = capsys.readouterr().out
    assert "${secret:gitlab/GITLAB_TOKEN}" in out and "typed-secret" not in out
    assert secrets.get_secret("gitlab", "GITLAB_TOKEN") == "typed-secret"
    run(monkeypatch, "secrets", "delete", "gitlab", "GITLAB_TOKEN")
    assert "Deleted." in capsys.readouterr().out
    run(monkeypatch, "secrets", "delete", "gitlab", "GITLAB_TOKEN")
    assert "no such secret" in capsys.readouterr().out


def test_cli_explains_when_there_is_no_store(hub_config, monkeypatch):
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        with pytest.raises(SystemExit) as stop:
            run(monkeypatch, "secrets", "list")
        assert "No credential store" in str(stop.value)
    finally:
        keyring.set_keyring(previous)
