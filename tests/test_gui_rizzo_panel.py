# tests/test_gui_rizzo_panel.py
"""The built-in Rizzo Flow / Jev tab (headless, offscreen Qt).

Set MCP_HUB_SHOTS=<dir> to also save a screenshot of every state."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import httpx
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from mcp_hub import rizzo_setup as rs
from mcp_hub.config import BUILTIN_RIZZO, RizzoSettings
from mcp_hub.gui import rizzo_panel as rp
from mcp_hub.gui.main_window import MainWindow
from mcp_hub.gui.rizzo_panel import RizzoPanel
from mcp_hub.rizzo_setup import Phase, StepState
from test_rizzo_setup import install_everything, make_env, tiny_weights  # noqa: F401 (fixture)

SHOTS = os.environ.get("MCP_HUB_SHOTS")


class FakeClient:
    def __init__(self, settings=None, reachable=True):
        self.settings = settings or RizzoSettings()
        self.calls = []
        self.reachable = reachable

    def rizzo(self):
        return {"settings": self.settings.__dict__.copy(), "status": None}

    def set_rizzo(self, data):
        if not self.reachable:
            raise httpx.ConnectError("hub down")
        self.calls.append(("set_rizzo", data))
        self.settings = RizzoSettings(**data)

    def start(self, name):
        self.calls.append(("start", name))
        return "starting"

    def stop(self, name):
        self.calls.append(("stop", name))
        return "stopped"

    def logs(self, name):
        return ["INFO: server log line"]

    def status(self):
        return {}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def shot(panel, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        panel.resize(860, 1000)
        panel.grab().save(str(Path(SHOTS) / f"{name}.png"))


def detection(tmp_path, *, installed=False, registered=False, running=False, partial=False, tools=None, port=8017):
    if installed:
        install_everything(tmp_path)
    elif partial:
        (tmp_path / "rizzo-flow" / ".git").mkdir(parents=True)
    kw = {"tools": tools} if tools else {}
    env, _, _ = make_env(tmp_path, settings=RizzoSettings(installDir=str(tmp_path) if registered else None, port=port),
                         health=running, **kw)
    return rs.detect(rs.SetupOptions(install_dir=tmp_path), env)


def make_panel(qapp, tmp_path, client=None, **kw):
    settings = RizzoSettings(installDir=str(tmp_path)) if kw.pop("registered", False) else RizzoSettings()
    client = client or FakeClient(settings)
    panel = RizzoPanel(client, initial=settings, auto_poll=False, **kw)
    panel.dir_edit.setText(str(tmp_path))
    return panel


def set_state(panel, det, hub=None, reachable=True):
    panel.apply_detection(det, panel._persisted)
    panel.on_hub_status({"status": hub, "builtin": True} if hub else None, reachable)


def step(panel, i):
    """(state value, detail text) of step row i."""
    item = panel.steps.topLevelItem(i)
    return item.data(0, Qt.ItemDataRole.UserRole), item.text(2)


def wait(worker):
    assert worker.wait(5000)
    QApplication.processEvents()


# ------------------------------------------------------------ states

def test_not_installed_offers_install_and_hides_start_stop(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path)
    assert panel.phase() == Phase.CHECKING
    set_state(panel, detection(tmp_path))
    assert panel.phase() == Phase.NOT_INSTALLED
    assert panel.state_label.text() == "Non installato"
    assert panel.install_btn.isVisibleTo(panel) and panel.install_btn.text() == "Installa Rizzo Flow"
    assert not panel.start_btn.isVisibleTo(panel) and not panel.stop_btn.isVisibleTo(panel)
    assert panel.install_box.isVisibleTo(panel) and not panel.repair_btn.isVisibleTo(panel)
    assert step(panel, 0)[0] == "skipped"                        # prerequisites ok
    assert step(panel, 1) == ("pending", str(tmp_path / "rizzo-flow"))
    assert not panel.steps.topLevelItem(0).icon(0).isNull()      # style icon, not a font glyph
    assert "ultimo health check" not in panel.details_label.text()  # nothing to check yet
    assert "GPU: NVIDIA GeForce RTX 3060 Laptop GPU (6144 MiB)" in panel.details_label.text()
    assert "Porta 8017" in panel.details_label.text() and "ctx 16384" in panel.details_label.text()
    assert "rizzo-latest" in panel.details_label.text()
    assert panel.tab_title() == "Rizzo Flow / Jev — da installare"
    shot(panel, "1-not-installed")


def test_partial_install_offers_resume(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path)
    set_state(panel, detection(tmp_path, partial=True))
    assert panel.phase() == Phase.PARTIAL and panel.install_btn.text() == "Riprendi installazione"
    assert step(panel, 1) == ("skipped", "gia' presente")
    assert step(panel, 2)[0] == "pending" and panel.steps.topLevelItem(2).text(0) == "○"  # uv sync to do
    shot(panel, "2-partial")


def test_missing_prerequisite_and_gpu_warning_are_visible(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path)
    set_state(panel, detection(tmp_path, tools=("git",)))
    assert step(panel, 0)[0] == "pending" and "mancano" in step(panel, 0)[1]
    assert "GPU NVIDIA non rilevata" in panel.warning_label.text()
    assert panel.warning_label.isVisibleTo(panel)


def test_installing_shows_step_and_download_progress(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path)
    set_state(panel, detection(tmp_path, partial=True))
    panel._installing = True
    panel._step_state = {sid: (StepState.PENDING, "") for sid in rs.STEP_IDS}
    for sid in ("prereqs", "clone_rizzo", "uv_sync"):
        panel._on_step(sid, "done", "")
    panel._on_step("download", "running", "")
    assert panel.current_bar.maximum() == 0                       # busy until a size is known
    panel._on_progress("download", 1_288_490_189, 3_914_315_482, 8_808_038)
    assert panel.phase() == Phase.INSTALLING
    assert panel.state_label.text() == "Installazione in corso: Scarica pesi e runtime CUDA (~3,9 GB) (3/8)"
    assert panel.current_label.text() == "Download pesi e runtime: 1.2 GB / 3.6 GB (33%), 8 MB/s"
    assert panel.current_bar.maximum() == 1000 and 320 < panel.current_bar.value() < 340
    assert panel.overall_bar.value() == 3
    assert panel.cancel_btn.isVisibleTo(panel) and not panel.install_btn.isVisibleTo(panel)
    assert not panel.recheck_btn.isEnabled()
    assert step(panel, 3)[0] == "running" and panel.steps.topLevelItem(3).text(0) == "▶"
    assert [step(panel, i)[0] for i in range(3)] == ["done"] * 3
    shot(panel, "3-installing-download")


def test_failed_step_is_marked_and_keeps_the_box_open(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path)
    set_state(panel, detection(tmp_path, partial=True))
    panel._installing = True
    panel._on_step("uv_sync", "failed", "uv sync fallito (codice 1): vedi il log")
    panel._on_log("[errore] uv sync fallito (codice 1): vedi il log")
    panel._on_finished(False)
    assert step(panel, 2)[0] == "failed"
    assert "uv sync fallito" in step(panel, 2)[1]
    assert "Passo fallito" in panel.current_label.text()
    assert panel.install_box.isVisibleTo(panel)
    assert "uv sync fallito" in panel.log_text.toPlainText()
    shot(panel, "4-failed")


def test_installed_stopped_opens_directly_with_start_and_no_wizard(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path, registered=True)
    set_state(panel, detection(tmp_path, installed=True, registered=True), hub="stopped")
    assert panel.phase() == Phase.STOPPED and panel.state_label.text() == "Installato, fermo"
    assert panel.start_btn.isVisibleTo(panel) and panel.start_btn.isEnabled()
    assert panel.stop_btn.isVisibleTo(panel) and not panel.stop_btn.isEnabled()
    assert not panel.install_box.isVisibleTo(panel)               # no wizard: already installed
    assert not panel.install_btn.isVisibleTo(panel) and panel.repair_btn.isVisibleTo(panel)
    assert panel.tab_title() == "Rizzo Flow / Jev — fermo"
    assert panel.log_label.text() == "Log del server"             # installed: the log that matters
    assert "ultimo health check" in panel.details_label.text()
    shot(panel, "5-installed-stopped")


def test_start_and_stop_use_the_client_on_the_reserved_service(qapp, tmp_path):
    client = FakeClient(RizzoSettings(installDir=str(tmp_path)))
    panel = make_panel(qapp, tmp_path, client, registered=True)
    set_state(panel, detection(tmp_path, installed=True, registered=True), hub="stopped")
    panel.start_btn.click()
    assert not panel.start_btn.isEnabled()                        # busy while the call runs
    wait(panel._action_workers[0])
    assert client.calls == [("start", BUILTIN_RIZZO)]
    panel.on_hub_status({"status": "running", "builtin": True})
    assert panel.stop_btn.isEnabled() and not panel.start_btn.isEnabled()
    panel.stop_btn.click()
    wait(panel._action_workers[-1])
    assert client.calls[-1] == ("stop", BUILTIN_RIZZO)


def test_starting_running_error_and_external(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path, registered=True)
    det = detection(tmp_path, installed=True, registered=True)
    set_state(panel, det, hub="starting")
    assert panel.phase() == Phase.STARTING and panel.stop_btn.isEnabled()
    set_state(panel, det, hub="running")
    assert panel.phase() == Phase.RUNNING and panel.tab_title().endswith("in esecuzione")
    shot(panel, "6-running")
    set_state(panel, det, hub="crashed")
    assert panel.phase() == Phase.ERROR and panel.start_btn.isEnabled()
    shot(panel, "7-error")
    running_outside = detection(tmp_path, installed=False, registered=True, running=True)
    set_state(panel, running_outside, hub="stopped")
    assert panel.phase() == Phase.RUNNING_EXTERNAL and not panel.start_btn.isEnabled()


def test_stop_does_not_leave_a_stale_running_external_state(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path, registered=True)
    det = detection(tmp_path, installed=True, registered=True, running=True)
    set_state(panel, det, hub="running")
    assert panel.phase() == Phase.RUNNING
    panel.on_hub_status({"status": "stopped", "builtin": True})  # the hub stopped it
    assert panel.phase() == Phase.STOPPED                        # not "running outside the hub"


def test_hub_unreachable(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path, registered=True)
    set_state(panel, detection(tmp_path, installed=True, registered=True), hub=None, reachable=False)
    assert panel.phase() == Phase.HUB_DOWN and panel.state_label.text() == "Hub non raggiungibile"
    assert not panel.start_btn.isEnabled()


def test_installed_but_not_activated_offers_completion(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path)
    set_state(panel, detection(tmp_path, installed=True, registered=False))
    assert panel.phase() == Phase.INACTIVE and panel.install_btn.text() == "Completa installazione"
    assert not panel.start_btn.isVisibleTo(panel)


# ------------------------------------------------------------ running the steps

class FakeRunner:
    last = None

    def __init__(self, options, env, on_step, on_progress, on_log, **_):
        self.on_step, self.on_progress, self.on_log = on_step, on_progress, on_log
        self.cancelled = False
        self.force = None
        FakeRunner.last = self

    def run(self, force=frozenset()):
        self.force = force
        self.on_step("prereqs", StepState.DONE, "ok")
        self.on_step("clone_rizzo", StepState.SKIPPED, "gia' presente")
        self.on_progress("download", 500, 1000, 10.0)
        self.on_log("hello from the runner")
        self.on_step("download", StepState.FAILED, "spazio libero insufficiente")
        return False

    def cancel(self):
        self.cancelled = True


def test_install_runs_in_a_worker_and_reports_back(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path, runner_factory=FakeRunner)
    set_state(panel, detection(tmp_path))
    panel.install_btn.click()
    assert panel._installing and panel.phase() == Phase.INSTALLING
    wait(panel._setup_worker)
    assert not panel._installing
    assert step(panel, 3)[0] == "failed"
    assert "hello from the runner" in panel.log_text.toPlainText()
    assert FakeRunner.last.force == frozenset()


def test_cancel_reaches_the_runner(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path, runner_factory=FakeRunner)
    set_state(panel, detection(tmp_path))
    panel.install_btn.click()
    panel._cancel()
    assert FakeRunner.last.cancelled
    wait(panel._setup_worker)


def test_repair_forces_only_the_cheap_steps(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    panel = make_panel(qapp, tmp_path, registered=True, runner_factory=FakeRunner)
    set_state(panel, detection(tmp_path, installed=True, registered=True), hub="stopped")
    panel.repair_btn.click()
    wait(panel._setup_worker)
    assert FakeRunner.last.force == rs.REPAIR_STEPS and "download" not in FakeRunner.last.force


# ------------------------------------------------------------ settings

def test_settings_save_goes_through_the_hub_and_confirms_when_running(qapp, tmp_path, monkeypatch):
    client = FakeClient(RizzoSettings(installDir=str(tmp_path)))
    panel = make_panel(qapp, tmp_path, client, registered=True)
    set_state(panel, detection(tmp_path, installed=True, registered=True), hub="running")
    panel.settings_toggle.setChecked(True)
    assert panel.settings_box.isVisibleTo(panel)
    panel.ctx_spin.setValue(8192)
    panel.autostart_check.setChecked(True)
    shot(panel, "9-settings-open")
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes)
    panel._save_settings()
    assert "fermato" in asked[0]
    name, data = client.calls[-1]
    assert name == "set_rizzo" and data["ctx"] == 8192 and data["autostart"] is True
    assert data["installDir"] == str(tmp_path) and data["kvType"] == "q8_0"


def test_settings_before_install_are_not_persisted_yet(qapp, tmp_path, monkeypatch):
    client = FakeClient()
    panel = make_panel(qapp, tmp_path, client)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    panel._save_settings()
    assert client.calls == []


def test_save_settings_falls_back_to_config_json_when_the_hub_is_down(tmp_path, monkeypatch):
    from mcp_hub.config import load_config, save_config
    path = tmp_path / "config.json"
    monkeypatch.setattr(rp, "load_config", lambda: load_config(path))
    monkeypatch.setattr(rp, "save_config", lambda c: save_config(c, path))
    rp.save_settings(FakeClient(reachable=False), RizzoSettings(installDir=str(tmp_path), port=9100).__dict__)
    assert load_config(path).rizzo.port == 9100


def test_hub_validation_errors_are_not_swallowed(tmp_path):
    class Rejecting(FakeClient):
        def set_rizzo(self, data):
            raise RuntimeError("invalid rizzo settings: port")

    with pytest.raises(RuntimeError):
        rp.save_settings(Rejecting(), {})


# ------------------------------------------------------------ Jev section

def test_jev_section_has_copyable_config_commands_and_connection_test(qapp, tmp_path):
    panel = make_panel(qapp, tmp_path)
    set_state(panel, detection(tmp_path))
    text = panel.config_text.toPlainText()
    for needle in ("baseUrl", "http://127.0.0.1:8017/v1/systemone", "rizzo-latest", "maxStateTokens",
                   "12000", "14000", "maxQuestionsPerRequest", "64", "apiKey"):
        assert needle in text
    assert [e.text() for e in panel.cmd_edits] == [
        "/plugin marketplace add LuigiElleBalotta/fast-jev-compaction",
        "/plugin install fast-jev-compaction@fast-jev-compaction"]
    panel._copy(text)
    assert QApplication.clipboard().text() == text
    panel.port_spin.setValue(9100)
    panel._render()
    assert ":9100/v1/systemone" in panel.config_text.toPlainText()
    panel.show_connection_result(rs.ConnectionResult(True, 750.0, 0.7, "rizzo-flow-4b-q4_k_m", 4219469824))
    assert panel.test_label.text() == "OK in 750 ms, modello rizzo-flow-4b-q4_k_m, VRAM 3.9 GB"
    panel.show_connection_result(rs.ConnectionResult(False, error="nessuna risposta su :9100"))
    assert panel.test_label.text().startswith("KO: nessuna risposta")
    shot(panel, "8-jev-section")


# ------------------------------------------------------------ main window

def test_main_window_has_a_rizzo_tab_and_keeps_the_builtin_out_of_the_table(qapp, monkeypatch):
    monkeypatch.setattr(MainWindow, "_check_for_updates", lambda self: None)

    class Client(FakeClient):
        def status(self):
            return {}

    win = MainWindow(client=Client())
    win.timer.stop()
    try:
        assert [win.tabs.tabText(i).split(" — ")[0] for i in range(win.tabs.count())] == ["Server", "Rizzo Flow / Jev"]
        win._on_status_result(True, {
            "gitlab": {"status": "running", "concurrency": "parallel", "type": "mcp"},
            BUILTIN_RIZZO: {"status": "running", "concurrency": "exclusive", "type": "service",
                            "port": 8017, "builtin": True}})
        names = [win.table.item(r, 0).text() for r in range(win.table.rowCount())]
        assert names == ["gitlab"]                                # not a generic, editable row
        assert win.rizzo_panel._hub_status() == "running"
        win._on_status_result(False, None)
        assert win.rizzo_panel._hub_reachable is False
    finally:
        win.tray_icon.hide()
        win.close()
