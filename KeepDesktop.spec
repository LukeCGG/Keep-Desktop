# PyInstaller spec for KeepDesktop
# Build with:  pyinstaller KeepDesktop.spec
#
# Produces dist/KeepDesktop/KeepDesktop.exe (one-folder, windowed).
# We keep --onedir (not --onefile) so the Inno installer can ship the
# whole folder; this avoids the 2-3s extraction lag of one-file builds
# and plays nicely with QtWebEngine's resource files.

# ruff: noqa
# type: ignore
import sys
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

block_cipher = None

project_root = Path(SPECPATH).resolve()

# Packages that call importlib.metadata.version(__name__) at import time.
# Their .dist-info folders must be bundled or the import will crash.
_metadata = []
for pkg in ("gpsoauth", "gkeepapi", "requests", "urllib3"):
    try:
        _metadata += copy_metadata(pkg)
    except Exception:
        pass

a = Analysis(
    ['main.py'],
    pathex=[str(project_root)],
    binaries=[],
    datas=_metadata,
    hiddenimports=[
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtNetwork',
        'gkeepapi',
        'gpsoauth',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'unittest',
        'test',
        'pydoc',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='KeepDesktop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,               # add 'icon.ico' here when you have one
    version=None,            # add 'file_version_info.txt' for full Win metadata
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='KeepDesktop',
)
