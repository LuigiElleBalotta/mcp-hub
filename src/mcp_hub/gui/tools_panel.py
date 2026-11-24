"""Tools tab (hub-gui-and-clients R2): the selected server's tools with a checkbox
each (which ones clients may see), and a tester that calls one from the GUI.

Every network call runs off the GUI thread. Loading a tool list of a lazy or idle
server STARTS it, so the list is only fetched when the tab is open (or on
"Aggiorna"), never as a side effect of selecting a row."""
from __future__ import annotations

import json

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from mcp_hub.filters import edit_filters


class _ApiWorker(QThread):
    done = Signal(object, object)  # (result, error text | None)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn(), None)
        except Exception as exc:  # noqa: BLE001 - shown to the user as text
            self.done.emit(None, str(exc) or exc.__class__.__name__)


class ArgsForm(QWidget):
    """Arguments of one tool, as a form built from its JSON Schema (flat
    properties: string, integer, number, boolean, enum; anything else is typed
    as JSON). A schema with no properties falls back to a JSON text box."""

    def __init__(self, schema: dict | None, parent=None):
        super().__init__(parent)
        props = schema.get("properties") if isinstance(schema, dict) else None
        required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
        self._fields: dict[str, tuple[str, QWidget, bool]] = {}
        self._json: QPlainTextEdit | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if not isinstance(props, dict) or not props:
            self._json = QPlainTextEdit("{}")
            self._json.setPlaceholderText("Argomenti come oggetto JSON")
            self._json.setMaximumHeight(90)
            layout.addWidget(QLabel("Argomenti (JSON)"))
            layout.addWidget(self._json)
            return
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        for name, spec in props.items():
            spec = spec if isinstance(spec, dict) else {}
            kind = spec.get("type") if isinstance(spec.get("type"), str) else "json"
            if isinstance(spec.get("enum"), list) and spec["enum"]:
                widget: QWidget = QComboBox()
                if name not in required:
                    widget.addItem("", None)
                for option in spec["enum"]:
                    widget.addItem(str(option), option)
                kind = "enum"
            elif kind == "boolean":
                widget = QCheckBox()
            else:
                widget = QLineEdit()
                if kind not in ("string", "integer", "number"):
                    kind = "json"
                    widget.setPlaceholderText("JSON")
            if spec.get("description"):
                widget.setToolTip(str(spec["description"]))
            self._fields[name] = (kind, widget, name in required)
            form.addRow(f"{name}{' *' if name in required else ''}", widget)
        layout.addLayout(form)

    def arguments(self) -> dict:
        """The arguments typed in; raises ValueError (readable) when they are not valid."""
        if self._json is not None:
            try:
                value = json.loads(self._json.toPlainText() or "{}")
            except ValueError as exc:
                raise ValueError(f"JSON non valido: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError("Gli argomenti devono essere un oggetto JSON")
            return value
        out: dict = {}
        for name, (kind, widget, required) in self._fields.items():
            if kind == "boolean":
                if required or widget.isChecked():
                    out[name] = widget.isChecked()
            elif kind == "enum":
                value = widget.currentData()
                if value is None and required:
                    raise ValueError(f"'{name}' e' obbligatorio")
                if value is not None:
                    out[name] = value
            else:
                text = widget.text().strip()
                if not text:
                    if required:
                        raise ValueError(f"'{name}' e' obbligatorio")
                    continue
                try:
                    out[name] = (int(text) if kind == "integer" else float(text) if kind == "number"
                                 else text if kind == "string" else json.loads(text))
                except ValueError as exc:
                    raise ValueError(f"'{name}': valore non valido per il tipo {kind}") from exc
        return out


class ToolsPanel(QWidget):
    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client
        self._server: str | None = None
        self._loaded_for: str | None = None
        self._tools: list[dict] = []
        self._payload: dict = {}
        self._baseline: set[str] = set()
        self._workers: list[_ApiWorker] = []

        self.title = QLabel("Tool: nessun server selezionato")
        self.title.setStyleSheet("font-weight: bold;")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Cerca tool...")
        self.search.textChanged.connect(self._apply_search)
        self.refresh_btn = QPushButton("Aggiorna")
        self.refresh_btn.clicked.connect(self.reload)
        self.apply_btn = QPushButton("Applica filtri")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setToolTip("Le spunte decidono quali tool i client vedono. Nessun riavvio: le sessioni restano.")
        self.apply_btn.clicked.connect(self.apply_filters)
        self.list = QListWidget()
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.currentItemChanged.connect(self._on_selected)

        self.form_holder = QVBoxLayout()
        self.form: ArgsForm | None = None
        self.run_btn = QPushButton("Esegui")
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self.run_tool)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setPlaceholderText("Il risultato della chiamata appare qui")

        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(self.refresh_btn)
        left = QVBoxLayout()
        left.addWidget(self.search)
        left.addWidget(self.list, 1)
        left.addWidget(self.apply_btn)
        right = QVBoxLayout()
        right.addWidget(QLabel("Prova il tool selezionato"))
        right.addLayout(self.form_holder)
        right.addWidget(self.run_btn)
        right.addWidget(self.result, 1)
        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addLayout(right, 2)
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.addLayout(top)
        root.addWidget(self.status)
        root.addLayout(body, 1)

    # ------------------------------------------------------------ selection / loading

    def set_server(self, name: str | None, load: bool = False, supported: bool = True) -> None:
        """Points the panel at a server. `load=True` fetches its tools now."""
        if name != self._server:
            self._server = name
            self._loaded_for = None
            self._tools = []
            self.list.clear()
            self._show_form(None)
            self.result.setPlainText("")
            self.apply_btn.setEnabled(False)
        self.title.setText(f"Tool: {name}" if name else "Tool: nessun server selezionato")
        if name is None:
            self.status.setText("")
        elif not supported:
            self.status.setText("Solo i server MCP hanno dei tool.")
            self.refresh_btn.setEnabled(False)
            return
        self.refresh_btn.setEnabled(name is not None)
        if load:
            self.ensure_loaded()

    def ensure_loaded(self) -> None:
        if self._server is not None and self._loaded_for != self._server and self.refresh_btn.isEnabled():
            self.reload()

    def reload(self) -> None:
        name = self._server
        if name is None:
            return
        self.status.setText("Carico i tool (un server fermo viene avviato)...")
        self._run(lambda: self.client.tools(name), lambda result, error: self._filled(name, result, error))

    def _run(self, fn, done) -> None:
        worker = _ApiWorker(fn, self)
        self._workers.append(worker)

        def finished(result, error):
            if worker in self._workers:
                self._workers.remove(worker)
            done(result, error)

        worker.done.connect(finished)
        worker.start()

    def _filled(self, name: str, result: dict | None, error: str | None) -> None:
        if name != self._server:
            return  # the selection moved on while this was loading
        if error or result is None:
            self.status.setText(f"Impossibile leggere i tool: {error}")
            return
        self._loaded_for = name
        self._tools = sorted(result.get("tools", []), key=lambda t: t["name"])
        self._payload = {k: result.get(k) for k in ("toolsAllow", "toolsDeny", "toolPrefix")}
        self.list.blockSignals(True)
        self.list.clear()
        for tool in self._tools:
            label = tool["clientName"] + ("   [sola lettura]" if tool.get("readOnly") else "")
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, tool["name"])
            item.setToolTip(tool.get("description") or tool["name"])
            if tool.get("lockedByPattern"):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setToolTip("Nascosto da un pattern in 'Tool nascosti' (es. *x*): modificalo dal dialog del server.")
                item.setForeground(Qt.GlobalColor.gray)
            else:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if tool.get("visible") else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._baseline = self._visible_names()
        self.apply_btn.setEnabled(False)
        hidden = sum(1 for t in self._tools if not t.get("visible"))
        self.status.setText(
            f"{len(self._tools)} tool ({result.get('source')}); {hidden} nascosti ai client."
            if self._tools else "Il server non espone tool.")
        self._apply_search(self.search.text())

    def _apply_search(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    # ------------------------------------------------------------------- filters

    def _visible_names(self) -> set[str]:
        return {self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked}

    def _on_item_changed(self, _item) -> None:
        self.apply_btn.setEnabled(self._visible_names() != self._baseline)

    def apply_filters(self) -> None:
        name = self._server
        if name is None:
            return
        names = [t["name"] for t in self._tools]
        allow, deny, locked = edit_filters(
            names, self._visible_names(), self._payload.get("toolsAllow") or [], self._payload.get("toolsDeny") or [])
        self.apply_btn.setEnabled(False)
        self.status.setText("Applico i filtri...")

        def done(result, error):
            if error:
                self.status.setText(f"Filtri non applicati: {error}")
                return
            self.reload()
            if locked:
                self.status.setText("Applicato, ma restano nascosti da un pattern: " + ", ".join(locked))

        self._run(lambda: self.client.set_filters(name, allow, deny), done)

    # -------------------------------------------------------------------- tester

    def _selected_tool(self) -> dict | None:
        item = self.list.currentItem()
        if item is None:
            return None
        raw = item.data(Qt.ItemDataRole.UserRole)
        return next((t for t in self._tools if t["name"] == raw), None)

    def _show_form(self, tool: dict | None) -> None:
        if self.form is not None:
            self.form.setParent(None)
            self.form.deleteLater()
            self.form = None
        if tool is not None:
            self.form = ArgsForm(tool.get("inputSchema"))
            self.form_holder.addWidget(self.form)
        self.run_btn.setEnabled(tool is not None)

    def _on_selected(self, *_args) -> None:
        self._show_form(self._selected_tool())

    def run_tool(self) -> None:
        tool, name = self._selected_tool(), self._server
        if tool is None or name is None or self.form is None:
            return
        try:
            arguments = self.form.arguments()
        except ValueError as exc:
            self.result.setPlainText(f"Argomenti non validi: {exc}")
            return
        confirm = False
        if not tool.get("readOnly"):
            answer = QMessageBox.question(
                self, "Eseguire il tool?",
                f"'{tool['name']}' non e' dichiarato di sola lettura: potrebbe avere effetti.\n\n"
                f"Argomenti:\n{json.dumps(arguments, indent=2, ensure_ascii=False)}\n\nEseguirlo davvero?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            confirm = True
        self.run_btn.setEnabled(False)
        self.result.setPlainText("In esecuzione...")

        def done(result, error):
            self.run_btn.setEnabled(True)
            if error:
                self.result.setPlainText(f"Errore: {error}")
                return
            head = f"{result.get('ms')} ms\n"
            body = result.get("error") if result.get("error") is not None else result.get("result")
            self.result.setPlainText(head + json.dumps(body, indent=2, ensure_ascii=False))

        self._run(lambda: self.client.call_tool(name, tool["name"], arguments, confirm), done)
