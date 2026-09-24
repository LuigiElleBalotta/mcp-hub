from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout,
    QCheckBox, QLabel, QWidget, QMessageBox,
)

from mcp_hub.gui.api_client import HubApiClient


class SettingsDialog(QDialog):
    """Edits every `HubConfig` field. `host`/`port`/`authToken` are written
    straight to config.json (same direct-file-access pattern as
    `MainWindow._import_from_claude`) since the running hub can't rebind its
    already-listening socket -- a restart is required, which the dialog says
    up front. `checkForUpdates`/`includeBetaUpdates` are additionally pushed
    to the running hub via the API so they take effect immediately.
    """

    def __init__(self, client: HubApiClient, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.client = client

        from mcp_hub.config import load_config
        self._config = load_config()
        hub = self._config.hub
        self._initial_autostart = hub.autostart

        self.host_edit = QLineEdit(hub.host)
        self.port_edit = QLineEdit(str(hub.port))

        self.token_edit = QLineEdit(hub.authToken or "")
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        show_token_checkbox = QCheckBox("Show")
        show_token_checkbox.toggled.connect(
            lambda checked: self.token_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        token_row = QHBoxLayout()
        token_row.addWidget(self.token_edit)
        token_row.addWidget(show_token_checkbox)
        token_widget = QWidget()
        token_widget.setLayout(token_row)

        form = QFormLayout()
        form.addRow("Host", self.host_edit)
        form.addRow("Porta", self.port_edit)
        form.addRow("Auth token", token_widget)

        warning = QLabel("Host / porta / token: serve riavviare l'hub per applicare le modifiche.")
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #856404;")

        self.autostart_checkbox = QCheckBox("Avvia con Windows")
        self.autostart_checkbox.setChecked(hub.autostart)
        self.check_updates_checkbox = QCheckBox("Controlla aggiornamenti")
        self.check_updates_checkbox.setChecked(hub.checkForUpdates)
        self.beta_checkbox = QCheckBox("Includi versioni beta")
        self.beta_checkbox.setChecked(hub.includeBetaUpdates)

        buttons = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(ok_btn)
        buttons.addWidget(cancel_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(warning)
        layout.addWidget(self.autostart_checkbox)
        layout.addWidget(self.check_updates_checkbox)
        layout.addWidget(self.beta_checkbox)
        layout.addLayout(buttons)

    def accept(self) -> None:
        try:
            int(self.port_edit.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Settings", "Porta non valida.")
            return
        super().accept()

    def apply(self) -> None:
        """Persists every field to config.json, pushes the live-reloadable
        fields to the running hub, and re-registers the Task Scheduler
        autostart entry if that checkbox changed. Call only after `exec()`
        returned accepted."""
        from mcp_hub.config import save_config

        hub = self._config.hub
        hub.host = self.host_edit.text().strip() or hub.host
        hub.port = int(self.port_edit.text().strip())
        hub.authToken = self.token_edit.text() or None
        new_autostart = self.autostart_checkbox.isChecked()
        hub.autostart = new_autostart
        hub.checkForUpdates = self.check_updates_checkbox.isChecked()
        hub.includeBetaUpdates = self.beta_checkbox.isChecked()
        save_config(self._config)

        try:
            self.client.update_settings(
                checkForUpdates=hub.checkForUpdates,
                includeBetaUpdates=hub.includeBetaUpdates,
            )
        except Exception:
            pass  # hub unreachable right now -- config.json is still saved, picked up on next hub start

        if new_autostart != self._initial_autostart:
            import subprocess
            from pathlib import Path
            script = Path(__file__).resolve().parents[3] / "scripts" / "install_task.ps1"
            flag = "-Enable" if new_autostart else "-Disable"
            subprocess.run(["powershell", "-File", str(script), flag], check=False)
