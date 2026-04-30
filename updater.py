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

from PySide6.QtCore import Qt, QObject, Signal, Slot, QTimer
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

    log.info("Downloading update from %s -> %s", url, dest)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", "0") or 0)
            log.info("Download started; Content-Length=%s", total)
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
        size = os.path.getsize(dest)
        log.info("Download finished: %s bytes -> %s", size, dest)
        if size < 100_000:  # sanity check; real installer is several MB
            log.error("Downloaded file is suspiciously small (%s bytes)", size)
        return dest
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to download installer: %s", exc)
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
        # IMPORTANT: declare argtypes so Python str is correctly converted
        # to wide (UTF-16) strings. Without this, ctypes passes ANSI
        # bytes to the *W* function and the path is silently mangled,
        # making ShellExecuteW return SE_ERR_FNF (2) without launching
        # anything — which looks to the user like the updater hangs.
        ShellExecuteW.argtypes = [
            wintypes.HWND,    # hwnd
            wintypes.LPCWSTR, # lpOperation (verb)
            wintypes.LPCWSTR, # lpFile
            wintypes.LPCWSTR, # lpParameters
            wintypes.LPCWSTR, # lpDirectory
            ctypes.c_int,     # nShowCmd
        ]
        ShellExecuteW.restype = wintypes.HINSTANCE
        result = ShellExecuteW(
            None, None, installer_path, params, None, SW_SHOWNORMAL
        )
        # Return value > 32 means success; <= 32 is a SE_ERR_* code.
        rc = int(result)
        if rc <= 32:
            log.error("ShellExecuteW failed with code %s for %s", rc, installer_path)
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


def _notes_to_html(markdown: str) -> str:
    """Tiny markdown->HTML for the release-notes preview.

    Handles bare URLs and `[text](url)` links so the changelog link in
    GitHub's auto-generated notes is actually clickable.
    """
    import html
    text = html.escape(markdown)
    # [text](url) → <a href="url">text</a>
    text = re.sub(
        r'\[([^\]]+)\]\((https?://[^)\s]+)\)',
        r'<a href="\2">\1</a>',
        text,
    )
    # Bare URLs (skip ones already inside an href="...")
    text = re.sub(
        r'(?<!")(?<!=)(https?://[^\s<)]+)',
        r'<a href="\1">\1</a>',
        text,
    )
    return text.replace("\n", "<br>")


def prompt_and_install(parent, info: ReleaseInfo):
    """Ask the user whether to install the new version, then do it."""
    short_notes = (info.notes or "").strip()
    if len(short_notes) > 800:
        short_notes = short_notes[:800] + "\n…"

    msg = QMessageBox(parent)
    msg.setIcon(QMessageBox.Icon.Information)
    msg.setWindowTitle(f"{APP_NAME} update available")
    msg.setTextFormat(Qt.TextFormat.RichText)
    msg.setText(
        f"<b>{APP_NAME} {info.version}</b> is available."
        f"<br>(You're on {APP_VERSION}.)"
    )
    if short_notes:
        msg.setInformativeText(
            "<b>Release notes</b><br><br>" + _notes_to_html(short_notes)
        )
    msg.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextBrowserInteraction
    )
    msg.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    msg.button(QMessageBox.StandardButton.Yes).setText("Update now")
    msg.button(QMessageBox.StandardButton.No).setText("Later")
    msg.setDefaultButton(QMessageBox.StandardButton.Yes)

    if msg.exec() != QMessageBox.StandardButton.Yes:
        return

    _start_download_install(parent, info)


# Keep a strong reference to the in-flight worker so it isn't GC'd.
_active_worker = None


class _DownloadWorker(QObject):
    """Runs the download on a worker thread and signals the main thread."""

    finished = Signal(str, str)  # (path_or_empty, error_message)

    def __init__(self, url: str):
        super().__init__()
        self._url = url

    def run(self):
        try:
            path = download_installer(self._url)
            if path:
                self.finished.emit(path, "")
            else:
                self.finished.emit("", "Download failed (see log file).")
        except Exception as exc:  # noqa: BLE001
            log.exception("Download worker crashed")
            self.finished.emit("", f"Download crashed: {exc}")


def _data_dir() -> str:
    try:
        from config import DATA_DIR
        return DATA_DIR
    except Exception:  # noqa: BLE001
        return tempfile.gettempdir()


def _release_url() -> str:
    try:
        from config import GITHUB_REPO
        return f"https://github.com/{GITHUB_REPO}/releases/latest"
    except Exception:  # noqa: BLE001
        return ""


def _start_download_install(parent, info: ReleaseInfo):
    """Show a progress dialog, download in a worker, install when done."""
    global _active_worker

    log_path = os.path.join(_data_dir(), "keepdesktop.log")
    log.info("Starting update: %s -> %s", APP_VERSION, info.version)
    log.info("Installer URL: %s", info.download_url)

    busy = QMessageBox(parent)
    busy.setIcon(QMessageBox.Icon.Information)
    busy.setWindowTitle(APP_NAME)
    busy.setText(
        f"Downloading {APP_NAME} {info.version}…\n"
        "This may take a moment."
    )
    busy.setStandardButtons(QMessageBox.StandardButton.NoButton)
    # Non-modal so the OS can paint it AND so the worker thread's
    # cross-thread signal can be delivered to our main event loop.
    busy.setModal(False)
    busy.show()
    QApplication.processEvents()

    worker = _DownloadWorker(info.download_url)
    _active_worker = worker

    def _on_finished(path: str, err: str):
        global _active_worker
        log.info("Download worker finished: path=%r err=%r", path, err)
        busy.close()

        if not path:
            _show_update_failed(parent, info, log_path,
                                err or "Update download failed.")
            _active_worker = None
            return

        ok, launch_err = _launch_installer(path)
        if not ok:
            _show_update_failed(
                parent, info, log_path,
                f"Couldn't launch the installer.\n{launch_err}\n\n"
                f"You can run it manually:\n{path}",
            )
            _active_worker = None
            return

        log.info("Installer launched; quitting in 2.5s to let it take over")
        QTimer.singleShot(2500, QApplication.instance().quit)
        _active_worker = None

    # Queued connection guarantees _on_finished runs on the main (GUI)
    # thread regardless of which thread emits the signal.
    worker.finished.connect(_on_finished, Qt.ConnectionType.QueuedConnection)

    threading.Thread(target=worker.run, daemon=True).start()


def _launch_installer(installer_path: str) -> tuple[bool, str]:
    """Try to start the installer. Returns (ok, error_message)."""
    if not os.path.isfile(installer_path):
        return False, f"Installer file not found: {installer_path}"
    try:
        ok = run_installer_and_quit(installer_path)
        if ok:
            return True, ""
        return False, "ShellExecuteW returned a failure code (see log file)."
    except Exception as exc:  # noqa: BLE001
        log.exception("Launch installer crashed")
        return False, str(exc)


def _show_update_failed(parent, info: ReleaseInfo, log_path: str, detail: str):
    """Show a clear error dialog with the log path and a fallback link."""
    log.error("Update failed: %s", detail)

    release_url = _release_url()
    body = (
        f"<b>Update to {APP_NAME} {info.version} failed.</b>"
        f"<br><br>{_notes_to_html(detail)}"
    )
    if release_url:
        body += (
            f"<br><br>You can download and install it manually from "
            f'<a href="{release_url}">{release_url}</a>.'
        )
    body += (
        f"<br><br><small>Log file:<br><code>{log_path}</code></small>"
    )

    err = QMessageBox(parent)
    err.setIcon(QMessageBox.Icon.Warning)
    err.setWindowTitle(f"{APP_NAME} update failed")
    err.setTextFormat(Qt.TextFormat.RichText)
    err.setText(body)
    err.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    err.setStandardButtons(QMessageBox.StandardButton.Ok)
    err.exec()
