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
        new_text = "\n".join(lines)
        if new_text == self.text.toPlainText():
            return
        # Called on every status tick: keep the view pinned to the bottom
        # only if the user was already there, otherwise leave their scroll.
        bar = self.text.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        previous = bar.value()
        self.text.setPlainText(new_text)
        bar.setValue(bar.maximum() if at_bottom else previous)
