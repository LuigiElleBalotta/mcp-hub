from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout,
)

from mcp_hub.claude_config import import_servers
from mcp_hub.config import Config, load_config, save_config


class _EnableServersDialog(QDialog):
    """Lets the user pick which just-imported servers to enable immediately
    -- import_servers() always adds new entries disabled (same safety
    default the CLI's `mcp_hub import` uses), so this is where "enabled: false
    for everything" gets turned into an actual choice for first-time setup."""

    def __init__(self, names: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Abilita server")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Seleziona quali server avviare subito (puoi cambiare dopo dalla tabella):"))

        self.list_widget = QListWidget()
        for name in names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)

        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        layout.addWidget(ok_btn)

    def selected_names(self) -> list[str]:
        selected = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                selected.append(item.text())
        return selected


def run_setup_wizard(parent=None) -> Config:
    """First-run flow, shown when %LOCALAPPDATA%\\mcp-hub\\config.json
    doesn't exist yet: offers to import from an existing .claude.json
    instead of making a new user hand-edit JSON, then lets them choose which
    imported servers to enable. Always ends by writing config.json (even an
    empty one, for "start from scratch") so this doesn't re-trigger on the
    next launch.

    Writes config.json directly rather than through the hub's management
    API -- there is no hub running yet to route through (it needs this file
    to start at all). `MainWindow._import_from_claude` already does the
    same direct load_config/save_config for the same reason.
    """
    config = load_config()

    choice = QMessageBox.question(
        parent, "Benvenuto in mcp-hub",
        "Nessuna configurazione trovata.\n\n"
        "Vuoi importare i server MCP da un .claude.json esistente?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
    )
    if choice == QMessageBox.StandardButton.Yes:
        path, _ = QFileDialog.getOpenFileName(parent, "Seleziona .claude.json", filter="*.json")
        if path:
            imported = import_servers(Path(path), config)
            if imported:
                dialog = _EnableServersDialog(imported, parent)
                if dialog.exec():
                    for name in dialog.selected_names():
                        config.servers[name].enabled = True

    save_config(config)
    return config
