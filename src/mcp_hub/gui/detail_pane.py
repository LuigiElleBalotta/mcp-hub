"""Dashboard detail of the selected server (hub-gui-and-clients R1): process,
sessions, call counters, latency and the most recent calls. The data comes from
`GET /api/servers/{name}/metrics`; nothing here ever shows arguments or results
(the hub does not keep them)."""
from __future__ import annotations

import time

from PySide6.QtWidgets import QGridLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget

NO_DATA = "Metriche non disponibili (hub piu' vecchio, metriche disattivate o server fermo)."


def format_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.0f} ms" if value >= 10 else f"{value:.1f} ms"


def format_recent(call: dict) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(call.get("at", 0)))
    outcome = "ok" if call.get("ok") else "ERRORE"
    return f"{stamp}  {call.get('method', '?')}  {format_ms(call.get('ms'))}  {outcome}"


class DetailPane(QWidget):
    FIELDS = (
        ("pid", "Processo"), ("uptime", "In esecuzione da"), ("restarts", "Riavvii"),
        ("sessions", "Sessioni"), ("inFlight", "Richieste in corso"), ("calls", "Chiamate"),
        ("errors", "Errori"), ("cacheHits", "Dalla cache"), ("p50", "Latenza p50"),
        ("p95", "Latenza p95"), ("lastError", "Ultimo errore"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = QLabel("Dettaglio: nessun server selezionato")
        self.title.setStyleSheet("font-weight: bold;")
        self._values: dict[str, QLabel] = {}
        # Two label/value pairs per row: eleven fields must not eat the window's height.
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        for i, (key, label) in enumerate(self.FIELDS):
            row, pair = divmod(i, 2)
            caption = QLabel(label + ":")
            caption.setStyleSheet("color: #666;")
            value = QLabel("-")
            value.setWordWrap(True)
            value.setTextInteractionFlags(value.textInteractionFlags().TextSelectableByMouse)
            self._values[key] = value
            grid.addWidget(caption, row, pair * 2)
            grid.addWidget(value, row, pair * 2 + 1)
        self.recent = QPlainTextEdit()
        self.recent.setReadOnly(True)
        self.recent.setPlaceholderText("Nessuna chiamata recente")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.title)
        layout.addLayout(grid)
        layout.addWidget(QLabel("Chiamate recenti (solo metodo e durata, mai argomenti o risultati)"))
        layout.addWidget(self.recent, 1)

    def clear(self) -> None:
        self.title.setText("Dettaglio: nessun server selezionato")
        self._set_all("-")
        self.recent.setPlainText("")

    def _set_all(self, text: str) -> None:
        for label in self._values.values():
            label.setText(text)

    def show_metrics(self, name: str, data: dict | None) -> None:
        self.title.setText(f"Dettaglio: {name}")
        if not data:
            self._set_all("-")
            self._values["pid"].setText(NO_DATA)
            self.recent.setPlainText("")
            return
        texts = {
            "pid": str(data["pid"]) if data.get("pid") else "-",
            "uptime": format_uptime(data.get("uptime")),
            "restarts": str(data.get("restarts", 0)),
            "sessions": str(data.get("sessions", "-")),
            "inFlight": str(data.get("inFlight", "-")),
            "calls": f"{data.get('calls', 0)} ({data.get('callsPerMin', 0)}/min)",
            "errors": str(data.get("errors", 0)),
            "cacheHits": str(data.get("cacheHits", 0)),
            "p50": format_ms(data.get("p50")),
            "p95": format_ms(data.get("p95")),
            "lastError": data.get("lastError") or "-",
        }
        for key, text in texts.items():
            self._values[key].setText(text)
        new_text = "\n".join(format_recent(c) for c in data.get("recent", []))
        if new_text != self.recent.toPlainText():  # polled every tick: don't disturb the scroll for nothing
            self.recent.setPlainText(new_text)
