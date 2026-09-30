# tests/test_hub_control.py
"""Tray "Esci" must stop the background hub and everything it started."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
import sys
import time

import psutil
import pytest

from mcp_hub.gui.hub_control import shutdown_hub

# A parent that spawns a grandchild, then both sleep: stands in for the hub
# and a managed server (Rizzo) it started.
_TREE = (
    "import subprocess,sys,time;"
    "subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
    "time.sleep(120)"
)


@pytest.fixture
def tree():
    parent = subprocess.Popen([sys.executable, "-c", _TREE])
    deadline = time.time() + 10
    while time.time() < deadline and not psutil.Process(parent.pid).children():
        time.sleep(0.05)
    procs = [psutil.Process(parent.pid), *psutil.Process(parent.pid).children(recursive=True)]
    yield parent, procs
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass


class FakeClient:
    def __init__(self, pid, on_shutdown=None):
        self._pid = pid
        self._on_shutdown = on_shutdown
        self.shutdown_calls = 0

    def pid(self):
        return self._pid

    def shutdown(self):
        self.shutdown_calls += 1
        if self._on_shutdown:
            self._on_shutdown()


def test_kills_hub_and_children_when_graceful_shutdown_does_nothing(tree):
    parent, procs = tree
    assert len(procs) >= 2
    client = FakeClient(parent.pid)  # hub ignores /api/shutdown
    assert shutdown_hub(client, wait=0.5) is True
    assert client.shutdown_calls == 1
    assert not any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in procs)


def test_graceful_shutdown_leaves_nothing_to_kill(tree):
    parent, procs = tree
    client = FakeClient(parent.pid, on_shutdown=lambda: [p.terminate() for p in procs])
    assert shutdown_hub(client, wait=10) is True
    assert not any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in procs)


def test_orphaned_child_is_killed_even_if_hub_exits_alone(tree):
    parent, procs = tree
    client = FakeClient(parent.pid, on_shutdown=lambda: procs[0].kill())  # only the hub dies
    assert shutdown_hub(client, wait=0.5) is True
    assert not procs[1].is_running()


def test_unreachable_hub_is_treated_as_stopped():
    class Down:
        def pid(self):
            raise ConnectionError("hub down")

    assert shutdown_hub(Down()) is True


def test_tray_quit_stops_hub_then_quits(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from mcp_hub.gui import hub_control
    from mcp_hub.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    calls = []
    monkeypatch.setattr(hub_control, "shutdown_hub", lambda client, wait=30.0: calls.append(client) or True)
    quit_called = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quit_called.append(True)))

    class Client:
        def status(self):
            return {}

        def logs(self, name):
            return []

    monkeypatch.setattr(MainWindow, "_check_for_updates", lambda self: None)
    client = Client()
    window = MainWindow(client=client)
    window.timer.stop()
    try:
        window._quit_from_tray()
        window._quit_thread.wait(5000)
        app.processEvents()
        assert calls == [client]
        assert quit_called == [True]
    finally:
        window.tray_icon.hide()
        window.close()
