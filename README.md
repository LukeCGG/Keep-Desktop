# KeepDesktop

**Google Keep as floating sticky notes on your Windows desktop.** Each note gets its own draggable window — just like Windows Sticky Notes, but synced with Google Keep.

![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)

## Download & Install

Grab the latest one-click installer from the [Releases page](https://github.com/LukeCGG/Keep-Desktop/releases/latest):

- **`KeepDesktop-Setup-x.y.z.exe`** — Windows installer (recommended)

Run it, click through, done. KeepDesktop will be in your Start menu and (optionally) start with Windows.

> The app **auto-checks for updates** on startup and from the tray menu (`Check for updates…`). When a new release is published on GitHub, it offers to download and install it for you.

## Features

- **One window per note** — drag them anywhere on screen
- **Remembers positions** — every window reopens exactly where you left it
- **Google Keep sync** — two-way sync keeps your notes in the cloud
- **System tray** — lives quietly in your taskbar tray
- **Auto-start** — optional "Start with Windows" toggle
- **Auto-update** — silent in-place updates from GitHub Releases
- **Color-coded** — all 12 Google Keep colors
- **Pin notes** — toggle always-on-top per note
- **Minimal UI** — frameless, clean, stays out of your way

## Connecting Google Keep

1. Go to [Google Account → Security → App passwords](https://myaccount.google.com/apppasswords)
2. Create an app password (select "Other", name it "KeepDesktop")
3. Right-click the tray icon → **Sign in to Google Keep**
4. Enter your Gmail and the 16-character app password

> You need 2-Step Verification enabled on your Google account to create app passwords.

## Usage

| Action | How |
|---|---|
| **New note** | Right-click tray → "New note" |
| **Move a note** | Drag the handle (≡) on the title bar |
| **Edit title** | Click the title text |
| **Resize** | Drag the bottom-right corner grip |
| **Change color** | Click 🎨 on the title bar |
| **Pin on top** | Click 📌 on the title bar |
| **Hide a note** | Click ✕ (note is hidden, not deleted) |
| **Delete a note** | Right-click the note body → "Delete note" |
| **Manage notes** | Double-click tray icon |
| **Sync now** | Right-click tray → "Sync now" |
| **Check for updates** | Right-click tray → "Check for updates…" |
| **Auto-start** | Right-click tray → "Start with Windows" |

## Building from Source

### Run from Python

```powershell
pip install -r requirements.txt
python main.py
```

### Build the Windows installer

Requires [Inno Setup 6](https://jrsoftware.org/isdl.php) on your `PATH`.

```powershell
pip install -r requirements.txt pyinstaller
pyinstaller KeepDesktop.spec
ISCC.exe installer.iss
```

The installer is written to `Output\KeepDesktop-Setup-<version>.exe`.

GitHub Actions does this automatically on every `v*` tag — see `.github/workflows/release.yml`.

## How It Works

- Config, positions, notes cache, and the auth token live in `%APPDATA%\KeepDesktop\`
- Sync uses the unofficial [`gkeepapi`](https://github.com/kiwiz/gkeepapi) library
- Auth uses [`gpsoauth`](https://github.com/simon-weber/gpsoauth) — your password is never stored, only the master token
- Notes created offline are pushed to Keep on the next sync

## Files

| File | Purpose |
|---|---|
| `main.py` | Entry point |
| `app_controller.py` | Tray icon, note lifecycle, sync orchestration |
| `note_window.py` | Individual sticky-note window (frameless Qt widget) |
| `keep_sync.py` | Google Keep API wrapper |
| `updater.py` | GitHub Releases auto-updater |
| `config.py` | Settings, positions, autostart, paths, version |
| `KeepDesktop.spec` | PyInstaller build spec |
| `installer.iss` | Inno Setup installer script |

## License

KeepDesktop is licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0).

This means:

- You may use, copy, modify, and redistribute it freely.
- You may run it for any purpose, personal or internal-business.
- If you distribute it, modified or not, the **complete corresponding source code** must be made available under the same AGPL-3.0 license.
- If you run a modified version on a network server that users interact with, you must offer them the modified source code.
- You **may not** rebrand it, repackage it, or sell it as a closed-source / proprietary product.

See [LICENSE](LICENSE) for the full legal text.

## Contributing

Issues and pull requests welcome at <https://github.com/LukeCGG/Keep-Desktop>.

## Disclaimer

KeepDesktop uses an **unofficial** Google Keep API. Google may change or break this at any time. Use at your own risk; do not rely on this for mission-critical data. The project is not affiliated with or endorsed by Google.
