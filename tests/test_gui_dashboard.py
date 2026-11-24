# tests/test_gui_dashboard.py
"""Dashboard columns and the detail pane (hub-gui-and-clients R1, headless Qt)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from mcp_hub.gui.api_client import HubApiClient
from mcp_hub.gui.detail_pane import DetailPane, format_ms, format_recent, format_uptime, NO_DATA
from mcp_hub.gui.main_window import COL_CALLS, COL_P95, COL_SESSIONS, COLUMNS, MainWindow, dashboard_cells

STATUSES = {
    "gitlab": {"status": "running", "concurrency": "parallel", "type": "mcp", "pid": 4242, "uptime": 75.0,
               "sessions": 3, "inFlight": 1, "calls": 120, "errors": 2, "callsPerMin": 14, "p50": 8.2, "p95": 143.7},
    "old-hub-entry": {"status": "running", "concurrency": "exclusive", "type": "mcp"},
    "svc": {"status": "running", "concurrency": "exclusive", "type": "service", "port": 8017},
}
DETAIL = {"status": "running", "pid": 4242, "uptime": 3725.0, "restarts": 1, "sessions": 3, "inFlight": 0,
          "calls": 120, "callsPerMin": 14, "errors": 2, "cacheHits": 40, "p50": 8.2, "p95": 143.7,
          "lastError": "managed server 'gitlab' subprocess exited", "enabled": True,
          "recent": [{"at": 1_700_000_000.0, "method": "tools/call read_file", "ms": 12.34, "ok": True},
                     {"at": 1_699_999_990.0, "method": "tools/list", "ms": 3.0, "ok": False}]}


class FakeClient:
    def status(self):
        return STATUSES

    def logs(self, name):
        return []

    def metrics(self, name):
        return DETAIL if name == "gitlab" else None


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


def test_formatters():
    assert format_uptime(None) == "-" and format_uptime(7) == "7s" and format_uptime(75) == "1m 15s"
    assert format_uptime(3725) == "1h 02m"
    assert format_ms(None) == "-" and format_ms(3.04) == "3.0 ms" and format_ms(143.7) == "144 ms"
    line = format_recent({"at": 0, "method": "tools/list", "ms": 3.0, "ok": False})
    assert "tools/list" in line and "ERRORE" in line and "3.0 ms" in line


def test_dashboard_cells_for_mcp_service_and_old_hub():
    assert dashboard_cells(STATUSES["gitlab"], False) == ("14", "144 ms", "3")
    assert dashboard_cells(STATUSES["old-hub-entry"], False) == ("-", "-", "-"), "an older hub sends none of it"
    assert dashboard_cells(STATUSES["svc"], True) == ("", "", ""), "a service has no calls or sessions"


def test_table_shows_the_dashboard_columns(window):
    assert [window.table.horizontalHeaderItem(i).text() for i in range(len(COLUMNS))] == COLUMNS
    window._on_status_result(True, STATUSES, None)
    row = _row(window, "gitlab")
    assert [window.table.item(row, c).text() for c in (COL_CALLS, COL_P95, COL_SESSIONS)] == ["14", "144 ms", "3"]
    assert window.table.item(_row(window, "old-hub-entry"), COL_P95).text() == "-"
    assert window.table.item(_row(window, "svc"), COL_CALLS).text() == ""


def test_detail_pane_follows_the_selected_server(window):
    window._on_status_result(True, STATUSES, None)
    window.table.setCurrentCell(_row(window, "gitlab"), 0)
    window._on_status_result(True, STATUSES, None, ("gitlab", DETAIL))
    pane = window.detail_pane
    assert pane.title.text() == "Dettaglio: gitlab"
    texts = {key: label.text() for key, label in pane._values.items()}
    assert texts["pid"] == "4242" and texts["uptime"] == "1h 02m" and texts["restarts"] == "1"
    assert texts["calls"] == "120 (14/min)" and texts["p95"] == "144 ms" and texts["cacheHits"] == "40"
    assert "subprocess exited" in texts["lastError"]
    recent = pane.recent.toPlainText().splitlines()
    assert "tools/call read_file" in recent[0] and "tools/list" in recent[1] and "ERRORE" in recent[1]


def test_metrics_of_another_server_do_not_overwrite_the_selected_one(window):
    window._on_status_result(True, STATUSES, None)
    window.table.setCurrentCell(_row(window, "svc"), 0)
    window._on_status_result(True, STATUSES, None, ("gitlab", DETAIL))  # a late answer for a row no longer selected
    assert window.detail_pane._values["pid"].text() == "-"


def test_detail_pane_says_so_when_there_are_no_metrics(qapp):
    pane = DetailPane()
    pane.show_metrics("gitlab", None)
    assert pane.title.text() == "Dettaglio: gitlab" and pane._values["pid"].text() == NO_DATA
    pane.show_metrics("gitlab", DETAIL)
    pane.clear()
    assert pane.title.text().endswith("nessun server selezionato") and pane.recent.toPlainText() == ""


def _client_answering(status, content_type, body):
    client = HubApiClient()
    client._client = httpx.Client(
        base_url="http://127.0.0.1:37450", auth=client._auth,
        transport=httpx.MockTransport(lambda req: httpx.Response(status, headers={"content-type": content_type},
                                                                  content=body)))
    return client


def test_client_returns_metrics_or_none_for_an_old_hub():
    assert _client_answering(200, "application/json", b'{"pid": 1}').metrics("x") == {"pid": 1}
    assert _client_answering(404, "text/plain", b"Not Found").metrics("x") is None, "a hub without the route"
    assert _client_answering(404, "application/json", b'{"error": "unknown server: x"}').metrics("x") is None
