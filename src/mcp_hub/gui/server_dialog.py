from __future__ import annotations

import re

from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QComboBox, QPushButton, QVBoxLayout,
    QHBoxLayout, QTableWidget, QTableWidgetItem, QCheckBox, QWidget,
)

_SECRET_KEY_RE = re.compile(r"TOKEN|SECRET|PASS|KEY|AUTH", re.IGNORECASE)


class ServerDialog(QDialog):
    def __init__(self, name: str = "", existing: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Server")
        existing = existing or {}

        self.name_edit = QLineEdit(name)
        self.command_edit = QLineEdit(existing.get("command", ""))
        self.args_edit = QLineEdit(" ".join(existing.get("args", [])))
        self.concurrency_combo = QComboBox()
        self.concurrency_combo.addItems(["exclusive", "parallel"])
        self.concurrency_combo.setCurrentText(existing.get("concurrency", "exclusive"))

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

        form = QFormLayout()
        self._form = form
        form.addRow("Type", self.type_combo)
        form.addRow("Name", self.name_edit)
        form.addRow("Command", self.command_edit)
        form.addRow("Args (space-separated)", self.args_edit)
        form.addRow("Concurrency", self.concurrency_combo)
        form.addRow("Working directory", self.cwd_edit)
        form.addRow("Health URL", self.health_url_edit)
        form.addRow("Port", self.port_edit)
        form.addRow("", self.autostart_check)
        self._mcp_only_widgets = [self.concurrency_combo]
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
