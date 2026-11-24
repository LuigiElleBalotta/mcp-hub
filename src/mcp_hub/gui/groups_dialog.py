"""Manage groups of servers (hub-gui-and-clients R5): create, rename-by-recreate,
delete, and pick the members of each. Changes are sent to the hub, which saves
them in config.json. A group is only a named set: no endpoint stands behind it."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from mcp_hub.config import valid_group_name


class GroupsDialog(QDialog):
    def __init__(self, client, servers: list[str], groups: dict[str, list[str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Gruppi di server")
        self.resize(560, 380)
        self.client = client
        self.servers = sorted(servers)
        self.groups = {name: list(members) for name, members in groups.items()}
        self._original = {name: list(members) for name, members in groups.items()}
        self.changed = False

        hint = QLabel("Un gruppo e' un insieme con un nome: serve a filtrare l'elenco, ad avviare e fermare "
                      "i server insieme e a scrivere solo quel gruppo in un client (apply --group).")
        hint.setWordWrap(True)
        self.group_list = QListWidget()
        self.group_list.currentItemChanged.connect(self._show_members)
        self.member_list = QListWidget()
        self.member_list.itemChanged.connect(self._members_changed)
        self.new_btn = QPushButton("Nuovo gruppo...")
        self.new_btn.clicked.connect(self._new_group)
        self.delete_btn = QPushButton("Elimina")
        self.delete_btn.clicked.connect(self._delete_group)
        self.save_btn = QPushButton("Salva")
        self.save_btn.clicked.connect(self.save)
        close_btn = QPushButton("Chiudi")
        close_btn.clicked.connect(self.reject)
        left = QVBoxLayout()
        left.addWidget(QLabel("Gruppi"))
        left.addWidget(self.group_list, 1)
        left.addWidget(self.new_btn)
        left.addWidget(self.delete_btn)
        right = QVBoxLayout()
        right.addWidget(QLabel("Server del gruppo"))
        right.addWidget(self.member_list, 1)
        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addLayout(right, 2)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.save_btn)
        bottom.addWidget(close_btn)
        root = QVBoxLayout(self)
        root.addWidget(hint)
        root.addLayout(body, 1)
        root.addLayout(bottom)
        self._fill_groups()
        self.status = QLabel("")
        root.addWidget(self.status)

    def _fill_groups(self, select: str | None = None) -> None:
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for name in self.groups:
            self.group_list.addItem(name)
        self.group_list.blockSignals(False)
        names = list(self.groups)
        if names:
            self.group_list.setCurrentRow(names.index(select) if select in names else 0)
        else:
            self.member_list.clear()
        self.delete_btn.setEnabled(bool(names))

    def _current(self) -> str | None:
        item = self.group_list.currentItem()
        return item.text() if item else None

    def _show_members(self, *_args) -> None:
        name = self._current()
        self.member_list.blockSignals(True)
        self.member_list.clear()
        if name is not None:
            members = set(self.groups[name])
            for server in self.servers:
                item = QListWidgetItem(server)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if server in members else Qt.CheckState.Unchecked)
                self.member_list.addItem(item)
        self.member_list.blockSignals(False)

    def _members_changed(self, _item) -> None:
        name = self._current()
        if name is None:
            return
        self.groups[name] = [self.member_list.item(i).text() for i in range(self.member_list.count())
                             if self.member_list.item(i).checkState() == Qt.CheckState.Checked]

    def _new_group(self) -> None:
        name, ok = QInputDialog.getText(self, "Nuovo gruppo", "Nome del gruppo:")
        self.add_group(name.strip()) if ok else None

    def add_group(self, name: str) -> bool:
        if not valid_group_name(name):
            QMessageBox.warning(self, "Gruppi", "Nome non valido: da 1 a 40 caratteri tra lettere, cifre, spazio . _ -")
            return False
        if name in self.groups:
            QMessageBox.warning(self, "Gruppi", f"Il gruppo '{name}' esiste gia'.")
            return False
        self.groups[name] = []
        self._fill_groups(select=name)
        return True

    def _delete_group(self) -> None:
        name = self._current()
        if name is None:
            return
        if QMessageBox.question(self, "Elimina", f"Eliminare il gruppo '{name}'? I server restano.") \
                == QMessageBox.StandardButton.Yes:
            del self.groups[name]
            self._fill_groups()

    def save(self) -> bool:
        """Sends the differences to the hub. Returns whether everything was saved."""
        try:
            for name in [n for n in self._original if n not in self.groups]:
                self.client.delete_group(name)
            for name, members in self.groups.items():
                if self._original.get(name) != members:
                    self.client.put_group(name, members)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.status.setText(f"Non salvato: {exc}")
            return False
        self._original = {name: list(members) for name, members in self.groups.items()}
        self.changed = True
        self.status.setText("Salvato.")
        return True
