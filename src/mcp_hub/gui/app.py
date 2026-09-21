from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from mcp_hub.gui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(700, 400)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
