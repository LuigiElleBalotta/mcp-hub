# tests/test_gui_service.py
"""GUI handling of `type: "service"` servers (headless, offscreen Qt)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

from mcp_hub.gui.main_window import MainWindow
from mcp_hub.gui.server_dialog import ServerDialog


class FakeClient:
    def __init__(self):
        self.calls = []

    def status(self):
        return STATUSES

    def logs(self, name):
        return [f"log of {name}"]

    def start(self, name):
        self.calls.append(("start", name))
        return "starting"

    def stop(self, name):
        self.calls.append(("stop", name))
        return "stopped"


STATUSES = {
    "gitlab": {"status": "running", "concurrency": "parallel", "type": "mcp"},
    "rizzo-flow": {"status": "stopped", "concurrency": "exclusive", "type": "service", "port": 8017},
    "broken": {"status": "crashed", "concurrency": "exclusive", "type": "service", "port": 9},
}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_check_for_updates", lambda self: None)
    win = MainWindow(client=FakeClient())
    win.timer.stop()  # no polling: the test drives `_on_status_result` itself
    yield win
    win.tray_icon.hide()
    win.close()


def _row(win, name):
    for row in range(win.table.rowCount()):
        if win.table.item(row, 0).text() == name:
            return row
    raise AssertionError(name)


def _buttons(win, name):
    cell = win.table.cellWidget(_row(win, name), 4)
    return {b.text(): b for b in cell.findChildren(QPushButton)}


def test_table_distinguishes_mcp_from_service(window):
    window._on_status_result(True, STATUSES, None)
    mcp, svc = _row(window, "gitlab"), _row(window, "rizzo-flow")
    assert window.table.item(mcp, 1).text() == "MCP"
    assert window.table.item(mcp, 3).text() == "parallel"
    assert window.table.item(svc, 1).text() == "Service"
    # A service shows its port, never a concurrency mode / SSE endpoint.
    assert window.table.item(svc, 3).text() == "HTTP :8017"


def test_start_stop_buttons_follow_status(window):
    window._on_status_result(True, STATUSES, None)
    running, stopped, crashed = (_buttons(window, n) for n in ("gitlab", "rizzo-flow", "broken"))
    assert not running["Start"].isEnabled() and running["Stop"].isEnabled()
    assert stopped["Start"].isEnabled() and not stopped["Stop"].isEnabled()
    assert crashed["Start"].isEnabled() and not crashed["Stop"].isEnabled()


def test_start_button_calls_client_off_the_gui_thread(window, qapp):
    window._on_status_result(True, STATUSES, None)
    _buttons(window, "rizzo-flow")["Start"].click()
    assert "rizzo-flow" in window._busy
    for worker in list(window._action_workers):
        worker.wait(5000)
    qapp.processEvents()
    assert ("start", "rizzo-flow") in window.client.calls
    assert "rizzo-flow" not in window._busy


def test_selected_server_logs_refresh_with_status(window):
    window._on_status_result(True, STATUSES, None)
    window.table.setCurrentCell(_row(window, "rizzo-flow"), 0)
    window._on_status_result(True, STATUSES, ("rizzo-flow", ["starting...", "ready"]))
    assert "ready" in window.log_panel.text.toPlainText()
    # A late result for a server that is no longer selected is ignored.
    window._on_status_result(True, STATUSES, ("gitlab", ["stale"]))
    assert "stale" not in window.log_panel.text.toPlainText()


def test_dialog_shows_service_fields_only_for_service(qapp):
    dialog = ServerDialog()
    dialog.show()
    assert not dialog.is_service()
    assert dialog.concurrency_combo.isVisible() and not dialog.health_url_edit.isVisible()
    assert "type" not in dialog.result_config()

    dialog.type_combo.setCurrentIndex(1)
    assert dialog.is_service()
    assert not dialog.concurrency_combo.isVisible() and dialog.health_url_edit.isVisible()
    dialog.command_edit.setText("uv")
    dialog.args_edit.setText("run rizzo serve")
    dialog.cwd_edit.setText("C:/rizzo-flow")
    dialog.health_url_edit.setText("http://127.0.0.1:8017/health")
    dialog.autostart_check.setChecked(True)
    config = dialog.result_config()
    assert config["type"] == "service" and config["autostart"] is True
    assert config["healthUrl"] == "http://127.0.0.1:8017/health"
    assert config["port"] is None and config["cwd"] == "C:/rizzo-flow"
    dialog.close()


def test_dialog_edit_preserves_service_fields(qapp):
    existing = {"command": "uv", "args": ["run"], "env": {}, "type": "service", "port": 8017,
                "healthUrl": "http://127.0.0.1:8017/health", "healthTimeout": 300.0, "autostart": True}
    dialog = ServerDialog("rizzo-flow", existing=existing)
    assert dialog.is_service()
    config = dialog.result_config()
    assert config["port"] == 8017 and config["healthTimeout"] == 300.0 and config["autostart"] is True
