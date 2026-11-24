from __future__ import annotations

import re

from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QComboBox, QPushButton, QVBoxLayout,
    QHBoxLayout, QTableWidget, QTableWidgetItem, QCheckBox, QWidget, QSpinBox, QLabel,
)

_SECRET_KEY_RE = re.compile(r"TOKEN|SECRET|PASS|KEY|AUTH", re.IGNORECASE)

# One line each, shown under the concurrency combo (hub-proxy-efficiency R3/R8).
MODE_HELP = {
    "exclusive": "Una richiesta alla volta (predefinito): per server che non reggono chiamate sovrapposte.",
    "parallel": "Un solo processo condiviso, richieste in parallelo: per server senza stato.",
    "session-aware": "Come parallel, ma dice al server quale sessione fa ogni richiesta (params._meta).",
    "isolated": "Un processo separato per ogni sessione, chiuso quando la sessione finisce.",
}


def split_list(text: str) -> list[str]:
    """'a, b,,c' -> ['a', 'b', 'c']"""
    return [part.strip() for part in text.split(",") if part.strip()]


class ServerDialog(QDialog):
    def __init__(self, name: str = "", existing: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Server")
        existing = existing or {}

        self.name_edit = QLineEdit(name)
        self.command_edit = QLineEdit(existing.get("command", ""))
        self.args_edit = QLineEdit(" ".join(existing.get("args", [])))
        self.concurrency_combo = QComboBox()
        self.concurrency_combo.addItems(list(MODE_HELP))
        self.concurrency_combo.setCurrentText(existing.get("concurrency", "exclusive"))
        self.mode_help = QLabel()
        self.mode_help.setWordWrap(True)
        self.mode_help.setStyleSheet("color: #666;")
        self.concurrency_combo.currentTextChanged.connect(
            lambda mode: self.mode_help.setText(MODE_HELP.get(mode, ""))
        )
        self.mode_help.setText(MODE_HELP.get(self.concurrency_combo.currentText(), ""))

        # Start/stop policy and tool filters (hub-proxy-efficiency R2/R6): MCP only.
        self.start_mode_combo = QComboBox()
        self.start_mode_combo.addItem("All'avvio dell'hub", "eager")
        self.start_mode_combo.addItem("Alla prima richiesta (lazy)", "lazy")
        self.start_mode_combo.setCurrentIndex(1 if existing.get("startMode") == "lazy" else 0)
        self.idle_timeout_spin = QSpinBox()
        self.idle_timeout_spin.setRange(0, 86400)
        self.idle_timeout_spin.setSuffix(" s")
        self.idle_timeout_spin.setSpecialValueText("mai")
        self.idle_timeout_spin.setValue(int(existing.get("idleTimeout", 0)))
        self.idle_timeout_spin.setToolTip("Ferma il server dopo questo tempo senza richieste; riparte da solo alla prossima.")
        self.persistent_check = QCheckBox("Non fermarlo mai per inattivita' (tiene stato in memoria)")
        self.persistent_check.setChecked(bool(existing.get("persistent", False)))
        self.tools_allow_edit = QLineEdit(", ".join(existing.get("toolsAllow", [])))
        self.tools_allow_edit.setPlaceholderText("vuoto = tutti; es. read_*, list_*")
        self.tools_deny_edit = QLineEdit(", ".join(existing.get("toolsDeny", [])))
        self.tools_deny_edit.setPlaceholderText("nascosti anche se consentiti; es. *delete*")
        self.tool_prefix_edit = QLineEdit(existing.get("toolPrefix", ""))
        self.tool_prefix_edit.setMaxLength(64)
        self.tool_prefix_edit.setPlaceholderText("es. gl_  (lettere, cifre, _ - .)")

        # MCP-only supervision (hub-proxy-hardening R5/R6). `restartWindow` and
        # `healthFailures` are not edited here; an existing value is preserved.
        self.restart_combo = QComboBox()
        self.restart_combo.addItem("Riavvia se si ferma", "on-failure")
        self.restart_combo.addItem("Mai", "never")
        self.restart_combo.setCurrentIndex(1 if existing.get("restart") == "never" else 0)
        self.restart_attempts_spin = QSpinBox()
        self.restart_attempts_spin.setRange(1, 100)
        self.restart_attempts_spin.setValue(int(existing.get("restartMaxAttempts", 5)))
        self.restart_attempts_spin.setToolTip("Tentativi massimi nella finestra; poi resta in crashed finche' non lo avvii a mano.")
        self.health_interval_spin = QSpinBox()
        self.health_interval_spin.setRange(0, 3600)
        self.health_interval_spin.setSuffix(" s")
        self.health_interval_spin.setSpecialValueText("disattivato")
        self.health_interval_spin.setValue(int(existing.get("healthInterval", 30)))
        self.health_interval_spin.setToolTip("Ping periodico: un server vivo ma che non risponde viene riavviato.")

        # Service-only fields (type "service": a plain long-running process
        # such as a local HTTP server -- not an MCP server, never proxied).
        self._existing = existing
        self.type_combo = QComboBox()
        self.type_combo.addItem("MCP server (stdio, proxied via SSE)", "mcp")
        self.type_combo.addItem("Service (plain process, start/stop only)", "service")
        self.type_combo.setCurrentIndex(1 if existing.get("type") == "service" else 0)
        self.cwd_edit = QLineEdit(existing.get("cwd") or "")
        self.health_url_edit = QLineEdit(existing.get("healthUrl") or "")
        self.health_url_edit.setPlaceholderText("http://127.0.0.1:8017/health")
        self.port_edit = QLineEdit("" if existing.get("port") is None else str(existing["port"]))
        self.port_edit.setPlaceholderText("from health URL")
        self.autostart_check = QCheckBox("Start automatically with the hub")
        self.autostart_check.setChecked(bool(existing.get("autostart", False)))

        self.env_table = QTableWidget(0, 3)
        self.env_table.setHorizontalHeaderLabels(["Key", "Value", "Show"])
        # Re-apply masking whenever a key cell's text changes, so a row
        # added blank (via "Add env var") masks its value as soon as the
        # user types a key matching the secret pattern -- not just for
        # rows that already had a matching key when the row was created.
        self.env_table.itemChanged.connect(self._on_key_item_changed)
        for key, value in existing.get("env", {}).items():
            self._add_env_row(key, value)

        add_env_btn = QPushButton("Add env var")
        add_env_btn.clicked.connect(lambda: self._add_env_row("", ""))
        # Secrets in the OS credential store instead of config.json (R4). Hidden when the
        # system has none: plaintext env keeps working either way.
        self.move_secrets_btn = QPushButton("Sposta i segreti nel credential store")
        self.move_secrets_btn.setToolTip(
            "Salva i valori delle variabili che sembrano segreti (TOKEN, KEY, PASS...) nel credential "
            "del sistema e lascia in config.json solo un riferimento ${secret:server/CHIAVE}.")
        self.move_secrets_btn.clicked.connect(self._move_secrets)
        from mcp_hub import secrets as _secrets
        self.move_secrets_btn.setVisible(_secrets.available())

        form = QFormLayout()
        self._form = form
        form.addRow("Type", self.type_combo)
        form.addRow("Name", self.name_edit)
        form.addRow("Command", self.command_edit)
        form.addRow("Args (space-separated)", self.args_edit)
        form.addRow("Concurrency", self.concurrency_combo)
        form.addRow("", self.mode_help)
        form.addRow("Avvio", self.start_mode_combo)
        form.addRow("Ferma dopo inattivita'", self.idle_timeout_spin)
        form.addRow("", self.persistent_check)
        form.addRow("Tool consentiti", self.tools_allow_edit)
        form.addRow("Tool nascosti", self.tools_deny_edit)
        form.addRow("Prefisso tool", self.tool_prefix_edit)
        form.addRow("Riavvio automatico", self.restart_combo)
        form.addRow("Max riavvii", self.restart_attempts_spin)
        form.addRow("Ping di salute ogni", self.health_interval_spin)
        form.addRow("Working directory", self.cwd_edit)
        form.addRow("Health URL", self.health_url_edit)
        form.addRow("Port", self.port_edit)
        form.addRow("", self.autostart_check)
        self._mcp_only_widgets = [
            self.concurrency_combo, self.mode_help, self.start_mode_combo, self.idle_timeout_spin,
            self.persistent_check, self.tools_allow_edit, self.tools_deny_edit, self.tool_prefix_edit,
            self.restart_combo, self.restart_attempts_spin, self.health_interval_spin,
        ]
        self._service_only_widgets = [self.cwd_edit, self.health_url_edit, self.port_edit, self.autostart_check]
        self.type_combo.currentIndexChanged.connect(self._apply_type)
        self._apply_type()

        buttons = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(ok_btn)
        buttons.addWidget(cancel_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.env_table)
        layout.addWidget(add_env_btn)
        layout.addWidget(self.move_secrets_btn)
        layout.addLayout(buttons)

    def is_service(self) -> bool:
        return self.type_combo.currentData() == "service"

    def _apply_type(self) -> None:
        """Shows only the fields that mean something for the chosen type."""
        service = self.is_service()
        for widget in self._mcp_only_widgets:
            self._form.setRowVisible(widget, not service)
        for widget in self._service_only_widgets:
            self._form.setRowVisible(widget, service)

    def _add_env_row(self, key: str, value: str) -> None:
        row = self.env_table.rowCount()
        self.env_table.insertRow(row)
        self.env_table.setItem(row, 0, QTableWidgetItem(key))
        value_edit = QLineEdit(value)
        self.env_table.setCellWidget(row, 1, value_edit)

        show_checkbox = QCheckBox()
        show_checkbox.toggled.connect(lambda _checked, r=row: self._refresh_row_masking(r))
        self.env_table.setCellWidget(row, 2, show_checkbox)

        self._refresh_row_masking(row)

    def _on_key_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._refresh_row_masking(item.row())

    def _refresh_row_masking(self, row: int) -> None:
        """Mask the value field when its key matches the secret pattern,
        unless the row's "Show" checkbox is checked. Called on row creation,
        on every key-text edit, and on every checkbox toggle, so masking
        state always reflects the current key text -- not just whatever key
        the row happened to have when it was first added."""
        key_item = self.env_table.item(row, 0)
        value_edit = self.env_table.cellWidget(row, 1)
        show_checkbox = self.env_table.cellWidget(row, 2)
        if key_item is None or value_edit is None:
            return
        is_secret = bool(_SECRET_KEY_RE.search(key_item.text()))
        show = show_checkbox.isChecked() if show_checkbox is not None else False
        if is_secret and not show:
            value_edit.setEchoMode(QLineEdit.EchoMode.Password)
        else:
            value_edit.setEchoMode(QLineEdit.EchoMode.Normal)

    def _move_secrets(self) -> None:
        """Stores every plaintext secret-looking env value in the credential store and
        puts its `${secret:...}` reference in the field. Nothing is saved to config.json
        until the dialog is accepted."""
        from PySide6.QtWidgets import QMessageBox

        from mcp_hub import secrets

        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Segreti", "Dai prima un nome al server: i segreti sono salvati per nome.")
            return
        moved = 0
        for row in range(self.env_table.rowCount()):
            key_item = self.env_table.item(row, 0)
            value_widget = self.env_table.cellWidget(row, 1)
            if key_item is None or value_widget is None:
                continue
            key, value = key_item.text(), value_widget.text()
            if not key or not value or secrets.parse_ref(value) or not secrets.is_secret_key(key):
                continue
            try:
                secrets.set_secret(name, key, value)
            except secrets.SecretError as exc:
                QMessageBox.warning(self, "Segreti", f"Non riesco a salvare {key}: {exc}")
                return
            value_widget.setText(secrets.make_ref(name, key))
            moved += 1
        QMessageBox.information(
            self, "Segreti",
            f"{moved} segreti spostati nel credential store." if moved else "Nessun segreto in chiaro da spostare.")

    def result_name(self) -> str:
        return self.name_edit.text().strip()

    def result_config(self) -> dict:
        env = {}
        for row in range(self.env_table.rowCount()):
            key = self.env_table.item(row, 0).text()
            value_widget = self.env_table.cellWidget(row, 1)
            if key:
                env[key] = value_widget.text()
        config = {
            "command": self.command_edit.text(),
            "args": self.args_edit.text().split(),
            "env": env,
            "concurrency": self.concurrency_combo.currentText(),
            "enabled": True,
        }
        if not self.is_service():
            config.update({
                "restart": self.restart_combo.currentData(),
                "restartMaxAttempts": self.restart_attempts_spin.value(),
                "healthInterval": float(self.health_interval_spin.value()),
                "startMode": self.start_mode_combo.currentData(),
                "idleTimeout": float(self.idle_timeout_spin.value()),
                "persistent": self.persistent_check.isChecked(),
                "toolsAllow": split_list(self.tools_allow_edit.text()),
                "toolsDeny": split_list(self.tools_deny_edit.text()),
                "toolPrefix": self.tool_prefix_edit.text().strip(),
            })
            for key in ("restartWindow", "healthFailures"):
                if key in self._existing:
                    config[key] = self._existing[key]
            return config
        port_text = self.port_edit.text().strip()
        config.update({
            "type": "service",
            "cwd": self.cwd_edit.text().strip() or None,
            "healthUrl": self.health_url_edit.text().strip() or None,
            "port": int(port_text) if port_text.isdigit() else None,
            "healthTimeout": self._existing.get("healthTimeout", 120.0),
            "autostart": self.autostart_check.isChecked(),
        })
        return config
