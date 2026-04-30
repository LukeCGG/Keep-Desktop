"""KeepDesktop – Google Keep as floating sticky notes on your desktop."""

import sys
import logging
import logging.handlers
import os

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from config import ensure_data_dir, APP_NAME, APP_VERSION, DATA_DIR
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
        pass


def _setup_logging():
    """Configure logging to a rotating file in the user data dir.

    The PyInstaller GUI build has no console, so stderr-only logs are
    invisible. A log file is essential for diagnosing update failures
    and other silent errors.
    """
    ensure_data_dir()
    log_path = os.path.join(DATA_DIR, "keepdesktop.log")
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            log_path, maxBytes=512_000, backupCount=2, encoding="utf-8"
        ),
    ]
    # Also log to stderr in dev (when running from a console).
    if sys.stderr is not None and hasattr(sys.stderr, "isatty"):
        try:
            if sys.stderr.isatty():
                handlers.append(logging.StreamHandler(sys.stderr))
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger(__name__).info(
        "KeepDesktop %s starting; log file: %s", APP_VERSION, log_path
    )


def main():
    _setup_logging()
    _set_windows_app_id()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # keep running in tray
    app.setApplicationName("KeepDesktop")
    app.setWindowIcon(make_icon())

    controller = AppController()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
