# tests/test_gui_groups.py
"""Group selector and the groups dialog (hub-gui-and-clients R5, headless Qt)."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from mcp_hub.gui.groups_dialog import GroupsDialog
from mcp_hub.gui.main_window import MainWindow

STATUSES = {n: {"status": "running", "concurrency": "parallel", "type": "mcp"} for n in ("a", "b", "c")}


class FakeClient:
    def __init__(self):
        self.calls = []

    def status(self):
        return STATUSES

    def logs(self, name):
        return []

    def metrics(self, name):
        return None

    def group_action(self, name, action):
        self.calls.append(("group_action", name, action))
        return {"results": {}}

    def put_group(self, name, servers):
        self.calls.append(("put_group", name, list(servers)))

    def delete_group(self, name):
        self.calls.append(("delete_group", name))


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


def _names(win):
    return sorted(win.table.item(r, 0).text() for r in range(win.table.rowCount()))


def _wait(qapp, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        qapp.processEvents()
        time.sleep(0.01)


def test_the_group_selector_lists_all_servers_by_default_and_has_no_bulk_buttons_active(qapp, window):
    window._reload_groups({"Lavoro": ["a", "b"]})
    assert [window.group_combo.itemText(i) for i in range(window.group_combo.count())] == ["Tutti i server", "Lavoro"]
    window._on_status_result(True, STATUSES, None)
    assert _names(window) == ["a", "b", "c"]
    assert not window.group_start_btn.isEnabled() and not window.group_stop_btn.isEnabled()


def test_choosing_a_group_narrows_the_table_and_enables_the_bulk_buttons(qapp, window):
    window._on_status_result(True, STATUSES, None)
    window._reload_groups({"Lavoro": ["a", "b"], "Solo c": ["c"]})
    window.group_combo.setCurrentIndex(window.group_combo.findData("Lavoro"))
    window._on_status_result(True, STATUSES, None)
    assert _names(window) == ["a", "b"] and window.group_start_btn.isEnabled() and window.group_stop_btn.isEnabled()
    window.group_combo.setCurrentIndex(0)
    window._on_status_result(True, STATUSES, None)
    assert _names(window) == ["a", "b", "c"] and not window.group_start_btn.isEnabled()


def test_reloading_keeps_the_choice_if_the_group_still_exists_and_drops_it_if_not(qapp, window):
    window._reload_groups({"Lavoro": ["a"], "Altro": ["b"]})
    window.group_combo.setCurrentIndex(window.group_combo.findData("Altro"))
    window._reload_groups({"Lavoro": ["a"], "Altro": ["b", "c"]})
    assert window.group_combo.currentData() == "Altro"
    window._reload_groups({"Lavoro": ["a"]})
    assert window.group_combo.currentData() is None, "the group is gone: back to all servers"


def test_start_and_stop_group_call_the_hub_off_the_gui_thread(qapp, window):
    window._reload_groups({"Lavoro": ["a", "b"]})
    window.group_combo.setCurrentIndex(window.group_combo.findData("Lavoro"))
    window.group_start_btn.click()
    _wait(qapp, lambda: ("group_action", "Lavoro", "start") in window.client.calls)
    _wait(qapp, lambda: "Lavoro" not in window._busy)
    window.group_stop_btn.click()
    _wait(qapp, lambda: ("group_action", "Lavoro", "stop") in window.client.calls)


def test_a_server_removed_from_the_hub_still_shows_if_the_group_lists_it_only_when_present(qapp, window):
    window._reload_groups({"Lavoro": ["a", "ghost"]})
    window.group_combo.setCurrentIndex(window.group_combo.findData("Lavoro"))
    window._on_status_result(True, STATUSES, None)
    assert _names(window) == ["a"], "a stale member must not break or invent a row"


# ----------------------------------------------------------------------------- dialog

def test_dialog_shows_a_groups_members_as_checks(qapp):
    dlg = GroupsDialog(FakeClient(), ["a", "b", "c"], {"Lavoro": ["a", "c"]})
    checks = {dlg.member_list.item(i).text(): dlg.member_list.item(i).checkState() == Qt.CheckState.Checked
              for i in range(3)}
    assert checks == {"a": True, "b": False, "c": True}


def test_dialog_saves_only_what_changed(qapp, monkeypatch):
    client = FakeClient()
    dlg = GroupsDialog(client, ["a", "b", "c"], {"Lavoro": ["a"], "Altro": ["b"], "Vecchio": ["c"]})
    dlg.group_list.setCurrentRow(0)  # Lavoro
    dlg.member_list.item(1).setCheckState(Qt.CheckState.Checked)  # + b
    assert dlg.add_group("Nuovo") is True
    dlg.member_list.item(2).setCheckState(Qt.CheckState.Checked)  # Nuovo = [c]
    for i in range(dlg.group_list.count()):
        if dlg.group_list.item(i).text() == "Vecchio":
            dlg.group_list.setCurrentRow(i)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    dlg._delete_group()
    assert dlg.save() is True
    assert client.calls == [("delete_group", "Vecchio"), ("put_group", "Lavoro", ["a", "b"]),
                            ("put_group", "Nuovo", ["c"])], "'Altro' was untouched: nothing sent for it"
    assert dlg.changed and dlg.status.text() == "Salvato."
    client.calls.clear()
    dlg.save()
    assert client.calls == [], "saving twice sends nothing the second time"


def test_dialog_validates_group_names(qapp, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda p, t, text: warnings.append(text)))
    dlg = GroupsDialog(FakeClient(), ["a"], {"Lavoro": []})
    assert dlg.add_group("bad/name") is False and dlg.add_group("Lavoro") is False and dlg.add_group("") is False
    assert len(warnings) == 3 and "esiste" in warnings[1]
    assert list(dlg.groups) == ["Lavoro"]


def test_dialog_reports_a_failed_save_and_keeps_the_changes_pending(qapp):
    client = FakeClient()
    client.put_group = lambda *a: (_ for _ in ()).throw(RuntimeError("hub unreachable"))
    dlg = GroupsDialog(client, ["a"], {})
    dlg.add_group("G")
    dlg.member_list.item(0).setCheckState(Qt.CheckState.Checked)
    assert dlg.save() is False and "hub unreachable" in dlg.status.text() and dlg.changed is False
