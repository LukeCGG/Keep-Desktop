"""KeepDesktop – Google Keep as floating sticky notes on your desktop."""

import sys
import logging

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from config import ensure_data_dir, APP_NAME, APP_VERSION
from app_icon import make_icon
from app_controller import AppController


def _set_windows_app_id():
    """Tell Windows this is a distinct app (not pythonw.exe) so the
    taskbar uses our icon and groups note windows under one entry.
    Must be called before any window is created.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        app_id = f"LukeCGG.{APP_NAME}.{APP_VERSION}"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:  # noqa: BLE001
        # Non-fatal \u2014 we just lose proper taskbar grouping/icon if it fails.
        pass


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    ensure_data_dir()
    _set_windows_app_id()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # keep running in tray
    app.setApplicationName("KeepDesktop")
    # Universal K-note icon: inherited by every window and dialog.
    app.setWindowIcon(make_icon())

    controller = AppController()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
