from __future__ import annotations

from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPlainTextEdit


class LogPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.label = QLabel("No server selected")
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(self.text)

    def show_logs(self, name: str, lines: list[str]) -> None:
        self.label.setText(f"Logs: {name}")
        self.text.setPlainText("\n".join(lines))
