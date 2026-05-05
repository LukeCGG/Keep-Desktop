"""Configuration and persistent state management for KeepDesktop."""

import json
import os
import subprocess
import sys
import winreg

APP_NAME = "KeepDesktop"
APP_VERSION = "2.1.1"
# GitHub repository (owner/name) used for the auto-updater.
GITHUB_REPO = "LukeCGG/Keep-Desktop"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("APPDATA", APP_DIR), APP_NAME)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TOKEN_FILE = os.path.join(DATA_DIR, "keep_token.dat")
POSITIONS_FILE = os.path.join(DATA_DIR, "positions.json")
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")

# Path of the Startup-folder shortcut that controls "start with Windows".
# This MUST be the same path the Inno Setup installer creates (see
# installer.iss → [Icons] {userstartup}\KeepDesktop.lnk) so the in-app
# toggle and the installer checkbox are literally the same setting.
STARTUP_FOLDER = os.path.join(
    os.environ.get("APPDATA", APP_DIR),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
)
STARTUP_SHORTCUT = os.path.join(STARTUP_FOLDER, f"{APP_NAME}.lnk")
# Legacy registry path used by older versions (<= 2.0.2) — we still read
# it for migration but the canonical store is the .lnk above.
_LEGACY_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# Google Keep note colors mapped to hex (keys match gkeepapi ColorValue names)
KEEP_COLORS = {
    "White": "#FFFFFF",
    "Red": "#F28B82",
    "Orange": "#FBBC04",
    "Yellow": "#FFF475",
    "Green": "#CCFF90",
    "Teal": "#A7FFEB",
    "Blue": "#CBF0F8",
    "DarkBlue": "#AECBFA",
    "Purple": "#D7AEFB",
    "Pink": "#FDCFE8",
    "Brown": "#E6C9A8",
    "Gray": "#E8EAED",
}

# Dark variants for the same Keep colour names. These are LOCAL-ONLY: the
# value sent back to Google Keep is always the matching LIGHT colour from
# KEEP_COLORS above. Picking a dark variant flips ``KeepNote.dark_mode``
# but leaves ``color_hex`` pointing at the light value Keep understands.
KEEP_COLORS_DARK = {
    "White":    "#3C4043",
    "Red":      "#5C2B29",
    "Orange":   "#614A19",
    "Yellow":   "#635D19",
    "Green":    "#345920",
    "Teal":     "#16504B",
    "Blue":     "#2D555E",
    "DarkBlue": "#1E3A5F",
    "Purple":   "#42275E",
    "Pink":     "#5B2245",
    "Brown":    "#442F19",
    "Gray":     "#3C3F43",
}

# Default window dimensions
DEFAULT_WIDTH = 260
DEFAULT_HEIGHT = 300
MIN_WIDTH = 180
MIN_HEIGHT = 120

# Sync interval in milliseconds (30 seconds)
SYNC_INTERVAL_MS = 30_000


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_json(path, default=None):
    if default is None:
        default = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    ensure_data_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_config():
    defaults = {
        "sync_enabled": False,
        "autostart": False,
        "always_on_top": False,
        "opacity": 0.95,
        # Use the new keep_protocol-based sync (decodes/encodes Keep's
        # docs-nestedModel formatting state). Falls back to gkeepapi
        # when False. Default ON for new installs from 1.1.0 onward.
        "keep_protocol_v2": True,
    }
    saved = load_json(CONFIG_FILE, {})
    # Merge: saved values win, defaults fill in any missing keys
    merged = {**defaults, **saved}
    return merged


def save_config(cfg):
    save_json(CONFIG_FILE, cfg)


def load_positions():
    return load_json(POSITIONS_FILE, {})


def save_positions(positions):
    save_json(POSITIONS_FILE, positions)


def get_position(note_id):
    positions = load_positions()
    return positions.get(note_id, None)


def set_position(note_id, x, y, w, h, pinned=None):
    positions = load_positions()
    entry = positions.get(note_id, {}) or {}
    entry.update({"x": x, "y": y, "w": w, "h": h})
    if pinned is not None:
        entry["pinned"] = bool(pinned)
    positions[note_id] = entry
    save_positions(positions)


def remove_position(note_id):
    positions = load_positions()
    positions.pop(note_id, None)
    save_positions(positions)


def _autostart_target() -> tuple[str, str, str]:
    """Return (target_exe, arguments, working_dir) for the Startup
    shortcut. Differs between frozen builds and source runs."""
    if getattr(sys, "frozen", False):
        return sys.executable, "", os.path.dirname(sys.executable)
    # Running from source — prefer pythonw.exe so we don't get a
    # console window flash on every login.
    py = sys.executable
    pyw = py.replace("python.exe", "pythonw.exe")
    if os.path.exists(pyw):
        py = pyw
    main_py = os.path.join(APP_DIR, "main.py")
    return py, f'"{main_py}"', APP_DIR


def _remove_legacy_run_key() -> None:
    """Strip the old HKCU Run-key entry written by versions <= 2.0.2.

    Keeping it around would cause double-launch when both the legacy
    key AND the new Startup-folder shortcut are present.
    """
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _LEGACY_RUN_KEY, 0, winreg.KEY_SET_VALUE,
        )
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass
        finally:
            winreg.CloseKey(key)
    except OSError:
        pass


def is_autostart_enabled() -> bool:
    """True iff a Startup-folder shortcut for KeepDesktop exists.

    This is the SOURCE OF TRUTH — both the installer's tickbox and the
    in-app "Start with Windows" toggle write to the same .lnk path.
    """
    return os.path.isfile(STARTUP_SHORTCUT)


def set_autostart(enabled: bool) -> bool:
    """Add or remove the Startup-folder shortcut.

    Uses PowerShell's WScript.Shell COM helper (no extra deps). Returns
    True on success. Idempotent.
    """
    if not enabled:
        try:
            os.remove(STARTUP_SHORTCUT)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        _remove_legacy_run_key()
        return True

    target, args, workdir = _autostart_target()
    try:
        os.makedirs(STARTUP_FOLDER, exist_ok=True)
    except OSError:
        return False

    # Build the PowerShell snippet. Single-quoted string literals are
    # safest because backslashes don't need escaping; only embedded
    # single quotes do (we double them per PS rules).
    def _ps_quote(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$lnk = $ws.CreateShortcut({_ps_quote(STARTUP_SHORTCUT)}); "
        f"$lnk.TargetPath = {_ps_quote(target)}; "
        f"$lnk.Arguments = {_ps_quote(args)}; "
        f"$lnk.WorkingDirectory = {_ps_quote(workdir)}; "
        "$lnk.WindowStyle = 1; "
        "$lnk.Save()"
    )
    CREATE_NO_WINDOW = 0x08000000
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps],
            creationflags=CREATE_NO_WINDOW,
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError):
        return False

    _remove_legacy_run_key()
    return os.path.isfile(STARTUP_SHORTCUT)


def save_token(token):
    ensure_data_dir()
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(token)


def load_token():
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None
