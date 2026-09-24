from __future__ import annotations

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QHBoxLayout, QLabel,
)

from mcp_hub.gui.api_client import HubApiClient
from mcp_hub.updater import UpdateInfo, check_for_update

_STATUS_COLOR = {"running": "#2e7d32", "stopped": "#757575", "crashed": "#c62828", "starting": "#f9a825"}


class _UpdateCheckWorker(QThread):
    found = Signal(object)  # UpdateInfo | None

    def __init__(self, include_beta: bool, parent=None):
        super().__init__(parent)
        self._include_beta = include_beta

    def run(self) -> None:
        import mcp_hub
        self.found.emit(check_for_update(mcp_hub.__version__, include_beta=self._include_beta))


class MainWindow(QMainWindow):
    def __init__(self, client: HubApiClient | None = None):
        super().__init__()
        self.setWindowTitle("mcp-hub")
        self.client = client or HubApiClient()

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Server", "Status", "Concurrency", "Actions"])

        self.update_banner = QLabel()
        self.update_banner.setOpenExternalLinks(True)
        self.update_banner.setVisible(False)
        self.update_banner.setStyleSheet("background-color: #fff3cd; padding: 6px;")

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.update_banner)
        layout.addWidget(self.table)

        add_btn = QPushButton("Add server")
        add_btn.clicked.connect(self._add_server)
        layout.addWidget(add_btn)

        import_btn = QPushButton("Import from Claude Code config")
        apply_btn = QPushButton("Apply to Claude Code config")
        import_btn.clicked.connect(self._import_from_claude)
        apply_btn.clicked.connect(self._apply_to_claude)
        layout.addWidget(import_btn)
        layout.addWidget(apply_btn)

        from PySide6.QtWidgets import QCheckBox
        self.autostart_checkbox = QCheckBox("Avvia con Windows")
        self.autostart_checkbox.toggled.connect(self._toggle_autostart)
        layout.addWidget(self.autostart_checkbox)

        self.setCentralWidget(central)

        from mcp_hub.gui.log_panel import LogPanel
        self.log_panel = LogPanel()
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        layout.addWidget(self.log_panel)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

        self._update_thread: _UpdateCheckWorker | None = None
        self._check_for_updates()

    def _check_for_updates(self) -> None:
        try:
            settings = self.client.settings()
        except Exception:
            return
        if not settings.get("checkForUpdates", True):
            return
        self._update_thread = _UpdateCheckWorker(settings.get("includeBetaUpdates", False), self)
        self._update_thread.found.connect(self._on_update_check_result)
        self._update_thread.start()

    def _on_update_check_result(self, info: UpdateInfo | None) -> None:
        if info is None:
            return
        kind = "beta" if info.prerelease else "release"
        self.update_banner.setText(
            f'Nuova versione {kind} disponibile: <b>{info.version}</b> — '
            f'<a href="{info.url}">scarica</a>'
        )
        self.update_banner.setVisible(True)

    def refresh(self) -> None:
        statuses = self.client.status()
        self.table.setRowCount(len(statuses))
        for row, (name, status) in enumerate(sorted(statuses.items())):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            status_item = QTableWidgetItem(status)
            status_item.setForeground(QColor(_STATUS_COLOR.get(status, "#000000")))
            self.table.setItem(row, 1, status_item)

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            start_btn = QPushButton("Start")
            stop_btn = QPushButton("Stop")
            start_btn.clicked.connect(lambda _, n=name: self._start(n))
            stop_btn.clicked.connect(lambda _, n=name: self._stop(n))
            actions_layout.addWidget(start_btn)
            actions_layout.addWidget(stop_btn)
            self.table.setCellWidget(row, 3, actions)

    def _start(self, name: str) -> None:
        self.client.start(name)
        self.refresh()

    def _stop(self, name: str) -> None:
        self.client.stop(name)
        self.refresh()

    def _add_server(self) -> None:
        from mcp_hub.gui.server_dialog import ServerDialog
        from PySide6.QtWidgets import QMessageBox
        dialog = ServerDialog(parent=self)
        if dialog.exec():
            name = dialog.result_name()
            if not name:
                QMessageBox.warning(self, "Add server", "Name cannot be empty.")
                return
            self.client.upsert(name, dialog.result_config())
            self.refresh()

    def _toggle_autostart(self, checked: bool) -> None:
        import subprocess
        from pathlib import Path
        script = Path(__file__).resolve().parents[3] / "scripts" / "install_task.ps1"
        flag = "-Enable" if checked else "-Disable"
        subprocess.run(["powershell", "-File", str(script), flag], check=False)

    def _on_row_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        name = self.table.item(row, 0).text()
        self.log_panel.show_logs(name, self.client.logs(name))

    def _import_from_claude(self) -> None:
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getOpenFileName(self, "Select .claude.json", filter="*.json")
        if not path:
            return
        from mcp_hub.config import load_config, save_config
        from mcp_hub.claude_config import import_servers
        config = load_config()
        imported = import_servers(Path(path), config)
        save_config(config)
        QMessageBox.information(self, "Import", f"Imported (disabled): {', '.join(imported) or '(none)'}")
        self.refresh()

    def _apply_to_claude(self) -> None:
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getOpenFileName(self, "Select .claude.json", filter="*.json")
        if not path:
            return
        from mcp_hub.config import load_config
        from mcp_hub.claude_config import apply_servers
        config = load_config()
        enabled_names = [n for n, s in config.servers.items() if s.enabled]
        confirm = QMessageBox.question(
            self, "Apply",
            f"This will back up {path} and rewrite these servers to point at the hub:\n"
            + "\n".join(enabled_names)
            + "\n\nContinue?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        migrated = apply_servers(Path(path), config)
        QMessageBox.information(self, "Apply", f"Migrated: {', '.join(migrated) or '(none)'}")
