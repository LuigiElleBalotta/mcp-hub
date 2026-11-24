# tests/test_gui_registry.py
"""Registry search dialog, form pre-fill and the opt-in setting (hub-gui-and-clients R6, headless Qt)."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox

from mcp_hub import config as config_module
from mcp_hub.config import Config, HubConfig, hub_to_dict, load_config, save_config
from mcp_hub.gui.registry_dialog import RegistryDialog
from mcp_hub.gui.server_dialog import ServerDialog
from mcp_hub.gui.settings_dialog import SettingsDialog
from mcp_hub.registry import Draft, EnvVar, Option, RegistryError, SearchResult

NPM = Option("npm: @acme/weather@1.2.0", True, "npx", ["-y", "@acme/weather@1.2.0", "--port", "8080"],
             [EnvVar("WEATHER_API_KEY", "", secret=True, required=True, description="your key"),
              EnvVar("UNITS", "metric")], ["--db <path>"])
REMOTE = Option("remoto (sse): https://x", False, reason="server remoto: l'hub avvia processi locali")
DRAFTS = [
    Draft("io.github.acme/weather", "weather", "Weather", "Weather for agents", "1.2.0", [REMOTE, NPM]),
    Draft("io.github.acme/only-remote", "only-remote", "Only remote", "x", "1", [REMOTE]),
]


class FakeRegistry:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def search(self, query, cursor=None, limit=20):
        self.calls.append((query, cursor))
        if self.error:
            raise self.error
        return self.result


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


def _searched(qapp, result=None, error=None, text="weather"):
    reg = FakeRegistry(result or SearchResult(list(DRAFTS), "next-1"), error)
    dlg = RegistryDialog(client=reg)
    dlg.query_edit.setText(text)
    dlg.search()
    _wait(qapp, lambda: "Cerco" not in dlg.status.text())
    return dlg, reg


# --------------------------------------------------------------------------- dialog

def test_search_lists_results_and_picks_the_installable_option_first(qapp):
    dlg, reg = _searched(qapp)
    assert reg.calls == [("weather", None)] and dlg.results.count() == 2
    assert "2 risultati" in dlg.status.text() and "altri" in dlg.status.text() and dlg.more_btn.isEnabled()
    assert dlg.results.currentRow() == 0
    labels = [dlg.option_combo.itemText(i) for i in range(dlg.option_combo.count())]
    assert "non installabile" in labels[0] and "non installabile" not in labels[1]
    assert dlg.option_combo.currentIndex() == 1, "the first option that can actually run is preselected"
    text = dlg.details.toPlainText()
    assert "npx -y @acme/weather@1.2.0 --port 8080" in text and "WEATHER_API_KEY  [segreto, obbligatoria]" in text
    assert "DA COMPLETARE" in text and "--db <path>" in text
    assert dlg.use_btn.isEnabled()


def test_a_non_installable_choice_explains_why_and_cannot_be_used(qapp):
    dlg, _ = _searched(qapp)
    dlg.results.setCurrentRow(1)
    assert "Non installabile qui" in dlg.details.toPlainText() and "server remoto" in dlg.details.toPlainText()
    assert not dlg.use_btn.isEnabled()
    dlg.use()
    assert dlg.chosen is None


def test_using_a_result_returns_the_draft_without_running_anything(qapp):
    dlg, _ = _searched(qapp)
    dlg.use()
    assert dlg.chosen == {"name": "weather", "command": "npx", "args": ["-y", "@acme/weather@1.2.0", "--port", "8080"],
                          "env": {"WEATHER_API_KEY": "", "UNITS": "metric"}, "needs": ["--db <path>"]}


def test_short_queries_and_failures_are_reported_in_the_dialog(qapp):
    dlg, _ = _searched(qapp, error=ValueError("scrivi almeno 3 caratteri"), text="ab")
    assert "scrivi almeno 3 caratteri" in dlg.status.text() and dlg.results.count() == 0
    dlg, _ = _searched(qapp, error=RegistryError("registry non raggiungibile: ConnectError"))
    assert "non raggiungibile" in dlg.status.text() and dlg.search_btn.isEnabled()


def test_no_results_and_a_stale_cache_are_said(qapp):
    dlg, _ = _searched(qapp, SearchResult([], None))
    assert dlg.status.text() == "Nessun risultato." and not dlg.more_btn.isEnabled()
    dlg, _ = _searched(qapp, SearchResult(list(DRAFTS), None, stale=True))
    assert "dalla cache" in dlg.status.text()


def test_more_results_are_appended_not_replaced(qapp):
    dlg, reg = _searched(qapp)
    reg.result = SearchResult([Draft("a/three", "three", "Three", "d", "1", [NPM])], None)
    dlg.load_more()
    _wait(qapp, lambda: dlg.results.count() == 3)
    assert reg.calls[-1] == ("weather", "next-1") and not dlg.more_btn.isEnabled()
    assert [d.suggested_name for d in dlg.drafts] == ["weather", "only-remote", "three"]


def test_a_late_answer_for_an_old_query_is_ignored(qapp):
    dlg, _ = _searched(qapp)
    dlg._query = "something else"
    dlg._got("weather", SearchResult([Draft("x/y", "y", "Y", "", "1", [NPM])], None), None, append=False)
    assert len(dlg.drafts) == 2


# ---------------------------------------------------------------------- form pre-fill

def test_the_registry_button_exists_only_when_adding_and_when_enabled(qapp):
    dlg = ServerDialog(registry_enabled=True)
    assert dlg._registry_enabled is True
    assert ServerDialog()._registry_enabled is False, "off unless the user enabled it"
    assert ServerDialog(name="x", existing={"command": "y"}, registry_enabled=True)._registry_enabled is False, \
        "editing an existing server never offers it"


def test_a_choice_only_fills_the_form(qapp):
    dlg = ServerDialog(registry_enabled=True)
    dlg.apply_registry_choice({"name": "weather", "command": "npx", "args": ["-y", "@acme/weather@1.2.0"],
                               "env": {"WEATHER_API_KEY": "", "UNITS": "metric"}, "needs": ["--db <path>"]})
    assert dlg.name_edit.text() == "weather" and dlg.command_edit.text() == "npx"
    assert dlg.args_edit.text() == "-y @acme/weather@1.2.0" and "--db <path>" in dlg.args_edit.toolTip()
    env = {dlg.env_table.item(r, 0).text(): dlg.env_table.cellWidget(r, 1).text() for r in range(dlg.env_table.rowCount())}
    assert env == {"WEATHER_API_KEY": "", "UNITS": "metric"}, "the secret is left for the user to type"
    cfg = dlg.result_config()
    assert cfg["command"] == "npx" and cfg["env"]["UNITS"] == "metric"


def test_a_choice_keeps_a_name_the_user_already_typed_and_does_not_duplicate_env_keys(qapp):
    dlg = ServerDialog(name="mine", registry_enabled=True)
    dlg._add_env_row("UNITS", "imperial")
    dlg.apply_registry_choice({"name": "weather", "command": "npx", "args": [], "env": {"UNITS": "metric"}, "needs": []})
    assert dlg.name_edit.text() == "mine"
    assert [dlg.env_table.item(r, 0).text() for r in range(dlg.env_table.rowCount())] == ["UNITS"]
    assert dlg.env_table.cellWidget(0, 1).text() == "imperial", "what the user typed wins"


# ----------------------------------------------------------------------------- setting

def test_the_setting_is_off_by_default_omitted_from_config_and_saved_from_the_dialog(qapp, tmp_path, monkeypatch):
    assert HubConfig().registrySearch is False and "registrySearch" not in hub_to_dict(HubConfig())
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(), servers={}), path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)

    class Client:
        def update_settings(self, **fields):
            return fields

    dlg = SettingsDialog(client=Client())
    assert dlg.registry_checkbox.isChecked() is False
    dlg.registry_checkbox.setChecked(True)
    dlg.apply()
    assert load_config(path).hub.registrySearch is True
    assert SettingsDialog(client=Client()).registry_checkbox.isChecked() is True
