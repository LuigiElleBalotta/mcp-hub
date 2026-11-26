"""Enabling / disabling a server from the GUI (a disabled one used to have no way back in the UI:
the dialog hard-coded enabled=True and the table did not even say which servers were disabled)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from dataclasses import asdict

import httpx
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

from mcp_hub.config import Config, HubConfig, ServerConfig, load_config, save_config
from mcp_hub.gui.main_window import ACTIONS_COL, MainWindow
from mcp_hub.gui.server_dialog import ServerDialog
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager

BASE = "http://127.0.0.1:37450"


def _sleeper(enabled):
    return ServerConfig(enabled=enabled, command=sys.executable, args=["-c", "import time; time.sleep(30)"],
                        env={"K": "v"}, restart="never", toolsDeny=["x*"], toolPrefix="p_", persistent=True)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ------------------------------------------------------------------------------ dialog

def test_dialog_defaults_to_enabled_and_keeps_what_an_existing_server_has(qapp):
    assert ServerDialog().result_config()["enabled"] is True
    assert ServerDialog("a", existing=asdict(_sleeper(False))).result_config()["enabled"] is False
    assert ServerDialog("a", existing=asdict(_sleeper(True))).result_config()["enabled"] is True


def test_the_checkbox_changes_the_saved_value(qapp):
    dialog = ServerDialog("a", existing=asdict(_sleeper(False)))
    dialog.enabled_check.setChecked(True)
    assert dialog.result_config()["enabled"] is True
    dialog.enabled_check.setChecked(False)
    assert dialog.result_config()["enabled"] is False


# ------------------------------------------------------------------------------- status

def test_status_marks_only_disabled_servers():
    hub = HubManager(Config(hub=HubConfig(), servers={"on": _sleeper(True), "off": _sleeper(False)}))
    snap = hub.status_snapshot()
    assert snap["off"]["enabled"] is False
    assert "enabled" not in snap["on"], "additive: nothing new for an enabled server"


# --------------------------------------------------------------------------------- table

class FakeClient:
    def __init__(self):
        self.upserts = []
        self.starts = []

    def status(self):
        return {"on": {"status": "stopped", "concurrency": "exclusive", "type": "mcp"},
                "off": {"status": "stopped", "concurrency": "exclusive", "type": "mcp", "enabled": False}}

    def logs(self, name):
        return []

    def metrics(self, name):
        return None

    def upsert(self, name, config):
        self.upserts.append((name, config))
        return "running"

    def start(self, name):
        self.starts.append(name)
        return "running"


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    save_config(Config(hub=HubConfig(), servers={"on": _sleeper(True), "off": _sleeper(False)}))
    monkeypatch.setattr(MainWindow, "_check_for_updates", lambda self: None)
    win = MainWindow(client=FakeClient())
    win.timer.stop()
    win._on_status_result(True, win.client.status(), None)
    yield win
    win.tray_icon.hide()
    win.close()


def _row(win, name):
    return next(r for r in range(win.table.rowCount()) if win.table.item(r, 0).text() == name)


def _buttons(win, name):
    return {b.text(): b for b in win.table.cellWidget(_row(win, name), ACTIONS_COL).findChildren(QPushButton)}


def test_a_disabled_server_says_so_and_offers_enable_instead_of_start(window):
    assert "disabilitato" in window.table.item(_row(window, "off"), 2).text()
    assert "disabilitato" not in window.table.item(_row(window, "on"), 2).text()
    assert "Abilita" in _buttons(window, "off") and "Start" not in _buttons(window, "off")
    assert "Start" in _buttons(window, "on") and "Abilita" not in _buttons(window, "on")


def test_enable_sends_the_whole_stored_config_with_only_enabled_changed(window, qapp):
    _buttons(window, "off")["Abilita"].click()
    for _ in range(100):
        if window.client.upserts:
            break
        qapp.processEvents()
        import time
        time.sleep(0.02)
    assert len(window.client.upserts) == 1
    name, sent = window.client.upserts[0]
    stored = asdict(load_config().servers["off"])
    assert name == "off" and sent["enabled"] is True
    assert {k: v for k, v in sent.items() if k != "enabled"} == {k: v for k, v in stored.items() if k != "enabled"}, \
        "nothing but `enabled` may change (env, filters, prefix, persistent... all kept)"
    assert window.client.starts == [], "enabling is one upsert; the hub starts it"


# ------------------------------------------------------------------------- real hub API

@pytest.mark.asyncio
async def test_enabling_through_the_api_makes_the_server_startable_and_persists_it():
    hub = HubManager(Config(hub=HubConfig(), servers={"off": _sleeper(False)}))
    save_config(hub.config)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(hub)), base_url=BASE) as api:
            assert (await api.post("/api/servers/off/start")).status_code == 409
            body = {**asdict(hub.config.servers["off"]), "enabled": True}
            assert (await api.post("/api/servers/off", json=body)).status_code == 200
            assert hub.config.servers["off"].enabled is True and load_config().servers["off"].enabled is True
            assert (await api.get("/api/status")).json()["servers"]["off"].get("enabled", True) is True
            assert hub.config.servers["off"].toolPrefix == "p_" and hub.config.servers["off"].env == {"K": "v"}
    finally:
        await hub.stop_all()
