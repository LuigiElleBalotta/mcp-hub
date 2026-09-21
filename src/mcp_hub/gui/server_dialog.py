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
        form.addRow("Name", self.name_edit)
        form.addRow("Command", self.command_edit)
        form.addRow("Args (space-separated)", self.args_edit)
        form.addRow("Concurrency", self.concurrency_combo)

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
        return {
            "command": self.command_edit.text(),
            "args": self.args_edit.text().split(),
            "env": env,
            "concurrency": self.concurrency_combo.currentText(),
            "enabled": True,
        }
