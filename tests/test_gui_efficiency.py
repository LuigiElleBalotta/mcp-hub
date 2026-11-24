# tests/test_gui_efficiency.py
"""GUI side of hub-proxy-efficiency R8 (headless, offscreen Qt)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

from mcp_hub import config as config_module
from mcp_hub.config import CONCURRENCY_MODES, Config, HubConfig, load_config, save_config
from mcp_hub.gui.main_window import ACTIONS_COL, MainWindow
from mcp_hub.gui.server_dialog import MODE_HELP, ServerDialog, split_list
from mcp_hub.gui.settings_dialog import SettingsDialog

STATUSES = {
    "lazy": {"status": "idle", "concurrency": "parallel", "type": "mcp"},
    "iso-idle": {"status": "idle", "concurrency": "isolated", "type": "mcp", "sessions": 0},
    "iso-busy": {"status": "running", "concurrency": "isolated", "type": "mcp", "sessions": 3},
    "svc": {"status": "stopped", "concurrency": "exclusive", "type": "service", "port": 8017},
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


def _buttons(win, name):
    return {b.text(): b for b in win.table.cellWidget(_row(win, name), ACTIONS_COL).findChildren(QPushButton)}


def test_idle_status_color_and_start_button(window):
    window._on_status_result(True, STATUSES, None)
    item = window.table.item(_row(window, "lazy"), 2)
    assert item.text() == "idle" and item.foreground().color().name() == "#546e7a"
    assert _buttons(window, "lazy")["Start"].isEnabled(), "Start brings an idle server up now"
    assert not _buttons(window, "lazy")["Stop"].isEnabled()


def test_isolated_server_shows_its_session_count_and_has_no_start(window):
    window._on_status_result(True, STATUSES, None)
    assert window.table.item(_row(window, "iso-busy"), 3).text() == "isolated (3)"
    assert window.table.item(_row(window, "iso-idle"), 3).text() == "isolated (0)"
    for name in ("iso-idle", "iso-busy"):
        assert not _buttons(window, name)["Start"].isEnabled(), "no shared process to start"
    assert _buttons(window, "iso-busy")["Stop"].isEnabled(), "Stop ends the sessions' processes"
    assert _buttons(window, "svc")["Start"].isEnabled()  # services unaffected


def test_concurrency_combo_offers_all_four_modes_with_a_line_of_help_each(qapp):
    dlg = ServerDialog(name="x", existing={"command": "npx", "args": []})
    modes = [dlg.concurrency_combo.itemText(i) for i in range(dlg.concurrency_combo.count())]
    assert tuple(modes) == CONCURRENCY_MODES
    for mode in modes:
        dlg.concurrency_combo.setCurrentText(mode)
        assert dlg.mode_help.text() == MODE_HELP[mode] and dlg.mode_help.text()


def test_dialog_defaults_for_the_new_fields(qapp):
    cfg = ServerDialog(name="x", existing={"command": "npx", "args": []}).result_config()
    assert cfg["startMode"] == "eager" and cfg["idleTimeout"] == 0.0 and cfg["persistent"] is False
    assert cfg["toolsAllow"] == [] and cfg["toolsDeny"] == [] and cfg["toolPrefix"] == ""


def test_dialog_edits_and_round_trips_the_new_fields(qapp):
    existing = {"command": "npx", "args": [], "concurrency": "isolated", "startMode": "lazy",
                "idleTimeout": 120.0, "persistent": True, "toolsAllow": ["read_*"], "toolsDeny": ["*x*"],
                "toolPrefix": "gl_"}
    dlg = ServerDialog(name="x", existing=existing)
    assert dlg.concurrency_combo.currentText() == "isolated" and dlg.start_mode_combo.currentData() == "lazy"
    assert dlg.idle_timeout_spin.value() == 120 and dlg.persistent_check.isChecked()
    assert dlg.tools_allow_edit.text() == "read_*" and dlg.tool_prefix_edit.text() == "gl_"
    dlg.tools_allow_edit.setText(" list_* ,get_* ,, ")
    dlg.tools_deny_edit.setText("")
    dlg.start_mode_combo.setCurrentIndex(0)
    dlg.idle_timeout_spin.setValue(0)
    dlg.persistent_check.setChecked(False)
    dlg.tool_prefix_edit.setText("  p.  ")
    cfg = dlg.result_config()
    assert cfg["concurrency"] == "isolated"
    assert cfg["toolsAllow"] == ["list_*", "get_*"] and cfg["toolsDeny"] == [] and cfg["toolPrefix"] == "p."
    assert cfg["startMode"] == "eager" and cfg["idleTimeout"] == 0.0 and cfg["persistent"] is False


def test_service_dialog_hides_and_omits_the_mcp_only_fields(qapp):
    dlg = ServerDialog(name="svc", existing={"command": "x", "args": [], "type": "service"})
    cfg = dlg.result_config()
    for key in ("startMode", "idleTimeout", "persistent", "toolsAllow", "toolsDeny", "toolPrefix"):
        assert key not in cfg
    for widget in (dlg.start_mode_combo, dlg.tools_allow_edit, dlg.tool_prefix_edit, dlg.mode_help):
        assert dlg._form.isRowVisible(widget) is False


def test_split_list():
    assert split_list("a, b,,c ,") == ["a", "b", "c"] and split_list("") == []


def test_settings_dialog_saves_transport_and_drain_timeout(qapp, tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(), servers={}), path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    dlg = SettingsDialog(client=FakeClient())
    assert dlg.transport_combo.currentData() == "sse" and dlg.drain_spin.value() == 20
    dlg.transport_combo.setCurrentIndex(1)
    dlg.drain_spin.setValue(45)
    dlg.apply()
    hub = load_config(path).hub
    assert hub.preferredTransport == "http" and hub.drainTimeout == 45.0
    dlg2 = SettingsDialog(client=FakeClient())
    assert dlg2.transport_combo.currentData() == "http" and dlg2.drain_spin.value() == 45
