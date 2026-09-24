from __future__ import annotations

import sys

from PySide6.QtCore import QSharedMemory
from PySide6.QtWidgets import QApplication, QMessageBox

from mcp_hub.config import CONFIG_PATH
from mcp_hub.gui.main_window import MainWindow

# A per-user, cross-process named lock (QSharedMemory's OS-backed segment
# survives independent of any one process, unlike a plain in-process flag)
# so a second `mcp-hub-gui.exe` launch doesn't spawn a duplicate instance.
# This became a real problem once minimize/close went to the tray (R3):
# with no visible taskbar window to remind the user one is already running,
# double-clicking the exe again (or Start Menu) silently opened another
# copy, each polling the hub independently -- confirmed live, 4 stacked
# instances found in one session. `QSharedMemory.attach()` succeeding means
# a live instance already holds the segment (Windows releases it itself
# when that process exits, no cleanup needed here).
_SINGLETON_KEY = "mcp-hub-gui-singleton-lock"


def _launch_hub() -> None:
    """Starts the hub detached, the same way a user running the exe for the
    first time would otherwise have to remember to do themselves (README's
    "Installing the exe" step 4). Best-effort: if this fails, the GUI still
    opens -- `MainWindow.refresh()` just keeps retrying every 2s until
    something (this, Task Scheduler autostart, or the user manually) gets
    the hub up."""
    import subprocess
    from mcp_hub import self_update

    try:
        if self_update.is_frozen():
            self_update.launch_detached([str(self_update.hub_exe_path()), "serve"])
        else:
            self_update.launch_detached([sys.executable, "-m", "mcp_hub", "serve"])
    except (OSError, subprocess.SubprocessError):
        pass


def main() -> None:
    app = QApplication(sys.argv)

    lock = QSharedMemory(_SINGLETON_KEY)
    if lock.attach():
        QMessageBox.information(
            None, "mcp-hub",
            "mcp-hub è già in esecuzione. Controlla il system tray.",
        )
        sys.exit(0)
    lock.create(1)
    app._mcp_hub_singleton_lock = lock  # keep it alive for the app's lifetime

    if not CONFIG_PATH.exists():
        from mcp_hub.gui.setup_wizard import run_setup_wizard
        run_setup_wizard()
        _launch_hub()

    window = MainWindow()
    window.resize(700, 400)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
