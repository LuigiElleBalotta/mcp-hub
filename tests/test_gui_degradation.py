# tests/test_gui_degradation.py
"""The new GUI against a hub that is a version behind (no metrics/tools/groups routes), plus the
Settings switches of hub-gui-and-clients R8/R7 (headless Qt)."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
import keyring
import pytest

pytest.importorskip("PySide6")

from keyring.backends import fail
from PySide6.QtWidgets import QApplication

from mcp_hub import config as config_module
from mcp_hub import secrets
from mcp_hub.config import Config, HubConfig, load_config, save_config
from mcp_hub.gui.api_client import OLD_HUB_MESSAGE, HubApiClient
from mcp_hub.gui.groups_dialog import GroupsDialog
from mcp_hub.gui.main_window import MainWindow
from mcp_hub.gui.settings_dialog import SettingsDialog
from mcp_hub.gui.tools_panel import ToolsPanel

OLD_STATUS = {"gitlab": {"status": "running", "concurrency": "parallel", "type": "mcp"}}


def old_hub_client() -> HubApiClient:
    """What an old hub answers for a route it does not have: a plain-text 404."""
    def handler(request):
        if request.url.path == "/api/status":
            return httpx.Response(200, json={"servers": OLD_STATUS})
        return httpx.Response(404, text="Not Found", headers={"content-type": "text/plain"})

    client = HubApiClient()
    client._client = httpx.Client(base_url="http://127.0.0.1:37450", transport=httpx.MockTransport(handler),
                                  auth=client._auth)
    return client


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


# ------------------------------------------------------------------------------ client

def test_every_new_client_call_degrades_to_one_clear_message_or_none():
    client = old_hub_client()
    assert client.metrics("gitlab") is None
    for call in (lambda: client.tools("gitlab"), lambda: client.call_tool("gitlab", "t", {}),
                 lambda: client.set_filters("gitlab", [], []), lambda: client.groups(),
                 lambda: client.put_group("G", []), lambda: client.delete_group("G"),
                 lambda: client.group_action("G", "start")):
        with pytest.raises(RuntimeError) as err:
            call()
        assert str(err.value) == OLD_HUB_MESSAGE


# ------------------------------------------------------------------------------ widgets

def test_the_main_window_works_against_an_old_hub(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_check_for_updates", lambda self: None)
    win = MainWindow(client=old_hub_client())
    win.timer.stop()
    try:
        win.refresh()
        _wait(qapp, lambda: win.table.rowCount() == 1)
        assert [win.table.item(0, c).text() for c in (4, 5, 6)] == ["-", "-", "-"], "no metrics: dashes, not a crash"
        win.table.setCurrentCell(0, 0)
        win.refresh()
        _wait(qapp, lambda: "non disponibil" in win.detail_pane._values["pid"].text())
        win.bottom_tabs.setCurrentWidget(win.tools_panel)
        _wait(qapp, lambda: "Impossibile leggere i tool" in win.tools_panel.status.text())
        assert OLD_HUB_MESSAGE in win.tools_panel.status.text()
    finally:
        win.tray_icon.hide()
        win.close()


def test_the_tool_panel_and_group_dialog_report_an_old_hub(qapp):
    panel = ToolsPanel(old_hub_client())
    panel.set_server("gitlab", load=True)
    _wait(qapp, lambda: "Impossibile" in panel.status.text())
    assert OLD_HUB_MESSAGE in panel.status.text() and panel.list.count() == 0

    dlg = GroupsDialog(old_hub_client(), ["a"], {})
    dlg.add_group("G")
    assert dlg.save() is False and OLD_HUB_MESSAGE in dlg.status.text() and dlg.changed is False


# ------------------------------------------------------------------------------ settings

@pytest.fixture
def settings_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(), servers={}), path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    return path


class _Client:
    def update_settings(self, **fields):
        return fields


def test_settings_has_the_metrics_switch_and_says_which_credential_store_is_in_use(qapp, settings_config):
    from test_secrets import MemoryKeyring

    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    try:
        dlg = SettingsDialog(client=_Client())
        assert dlg.metrics_checkbox.isChecked() is True
        assert "MemoryKeyring" in dlg.secret_store_label.text(), "it names the store in use"
        assert "non disponibile" not in dlg.secret_store_label.text()
        dlg.metrics_checkbox.setChecked(False)
        dlg.apply()
        assert load_config(settings_config).hub.metrics is False
        assert SettingsDialog(client=_Client()).metrics_checkbox.isChecked() is False
    finally:
        keyring.set_keyring(previous)


def test_settings_says_when_there_is_no_credential_store(qapp, settings_config):
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        assert secrets.backend_name() is None
        assert "non disponibile" in SettingsDialog(client=_Client()).secret_store_label.text()
    finally:
        keyring.set_keyring(previous)
