from __future__ import annotations

import secrets
import sys

from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout,
    QCheckBox, QLabel, QWidget, QMessageBox, QComboBox, QSpinBox,
)

from mcp_hub.gui.api_client import HubApiClient


def _split_list(text: str) -> list[str]:
    """'a, b,,c' -> ['a', 'b', 'c']"""
    return [part.strip() for part in text.split(",") if part.strip()]


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
        generate_btn = QPushButton("Genera")
        generate_btn.setToolTip("Crea un token casuale. Dopo aver riavviato l'hub, riesegui 'apply' per aggiornare i client.")

        def _generate_token() -> None:
            self.token_edit.setText(secrets.token_urlsafe(32))
            show_token_checkbox.setChecked(True)  # visible, so it can be copied

        generate_btn.clicked.connect(_generate_token)
        self.generate_token_btn = generate_btn
        token_row = QHBoxLayout()
        token_row.addWidget(self.token_edit)
        token_row.addWidget(show_token_checkbox)
        token_row.addWidget(generate_btn)
        token_widget = QWidget()
        token_widget.setLayout(token_row)

        form = QFormLayout()
        form.addRow("Host", self.host_edit)
        form.addRow("Porta", self.port_edit)
        form.addRow("Auth token", token_widget)

        self.allowed_hosts_edit = QLineEdit(", ".join(hub.allowedHosts))
        self.allowed_hosts_edit.setPlaceholderText("nomi host extra separati da virgola (es. hub.interno)")
        self.allowed_origins_edit = QLineEdit(", ".join(hub.allowedOrigins))
        self.allowed_origins_edit.setPlaceholderText("origin extra separati da virgola (es. https://tool.interno)")
        form.addRow("Host consentiti", self.allowed_hosts_edit)
        form.addRow("Origin consentiti", self.allowed_origins_edit)

        self.transport_combo = QComboBox()
        self.transport_combo.addItem("SSE (come sempre)", "sse")
        self.transport_combo.addItem("Streamable HTTP", "http")
        self.transport_combo.setCurrentIndex(1 if hub.preferredTransport == "http" else 0)
        self.transport_combo.setToolTip("Quale endpoint 'apply' scrive nella config dei client: /<server>/sse o /<server>/mcp.")
        self.drain_spin = QSpinBox()
        self.drain_spin.setRange(0, 3600)
        self.drain_spin.setSuffix(" s")
        self.drain_spin.setSpecialValueText("subito")
        self.drain_spin.setValue(int(hub.drainTimeout))
        self.drain_spin.setToolTip("Alla chiusura (o all'aggiornamento) l'hub aspetta tanto le richieste in corso prima di uscire.")
        self.metrics_checkbox = QCheckBox("Raccogli le metriche di utilizzo (colonne e scheda Dettaglio)")
        self.metrics_checkbox.setChecked(hub.metrics)
        self.metrics_checkbox.setToolTip(
            "Conteggi, latenza e chiamate recenti per server: solo il nome del metodo (e del tool) e la durata, "
            "mai argomenti o risultati. Serve riavviare l'hub.")
        from mcp_hub import secrets as _secrets
        store = _secrets.backend_name()
        self.secret_store_label = QLabel(
            f"Credential store per i segreti: {store}" if store
            else "Credential store per i segreti: non disponibile (le variabili restano in config.json)")
        self.secret_store_label.setStyleSheet("color: #666;")
        self.registry_checkbox = QCheckBox("Abilita la ricerca nel registry MCP")
        self.registry_checkbox.setChecked(hub.registrySearch)
        self.registry_checkbox.setToolTip(
            "Aggiunge 'Cerca nel registry MCP...' al dialog Add server. Una ricerca invia il testo cercato a "
            "registry.modelcontextprotocol.io (nient'altro). Spento di default.")
        form.addRow("Trasporto per 'apply'", self.transport_combo)
        form.addRow("Attesa alla chiusura", self.drain_spin)

        warning = QLabel(
            "Host / porta / token / host e origin consentiti: serve riavviare l'hub per applicare le modifiche. "
            "Cambiato il token, riesegui 'apply' perche' i client lo ricevano."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #856404;")

        self.autostart_checkbox = QCheckBox("Avvia con Windows")
        self.autostart_checkbox.setChecked(hub.autostart)
        if sys.platform != "win32":
            # The autostart entry is a Windows Task Scheduler task (install_task.ps1).
            self.autostart_checkbox.setText("Avvia all'accesso")
            self.autostart_checkbox.setEnabled(False)
            self.autostart_checkbox.setToolTip("Non ancora disponibile su questo sistema: avvia mcp-hub a mano o aggiungilo agli elementi di login.")
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
        layout.addWidget(self.metrics_checkbox)
        layout.addWidget(self.registry_checkbox)
        layout.addWidget(self.secret_store_label)
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
        hub.allowedHosts = _split_list(self.allowed_hosts_edit.text())
        hub.allowedOrigins = _split_list(self.allowed_origins_edit.text())
        hub.preferredTransport = self.transport_combo.currentData()
        hub.drainTimeout = float(self.drain_spin.value())
        hub.registrySearch = self.registry_checkbox.isChecked()
        hub.metrics = self.metrics_checkbox.isChecked()
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

        if new_autostart != self._initial_autostart and sys.platform == "win32":
            import subprocess
            from pathlib import Path
            script = Path(__file__).resolve().parents[3] / "scripts" / "install_task.ps1"
            flag = "-Enable" if new_autostart else "-Disable"
            subprocess.run(["powershell", "-File", str(script), flag], check=False)
