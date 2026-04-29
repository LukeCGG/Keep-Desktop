"""KeepDesktop – Google Keep as floating sticky notes on your desktop."""

import sys
import logging

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from config import ensure_data_dir
from app_icon import make_icon
from app_controller import AppController


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    ensure_data_dir()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # keep running in tray
    app.setApplicationName("KeepDesktop")
    # Universal K-note icon: inherited by every window and dialog.
    app.setWindowIcon(make_icon())

    controller = AppController()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
