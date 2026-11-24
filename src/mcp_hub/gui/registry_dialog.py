"""Search the MCP Registry and pick a server to pre-fill the Add-server form
(hub-gui-and-clients R6). It never runs anything: the choice only fills the form,
whose full command line the user reads and saves themselves."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from mcp_hub.gui.workers import ApiWorker
from mcp_hub.registry import Draft, Option, RegistryClient, SearchResult


class RegistryDialog(QDialog):
    def __init__(self, client: RegistryClient | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cerca nel registry MCP")
        self.resize(720, 560)
        if client is None:
            from mcp_hub.paths import data_dir
            client = RegistryClient(data_dir() / "cache" / "registry")
        self.client = client
        self.drafts: list[Draft] = []
        self._cursor: str | None = None
        self._query = ""
        self._workers: list[ApiWorker] = []
        self.chosen: dict | None = None

        privacy = QLabel("La ricerca invia il testo cercato a registry.modelcontextprotocol.io (nient'altro). "
                         "Le voci del registry le scrive chiunque: controlla il comando prima di salvarlo.")
        privacy.setWordWrap(True)
        privacy.setStyleSheet("color: #856404;")
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("Cerca un server (almeno 3 caratteri), es. github")
        self.query_edit.returnPressed.connect(self.search)
        self.search_btn = QPushButton("Cerca")
        self.search_btn.clicked.connect(self.search)
        self.more_btn = QPushButton("Altri risultati")
        self.more_btn.setEnabled(False)
        self.more_btn.clicked.connect(self.load_more)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.results = QListWidget()
        self.results.currentRowChanged.connect(self._show_draft)
        self.option_combo = QComboBox()
        self.option_combo.currentIndexChanged.connect(self._show_option)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.use_btn = QPushButton("Usa nel form")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self.use)
        cancel = QPushButton("Annulla")
        cancel.clicked.connect(self.reject)

        top = QHBoxLayout()
        top.addWidget(self.query_edit, 1)
        top.addWidget(self.search_btn)
        bottom = QHBoxLayout()
        bottom.addWidget(self.more_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.use_btn)
        bottom.addWidget(cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(privacy)
        layout.addLayout(top)
        layout.addWidget(self.status)
        layout.addWidget(self.results, 2)
        layout.addWidget(QLabel("Come avviarlo"))
        layout.addWidget(self.option_combo)
        layout.addWidget(self.details, 2)
        layout.addLayout(bottom)

    # ------------------------------------------------------------------ searching

    def _run(self, fn, done) -> None:
        worker = ApiWorker(fn, self)
        self._workers.append(worker)

        def finished(result, error):
            if worker in self._workers:
                self._workers.remove(worker)
            done(result, error)

        worker.done.connect(finished)
        worker.start()

    def search(self) -> None:
        query = self.query_edit.text().strip()
        self._query = query
        self._cursor = None
        self.drafts = []
        self.results.clear()
        self._busy(True)
        self._run(lambda: self.client.search(query), lambda r, e: self._got(query, r, e, append=False))

    def load_more(self) -> None:
        if not self._cursor:
            return
        query, cursor = self._query, self._cursor
        self._busy(True)
        self._run(lambda: self.client.search(query, cursor=cursor), lambda r, e: self._got(query, r, e, append=True))

    def _busy(self, busy: bool) -> None:
        self.search_btn.setEnabled(not busy)
        self.more_btn.setEnabled(False)
        if busy:
            self.status.setText("Cerco...")

    def _got(self, query: str, result: SearchResult | None, error: str | None, append: bool) -> None:
        self.search_btn.setEnabled(True)
        if query != self._query:
            return  # the user searched for something else meanwhile
        if error or result is None:
            self.status.setText(f"Ricerca non riuscita: {error}")
            return
        first_new = len(self.drafts)
        self.drafts.extend(result.drafts)
        for draft in result.drafts:
            self.results.addItem(f"{draft.suggested_name}  -  {draft.description[:90]}")
        self._cursor = result.next_cursor
        self.more_btn.setEnabled(bool(self._cursor))
        note = " (dalla cache: il registry non risponde)" if result.stale else ""
        self.status.setText((f"{len(self.drafts)} risultati{note}." if self.drafts else "Nessun risultato.")
                            + (" Ce ne sono altri." if self._cursor else ""))
        if self.drafts and (not append or self.results.currentRow() < 0):
            self.results.setCurrentRow(first_new if append else 0)

    # ------------------------------------------------------------------- details

    def _current_draft(self) -> Draft | None:
        row = self.results.currentRow()
        return self.drafts[row] if 0 <= row < len(self.drafts) else None

    def _show_draft(self, _row: int) -> None:
        draft = self._current_draft()
        self.option_combo.blockSignals(True)
        self.option_combo.clear()
        if draft is not None:
            for option in draft.options:
                self.option_combo.addItem(option.label + ("" if option.installable else "   (non installabile qui)"))
            first = next((i for i, o in enumerate(draft.options) if o.installable), 0)
            if draft.options:
                self.option_combo.setCurrentIndex(first)
        self.option_combo.blockSignals(False)
        self._show_option()

    def _current_option(self) -> Option | None:
        draft = self._current_draft()
        index = self.option_combo.currentIndex()
        return draft.options[index] if draft is not None and 0 <= index < len(draft.options) else None

    def _show_option(self, *_args) -> None:
        draft, option = self._current_draft(), self._current_option()
        self.use_btn.setEnabled(bool(option and option.installable))
        if draft is None:
            self.details.setPlainText("")
            return
        lines = [f"{draft.title}  (versione {draft.version or '?'})", draft.registry_name, "", draft.description, ""]
        if option is None:
            lines.append("Questa voce non ha pacchetti.")
        elif not option.installable:
            lines.append(f"Non installabile qui: {option.reason}")
        else:
            lines.append("Comando che verra' inserito nel form:")
            lines.append("  " + option.command_line())
            if option.env:
                lines.append("")
                lines.append("Variabili d'ambiente:")
                for var in option.env:
                    flags = ", ".join(f for f, on in (("segreto", var.secret), ("obbligatoria", var.required)) if on)
                    lines.append(f"  {var.name}" + (f"  [{flags}]" if flags else "")
                                 + (f" - {var.description}" if var.description else ""))
            if option.needs:
                lines.append("")
                lines.append("DA COMPLETARE a mano (il registry non li precompila): " + ", ".join(option.needs))
        self.details.setPlainText("\n".join(lines))

    def use(self) -> None:
        draft, option = self._current_draft(), self._current_option()
        if draft is None or option is None or not option.installable:
            return
        self.chosen = {
            "name": draft.suggested_name,
            "command": option.command,
            "args": list(option.args),
            "env": {var.name: var.value for var in option.env},
            "needs": list(option.needs),
        }
        self.accept()
