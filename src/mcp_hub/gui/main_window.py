from __future__ import annotations

from PySide6.QtCore import QEvent, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QHBoxLayout, QLabel, QHeaderView, QMenu, QStyle,
    QSystemTrayIcon,
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


class _InstallUpdateWorker(QThread):
    finished_ok = Signal(bool, str)  # (success, error message)

    def __init__(self, info: UpdateInfo, hub_pid: int, parent=None):
        super().__init__(parent)
        self._info = info
        self._hub_pid = hub_pid

    def run(self) -> None:
        import os
        from pathlib import Path

        from mcp_hub import self_update

        try:
            work_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "mcp-hub" / "update-staging"
            self_update.apply_update(self._info, self._hub_pid, work_dir)
            self.finished_ok.emit(True, "")
        except Exception as exc:
            self.finished_ok.emit(False, str(exc))


class MainWindow(QMainWindow):
    def __init__(self, client: HubApiClient | None = None):
        super().__init__()
        import mcp_hub
        self.setWindowTitle(f"mcp-hub v{mcp_hub.__version__}")
        self.client = client or HubApiClient()

        self.setMinimumSize(480, 320)

        self._ever_connected = False

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Server", "Status", "Concurrency", "Actions"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        self.connection_banner = QLabel("Hub non raggiungibile — nuovo tentativo tra 2s...")
        self.connection_banner.setVisible(False)
        self.connection_banner.setStyleSheet("background-color: #f8d7da; padding: 6px;")

        self.update_banner = QLabel()
        self.update_banner.setOpenExternalLinks(True)
        self.update_banner.setVisible(False)
        self.update_banner.setStyleSheet("background-color: #fff3cd; padding: 6px;")

        self.install_update_btn = QPushButton("Installa e riavvia")
        self.install_update_btn.setVisible(False)
        self.install_update_btn.clicked.connect(self._install_update)

        update_row = QHBoxLayout()
        update_row.addWidget(self.update_banner)
        update_row.addWidget(self.install_update_btn)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(update_row)
        layout.addWidget(self.connection_banner)
        layout.addWidget(self.table, 2)

        add_btn = QPushButton("Add server")
        add_btn.clicked.connect(self._add_server)
        layout.addWidget(add_btn)

        import_btn = QPushButton("Import from Claude Code config")
        apply_btn = QPushButton("Apply to Claude Code config")
        settings_btn = QPushButton("Settings")
        import_btn.clicked.connect(self._import_from_claude)
        apply_btn.clicked.connect(self._apply_to_claude)
        settings_btn.clicked.connect(self._open_settings)
        layout.addWidget(import_btn)
        layout.addWidget(apply_btn)
        layout.addWidget(settings_btn)

        self.setCentralWidget(central)

        from mcp_hub.gui.log_panel import LogPanel
        self.log_panel = LogPanel()
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        layout.addWidget(self.log_panel, 1)

        version_label = QLabel(f"mcp-hub v{mcp_hub.__version__}")
        version_label.setStyleSheet("color: #888; padding: 2px 4px;")
        layout.addWidget(version_label)

        self._init_tray()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

        self._update_thread: _UpdateCheckWorker | None = None
        self._install_thread: _InstallUpdateWorker | None = None
        self._pending_update: UpdateInfo | None = None
        self._check_for_updates()

    def _init_tray(self) -> None:
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip(self.windowTitle())

        menu = QMenu()
        open_action = menu.addAction("Apri")
        open_action.triggered.connect(self._restore_from_tray)
        quit_action = menu.addAction("Esci")
        quit_action.triggered.connect(self._quit_from_tray)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        from PySide6.QtWidgets import QApplication
        self.tray_icon.hide()
        QApplication.quit()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                self.windowTitle(), "mcp-hub è ancora attivo nel system tray.",
                QSystemTrayIcon.MessageIcon.Information, 2000,
            )
            return
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        if self.tray_icon.isVisible():
            event.ignore()
            self.hide()
        else:
            super().closeEvent(event)

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
        self._pending_update = info
        kind = "beta" if info.prerelease else "release"
        self.update_banner.setText(
            f'Nuova versione {kind} disponibile: <b>{info.version}</b> — '
            f'<a href="{info.url}">scarica</a>'
        )
        self.update_banner.setVisible(True)

        from mcp_hub import self_update
        can_auto_install = (
            self_update.is_frozen()
            and self_update.HUB_EXE_NAME in info.assets
            and self_update.GUI_EXE_NAME in info.assets
        )
        self.install_update_btn.setVisible(can_auto_install)

    def _install_update(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        if self._pending_update is None:
            return
        confirm = QMessageBox.question(
            self, "Installa aggiornamento",
            f"Scarica e installa {self._pending_update.version}.\n\n"
            "L'hub verrà riavviato: le sessioni Claude Code che lo usano ora "
            "perderanno la connessione per qualche secondo, poi tornano "
            "operative con la nuova versione.\n\nContinuare?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            hub_pid = self.client.pid()
        except Exception as exc:
            QMessageBox.warning(self, "Installa aggiornamento", f"Impossibile contattare l'hub: {exc}")
            return

        self.install_update_btn.setEnabled(False)
        self.install_update_btn.setText("Download in corso...")
        self._install_thread = _InstallUpdateWorker(self._pending_update, hub_pid, self)
        self._install_thread.finished_ok.connect(self._on_install_result)
        self._install_thread.start()

    def _on_install_result(self, success: bool, error: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        if not success:
            QMessageBox.warning(self, "Installa aggiornamento", f"Aggiornamento fallito: {error}")
            self.install_update_btn.setEnabled(True)
            self.install_update_btn.setText("Installa e riavvia")
            return
        # The detached helper is now waiting for the hub's process and this
        # GUI's own process to exit before it replaces the exes and
        # relaunches both -- shut the hub down gracefully, then close
        # ourselves so its file lock is released too.
        try:
            self.client.shutdown()
        except Exception:
            pass
        self.close()

    def refresh(self) -> None:
        try:
            statuses = self.client.status()
        except Exception:
            # Hub unreachable -- could be the first-run wizard's freshly
            # spawned hub still starting up, or a transient blip on an
            # already-running hub. Only wipe the table if we've never had a
            # successful status yet; otherwise keep showing the last known
            # state instead of flashing it empty every failed 2s tick, and
            # surface a banner so the user knows why nothing is updating.
            self.connection_banner.setVisible(True)
            if not self._ever_connected:
                self.table.setRowCount(0)
            return
        self._ever_connected = True
        self.connection_banner.setVisible(False)
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
            edit_btn = QPushButton("Edit")
            remove_btn = QPushButton("Remove")
            start_btn.clicked.connect(lambda _, n=name: self._start(n))
            stop_btn.clicked.connect(lambda _, n=name: self._stop(n))
            edit_btn.clicked.connect(lambda _, n=name: self._edit(n))
            remove_btn.clicked.connect(lambda _, n=name: self._remove(n))
            actions_layout.addWidget(start_btn)
            actions_layout.addWidget(stop_btn)
            actions_layout.addWidget(edit_btn)
            actions_layout.addWidget(remove_btn)
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

    def _edit(self, name: str) -> None:
        from dataclasses import asdict
        from mcp_hub.config import load_config
        from mcp_hub.gui.server_dialog import ServerDialog
        from PySide6.QtWidgets import QMessageBox

        config = load_config()
        existing_sc = config.servers.get(name)
        dialog = ServerDialog(name, existing=asdict(existing_sc) if existing_sc else None, parent=self)
        if dialog.exec():
            new_name = dialog.result_name()
            if not new_name:
                QMessageBox.warning(self, "Edit server", "Name cannot be empty.")
                return
            self.client.upsert(new_name, dialog.result_config())
            self.refresh()

    def _remove(self, name: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        confirm = QMessageBox.question(
            self, "Remove server",
            f"Rimuovere '{name}'? Il processo verrà fermato.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.client.remove(name)
        self.refresh()

    def _open_settings(self) -> None:
        from mcp_hub.gui.settings_dialog import SettingsDialog
        dialog = SettingsDialog(client=self.client, parent=self)
        if dialog.exec():
            dialog.apply()

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
