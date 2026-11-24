# tests/test_gui_secrets.py
"""The 'move secrets to the credential store' button (hub-gui-and-clients R4, headless Qt)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import keyring
import pytest

pytest.importorskip("PySide6")

from keyring.backends import fail
from PySide6.QtWidgets import QApplication, QMessageBox

from mcp_hub import secrets
from mcp_hub.gui.server_dialog import ServerDialog
from test_secrets import MemoryKeyring


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def store():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def messages(monkeypatch):
    seen = []
    for kind in ("information", "warning"):
        monkeypatch.setattr(QMessageBox, kind, staticmethod(lambda parent, title, text, _k=kind: seen.append((_k, text))))
    return seen


def _dlg(env, name="gitlab"):
    return ServerDialog(name=name, existing={"command": "npx", "args": [], "env": env})


def _env_values(dlg):
    return {dlg.env_table.item(r, 0).text(): dlg.env_table.cellWidget(r, 1).text() for r in range(dlg.env_table.rowCount())}


def test_the_button_is_visible_only_when_a_credential_store_exists(qapp, store):
    # (isVisibleTo: the dialog itself is never shown in a headless test)
    with_store = _dlg({})
    assert with_store.move_secrets_btn.isVisibleTo(with_store) is True
    keyring.set_keyring(fail.Keyring())
    dlg = _dlg({})
    assert dlg.move_secrets_btn.isVisibleTo(dlg) is False


def test_moving_stores_secrets_leaves_references_and_ignores_everything_else(qapp, store, messages):
    dlg = _dlg({"GITLAB_TOKEN": "glpat-1", "REGION": "eu", "DONE_KEY": "${secret:gitlab/DONE_KEY}", "EMPTY_PASS": ""})
    dlg._move_secrets()
    assert _env_values(dlg) == {"GITLAB_TOKEN": "${secret:gitlab/GITLAB_TOKEN}", "REGION": "eu",
                                "DONE_KEY": "${secret:gitlab/DONE_KEY}", "EMPTY_PASS": ""}
    assert secrets.get_secret("gitlab", "GITLAB_TOKEN") == "glpat-1"
    assert secrets.get_secret("gitlab", "DONE_KEY") is None
    assert messages == [("information", "1 segreti spostati nel credential store.")]
    assert dlg.result_config()["env"]["GITLAB_TOKEN"] == "${secret:gitlab/GITLAB_TOKEN}", "what gets saved is the reference"


def test_moving_twice_finds_nothing_more(qapp, store, messages):
    dlg = _dlg({"A_TOKEN": "x"})
    dlg._move_secrets()
    dlg._move_secrets()
    assert messages[-1] == ("information", "Nessun segreto in chiaro da spostare.")


def test_a_server_without_a_name_cannot_store_secrets(qapp, store, messages):
    dlg = _dlg({"A_TOKEN": "x"}, name="")
    dlg._move_secrets()
    assert messages[0][0] == "warning" and "nome" in messages[0][1]
    assert _env_values(dlg) == {"A_TOKEN": "x"} and store.store == {}


def test_a_store_failure_is_reported_and_leaves_the_field_alone(qapp, store, messages, monkeypatch):
    monkeypatch.setattr(secrets, "set_secret", lambda *a: (_ for _ in ()).throw(secrets.SecretError("locked")))
    dlg = _dlg({"A_TOKEN": "x"})
    dlg._move_secrets()
    assert messages[0][0] == "warning" and "locked" in messages[0][1]
    assert _env_values(dlg) == {"A_TOKEN": "x"}
