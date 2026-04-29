"""Auto-update support for KeepDesktop.

Polls the GitHub Releases API for a newer version, downloads the
installer asset, and launches it (Inno Setup is told to silently
install over the top of the existing install and relaunch the app).

Designed for a *public* repository — it uses the anonymous REST API,
which fails harmlessly while the repo is still private.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal, Slot, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from config import APP_NAME, APP_VERSION, GITHUB_REPO

log = logging.getLogger(__name__)

RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"

# Asset filename pattern produced by the Inno Setup script.
ASSET_PATTERN = re.compile(r"^KeepDesktop-Setup-.+\.exe$", re.IGNORECASE)


@dataclass
class ReleaseInfo:
    version: str            # e.g. "1.2.3" (no leading "v")
    tag: str                # raw tag, e.g. "v1.2.3"
    name: str               # release title
    notes: str              # release body / changelog markdown
    download_url: str       # direct asset URL


def _parse_version(s: str) -> tuple[int, ...]:
    """Loose semver parser. 'v1.2.3' -> (1, 2, 3)."""
    cleaned = s.lstrip("vV").strip()
    parts: list[int] = []
    for piece in cleaned.split("."):
        m = re.match(r"(\d+)", piece)
        parts.append(int(m.group(1)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(remote: str, local: str) -> bool:
    return _parse_version(remote) > _parse_version(local)


def fetch_latest_release(timeout: float = 10.0) -> ReleaseInfo | None:
    """Hit the GitHub API and return the latest release, or None."""
    req = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 404 = repo private / no releases yet. Treat as "no update".
        if exc.code in (401, 403, 404):
            log.info("No release info available (HTTP %s).", exc.code)
            return None
        log.warning("Update check HTTP error: %s", exc)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("Update check network error: %s", exc)
        return None
    except json.JSONDecodeError as exc:
        log.warning("Update check JSON error: %s", exc)
        return None

    tag = data.get("tag_name") or ""
    if not tag:
        return None

    # Find the installer asset
    download_url = ""
    for asset in data.get("assets", []) or []:
        name = asset.get("name", "")
        if ASSET_PATTERN.match(name):
            download_url = asset.get("browser_download_url", "")
            break

    if not download_url:
        log.info("Latest release %s has no installer asset; ignoring.", tag)
        return None

    return ReleaseInfo(
        version=tag.lstrip("vV"),
        tag=tag,
        name=data.get("name") or tag,
        notes=data.get("body") or "",
        download_url=download_url,
    )


def download_installer(url: str, progress_cb=None) -> str | None:
    """Stream-download the installer to a temp file. Returns the path."""
    suffix = os.path.basename(url) or "KeepDesktop-Setup.exe"
    fd, dest = tempfile.mkstemp(prefix="KeepDesktopUpdate-", suffix="-" + suffix)
    os.close(fd)

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", "0") or 0)
            downloaded = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        try:
                            progress_cb(downloaded, total)
                        except Exception:  # noqa: BLE001
                            pass
        return dest
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to download installer: %s", exc)
        try:
            os.remove(dest)
        except OSError:
            pass
        return None


def run_installer_and_quit(installer_path: str) -> bool:
    """Launch the installer (with UAC if needed) and quit the app.

    Uses ShellExecuteW so the installer's embedded manifest can trigger
    UAC when the existing install lives in Program Files. /SILENT (not
    /VERYSILENT) so the user sees the progress bar; we deliberately do
    not pass /SUPPRESSMSGBOXES so failures are visible.
    """
    if not os.path.isfile(installer_path):
        return False

    params = " ".join([
        "/SILENT",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        "/RESTARTAPPLICATIONS",
    ])

    try:
        import ctypes
        from ctypes import wintypes

        SW_SHOWNORMAL = 1
        # ShellExecuteW honours the installer's manifest -> UAC prompt
        # appears automatically if elevation is required. Verb=None lets
        # Windows pick the default ("open"); use "runas" to force UAC.
        ShellExecuteW = ctypes.windll.shell32.ShellExecuteW
        ShellExecuteW.restype = wintypes.HINSTANCE
        result = ShellExecuteW(
            None, None, installer_path, params, None, SW_SHOWNORMAL
        )
        # Return value > 32 means success; <= 32 is a SE_ERR_* code.
        if int(result) <= 32:
            log.error("ShellExecuteW failed with code %s", int(result))
            return False
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to launch installer: %s", exc)
        return False

    # Give Inno a moment to spin up (and for the user to confirm UAC if
    # needed) before quitting, so it can take over our running EXE.
    QTimer.singleShot(2500, QApplication.instance().quit)
    return True


# ───────────────────────────────────────────────────────────────────────
#  Qt-friendly controller
# ───────────────────────────────────────────────────────────────────────


class UpdateChecker(QObject):
    """Runs an update check on a worker thread and exposes Qt signals."""

    update_available = Signal(object)   # ReleaseInfo
    no_update = Signal()
    error = Signal(str)

    def check_async(self):
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            info = fetch_latest_release()
        except Exception as exc:  # noqa: BLE001
            log.exception("Update check crashed")
            self.error.emit(str(exc))
            return
        if info is None:
            self.no_update.emit()
            return
        if is_newer(info.version, APP_VERSION):
            log.info("Update available: %s (current %s)", info.version, APP_VERSION)
            self.update_available.emit(info)
        else:
            log.info("Already up to date (current %s, latest %s)",
                     APP_VERSION, info.version)
            self.no_update.emit()


def prompt_and_install(parent, info: ReleaseInfo):
    """Ask the user whether to install the new version, then do it."""
    short_notes = (info.notes or "").strip()
    if len(short_notes) > 800:
        short_notes = short_notes[:800] + "\n…"

    msg = QMessageBox(parent)
    msg.setIcon(QMessageBox.Icon.Information)
    msg.setWindowTitle(f"{APP_NAME} update available")
    msg.setText(
        f"<b>{APP_NAME} {info.version}</b> is available."
        f"<br>(You're on {APP_VERSION}.)"
    )
    if short_notes:
        msg.setInformativeText("Release notes:\n\n" + short_notes)
    msg.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    msg.button(QMessageBox.StandardButton.Yes).setText("Update now")
    msg.button(QMessageBox.StandardButton.No).setText("Later")
    msg.setDefaultButton(QMessageBox.StandardButton.Yes)

    if msg.exec() != QMessageBox.StandardButton.Yes:
        return

    # Show a non-blocking "downloading" dialog while we fetch.
    busy = QMessageBox(parent)
    busy.setIcon(QMessageBox.Icon.Information)
    busy.setWindowTitle(f"{APP_NAME}")
    busy.setText("Downloading update…\nThis may take a moment.")
    busy.setStandardButtons(QMessageBox.StandardButton.NoButton)
    busy.show()
    QApplication.processEvents()

    def _download():
        path = download_installer(info.download_url)
        QTimer.singleShot(0, lambda: _after_download(path))

    def _after_download(path):
        busy.close()
        if not path:
            QMessageBox.warning(
                parent, APP_NAME,
                "Update download failed. Please try again later."
            )
            return
        if not run_installer_and_quit(path):
            QMessageBox.warning(
                parent, APP_NAME,
                "Couldn't launch the installer. The downloaded file is here:\n"
                + path
            )

    threading.Thread(target=_download, daemon=True).start()
