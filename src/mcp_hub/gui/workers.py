"""Small background-thread helper shared by the GUI panels: every network call
runs off the GUI thread (a hub or registry that is slow must never freeze the window)."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal


class ApiWorker(QThread):
    done = Signal(object, object)  # (result, error text | None)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn(), None)
        except Exception as exc:  # noqa: BLE001 - shown to the user as text
            self.done.emit(None, str(exc) or exc.__class__.__name__)
