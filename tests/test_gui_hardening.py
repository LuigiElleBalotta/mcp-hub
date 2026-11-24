# tests/test_gui_hardening.py
"""GUI side of hub-proxy-hardening R8 (headless, offscreen Qt)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

from mcp_hub import config as config_module
from mcp_hub.config import Config, HubConfig, load_config, save_config
from mcp_hub.gui.main_window import ACTIONS_COL, MainWindow
from mcp_hub.gui.server_dialog import ServerDialog
from mcp_hub.gui.settings_dialog import SettingsDialog

STATUSES = {
    "flaky": {"status": "restarting", "concurrency": "parallel", "type": "mcp", "restarts": 3},
    "steady": {"status": "running", "concurrency": "parallel", "type": "mcp"},
}


class FakeClient:
    def status(self):
        return STATUSES

    def logs(self, name):
        return []

    def update_settings(self, **fields):
        return fields


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_check_for_updates", lambda self: None)
    win = MainWindow(client=FakeClient())
    win.timer.stop()
    yield win
    win.tray_icon.hide()
    win.close()


def _row(win, name):
    return next(r for r in range(win.table.rowCount()) if win.table.item(r, 0).text() == name)


def test_restarting_status_has_its_own_color_tooltip_and_stop_button(window):
    window._on_status_result(True, STATUSES, None)
    row = _row(window, "flaky")
    item = window.table.item(row, 2)
    assert item.text() == "restarting"
    assert item.foreground().color().name() == "#ef6c00"
    assert "3 volte" in item.toolTip()
    buttons = {b.text(): b for b in window.table.cellWidget(row, ACTIONS_COL).findChildren(QPushButton)}
    assert buttons["Stop"].isEnabled(), "Stop cancels the pending automatic restart"
    assert not buttons["Start"].isEnabled()
    assert window.table.item(_row(window, "steady"), 2).toolTip() == ""


def test_server_dialog_defaults_and_result_for_an_mcp_server(qapp):
    dlg = ServerDialog(name="x", existing={"command": "npx", "args": ["a"]})
    cfg = dlg.result_config()
    assert cfg["restart"] == "on-failure" and cfg["restartMaxAttempts"] == 5 and cfg["healthInterval"] == 30.0
    assert "restartWindow" not in cfg and "healthFailures" not in cfg


def test_server_dialog_edits_and_preserves_unedited_settings(qapp):
    existing = {"command": "npx", "args": [], "restart": "never", "restartMaxAttempts": 2,
                "healthInterval": 0.0, "restartWindow": 60.0, "healthFailures": 4}
    dlg = ServerDialog(name="x", existing=existing)
    assert dlg.restart_combo.currentData() == "never"
    assert dlg.health_interval_spin.value() == 0
    dlg.restart_combo.setCurrentIndex(0)
    dlg.restart_attempts_spin.setValue(7)
    dlg.health_interval_spin.setValue(15)
    cfg = dlg.result_config()
    assert cfg["restart"] == "on-failure" and cfg["restartMaxAttempts"] == 7 and cfg["healthInterval"] == 15.0
    assert cfg["restartWindow"] == 60.0 and cfg["healthFailures"] == 4  # carried over untouched


def test_supervision_fields_are_hidden_for_a_service(qapp):
    dlg = ServerDialog(name="svc", existing={"command": "x", "args": [], "type": "service"})
    cfg = dlg.result_config()
    assert cfg["type"] == "service"
    assert "restart" not in cfg and "healthInterval" not in cfg
    assert dlg._form.isRowVisible(dlg.restart_combo) is False


def test_settings_dialog_saves_allow_lists_and_can_generate_a_token(qapp, tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(allowedHosts=["old.host"]), servers={}), path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)

    dlg = SettingsDialog(client=FakeClient())
    assert dlg.allowed_hosts_edit.text() == "old.host"
    dlg.allowed_hosts_edit.setText("hub.internal, ,  other.lan ")
    dlg.allowed_origins_edit.setText("https://tool.internal")
    dlg.generate_token_btn.click()
    token = dlg.token_edit.text()
    assert len(token) >= 32
    dlg.generate_token_btn.click()
    assert dlg.token_edit.text() != token, "each click makes a new random token"
    token = dlg.token_edit.text()
    dlg.apply()

    hub = load_config(path).hub
    assert hub.allowedHosts == ["hub.internal", "other.lan"]
    assert hub.allowedOrigins == ["https://tool.internal"]
    assert hub.authToken == token
