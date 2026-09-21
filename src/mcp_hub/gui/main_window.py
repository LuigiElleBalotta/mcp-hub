from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QHBoxLayout,
)

from mcp_hub.gui.api_client import HubApiClient

_STATUS_COLOR = {"running": "#2e7d32", "stopped": "#757575", "crashed": "#c62828", "starting": "#f9a825"}


class MainWindow(QMainWindow):
    def __init__(self, client: HubApiClient | None = None):
        super().__init__()
        self.setWindowTitle("mcp-hub")
        self.client = client or HubApiClient()

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Server", "Status", "Concurrency", "Actions"])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.table)
        self.setCentralWidget(central)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def refresh(self) -> None:
        statuses = self.client.status()
        self.table.setRowCount(len(statuses))
        for row, (name, status) in enumerate(sorted(statuses.items())):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            status_item = QTableWidgetItem(status)
            status_item.setForeground(QColor(_STATUS_COLOR.get(status, "#000000")))
            self.table.setItem(row, 1, status_item)

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            start_btn = QPushButton("Start")
            stop_btn = QPushButton("Stop")
            start_btn.clicked.connect(lambda _, n=name: self._start(n))
            stop_btn.clicked.connect(lambda _, n=name: self._stop(n))
            actions_layout.addWidget(start_btn)
            actions_layout.addWidget(stop_btn)
            self.table.setCellWidget(row, 3, actions)

    def _start(self, name: str) -> None:
        self.client.start(name)
        self.refresh()

    def _stop(self, name: str) -> None:
        self.client.stop(name)
        self.refresh()
