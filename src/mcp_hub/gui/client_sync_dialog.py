"""Sync the hub's endpoints into AI clients' config files (hub-gui-and-clients R3).

Preview first (a diff with secrets masked), then apply (backup + atomic write),
and a restore button for the newest backup. Planning and writing are quick local
file operations, so they run on the GUI thread."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)

from mcp_hub.client_sync import (
    ADAPTERS, ClientAdapter, Plan, apply_plan, backups, detect_clients, plan_sync, rollback, sync_names,
)
from mcp_hub.config import Config


@dataclass
class Target:
    adapter: ClientAdapter
    path: Path


class ClientSyncDialog(QDialog):
    def __init__(self, hub_config: Config | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sincronizza i client")
        self.resize(760, 560)
        if hub_config is None:
            from mcp_hub.config import load_config
            hub_config = load_config()
        self.config = hub_config
        self.names = sync_names(hub_config)
        self.targets: list[Target] = []
        self._plans: list[Plan] = []

        intro = QLabel(
            f"Scrive nei client gli endpoint dell'hub per {len(self.names)} server "
            f"({', '.join(self.names) or 'nessuno abilitato'}). Le altre voci dei file non vengono toccate; "
            "prima di scrivere si fa un backup, e l'anteprima nasconde i segreti.")
        intro.setWordWrap(True)
        self.list = QListWidget()
        self.list.setMaximumHeight(130)
        self.add_btn = QPushButton("Aggiungi file di progetto...")
        self.add_btn.clicked.connect(self._add_project)
        self.preview_btn = QPushButton("Anteprima")
        self.preview_btn.clicked.connect(self.preview)
        self.apply_btn = QPushButton("Applica")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply)
        self.restore_btn = QPushButton("Ripristina ultimo backup")
        self.restore_btn.clicked.connect(self.restore)
        close_btn = QPushButton("Chiudi")
        close_btn.clicked.connect(self.accept)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("L'anteprima delle modifiche appare qui")
        buttons = QHBoxLayout()
        for b in (self.add_btn, self.preview_btn, self.apply_btn, self.restore_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.list)
        layout.addLayout(buttons)
        layout.addWidget(self.output, 1)

        for found in detect_clients():
            self.add_target(found.adapter, found.path, checked=True)
        if not self.targets:
            self.output.setPlainText(
                "Nessun client trovato in questo sistema. Aggiungi un file di progetto "
                "(.mcp.json, .cursor/mcp.json, .vscode/mcp.json).")

    # ------------------------------------------------------------------- targets

    def add_target(self, adapter: ClientAdapter, path: Path, checked: bool = True) -> None:
        self.targets.append(Target(adapter, path))
        note = "esiste" if path.exists() else "verra' creato"
        item = QListWidgetItem(f"{adapter.label}  -  {path}  ({note})")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.list.addItem(item)
        self._plans = []
        self.apply_btn.setEnabled(False)

    def _add_project(self) -> None:
        labels = [a.label for a in ADAPTERS.values()]
        chosen, ok = QInputDialog.getItem(self, "Client", "Per quale client?", labels, 0, False)
        if not ok:
            return
        folder = QFileDialog.getExistingDirectory(self, "Cartella del progetto")
        if not folder:
            return
        adapter = next(a for a in ADAPTERS.values() if a.label == chosen)
        self.add_target(adapter, adapter.project_path(Path(folder)))

    def _checked(self) -> list[Target]:
        return [t for i, t in enumerate(self.targets) if self.list.item(i).checkState() == Qt.CheckState.Checked]

    # --------------------------------------------------------------- preview / apply

    def preview(self) -> None:
        """Plans every checked target (reads only) and shows what would change."""
        self._plans = [plan_sync(t.adapter, t.path, self.config, self.names) for t in self._checked()]
        if not self._plans:
            self.output.setPlainText("Seleziona almeno un file.")
            self.apply_btn.setEnabled(False)
            return
        blocks = []
        for plan in self._plans:
            block = [f"== {plan.summary()}", f"   {plan.path}"]
            if plan.error:
                block.append("   (non verra' scritto)")
            elif plan.changed:
                block.append(plan.diff())
            else:
                block.append("   niente da cambiare")
            blocks.append("\n".join(block))
        self.output.setPlainText("\n\n".join(blocks))
        self.apply_btn.setEnabled(any(p.changed for p in self._plans))

    def apply(self) -> None:
        todo = [p for p in self._plans if p.changed]
        if not todo:
            return
        answer = QMessageBox.question(
            self, "Applica",
            "Scrivo queste modifiche (con un backup di ogni file esistente)?\n\n"
            + "\n".join(f"- {p.adapter.label}: {p.path}" for p in todo))
        if answer != QMessageBox.StandardButton.Yes:
            return
        report = []
        for plan in todo:
            try:
                backup = apply_plan(plan)
            except (OSError, ValueError) as exc:
                report.append(f"ERRORE {plan.adapter.label}: {exc}")
                continue
            report.append(f"Scritto {plan.path}" + (f"  (backup: {backup.name})" if backup else "  (file nuovo)"))
        self._plans = []
        self.apply_btn.setEnabled(False)
        self.output.setPlainText("\n".join(report))

    def restore(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            self.output.setPlainText("Seleziona il file da ripristinare nella lista.")
            return
        target = self.targets[row]
        available = backups(target.path)
        if not available:
            self.output.setPlainText(f"Nessun backup di {target.path}.")
            return
        answer = QMessageBox.question(
            self, "Ripristina",
            f"Ripristino {target.path} da {available[0].name}?\n"
            "Lo stato attuale viene salvato a sua volta in un backup.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            restored = rollback(target.path)
        except (OSError, ValueError) as exc:
            self.output.setPlainText(f"ERRORE: {exc}")
            return
        self._plans = []
        self.apply_btn.setEnabled(False)
        self.output.setPlainText(f"Ripristinato {target.path} da {restored.name}.")
