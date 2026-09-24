from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from mcp_hub.config import CONFIG_PATH
from mcp_hub.gui.main_window import MainWindow


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
