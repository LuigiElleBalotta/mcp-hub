"""The GUI asks before updating itself: once per version, never installs on its own."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from mcp_hub import self_update
from mcp_hub.gui import main_window as mw
from mcp_hub.gui.main_window import MainWindow, skip_update_version, skipped_update_version
from mcp_hub.updater import UpdateInfo

ASSETS = {self_update.HUB_EXE_NAME: "http://x/hub.exe", self_update.GUI_EXE_NAME: "http://x/gui.exe"}


class FakeClient:
    def status(self):
        return {}

    def logs(self, name):
        return []

    def metrics(self, name):
        return None


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # the skip file lives under data_dir()
    monkeypatch.setattr(MainWindow, "_check_for_updates", lambda self: None)
    monkeypatch.setattr(self_update, "is_supported", lambda: True)
    monkeypatch.setattr(self_update, "is_frozen", lambda: True)
    monkeypatch.setattr(self_update, "brew_cask_installed", lambda: False)
    win = MainWindow(client=FakeClient())
    win.timer.stop()
    yield win
    win.tray_icon.hide()
    win.close()


@pytest.fixture
def answer(monkeypatch):
    """Replaces the modal box: records that it was shown and 'clicks' the wanted button."""
    state = {"shown": 0, "choice": "Più tardi", "text": None}

    def fake_exec(self):
        state["shown"] += 1
        state["text"] = self.text()

    def fake_clicked(self):
        return next(b for b in self.buttons() if b.text() == state["choice"])

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", fake_clicked)
    return state


def _found(window, version="9.9.9", assets=ASSETS):
    window._on_update_check_result(UpdateInfo(version, "http://x/rel", False, dict(assets)))
    QCoreApplication.processEvents()  # the dialog is scheduled with a zero-delay timer


def test_a_new_version_is_offered_and_the_default_answer_is_later(window, answer, monkeypatch):
    started = []
    monkeypatch.setattr(window, "_start_install_update", lambda: started.append(1))
    _found(window)
    assert answer["shown"] == 1 and "9.9.9" in answer["text"]
    assert started == [], "nothing is installed unless the user chooses it"


def test_installing_is_started_only_on_the_install_button(window, answer, monkeypatch):
    started = []
    monkeypatch.setattr(window, "_start_install_update", lambda: started.append(1))
    answer["choice"] = "Installa e riavvia"
    _found(window)
    assert started == [1]


def test_the_same_version_is_not_offered_twice_in_one_run(window, answer):
    _found(window)
    _found(window)
    assert answer["shown"] == 1
    _found(window, "9.9.10")
    assert answer["shown"] == 2, "a newer one is a new question"


def test_skip_this_version_is_remembered_across_runs(window, answer, qapp):
    answer["choice"] = "Salta questa versione"
    _found(window)
    assert skipped_update_version() == "9.9.9"
    window._offered_version = None  # as if the GUI had been restarted
    _found(window)
    assert answer["shown"] == 1, "a skipped version is not asked again"
    _found(window, "9.9.10")
    assert answer["shown"] == 2, "but the next version is"


def test_no_question_when_the_app_cannot_install_itself(window, answer, monkeypatch):
    monkeypatch.setattr(self_update, "is_frozen", lambda: False)  # running from source
    _found(window)
    assert answer["shown"] == 0
    assert window.update_banner.isVisible() is False or "9.9.9" in window.update_banner.text()


def test_no_question_when_the_release_lacks_an_exe(window, answer):
    _found(window, assets={self_update.HUB_EXE_NAME: "http://x/hub.exe"})
    assert answer["shown"] == 0


def test_skip_helpers_create_the_folder_and_survive_an_unwritable_one(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "fresh"))
    assert skipped_update_version() is None
    skip_update_version("1.0.0")  # the data folder does not exist yet: it is created
    assert skipped_update_version() == "1.0.0"

    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(blocker))  # a FILE where the folder should be
    skip_update_version("2.0.0")  # must not raise
    assert skipped_update_version() is None


def test_the_gui_rechecks_for_updates_periodically(window):
    assert window._update_timer.isActive() and window._update_timer.interval() == mw.UPDATE_RECHECK_MS
