# tests/test_gui_client_sync.py
"""The Sync clients dialog (hub-gui-and-clients R3, headless Qt)."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from mcp_hub import client_sync
from mcp_hub.client_sync import ADAPTERS
from mcp_hub.config import Config, HubConfig, ServerConfig
from mcp_hub.gui.client_sync_dialog import ClientSyncDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def cfg():
    return Config(hub=HubConfig(authToken="hub-token-xyz"), servers={
        "gitlab": ServerConfig(enabled=True, command="npx", args=[]),
        "off": ServerConfig(enabled=False, command="npx", args=[]),
    })


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    return tmp_path


def _dialog(cfg):
    return ClientSyncDialog(hub_config=cfg)


def test_lists_the_clients_found_on_this_machine(qapp, cfg, home):
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    (home / ".cursor").mkdir()
    dlg = _dialog(cfg)
    assert [t.adapter.id for t in dlg.targets] == ["claude", "cursor"]
    assert "esiste" in dlg.list.item(0).text() and "verra' creato" in dlg.list.item(1).text()
    assert all(dlg.list.item(i).checkState() == Qt.CheckState.Checked for i in range(2))


def test_says_so_when_no_client_is_installed(qapp, cfg, home):
    dlg = _dialog(cfg)
    assert dlg.targets == [] and "Nessun client" in dlg.output.toPlainText()
    dlg.preview()
    assert "almeno un file" in dlg.output.toPlainText() and not dlg.apply_btn.isEnabled()


def test_preview_shows_the_masked_diff_and_enables_apply_only_when_something_changes(qapp, cfg, home):
    f = home / ".claude.json"
    f.write_text(json.dumps({"mcpServers": {"x": {"env": {"API_KEY": "sk-live-123"}}}}), encoding="utf-8")
    before = f.read_bytes()
    dlg = _dialog(cfg)
    assert dlg.apply_btn.isEnabled() is False, "nothing is applicable before a preview"
    dlg.preview()
    text = dlg.output.toPlainText()
    assert "1 nuovi" in text and "/gitlab/sse" in text and "sk-live-123" not in text and "hub-token-xyz" not in text
    assert dlg.apply_btn.isEnabled() and f.read_bytes() == before, "previewing writes nothing"


def test_apply_asks_first_then_writes_with_a_backup(qapp, cfg, home, monkeypatch):
    f = home / ".claude.json"
    f.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    dlg = _dialog(cfg)
    dlg.preview()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    dlg.apply()
    assert json.loads(f.read_text(encoding="utf-8"))["mcpServers"] == {}, "declined: nothing written"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg.apply()
    assert "gitlab" in json.loads(f.read_text(encoding="utf-8"))["mcpServers"]
    assert "Scritto" in dlg.output.toPlainText() and "backup" in dlg.output.toPlainText()
    assert len(list(home.glob(".claude.json.bak-*"))) == 1
    assert not dlg.apply_btn.isEnabled(), "applied: preview again before another write"


def test_a_broken_file_is_reported_and_not_written(qapp, cfg, home):
    f = home / ".claude.json"
    f.write_text("{ // comment\n}", encoding="utf-8")
    dlg = _dialog(cfg)
    dlg.preview()
    assert "non verra' scritto" in dlg.output.toPlainText() and not dlg.apply_btn.isEnabled()
    assert f.read_text(encoding="utf-8") == "{ // comment\n}"


def test_unchecked_targets_are_skipped(qapp, cfg, home):
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    dlg = _dialog(cfg)
    dlg.list.item(0).setCheckState(Qt.CheckState.Unchecked)
    dlg.preview()
    assert "almeno un file" in dlg.output.toPlainText()


def test_a_project_file_can_be_added_and_created(qapp, cfg, home, tmp_path, monkeypatch):
    dlg = _dialog(cfg)
    project = tmp_path / "proj"
    dlg.add_target(ADAPTERS["vscode"], project / ".vscode" / "mcp.json")
    assert "verra' creato" in dlg.list.item(0).text()
    dlg.preview()
    assert dlg.apply_btn.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg.apply()
    written = json.loads((project / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    assert "gitlab" in written["servers"] and "file nuovo" in dlg.output.toPlainText()


def test_restore_brings_back_the_previous_file_and_reports_when_there_is_nothing(qapp, cfg, home, monkeypatch):
    f = home / ".claude.json"
    original = json.dumps({"mcpServers": {"mine": {"command": "keep"}}})
    f.write_text(original, encoding="utf-8")
    dlg = _dialog(cfg)
    dlg.list.setCurrentRow(0)
    dlg.restore()
    assert "Nessun backup" in dlg.output.toPlainText()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dlg.preview()
    dlg.apply()
    assert "gitlab" in json.loads(f.read_text(encoding="utf-8"))["mcpServers"]
    dlg.list.setCurrentRow(0)
    dlg.restore()
    assert f.read_text(encoding="utf-8") == original and "Ripristinato" in dlg.output.toPlainText()


def test_restore_without_a_selection_explains(qapp, cfg, home):
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    dlg = _dialog(cfg)
    dlg.list.setCurrentRow(-1)
    dlg.restore()
    assert "Seleziona" in dlg.output.toPlainText()
