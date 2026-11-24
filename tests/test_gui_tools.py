# tests/test_gui_tools.py
"""Tools tab: argument form, filter checkboxes, tester (hub-gui-and-clients R2; headless Qt)."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLineEdit, QMessageBox

from mcp_hub.gui.main_window import MainWindow
from mcp_hub.gui.tools_panel import ArgsForm, ToolsPanel

TOOLS = {
    "tools": [
        {"name": "read_a", "clientName": "gl_read_a", "description": "reads a", "readOnly": True, "visible": True,
         "lockedByPattern": False, "inputSchema": {"type": "object", "properties": {
             "path": {"type": "string", "description": "file"}, "limit": {"type": "integer"}},
             "required": ["path"]}},
        {"name": "read_secret", "clientName": "gl_read_secret", "description": "", "readOnly": True,
         "visible": False, "lockedByPattern": True, "inputSchema": {}},
        {"name": "write_b", "clientName": "gl_write_b", "description": "writes b", "readOnly": False,
         "visible": False, "lockedByPattern": False, "inputSchema": {"type": "object", "properties": {}}},
    ],
    # write_b is hidden by the allow-list, read_secret by a deny glob: consistent with what the hub computes
    "source": "live", "filtered": True, "toolsAllow": ["read_*"], "toolsDeny": ["*secret*"], "toolPrefix": "gl_",
}


class FakeClient:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"gitlab": {"status": "running", "concurrency": "parallel", "type": "mcp"},
                "svc": {"status": "running", "concurrency": "exclusive", "type": "service"}}

    def logs(self, name):
        return []

    def metrics(self, name):
        return None

    def tools(self, name):
        self.calls.append(("tools", name))
        return TOOLS

    def set_filters(self, name, allow, deny, prefix=None):
        self.calls.append(("set_filters", name, allow, deny))
        return {}

    def call_tool(self, name, tool, arguments, confirm=False):
        self.calls.append(("call_tool", name, tool, arguments, confirm))
        return {"result": {"echo": arguments}, "error": None, "ms": 3.5, "readOnly": not confirm}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _wait(qapp, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        qapp.processEvents()
        time.sleep(0.01)


def _loaded_panel(qapp, client=None):
    client = client or FakeClient()
    panel = ToolsPanel(client)
    panel.set_server("gitlab", load=True)
    _wait(qapp, lambda: panel.list.count() == 3)
    return panel, client


# ---------------------------------------------------------------------------- ArgsForm

def test_form_builds_typed_fields_and_returns_typed_arguments(qapp):
    schema = {"type": "object", "required": ["name"], "properties": {
        "name": {"type": "string"}, "count": {"type": "integer"}, "ratio": {"type": "number"},
        "flag": {"type": "boolean"}, "mode": {"enum": ["fast", "slow"]}, "tags": {"type": "array"}}}
    form = ArgsForm(schema)
    f = form._fields
    assert isinstance(f["name"][1], QLineEdit) and f["name"][2] is True and isinstance(f["flag"][1], QCheckBox)
    assert isinstance(f["mode"][1], QComboBox) and f["tags"][0] == "json"
    f["name"][1].setText(" x ")
    f["count"][1].setText("7")
    f["ratio"][1].setText("0.5")
    f["flag"][1].setChecked(True)
    f["mode"][1].setCurrentIndex(2)  # blank, fast, slow
    f["tags"][1].setText('["a", "b"]')
    assert form.arguments() == {"name": "x", "count": 7, "ratio": 0.5, "flag": True, "mode": "slow",
                                "tags": ["a", "b"]}


def test_form_omits_empty_optional_fields_and_enforces_required_ones(qapp):
    form = ArgsForm({"properties": {"name": {"type": "string"}, "n": {"type": "integer"}}, "required": ["name"]})
    with pytest.raises(ValueError, match="name"):
        form.arguments()
    form._fields["name"][1].setText("a")
    assert form.arguments() == {"name": "a"}


@pytest.mark.parametrize("text,kind", [("abc", "integer"), ("1.2.3", "number"), ("{oops", "json")])
def test_form_rejects_values_of_the_wrong_type(qapp, text, kind):
    form = ArgsForm({"properties": {"v": {"type": kind if kind != "json" else "array"}}})
    form._fields["v"][1].setText(text)
    with pytest.raises(ValueError, match="'v'"):
        form.arguments()


def test_form_falls_back_to_json_text_when_there_are_no_properties(qapp):
    for schema in (None, {}, {"type": "object"}, {"properties": {}}):
        form = ArgsForm(schema)
        assert form._json is not None and form.arguments() == {}
    form._json.setPlainText('{"a": 1}')
    assert form.arguments() == {"a": 1}
    form._json.setPlainText("[1]")
    with pytest.raises(ValueError):
        form.arguments()
    form._json.setPlainText("nope")
    with pytest.raises(ValueError, match="JSON"):
        form.arguments()


# ---------------------------------------------------------------------------- panel

def test_selecting_a_server_never_fetches_until_asked(qapp):
    panel = ToolsPanel(FakeClient())
    panel.set_server("gitlab", load=False)  # e.g. the Tool tab is not showing: a lazy server must not start
    qapp.processEvents()
    assert panel.client.calls == [] and panel.list.count() == 0
    panel.ensure_loaded()  # the user opened the tab
    _wait(qapp, lambda: panel.list.count() == 3)
    assert panel.client.calls == [("tools", "gitlab")]
    panel.ensure_loaded()  # already loaded: no second fetch
    qapp.processEvents()
    assert panel.client.calls == [("tools", "gitlab")]


def test_list_shows_client_names_check_state_and_locks_pattern_hidden_tools(qapp):
    panel, _ = _loaded_panel(qapp)
    items = {panel.list.item(i).data(Qt.ItemDataRole.UserRole): panel.list.item(i) for i in range(3)}
    assert items["read_a"].text().startswith("gl_read_a") and "sola lettura" in items["read_a"].text()
    assert items["read_a"].checkState() == Qt.CheckState.Checked
    assert items["write_b"].checkState() == Qt.CheckState.Unchecked
    assert items["write_b"].flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert not items["read_secret"].flags() & Qt.ItemFlag.ItemIsUserCheckable, "a deny glob cannot be undone here"
    assert "3 tool" in panel.status.text() and "2 nascosti" in panel.status.text()
    assert panel.apply_btn.isEnabled() is False


def test_toggling_enables_apply_and_apply_sends_the_minimal_filter_change(qapp):
    panel, client = _loaded_panel(qapp)
    write_b = next(panel.list.item(i) for i in range(3) if panel.list.item(i).data(Qt.ItemDataRole.UserRole) == "write_b")
    write_b.setCheckState(Qt.CheckState.Checked)
    assert panel.apply_btn.isEnabled()
    write_b.setCheckState(Qt.CheckState.Unchecked)
    assert not panel.apply_btn.isEnabled(), "back to how it was: nothing to apply"
    write_b.setCheckState(Qt.CheckState.Checked)
    panel.apply_filters()
    _wait(qapp, lambda: any(c[0] == "set_filters" for c in client.calls))
    # the allow-list was what hid write_b, so it grows by that exact name; deny is untouched
    sent = next(c for c in client.calls if c[0] == "set_filters")
    assert sent[1] == "gitlab" and sent[2] == ["read_*", "write_b"] and sent[3] == ["*secret*"]
    _wait(qapp, lambda: client.calls.count(("tools", "gitlab")) == 2)  # reloaded after applying


def test_hiding_a_tool_denies_its_exact_name(qapp):
    panel, client = _loaded_panel(qapp)
    read_a = next(panel.list.item(i) for i in range(3) if panel.list.item(i).data(Qt.ItemDataRole.UserRole) == "read_a")
    read_a.setCheckState(Qt.CheckState.Unchecked)
    panel.apply_filters()
    _wait(qapp, lambda: any(c[0] == "set_filters" for c in client.calls))
    sent = next(c for c in client.calls if c[0] == "set_filters")
    assert sent[2] == ["read_*"] and sent[3] == ["*secret*", "read_a"]


def test_search_hides_non_matching_tools(qapp):
    panel, _ = _loaded_panel(qapp)
    panel.search.setText("WRITE")
    assert [panel.list.item(i).isHidden() for i in range(3)] == [True, True, False]
    panel.search.setText("")
    assert not any(panel.list.item(i).isHidden() for i in range(3))


def test_a_late_answer_for_a_server_no_longer_selected_is_ignored(qapp):
    panel = ToolsPanel(FakeClient())
    panel.set_server("gitlab", load=False)
    panel.set_server("other", load=False)
    panel._filled("gitlab", TOOLS, None)  # the old request finishing late
    assert panel.list.count() == 0 and panel.title.text() == "Tool: other"


def test_a_failed_load_is_said_not_swallowed(qapp):
    panel = ToolsPanel(FakeClient())
    panel.set_server("gitlab", load=False)
    panel._filled("gitlab", None, "managed server 'gitlab' subprocess is not running")
    assert "subprocess is not running" in panel.status.text() and panel.list.count() == 0


def test_a_service_has_no_tools_tab_content(qapp):
    panel = ToolsPanel(FakeClient())
    panel.set_server("svc", load=True, supported=False)
    assert "Solo i server MCP" in panel.status.text() and not panel.refresh_btn.isEnabled()
    assert panel.client.calls == []


# ---------------------------------------------------------------------------- tester

def _select(panel, tool):
    for i in range(panel.list.count()):
        if panel.list.item(i).data(Qt.ItemDataRole.UserRole) == tool:
            panel.list.setCurrentRow(i)
            return
    raise AssertionError(tool)


def test_running_a_read_only_tool_needs_no_prompt(qapp, monkeypatch):
    panel, client = _loaded_panel(qapp)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: pytest.fail("no prompt for a read-only tool"))
    _select(panel, "read_a")
    panel.form._fields["path"][1].setText("C:/x")
    panel.run_tool()
    _wait(qapp, lambda: "echo" in panel.result.toPlainText())
    assert ("call_tool", "gitlab", "read_a", {"path": "C:/x"}, False) in client.calls
    assert "3.5 ms" in panel.result.toPlainText()


def test_a_tool_that_may_have_effects_asks_first_and_can_be_declined(qapp, monkeypatch):
    panel, client = _loaded_panel(qapp)
    _select(panel, "write_b")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    panel.run_tool()
    qapp.processEvents()
    assert not any(c[0] == "call_tool" for c in client.calls), "declined: nothing was sent"

    seen = []
    monkeypatch.setattr(QMessageBox, "question", lambda parent, title, text: seen.append(text) or QMessageBox.StandardButton.Yes)
    panel.run_tool()
    _wait(qapp, lambda: any(c[0] == "call_tool" for c in client.calls))
    assert ("call_tool", "gitlab", "write_b", {}, True) in client.calls
    assert "write_b" in seen[0] and "effetti" in seen[0]


def test_invalid_arguments_are_reported_and_nothing_is_sent(qapp):
    panel, client = _loaded_panel(qapp)
    _select(panel, "read_a")  # "path" is required
    panel.run_tool()
    assert "path" in panel.result.toPlainText() and not any(c[0] == "call_tool" for c in client.calls)


def test_an_error_from_the_hub_is_shown(qapp):
    client = FakeClient()
    client.call_tool = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the tool did not answer in time"))
    panel, _ = _loaded_panel(qapp, client)
    _select(panel, "read_a")
    panel.form._fields["path"][1].setText("x")
    panel.run_tool()
    _wait(qapp, lambda: "did not answer" in panel.result.toPlainText())


# ----------------------------------------------------------------------- main window

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


def test_main_window_only_loads_tools_while_the_tool_tab_is_showing(qapp, window):
    window._on_status_result(True, window.client.status(), None)
    window.table.setCurrentCell(_row(window, "gitlab"), 0)
    qapp.processEvents()
    assert window.client.calls == [], "selecting a row must not start a server"
    window.bottom_tabs.setCurrentWidget(window.tools_panel)
    _wait(qapp, lambda: window.tools_panel.list.count() == 3)
    assert window.client.calls == [("tools", "gitlab")]


def test_main_window_tool_tab_follows_the_selection_and_skips_services(qapp, window):
    window._on_status_result(True, window.client.status(), None)
    window.bottom_tabs.setCurrentWidget(window.tools_panel)
    window.table.setCurrentCell(_row(window, "svc"), 0)
    qapp.processEvents()
    assert "Solo i server MCP" in window.tools_panel.status.text() and window.client.calls == []
    window.table.setCurrentCell(_row(window, "gitlab"), 0)
    _wait(qapp, lambda: window.tools_panel.list.count() == 3)
