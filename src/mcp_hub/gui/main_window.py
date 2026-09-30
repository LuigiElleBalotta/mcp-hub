from __future__ import annotations

from PySide6.QtCore import QEvent, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QHBoxLayout, QLabel, QHeaderView, QMenu, QStyle,
    QSystemTrayIcon, QTabWidget,
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


class _QuitWorker(QThread):
    """Stops the hub process tree for the tray's "Esci"."""

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self._client = client

    def run(self) -> None:
        from mcp_hub.gui.hub_control import shutdown_hub
        shutdown_hub(self._client)


class _StatusWorker(QThread):
    """Runs `client.status()` off the Qt main thread.

    `HubApiClient.status()` is a synchronous httpx call; calling it directly
    from `MainWindow.refresh()` (a 2s `QTimer` callback running ON the main
    thread) blocks the entire GUI -- repaints, clicks, drags, everything --
    for the call's full duration every single tick. That's mostly invisible
    when the hub answers in a few ms, but when the hub is unreachable
    (nothing listening on the port), Windows can take several seconds per
    connection attempt to give up (no immediate RST), so the window sits
    frozen for most of every 2s cycle -- exactly the "GUI molto lenta"
    symptom, confirmed live: a GUI launched with no hub running spent most
    of its time blocked in this call. httpx.Client is documented safe for
    concurrent use across threads, so sharing `client` with the main
    thread's own direct calls (start/stop/upsert/remove, all short-lived
    and user-initiated) is fine.
    """
    done = Signal(bool, object, object)  # (ok, statuses dict | None, (name, log lines) | None)

    def __init__(self, client: HubApiClient, log_name: str | None = None, parent=None):
        super().__init__(parent)
        self._client = client
        self._log_name = log_name

    def run(self) -> None:
        try:
            statuses = self._client.status()
        except Exception:
            self.done.emit(False, None, None)
            return
        # The selected server's log is refreshed on every tick too (a
        # service's startup output is the whole point of looking at it),
        # off the main thread like the status call itself.
        logs = None
        if self._log_name is not None and self._log_name in statuses:
            try:
                logs = (self._log_name, self._client.logs(self._log_name))
            except Exception:
                logs = None
        self.done.emit(True, statuses, logs)


class _ActionWorker(QThread):
    """Runs one start/stop call off the GUI thread: stopping a service kills
    its process tree and waits for the port to be released, which can take
    several seconds and must not freeze the window."""
    finished_ok = Signal(str, bool, str)  # (name, success, error message)

    def __init__(self, action, name: str, parent=None):
        super().__init__(parent)
        self._action = action
        self._name = name

    def run(self) -> None:
        try:
            self._action(self._name)
        except Exception as exc:
            self.finished_ok.emit(self._name, False, str(exc))
        else:
            self.finished_ok.emit(self._name, True, "")


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
            staging = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "mcp-hub" / self_update.STAGING_DIR_NAME
            work_dir = self_update.fresh_staging_dir(staging, self._info.version)
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
        self._last_hub_launch_attempt = 0.0
        self._status_worker: _StatusWorker | None = None

        self._action_workers: list[_ActionWorker] = []
        self._busy: set[str] = set()  # servers with a start/stop call in flight

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Server", "Type", "Status", "Details", "Actions"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        # Interactive, width set by `_fit_actions_column`: neither
        # ResizeToContents (ignores cell widgets) nor Fixed (uses the default
        # section size) honour the button row, which then overflowed
        # leftwards and covered the Status/Details text.
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self._actions_width = 0

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
        outer = QVBoxLayout(central)
        outer.addLayout(update_row)
        outer.addWidget(self.connection_banner)
        # Two first-class tabs: the generic MCP/service table, and the built-in
        # Rizzo Flow / Jev panel (which owns the `rizzo-flow` service: it is
        # not a row of the generic table).
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        servers_page = QWidget()
        layout = QVBoxLayout(servers_page)
        layout.addWidget(self.table, 2)

        add_btn = QPushButton("Add server")
        add_btn.clicked.connect(self._add_server)
        layout.addWidget(add_btn)

        import_btn = QPushButton("Import from Claude Code config")
        apply_btn = QPushButton("Apply to Claude Code config")
        reload_btn = QPushButton("Reload config.json")
        settings_btn = QPushButton("Settings")
        import_btn.clicked.connect(self._import_from_claude)
        apply_btn.clicked.connect(self._apply_to_claude)
        reload_btn.clicked.connect(self._reload_config)
        settings_btn.clicked.connect(self._open_settings)
        layout.addWidget(import_btn)
        layout.addWidget(apply_btn)
        layout.addWidget(reload_btn)
        layout.addWidget(settings_btn)

        self.setCentralWidget(central)

        from mcp_hub.gui.log_panel import LogPanel
        self.log_panel = LogPanel()
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        layout.addWidget(self.log_panel, 1)

        version_label = QLabel(f"mcp-hub v{mcp_hub.__version__}")
        version_label.setStyleSheet("color: #888; padding: 2px 4px;")
        layout.addWidget(version_label)

        self.tabs.addTab(servers_page, "Server")
        from mcp_hub.gui.rizzo_panel import RizzoPanel
        self.rizzo_panel = RizzoPanel(self.client)
        self.tabs.addTab(self.rizzo_panel, self.rizzo_panel.tab_title())
        self.rizzo_panel.state_changed.connect(lambda text: self.tabs.setTabText(1, text))
        self.tabs.currentChanged.connect(
            lambda i: self.rizzo_panel.refresh_detection() if i == 1 else None)

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
        # "Esci" must end the background hub too (and every server it
        # manages), not just this window: stop it off the UI thread -- a
        # graceful stop can take several seconds -- then quit.
        if getattr(self, "_quit_thread", None) is not None:
            return
        self.tray_icon.showMessage(
            self.windowTitle(), "Chiusura dell'hub in corso...",
            QSystemTrayIcon.MessageIcon.Information, 2000,
        )
        self._quit_thread = _QuitWorker(self.client, self)
        self._quit_thread.finished.connect(self._finish_quit)
        self._quit_thread.start()

    def _finish_quit(self) -> None:
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
        # Reads config.json directly rather than through the hub API: this
        # must still work when the hub is unreachable (arguably the most
        # important time to tell the user a newer build exists, if that's
        # why the hub isn't answering) -- `checkForUpdates`/
        # `includeBetaUpdates` don't need the running hub's in-memory state,
        # just what's on disk, same as `SettingsDialog` already reads.
        from mcp_hub.config import load_config
        hub_config = load_config().hub
        if not hub_config.checkForUpdates:
            return
        self._update_thread = _UpdateCheckWorker(hub_config.includeBetaUpdates, self)
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
        # relaunches both -- shut the hub down gracefully, then quit
        # ourselves so its file lock is released too. Must be a REAL quit
        # (QApplication.quit()), not self.close(): closeEvent() now hides to
        # tray instead of exiting (R3), so a plain self.close() here would
        # leave this process alive-but-hidden, the helper's `Wait-Process
        # -Id $GuiPid` would sit out its full 30s timeout, and the file
        # replace would then either fail (exe still locked) or race a still
        # -running old GUI against the freshly relaunched new one.
        from PySide6.QtWidgets import QApplication
        try:
            self.client.shutdown()
        except Exception:
            pass
        self.tray_icon.hide()
        QApplication.quit()

    def refresh(self) -> None:
        # Runs the actual HTTP call in `_StatusWorker` (see its docstring)
        # instead of blocking this 2s QTimer callback -- and thus the whole
        # GUI thread -- on `client.status()` directly. Skips spawning a new
        # worker if one from a previous tick is still in flight (a slow/
        # hung hub must not pile up overlapping requests).
        if self._status_worker is not None and self._status_worker.isRunning():
            return
        self._status_worker = _StatusWorker(self.client, self._selected_name(), self)
        self._status_worker.done.connect(self._on_status_result)
        self._status_worker.start()

    def _fit_actions_column(self) -> None:
        if self._actions_width:
            self.table.setColumnWidth(4, self._actions_width)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_actions_column()

    def _selected_name(self) -> str | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.text() if item is not None else None

    def _on_status_result(self, ok: bool, statuses: dict | None, logs: object = None) -> None:
        if not ok:
            # Hub unreachable -- could be the first-run wizard's freshly
            # spawned hub still starting up, or a transient blip on an
            # already-running hub. Only wipe the table if we've never had a
            # successful status yet; otherwise keep showing the last known
            # state instead of flashing it empty every failed 2s tick, and
            # surface a banner so the user knows why nothing is updating.
            self.connection_banner.setText(
                "Hub non raggiungibile — nuovo tentativo automatico in corso..."
            )
            self.connection_banner.setVisible(True)
            self.rizzo_panel.on_hub_status(None, reachable=False)
            if not self._ever_connected:
                self.table.setRowCount(0)
                self._maybe_launch_hub()
            return
        self._ever_connected = True
        self.connection_banner.setVisible(False)
        # The built-in Rizzo Flow service belongs to its own tab, not to this table.
        builtin = {n: i for n, i in statuses.items() if i.get("builtin")}
        self.rizzo_panel.on_hub_status(next(iter(builtin.values()), None), reachable=True)
        statuses = {n: i for n, i in statuses.items() if n not in builtin}
        selected = self._selected_name()
        self.table.setRowCount(len(statuses))
        for row, (name, info) in enumerate(sorted(statuses.items())):
            status = info["status"]
            is_service = info.get("type", "mcp") == "service"
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem("Service" if is_service else "MCP"))
            status_item = QTableWidgetItem(status)
            status_item.setForeground(QColor(_STATUS_COLOR.get(status, "#000000")))
            self.table.setItem(row, 2, status_item)
            # A service has no concurrency mode and no SSE endpoint: show its
            # port instead. MCP servers keep showing their concurrency.
            if is_service:
                port = info.get("port")
                details = f"HTTP :{port}" if port is not None else "process"
            else:
                details = info["concurrency"]
            self.table.setItem(row, 3, QTableWidgetItem(details))

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            start_btn = QPushButton("Start")
            stop_btn = QPushButton("Stop")
            busy = name in self._busy
            start_btn.setEnabled(not busy and status in ("stopped", "crashed"))
            stop_btn.setEnabled(not busy and status in ("starting", "running"))
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
            self.table.setCellWidget(row, 4, actions)
            buttons = (start_btn, stop_btn, edit_btn, remove_btn)
            self._actions_width = max(
                self._actions_width,
                sum(b.sizeHint().width() for b in buttons) + actions_layout.spacing() * (len(buttons) - 1) + 8,
            )
        self._fit_actions_column()
        # The header relayouts itself once more after the first populate and
        # resets this column, so apply the width again shortly after.
        QTimer.singleShot(100, self._fit_actions_column)
        # The table was repopulated: restore the selection (setRowCount +
        # sorted rows can shift it) and refresh the selected server's log.
        if selected is not None:
            self.table.blockSignals(True)  # no sync logs fetch from _on_row_selected
            try:
                for row in range(self.table.rowCount()):
                    if self.table.item(row, 0).text() == selected:
                        self.table.setCurrentCell(row, 0)
                        break
            finally:
                self.table.blockSignals(False)
        if logs is not None:
            name, lines = logs
            if name == self._selected_name():
                self.log_panel.show_logs(name, lines)

    def _maybe_launch_hub(self) -> None:
        """Best-effort attempt to start the hub ourselves when it's never
        answered at all in this GUI session -- covers every case past the
        first run that `app.py`'s setup-wizard-only `_launch_hub()` call
        doesn't: autostart disabled/failed, the hub crashed before this GUI
        connected even once, or the user just launched the GUI without ever
        starting the hub. Throttled to once per 10s (`time.monotonic()`) so
        the 2s poll timer can't spawn a new hub process on every tick; never
        fires again once `_ever_connected` is True (a hub that goes down
        AFTER we've talked to it once -- e.g. mid self-update restart -- is
        expected to come back on its own, not have us race it with a
        second hub trying to bind the same port)."""
        import time
        now = time.monotonic()
        if now - self._last_hub_launch_attempt < 10:
            return
        self._last_hub_launch_attempt = now
        from mcp_hub.gui.app import _launch_hub
        _launch_hub()

    def _start(self, name: str) -> None:
        self._run_action(self.client.start, name)

    def _stop(self, name: str) -> None:
        self._run_action(self.client.stop, name)

    def _run_action(self, action, name: str) -> None:
        if name in self._busy:
            return
        self._busy.add(name)
        worker = _ActionWorker(action, name, self)
        worker.finished_ok.connect(self._on_action_result)
        self._action_workers.append(worker)
        worker.start()
        self.refresh()

    def _on_action_result(self, name: str, success: bool, error: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        self._busy.discard(name)
        self._action_workers = [w for w in self._action_workers if w.isRunning()]
        if not success:
            QMessageBox.warning(self, name, f"Operazione fallita: {error}")
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
        from dataclasses import asdict
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getOpenFileName(self, "Select .claude.json", filter="*.json")
        if not path:
            return
        from mcp_hub.config import load_config, save_config
        from mcp_hub.claude_config import import_servers
        config = load_config()
        imported = import_servers(Path(path), config)
        # Writing config.json alone isn't enough for the table to show
        # these: it reflects the ALREADY-RUNNING hub's in-memory
        # HubManager, which never re-reads config.json on its own -- an
        # import that only touched disk stayed invisible until the next hub
        # restart. Push each new (disabled) entry through the same upsert
        # path Add/Edit use, so the live hub picks it up immediately;
        # upsert's own handler already persists to config.json server-side,
        # so only fall back to writing it here ourselves if the hub can't be
        # reached at all (still get it on disk for the next hub start).
        hub_unreachable = False
        for name in imported:
            try:
                self.client.upsert(name, asdict(config.servers[name]))
            except Exception:
                hub_unreachable = True
        if hub_unreachable:
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
        # Services are not MCP servers: `apply` never touches them.
        enabled_names = [n for n, s in config.servers.items() if s.enabled and not s.is_service]
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

    def _reload_config(self) -> None:
        """Picks up config.json changes the running hub never saw happen --
        a hand edit, or a script writing the file directly -- without a hub
        restart. See `HubManager.reload_from_disk`/`POST /api/reload`."""
        from PySide6.QtWidgets import QMessageBox
        try:
            result = self.client.reload()
        except Exception as exc:
            QMessageBox.warning(self, "Reload config.json", f"Impossibile contattare l'hub: {exc}")
            return
        added, updated, removed = result["added"], result["updated"], result["removed"]
        if not (added or updated or removed):
            QMessageBox.information(self, "Reload config.json", "Nessuna modifica trovata.")
        else:
            QMessageBox.information(
                self, "Reload config.json",
                f"Aggiunti: {', '.join(added) or '(nessuno)'}\n"
                f"Aggiornati: {', '.join(updated) or '(nessuno)'}\n"
                f"Rimossi: {', '.join(removed) or '(nessuno)'}",
            )
        self.refresh()
