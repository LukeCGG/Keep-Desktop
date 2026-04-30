"""Configuration and persistent state management for KeepDesktop."""

import json
import os
import sys
import winreg

APP_NAME = "KeepDesktop"
APP_VERSION = "1.0.6"
# GitHub repository (owner/name) used for the auto-updater.
GITHUB_REPO = "LukeCGG/Keep-Desktop"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("APPDATA", APP_DIR), APP_NAME)
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TOKEN_FILE = os.path.join(DATA_DIR, "keep_token.dat")
POSITIONS_FILE = os.path.join(DATA_DIR, "positions.json")
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")

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


def set_position(note_id, x, y, w, h):
    positions = load_positions()
    positions[note_id] = {"x": x, "y": y, "w": w, "h": h}
    save_positions(positions)


def remove_position(note_id):
    positions = load_positions()
    positions.pop(note_id, None)
    save_positions(positions)


def set_autostart(enabled):
    """Add or remove KeepDesktop from Windows startup registry."""
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enabled:
            exe = sys.executable
            script = os.path.join(APP_DIR, "main.py")
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}" "{script}"')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except OSError:
        pass


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
