"""Generate icon.ico in the project root.

Run this once before building with PyInstaller (the spec references icon.ico).

    python tools/generate_icon.py
"""

import sys
from pathlib import Path

# Need a QApplication for QPixmap rendering.
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app_icon import save_ico  # noqa: E402


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    out = ROOT / "icon.ico"
    save_ico(str(out))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
