"""System-tray icon and main application controller."""

import logging
import os
import uuid
import threading
from functools import partial

from PySide6.QtCore import Qt, QTimer, Slot, Signal, QObject, QUrl
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction, QFont
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QDialog,
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QFrame, QScrollArea, QWidget, QCheckBox, QSizePolicy,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile

from config import (
    load_config, save_config, set_autostart, save_token,
    SYNC_INTERVAL_MS, DATA_DIR, load_token,
    load_json, save_json, APP_NAME, APP_VERSION, GITHUB_REPO,
)
from app_icon import make_icon as _make_icon
from note_window import NoteWindow
from keep_sync import KeepSync, KeepNote
from updater import UpdateChecker, prompt_and_install

log = logging.getLogger(__name__)

VISIBILITY_FILE = os.path.join(DATA_DIR, "visibility.json")


def _set_window_app_id(window, app_id: str) -> None:
    """Set per-window AppUserModelID via IPropertyStore so Windows can
    group (or refuse to group) this HWND in the taskbar.

    No-op on non-Windows. Failures are non-fatal and logged by the caller.
    """
    import sys
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    hwnd = int(window.winId())

    # SHGetPropertyStoreForWindow + IPropertyStore::SetValue(PKEY_AppUserModel_ID).
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32),
                    ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16),
                    ("Data4", ctypes.c_ubyte * 8)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("wReserved1", ctypes.c_ushort),
            ("wReserved2", ctypes.c_ushort),
            ("wReserved3", ctypes.c_ushort),
            ("pwszVal", ctypes.c_wchar_p),
            ("padding", ctypes.c_uint64),
        ]

    # IPropertyStore IID and PKEY_AppUserModel_ID.
    IID_IPropertyStore = GUID(
        0x886D8EEB, 0x8CF2, 0x4446,
        (ctypes.c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99),
    )
    PKEY_AppUserModel_ID = PROPERTYKEY(
        GUID(0x9F4C2855, 0x9F79, 0x4B39,
             (ctypes.c_ubyte * 8)(0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3)),
        5,
    )

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32

    # InitPropVariantFromString equivalent.
    PropVariantInit = ole32.PropVariantInit
    PropVariantInit.argtypes = [ctypes.POINTER(PROPVARIANT)]
    PropVariantClear = ole32.PropVariantClear
    PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]

    SHGetPropertyStoreForWindow = shell32.SHGetPropertyStoreForWindow
    SHGetPropertyStoreForWindow.argtypes = [
        wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    SHGetPropertyStoreForWindow.restype = ctypes.c_long

    pStore = ctypes.c_void_p()
    hr = SHGetPropertyStoreForWindow(
        hwnd, ctypes.byref(IID_IPropertyStore), ctypes.byref(pStore)
    )
    if hr != 0 or not pStore.value:
        raise OSError(f"SHGetPropertyStoreForWindow hr=0x{hr & 0xFFFFFFFF:08X}")

    # IPropertyStore vtable: [QueryInterface, AddRef, Release, GetCount,
    #                         GetAt, GetValue, SetValue, Commit]
    vtbl = ctypes.cast(pStore, ctypes.POINTER(ctypes.c_void_p))[0]
    SetValue = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT),
    )(ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[6])
    Commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(
        ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[7]
    )
    Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
        ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[2]
    )

    pv = PROPVARIANT()
    PropVariantInit(ctypes.byref(pv))
    pv.vt = 31  # VT_LPWSTR
    pv.pwszVal = ctypes.c_wchar_p(app_id)
    try:
        SetValue(pStore, ctypes.byref(PKEY_AppUserModel_ID), ctypes.byref(pv))
        Commit(pStore)
    finally:
        PropVariantClear(ctypes.byref(pv))
        Release(pStore)


def _load_visibility() -> dict:
    return load_json(VISIBILITY_FILE, {})


def _save_visibility(vis: dict):
    save_json(VISIBILITY_FILE, vis)


def _escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ═══════════════════════════════════════════════════════════════════════
#  Login Dialog – Embedded browser Google sign-in
# ═══════════════════════════════════════════════════════════════════════

EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"


class _LoginSignals(QObject):
    """Cross-thread signal for login result."""
    result = Signal(bool, str)       # (success, email_or_error)
    token_found = Signal(str)        # oauth_token cookie value


class LoginDialog(QDialog):
    """Opens an embedded Google sign-in browser.

    After the user authenticates, the oauth_token cookie is captured
    and exchanged for a master token via gpsoauth.exchange_token.
    """

    def __init__(self, first_launch=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("KeepDesktop – Sign in to Google")
        self.resize(480, 640)
        self.signed_in = False
        self._keep_sync = None
        self._captured_token = None
        self._signals = _LoginSignals()
        self._signals.result.connect(self._on_login_result)
        self._signals.token_found.connect(self._on_token_found)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Info bar at top
        info = QLabel(
            "  Sign in with your Google account below. "
            "KeepDesktop will capture the token automatically."
        )
        info.setStyleSheet(
            "background: #E8F0FE; color: #1A73E8; padding: 8px;"
            "font-size: 12px;"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # Status label (hidden until needed)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("padding: 8px; font-size: 12px;")
        self.status_label.hide()
        layout.addWidget(self.status_label)

        # Embedded browser
        self._profile = QWebEngineProfile("KeepDesktopAuth", self)
        self._profile.cookieStore().cookieAdded.connect(self._on_cookie)

        self._browser = QWebEngineView()
        page = self._browser.page()
        from PySide6.QtWebEngineCore import QWebEnginePage
        new_page = QWebEnginePage(self._profile, self._browser)
        self._browser.setPage(new_page)
        self._browser.setUrl(QUrl(EMBEDDED_SETUP_URL))
        layout.addWidget(self._browser, stretch=1)

        # Give the browser keyboard focus so Enter goes to the web page
        self._browser.setFocus()

        # Bottom button row
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(8, 6, 8, 6)
        if first_launch:
            skip_btn = QPushButton("Skip – use offline")
            skip_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            skip_btn.clicked.connect(self.reject)
            btn_row.addWidget(skip_btn)
        else:
            cancel_btn = QPushButton("Cancel")
            cancel_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            cancel_btn.clicked.connect(self.reject)
            btn_row.addWidget(cancel_btn)
        btn_row.addStretch()

        # Manual token paste fallback
        self._paste_btn = QPushButton("Paste token manually…")
        self._paste_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._paste_btn.setStyleSheet("font-size: 11px; color: #666;")
        self._paste_btn.clicked.connect(self._show_manual_input)
        btn_row.addWidget(self._paste_btn)

        layout.addLayout(btn_row)

        # Hidden manual input row
        self._manual_frame = QFrame()
        self._manual_frame.hide()
        manual_layout = QHBoxLayout(self._manual_frame)
        manual_layout.setContentsMargins(8, 0, 8, 8)

        # Email input for manual flow
        self._manual_email = QLineEdit()
        self._manual_email.setPlaceholderText("Email")
        self._manual_email.setFixedWidth(160)
        manual_layout.addWidget(self._manual_email)

        self._manual_input = QLineEdit()
        self._manual_input.setPlaceholderText("Paste oauth_token cookie value here")
        manual_layout.addWidget(self._manual_input)
        submit_btn = QPushButton("Submit")
        submit_btn.clicked.connect(self._on_manual_submit)
        manual_layout.addWidget(submit_btn)
        layout.addWidget(self._manual_frame)

    def set_keep_sync(self, sync: KeepSync):
        self._keep_sync = sync

    def _show_manual_input(self):
        self._manual_frame.setVisible(not self._manual_frame.isVisible())

    def _on_cookie(self, cookie):
        """Called for every cookie set by the embedded browser."""
        name = bytes(cookie.name()).decode("utf-8", errors="replace")
        if name == "oauth_token" and not self._captured_token:
            value = bytes(cookie.value()).decode("utf-8", errors="replace")
            if value:
                self._captured_token = value
                self._signals.token_found.emit(value)

    @Slot(str)
    def _on_token_found(self, oauth_token):
        """OAuth cookie captured — exchange it for a master token."""
        self._browser.setHtml(
            "<html><body style='display:flex;align-items:center;"
            "justify-content:center;height:100vh;font-family:Segoe UI;'>"
            "<div style='text-align:center'>"
            "<h2>Token captured!</h2>"
            "<p>Exchanging for master token…</p>"
            "</div></body></html>"
        )
        self._paste_btn.setEnabled(False)

        # Try to extract email from cookies or the page URL
        email = self._manual_email.text().strip()
        threading.Thread(
            target=self._exchange_worker, args=(email, oauth_token), daemon=True
        ).start()

    def _on_manual_submit(self):
        token = self._manual_input.text().strip()
        email = self._manual_email.text().strip()
        if not token:
            self._show_status("Please paste the oauth_token value.", error=True)
            return
        self._captured_token = token
        self._paste_btn.setEnabled(False)
        self._show_status("Exchanging token…", error=False)
        threading.Thread(
            target=self._exchange_worker, args=(email, token), daemon=True
        ).start()

    def _exchange_worker(self, email, oauth_token):
        """Runs in a background thread: exchange oauth_token → master_token → login."""
        if not self._keep_sync:
            self._signals.result.emit(False, "Internal error: no sync object.")
            return

        resp = KeepSync.exchange_oauth_for_master(email, oauth_token)
        if not resp:
            self._signals.result.emit(
                False,
                "Token exchange failed. Please try again, or use "
                "'Paste token manually' with your email and token."
            )
            return

        master_token = resp.get("Token")
        # Google returns the canonical email in the response; prefer that
        # over whatever the user (didn't) type.
        resolved_email = resp.get("Email") or email or ""

        # Now authenticate with gkeepapi
        ok = self._keep_sync.login(resolved_email, master_token=master_token)
        if ok:
            self._signals.result.emit(True, resolved_email or "(unknown)")
        else:
            self._signals.result.emit(False, "Login failed after token exchange.")

    @Slot(bool, str)
    def _on_login_result(self, success, message):
        self._paste_btn.setEnabled(True)
        if success:
            self.signed_in = True
            self._email = message
            self._show_status(f"Signed in as {message}", error=False)
            QTimer.singleShot(600, self.accept)
        else:
            self._show_status(message, error=True)

    def _show_status(self, text, error=False):
        color = "#c62828" if error else "#1A73E8"
        self.status_label.setStyleSheet(
            f"color: {color}; font-size: 12px; padding: 8px;"
        )
        self.status_label.setText(text)
        self.status_label.show()

    def keyPressEvent(self, event):
        # Prevent Enter/Return from activating dialog buttons — let the browser handle it
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._browser.setFocus()
            return
        super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # Ensure browser gets focus when dialog appears
        QTimer.singleShot(100, self._browser.setFocus)

    def get_email(self):
        return getattr(self, "_email", self._manual_email.text().strip())


# ═══════════════════════════════════════════════════════════════════════
#  Note Manager Dialog
# ═══════════════════════════════════════════════════════════════════════

class NoteManagerDialog(QDialog):
    """Modal to manage which notes appear on the desktop."""

    note_deleted = Signal(str)  # emitted immediately on confirmed delete
    note_create_requested = Signal()  # emitted when user clicks "New note"
    visibility_changed = Signal(str, bool)  # note_id, is_visible (live)
    checklist_toggle_requested = Signal(str)  # note_id
    reorder_requested = Signal(str, str)      # note_id, action: top|up|down|bottom

    def __init__(self, notes: dict, visibility: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("KeepDesktop – Manage Notes")
        self.setMinimumSize(500, 480)
        self.result_visibility = dict(visibility)
        self._checkboxes: dict[str, QCheckBox] = {}
        self._rows: dict[str, dict] = {}  # note_id -> {frame, title_lbl, snip_lbl, pin_lbl}
        self._all_notes: dict = {}  # last-known notes (id -> KeepNote) for filtering
        self._search_text: str = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        header = QLabel("<b>Choose which notes to show on your desktop:</b>")
        layout.addWidget(header)

        # Search bar + New note button row
        search_row = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search title and contents…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search_changed)
        search_row.addWidget(self._search_edit, stretch=1)

        new_btn = QPushButton("✚  New note")
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(
            "QPushButton { background: #4285f4; color: white; border: none;"
            "              border-radius: 6px; font-size: 12px; font-weight: bold;"
            "              padding: 6px 14px; }"
            "QPushButton:hover { background: #3367d6; }"
        )
        new_btn.clicked.connect(self.note_create_requested.emit)
        search_row.addWidget(new_btn)
        layout.addLayout(search_row)

        # Quick-select row
        quick_row = QHBoxLayout()
        sel_all = QPushButton("Select All")
        sel_all.clicked.connect(lambda: self._set_all(True))
        quick_row.addWidget(sel_all)
        sel_none = QPushButton("Select None")
        sel_none.clicked.connect(lambda: self._set_all(False))
        quick_row.addWidget(sel_none)
        quick_row.addStretch()
        layout.addLayout(quick_row)

        # Scrollable note list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: 1px solid #ddd; border-radius: 6px; }")
        scroll_widget = QWidget()
        self._list_layout = QVBoxLayout(scroll_widget)
        self._list_layout.setContentsMargins(4, 4, 4, 4)
        self._list_layout.setSpacing(4)

        # Build initial rows
        self.refresh(notes, visibility)

        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll, stretch=1)

        # Bottom buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.setStyleSheet(
            "QPushButton { background: #4285f4; color: white; border: none;"
            "              border-radius: 6px; font-size: 13px; font-weight: bold;"
            "              padding: 6px 24px; }"
            "QPushButton:hover { background: #3367d6; }"
        )
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    # ── Row construction / refresh ─────────────────────────────────────

    def refresh(self, notes: dict, visibility: dict):
        """Update the row list to reflect the current notes/visibility.

        Existing rows are updated in place; new notes get a new row;
        removed notes are dropped. Called both at construction time and
        whenever notes change while the dialog is open.
        """
        # Remember the full set so search filtering can re-run later
        self._all_notes = dict(notes)

        # Merge visibility for any new notes (default visible)
        for nid in notes:
            if nid not in self.result_visibility:
                self.result_visibility[nid] = visibility.get(nid, True)

        # Apply current search filter
        query = self._search_text.strip().lower()
        if query:
            visible_notes = {
                nid: n for nid, n in notes.items()
                if query in (n.title or "").lower()
                or query in (n.text or "").lower()
            }
        else:
            visible_notes = dict(notes)

        # Drop rows whose notes are no longer in the (filtered) set
        for nid in list(self._rows.keys()):
            if nid not in visible_notes:
                self._remove_row_widget(nid)

        sorted_notes = sorted(
            visible_notes.values(),
            key=lambda n: (
                not n.pinned,
                # User-imposed order wins; fall back to Keep's sort_key.
                n.local_order if n.local_order else n.sort_key,
            ),
        )

        # Track desired order; we'll re-insert each frame in order
        # (cheaper than rebuilding from scratch and preserves widgets).
        # Remove the trailing stretch (if present) before re-inserting.
        self._strip_stretch()

        for idx, note in enumerate(sorted_notes):
            if note.id in self._rows:
                self._update_row(note)
                frame = self._rows[note.id]["frame"]
                # Re-insert at correct position
                self._list_layout.removeWidget(frame)
                self._list_layout.insertWidget(idx, frame)
            else:
                self._add_row(note, idx)

        self._list_layout.addStretch()

    def _on_search_changed(self, text: str):
        self._search_text = text
        if self._all_notes:
            self.refresh(self._all_notes, self.result_visibility)

    def _strip_stretch(self):
        # Stretch is always the last item if present
        count = self._list_layout.count()
        if count == 0:
            return
        last = self._list_layout.itemAt(count - 1)
        if last is not None and last.spacerItem() is not None:
            self._list_layout.takeAt(count - 1)

    def _add_row(self, note, position):
        row = QFrame()
        color = note.color_hex or "#FFF475"
        row.setStyleSheet(
            f"QFrame {{ background: {color}; border-radius: 6px;"
            f" border: 1px solid rgba(0,0,0,0.08); }}"
        )
        row.setMinimumHeight(52)
        row.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        row.customContextMenuRequested.connect(
            lambda pos, nid=note.id, w=row: self._show_row_menu(nid, w, pos)
        )

        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 6, 8, 6)
        row_layout.setSpacing(8)

        cb = QCheckBox()
        cb.setChecked(self.result_visibility.get(note.id, True))
        cb.stateChanged.connect(
            lambda state, nid=note.id: self._on_toggle(nid, state)
        )
        row_layout.addWidget(cb)
        self._checkboxes[note.id] = cb

        pin_lbl = QLabel("📌")
        pin_lbl.setStyleSheet("background: transparent; font-size: 12px;")
        pin_lbl.setFixedWidth(20)
        pin_lbl.setVisible(bool(note.pinned))
        row_layout.addWidget(pin_lbl)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)

        title_label = QLabel("")
        title_label.setStyleSheet("background: transparent; color: #333; font-size: 12px;")
        title_label.setMinimumWidth(0)
        title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text_col.addWidget(title_label)

        snip_label = QLabel("")
        snip_label.setStyleSheet(
            "background: transparent; color: #666; font-size: 11px;"
        )
        snip_label.setWordWrap(True)
        snip_label.setMaximumHeight(36)
        snip_label.setMinimumWidth(0)
        snip_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text_col.addWidget(snip_label)

        row_layout.addLayout(text_col, stretch=1)

        del_btn = QPushButton("🗑")
        del_btn.setFixedSize(28, 28)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setToolTip("Delete note")
        del_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; font-size: 14px; }"
            "QPushButton:hover { background: rgba(0,0,0,0.1); border-radius: 4px; }"
        )
        del_btn.clicked.connect(
            lambda checked, nid=note.id: self._delete_note(nid)
        )
        row_layout.addWidget(del_btn)

        self._list_layout.insertWidget(position, row)
        self._rows[note.id] = {
            "frame": row,
            "title_lbl": title_label,
            "snip_lbl": snip_label,
            "pin_lbl": pin_lbl,
        }
        self._update_row(note)

    def _update_row(self, note):
        row = self._rows.get(note.id)
        if not row:
            return
        color = note.color_hex or "#FFF475"
        row["frame"].setStyleSheet(
            f"QFrame {{ background: {color}; border-radius: 6px;"
            f" border: 1px solid rgba(0,0,0,0.08); }}"
        )
        row["pin_lbl"].setVisible(bool(note.pinned))

        title = note.title or ""
        snippet = (note.text[:120].replace("\n", " ").strip()) if note.text else ""

        if title:
            row["title_lbl"].setText(f"<b>{_escape_html(title)}</b>")
            row["snip_lbl"].setText(_escape_html(snippet))
            row["snip_lbl"].setVisible(bool(snippet))
        else:
            row["title_lbl"].setText(
                f"<i>{_escape_html(snippet[:50]) or '(empty note)'}</i>"
            )
            row["snip_lbl"].setText("")
            row["snip_lbl"].setVisible(False)

    def _remove_row_widget(self, note_id):
        """Remove just the row widget (used when filtering)."""
        row = self._rows.pop(note_id, None)
        if row:
            row["frame"].setParent(None)
            row["frame"].deleteLater()
        self._checkboxes.pop(note_id, None)

    def _remove_row(self, note_id):
        """Remove the row widget AND forget its visibility (used on delete)."""
        self._remove_row_widget(note_id)
        self.result_visibility.pop(note_id, None)

    def _on_toggle(self, note_id, state):
        is_visible = bool(state)
        self.result_visibility[note_id] = is_visible
        # Apply live so the user doesn't have to confirm
        self.visibility_changed.emit(note_id, is_visible)

    def _set_all(self, checked):
        for nid, cb in self._checkboxes.items():
            # Use blockSignals so we don't fire N individual events;
            # we'll emit our own per-id signals afterwards.
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)
            self.result_visibility[nid] = checked
            self.visibility_changed.emit(nid, checked)

    def set_checkbox(self, note_id: str, checked: bool):
        """Update a checkbox programmatically (e.g. when window is closed
        externally). Does NOT re-emit visibility_changed."""
        cb = self._checkboxes.get(note_id)
        if cb is None:
            return
        if cb.isChecked() == checked:
            return
        cb.blockSignals(True)
        cb.setChecked(checked)
        cb.blockSignals(False)
        self.result_visibility[note_id] = checked

    def _delete_note(self, note_id):
        reply = QMessageBox.question(
            self, "Delete Note",
            "Delete this note permanently?\n"
            "This will also trash it on Google Keep if synced.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.note_deleted.emit(note_id)
            self._remove_row(note_id)

    def _show_row_menu(self, note_id: str, row_widget, pos):
        note = self._all_notes.get(note_id)
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #ffffff; color: #222; border: 1px solid #ccc;"
            "        border-radius: 6px; padding: 4px; }"
            "QMenu::item { padding: 6px 16px; border-radius: 4px;"
            "              color: #222; }"
            "QMenu::item:selected { background: #e0e0e0; color: #000; }"
            "QMenu::separator { height: 1px; background: #ddd; margin: 4px 6px; }"
        )
        if note is not None:
            label = ("☑  Convert to plain text"
                     if getattr(note, "is_list", False)
                     else "☑  Convert to checklist")
        else:
            label = "☑  Toggle checklist"
        act_check = menu.addAction(label)
        menu.addSeparator()
        act_top = menu.addAction("⏫  Move to top")
        act_up = menu.addAction("⬆  Move up")
        act_down = menu.addAction("⬇  Move down")
        act_bottom = menu.addAction("⏬  Move to bottom")
        chosen = menu.exec(row_widget.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_check:
            self.checklist_toggle_requested.emit(note_id)
        elif chosen is act_top:
            self.reorder_requested.emit(note_id, "top")
        elif chosen is act_up:
            self.reorder_requested.emit(note_id, "up")
        elif chosen is act_down:
            self.reorder_requested.emit(note_id, "down")
        elif chosen is act_bottom:
            self.reorder_requested.emit(note_id, "bottom")


# ═══════════════════════════════════════════════════════════════════════
#  App Controller
# ═══════════════════════════════════════════════════════════════════════

class AppController(QObject):
    """Orchestrates note windows, tray icon, and Keep sync."""

    _remote_notes_ready = Signal(list)

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.sync = KeepSync()
        self.windows: dict[str, NoteWindow] = {}
        self._notes: dict[str, KeepNote] = {}
        self._dirty: set[str] = set()
        self._visibility = _load_visibility()
        self._show_in_taskbar = self.config.get("show_in_taskbar", False)

        # Marshal remote-note results from worker thread back to GUI thread
        self._remote_notes_ready.connect(self._apply_remote_notes)

        # Load persisted notes from disk
        self._load_notes_from_disk()

        self.tray = QSystemTrayIcon(_make_icon())
        self.tray.setToolTip("KeepDesktop")
        self.tray.activated.connect(self._on_tray_activated)
        self._build_tray_menu()
        self.tray.show()

        # Debounce timer for saving notes to disk
        self._save_debounce = QTimer()
        self._save_debounce.setSingleShot(True)
        self._save_debounce.setInterval(1000)
        self._save_debounce.timeout.connect(self._save_notes_to_disk)

        # Debounce timer for pushing edits to Keep
        self._sync_debounce = QTimer()
        self._sync_debounce.setSingleShot(True)
        self._sync_debounce.setInterval(5000)
        self._sync_debounce.timeout.connect(self._push_dirty_notes)

        self._sync_timer = QTimer()
        self._sync_timer.timeout.connect(self._periodic_sync)

        # Show persisted notes first
        self._show_persisted_notes()

        # Auto-login if we have a stored token
        email = self.config.get("email", "")
        if email and load_token():
            self._do_login(email)

        # First launch → login dialog; otherwise open notes
        if not self.sync.is_authenticated and not self.config.get("seen_login", False):
            self.config["seen_login"] = True
            save_config(self.config)
            QTimer.singleShot(300, self._first_launch_login)
        elif not self.windows and not self.sync.is_authenticated:
            self._new_note()

        # Background update check shortly after startup. Silent on failure
        # / when up-to-date; only prompts the user if a newer version is
        # genuinely available.
        self._updater = UpdateChecker()
        self._updater.update_available.connect(self._on_update_available)
        QTimer.singleShot(8000, self._updater.check_async)
        self._manual_update_in_progress = False

    def _build_tray_menu(self):
        menu = QMenu()

        new_action = QAction("✚  New note", menu)
        new_action.triggered.connect(self._new_note)
        menu.addAction(new_action)

        manage_action = QAction("📋  Manage notes…", menu)
        manage_action.triggered.connect(self._show_note_manager)
        menu.addAction(manage_action)

        show_action = QAction("👁  Bring notes to front", menu)
        show_action.triggered.connect(self._bring_to_front)
        menu.addAction(show_action)

        menu.addSeparator()

        sync_action = QAction("🔄  Sync now", menu)
        sync_action.triggered.connect(self._manual_sync)
        menu.addAction(sync_action)

        self._login_action = QAction("", menu)
        self._login_action.triggered.connect(self._on_login_action)
        menu.addAction(self._login_action)
        self._update_login_action_text()

        menu.addSeparator()

        self._taskbar_action = QAction("Show notes in taskbar", menu)
        self._taskbar_action.setCheckable(True)
        self._taskbar_action.setChecked(self._show_in_taskbar)
        self._taskbar_action.toggled.connect(self._toggle_taskbar)
        menu.addAction(self._taskbar_action)

        # Sub-option: only meaningful when notes are in the taskbar.
        # Default ON (group all notes together under one icon). Turning
        # it off gives each note its own taskbar entry.
        self._group_action = QAction("    \u2937 Group notes in taskbar", menu)
        self._group_action.setCheckable(True)
        self._group_action.setChecked(self.config.get("group_in_taskbar", True))
        self._group_action.setEnabled(self._show_in_taskbar)
        self._group_action.toggled.connect(self._toggle_taskbar_grouping)
        menu.addAction(self._group_action)

        autostart_action = QAction("Start with Windows", menu)
        autostart_action.setCheckable(True)
        autostart_action.setChecked(self.config.get("autostart", False))
        autostart_action.toggled.connect(self._toggle_autostart)
        menu.addAction(autostart_action)

        menu.addSeparator()

        about_action = QAction("ℹ  About KeepDesktop", menu)
        about_action.triggered.connect(self._show_about)
        menu.addAction(about_action)

        menu.addSeparator()

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)

    def _update_login_action_text(self):
        if self.sync.is_authenticated:
            email = self.config.get("email", "")
            self._login_action.setText(f"🔑  Sign out ({email})")
        else:
            self._login_action.setText("🔑  Sign in to Google Keep")

    def _on_login_action(self):
        if self.sync.is_authenticated:
            self._show_sign_out()
        else:
            self._show_login()

    # ── Note persistence ───────────────────────────────────────────────

    def _save_notes_to_disk(self):
        from config import NOTES_FILE
        data = {}
        for nid, note in self._notes.items():
            data[nid] = {
                "id": note.id,
                "title": note.title,
                "text": note.text,
                "html": note.html,
                "color_hex": note.color_hex,
                "pinned": note.pinned,
                "sort_key": note.sort_key,
                "is_list": note.is_list,
                "list_items": note.list_items,
                "local_order": note.local_order,
                "dark_mode": note.dark_mode,
            }
        save_json(NOTES_FILE, data)

    def _load_notes_from_disk(self):
        from config import NOTES_FILE
        data = load_json(NOTES_FILE, {})
        for nid, d in data.items():
            note = KeepNote(
                id=d["id"],
                title=d.get("title", ""),
                text=d.get("text", ""),
                html=d.get("html", ""),
                color_hex=d.get("color_hex", "#FFF475"),
                pinned=d.get("pinned", False),
                sort_key=d.get("sort_key", 0),
                is_list=d.get("is_list", False),
                list_items=d.get("list_items", []),
                local_order=d.get("local_order", 0),
                dark_mode=d.get("dark_mode", False),
            )
            self._notes[nid] = note
            if nid not in self._visibility:
                self._visibility[nid] = True

    def _show_persisted_notes(self):
        for note_id, note in self._notes.items():
            if self._visibility.get(note_id, True):
                win = self._create_window(note)
                self.windows[note_id] = win
                win.show()
        # Group/ungroup once all initial windows have HWNDs.
        QTimer.singleShot(0, self._apply_taskbar_grouping)

    # ── Note management ────────────────────────────────────────────────

    def _create_window(self, note: KeepNote) -> NoteWindow:
        win = NoteWindow(
            note_id=note.id,
            title=note.title,
            text=note.text,
            html=note.html,
            color_hex=note.color_hex,
            pinned=False,
            show_in_taskbar=self._show_in_taskbar,
            list_items=note.list_items if note.is_list else None,
            dark_mode=note.dark_mode,
        )
        win.note_changed.connect(self._on_note_changed)
        win.note_hidden.connect(self._on_note_hidden)
        win.note_deleted.connect(self._on_note_deleted)
        return win

    def _new_note(self):
        note_id = str(uuid.uuid4())
        note = KeepNote(id=note_id, color_hex="#FFF475")
        self._notes[note_id] = note
        self._visibility[note_id] = True
        _save_visibility(self._visibility)

        win = self._create_window(note)
        self.windows[note_id] = win
        win.show()

        if self.sync.is_authenticated:
            threading.Thread(target=self._push_new_note, args=(note,), daemon=True).start()
        QTimer.singleShot(0, self._apply_taskbar_grouping)

    def _push_new_note(self, note: KeepNote):
        result = self.sync.create_note(note.title, note.text, note.color_hex)
        if result:
            old_id = note.id
            note.id = result.id
            self._notes.pop(old_id, None)
            self._notes[result.id] = note
            was_vis = self._visibility.pop(old_id, True)
            self._visibility[result.id] = was_vis
            _save_visibility(self._visibility)
            # Migrate position data
            from config import load_positions, save_positions
            positions = load_positions()
            if old_id in positions:
                positions[result.id] = positions.pop(old_id)
                save_positions(positions)
            if old_id in self.windows:
                win = self.windows.pop(old_id)
                win.note_id = result.id
                self.windows[result.id] = win
            QTimer.singleShot(0, self._save_notes_to_disk)

    @Slot(str)
    def _on_note_changed(self, note_id):
        win = self.windows.get(note_id)
        note = self._notes.get(note_id)
        if win and note:
            note.text = win.get_text()
            note.title = win.get_title()
            note.color_hex = win.color_hex
            note.dark_mode = bool(getattr(win, "dark_mode", False))
            note.html = win.get_html()
            # The window may have been toggled in/out of checklist mode
            # by the user; pick that up so it persists.
            note.is_list = bool(getattr(win, "_is_list", False))
            if note.is_list:
                note.list_items = win.get_list_items()
            else:
                note.list_items = []
            self._dirty.add(note_id)
            # Debounce: save to disk in 1s, push to Keep in 5s
            self._save_debounce.start()
            if self.sync.is_authenticated:
                self._sync_debounce.start()
            self._refresh_manager_if_open()

    @Slot(str)
    def _on_note_hidden(self, note_id):
        win = self.windows.get(note_id)
        if win:
            win.save_geometry()
        self._visibility[note_id] = False
        _save_visibility(self._visibility)
        # If the manager is open, reflect this in its checkbox
        dlg = getattr(self, "_manager_dlg", None)
        if dlg is not None:
            dlg.set_checkbox(note_id, False)

    @Slot(str)
    def _on_note_deleted(self, note_id):
        win = self.windows.pop(note_id, None)
        note = self._notes.pop(note_id, None)
        self._visibility.pop(note_id, None)
        self._dirty.discard(note_id)
        _save_visibility(self._visibility)
        self._save_notes_to_disk()
        if win:
            win.close()
            win.deleteLater()
        if note and self.sync.is_authenticated:
            threading.Thread(target=self.sync.delete_note, args=(note_id,), daemon=True).start()
        self._refresh_manager_if_open()

    def _bring_to_front(self):
        for nid, win in self.windows.items():
            if self._visibility.get(nid, True) and win.isVisible():
                win.raise_()
                win.activateWindow()

    @Slot(str)
    def _toggle_note_checklist(self, note_id: str):
        """Convert the note between plain text and checklist mode."""
        note = self._notes.get(note_id)
        if note is None:
            return
        win = self.windows.get(note_id)
        if note.is_list:
            # Checklist -> plain text. Strip the prefixes.
            lines = []
            for item in note.list_items or []:
                lines.append(item.get("text", ""))
            note.is_list = False
            note.list_items = []
            note.text = "\n".join(lines)
            note.html = ""  # rich-text from checklist render is no longer valid
        else:
            # Plain text -> checklist.
            raw = note.text or ""
            items = [
                {"text": ln.strip(), "checked": False}
                for ln in raw.splitlines() if ln.strip()
            ]
            if not items:
                items = [{"text": "", "checked": False}]
            note.is_list = True
            note.list_items = items
            note.html = ""
        self._dirty.add(note_id)
        if win is not None:
            self._refresh_window(note_id)
        self._save_notes_to_disk()
        self._refresh_manager_if_open()
        if self.sync.is_authenticated:
            threading.Thread(
                target=self.sync.push_note, args=(note,), daemon=True
            ).start()

    @Slot(str, str)
    def _reorder_note(self, note_id: str, action: str):
        """Move ``note_id`` within the user-imposed order."""
        note = self._notes.get(note_id)
        if note is None:
            return
        # Build the current ordered list (mirrors NoteManagerDialog sort).
        ordered = sorted(
            self._notes.values(),
            key=lambda n: (
                not n.pinned,
                n.local_order if n.local_order else n.sort_key,
            ),
        )
        try:
            idx = ordered.index(note)
        except ValueError:
            return
        if action == "top":
            ordered.insert(0, ordered.pop(idx))
        elif action == "bottom":
            ordered.append(ordered.pop(idx))
        elif action == "up" and idx > 0:
            ordered[idx - 1], ordered[idx] = ordered[idx], ordered[idx - 1]
        elif action == "down" and idx < len(ordered) - 1:
            ordered[idx + 1], ordered[idx] = ordered[idx], ordered[idx + 1]
        else:
            return
        # Rewrite local_order densely so the new arrangement sticks.
        # Spacing of 10 leaves room for incremental tweaks later.
        for i, n in enumerate(ordered):
            n.local_order = (i + 1) * 10
        self._save_notes_to_disk()
        self._refresh_manager_if_open()

    # ── Note Manager ───────────────────────────────────────────────────

    def _show_note_manager(self):
        # If already open, just bring it forward instead of opening a duplicate
        existing = getattr(self, "_manager_dlg", None)
        if existing is not None:
            existing.refresh(self._notes, self._visibility)
            existing.raise_()
            existing.activateWindow()
            return
        dlg = NoteManagerDialog(self._notes, self._visibility)
        dlg.setModal(False)
        dlg.note_deleted.connect(self._on_note_deleted)
        dlg.note_create_requested.connect(self._new_note)
        dlg.visibility_changed.connect(self._on_manager_visibility_changed)
        dlg.checklist_toggle_requested.connect(self._toggle_note_checklist)
        dlg.reorder_requested.connect(self._reorder_note)
        dlg.accepted.connect(lambda: self._on_manager_accepted(dlg))
        dlg.rejected.connect(lambda: self._on_manager_closed())
        dlg.finished.connect(lambda _r: self._on_manager_closed())
        dlg.show()
        self._manager_dlg = dlg  # prevent GC

    def _on_manager_visibility_changed(self, note_id: str, is_visible: bool):
        """Apply a single checkbox change live (no Apply button needed)."""
        self._visibility[note_id] = is_visible
        _save_visibility(self._visibility)
        note = self._notes.get(note_id)
        if note is None:
            return
        win = self.windows.get(note_id)
        if is_visible:
            if win is None:
                win = self._create_window(note)
                self.windows[note_id] = win
            win.show()
            win.raise_()
        else:
            if win is not None:
                win.save_geometry()
                win.hide()

    def _on_manager_accepted(self, dlg):
        self._visibility = dlg.result_visibility
        _save_visibility(self._visibility)
        self._apply_visibility()

    def _on_manager_closed(self):
        self._manager_dlg = None

    def _refresh_manager_if_open(self):
        dlg = getattr(self, "_manager_dlg", None)
        if dlg is not None:
            dlg.refresh(self._notes, self._visibility)

    def _apply_visibility(self):
        for note_id, note in self._notes.items():
            should_show = self._visibility.get(note_id, True)
            win = self.windows.get(note_id)
            if should_show:
                if win is None:
                    win = self._create_window(note)
                    self.windows[note_id] = win
                win.show()
                win.raise_()
            else:
                if win is not None:
                    win.save_geometry()
                    win.hide()

    # ── Taskbar toggle ─────────────────────────────────────────────────

    def _toggle_taskbar(self, show):
        self._show_in_taskbar = show
        self.config["show_in_taskbar"] = show
        save_config(self.config)
        for win in self.windows.values():
            win.set_taskbar_visible(show)
        # Apply grouping immediately for the new visibility state.
        self._apply_taskbar_grouping()
        # Group toggle is only meaningful when notes are in the taskbar.
        if hasattr(self, "_group_action"):
            self._group_action.setEnabled(show)

    def _toggle_taskbar_grouping(self, group: bool):
        self.config["group_in_taskbar"] = group
        save_config(self.config)
        self._apply_taskbar_grouping()

    def _apply_taskbar_grouping(self):
        """Set per-window AppUserModelID so Windows groups (or doesn't)
        the notes in the taskbar.

        When grouping is ON: every note shares the process-level app id
        and they all stack under one icon.
        When grouping is OFF: each note gets a unique app id so Windows
        treats it as its own pinned program.
        """
        from config import APP_NAME, APP_VERSION
        group = self.config.get("group_in_taskbar", True)
        base = f"LukeCGG.{APP_NAME}.{APP_VERSION}"
        for nid, win in self.windows.items():
            try:
                if group:
                    _set_window_app_id(win, base)
                else:
                    _set_window_app_id(win, f"{base}.note.{nid[:12]}")
            except Exception as exc:  # noqa: BLE001
                log.warning("Couldn't set per-window app id: %s", exc)

    # ── Sign in / Sign out ─────────────────────────────────────────────

    def _show_sign_out(self):
        dlg = QDialog()
        dlg.setWindowTitle("KeepDesktop – Sign Out")
        dlg.setFixedWidth(380)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 20, 20, 20)

        email = self.config.get("email", "Unknown")
        lay.addWidget(QLabel(f"Signed in as <b>{_escape_html(email)}</b>"))
        lay.addWidget(QLabel("Are you sure you want to sign out?"))

        clear_cb = QCheckBox("Clear all local notes (remove note data)")
        lay.addWidget(clear_cb)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)
        signout_btn = QPushButton("Sign Out")
        signout_btn.setStyleSheet(
            "QPushButton { background: #d93025; color: white; border: none;"
            " border-radius: 6px; padding: 6px 20px; font-weight: bold; }"
            "QPushButton:hover { background: #c5221f; }"
        )
        signout_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(signout_btn)
        lay.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._do_sign_out(clear_cb.isChecked())

    def _do_sign_out(self, clear_notes=False):
        # Remove stored token
        token_path = os.path.join(DATA_DIR, "keep_token.dat")
        if os.path.exists(token_path):
            os.remove(token_path)

        self.sync._authenticated = False
        self._sync_timer.stop()
        self._sync_debounce.stop()

        if clear_notes:
            for win in list(self.windows.values()):
                win.close()
                win.deleteLater()
            self.windows.clear()
            self._notes.clear()
            self._visibility.clear()
            self._dirty.clear()
            _save_visibility({})
            self._save_notes_to_disk()
            # Remove positions
            from config import POSITIONS_FILE
            if os.path.exists(POSITIONS_FILE):
                os.remove(POSITIONS_FILE)

        self._update_login_action_text()

    # ── Sync ───────────────────────────────────────────────────────────

    def _do_login(self, email, password=None, master_token=None):
        ok = self.sync.login(email, password=password, master_token=master_token)
        if ok:
            self.config["email"] = email
            self.config["sync_enabled"] = True
            save_config(self.config)
            self._full_sync()
            self._sync_timer.start(SYNC_INTERVAL_MS)
            self._update_login_action_text()
            # Open the manager so the user can pick which notes to show.
            QTimer.singleShot(500, self._show_note_manager_if_not_open)
        return ok

    def _show_note_manager_if_not_open(self):
        if not self._manager_dialog_open():
            self._show_note_manager()

    def _first_launch_login(self):
        dlg = LoginDialog(first_launch=True)
        dlg.set_keep_sync(self.sync)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.signed_in:
            email = dlg.get_email()
            if email:
                self.config["email"] = email
            self.config["sync_enabled"] = True
            save_config(self.config)
            self._full_sync()
            self._sync_timer.start(SYNC_INTERVAL_MS)
            self._update_login_action_text()
            QTimer.singleShot(500, self._show_note_manager_if_not_open)
        else:
            if not self.windows:
                self._new_note()
            self.tray.showMessage(
                "KeepDesktop is running!",
                "Right-click the tray icon to manage notes or sign in to Google Keep.",
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )

    def _show_login(self):
        dlg = LoginDialog(first_launch=False)
        dlg.set_keep_sync(self.sync)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.signed_in:
            email = dlg.get_email()
            if email:
                self.config["email"] = email
            self.config["sync_enabled"] = True
            save_config(self.config)
            self._full_sync()
            self._sync_timer.start(SYNC_INTERVAL_MS)
            self._update_login_action_text()
            QMessageBox.information(None, "KeepDesktop", "Signed in! Your notes are syncing.")
            QTimer.singleShot(500, self._show_note_manager_if_not_open)

    def _full_sync(self):
        if not self.sync.is_authenticated:
            return
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self):
        # Push dirty notes first
        for note_id in list(self._dirty):
            note = self._notes.get(note_id)
            if note:
                self.sync.push_note(note)
            self._dirty.discard(note_id)

        # Pull remote notes
        remote_notes = self.sync.fetch_notes()
        # Hand back to main thread via signal (QTimer.singleShot from a
        # worker thread is unreliable / silently dropped).
        log.info("Sync worker emitting %d remote notes to main thread",
                 len(remote_notes))
        self._remote_notes_ready.emit(remote_notes)

    def _apply_remote_notes(self, remote_notes: list):
        """Apply remote note data on the main thread."""
        log.info("Applying %d remote notes (local dirty=%s)",
                 len(remote_notes), [d[:8] for d in self._dirty])
        new_remote_ids: list[str] = []
        for rn in remote_notes:
            existing = self._notes.get(rn.id)
            if existing is None:
                log.info("New remote note %s title=%r", rn.id[:8], rn.title)
                self._notes[rn.id] = rn
                if rn.id not in self._visibility:
                    # New notes coming in from another device are hidden
                    # by default — the user opts in via the Note Manager.
                    self._visibility[rn.id] = False
                    _save_visibility(self._visibility)
                    new_remote_ids.append(rn.id)
                if self._visibility.get(rn.id, False):
                    self._add_window_for_note(rn)
            else:
                # Update sort/pin info always
                existing.sort_key = rn.sort_key
                existing.pinned = rn.pinned
                # Only update content if not locally dirty
                if rn.id in self._dirty:
                    log.info("Skipping remote update for %s (locally dirty)", rn.id[:8])
                    continue
                # Detect what changed for diagnostics
                changes = []
                text_changed = (existing.text != rn.text)
                title_changed = (existing.title != rn.title)
                color_changed = (existing.color_hex != rn.color_hex)
                list_changed = (existing.is_list != rn.is_list
                                or existing.list_items != rn.list_items)
                if color_changed:
                    changes.append(f"color {existing.color_hex}->{rn.color_hex}")
                if title_changed:
                    changes.append("title")
                if text_changed:
                    changes.append("text")
                if existing.is_list != rn.is_list:
                    changes.append(f"is_list {existing.is_list}->{rn.is_list}")
                if existing.list_items != rn.list_items:
                    changes.append(
                        f"list_items ({len(existing.list_items)}->{len(rn.list_items)})"
                    )
                if changes:
                    log.info("Note %s changed: %s", rn.id[:8], ", ".join(changes))

                # Always overwrite local data with Keep data EXCEPT html.
                # Keep doesn't store rich-text formatting, so we keep the
                # local html unless the plain text actually changed (in
                # which case the local html is stale and would re-introduce
                # outdated content).
                existing.text = rn.text
                existing.title = rn.title
                if text_changed:
                    existing.html = ""  # text changed remotely — stale html
                if color_changed:
                    # User changed colour on the web → drop our local
                    # dark-mode override so we follow the new colour.
                    existing.dark_mode = False
                existing.color_hex = rn.color_hex
                existing.list_items = rn.list_items
                existing.is_list = rn.is_list
                # Only refresh the window if user is NOT actively typing,
                # AND something visible actually changed. A no-op refresh
                # would call setPlainText and silently wipe rich-text
                # formatting the user just applied.
                win = self.windows.get(rn.id)
                visible_changed = (text_changed or title_changed
                                   or color_changed or list_changed)
                if win and win.text_edit.hasFocus():
                    log.info("Skipping refresh for %s (user editing)", rn.id[:8])
                elif win and visible_changed:
                    log.info("Refreshing window %s", rn.id[:8])
                    self._refresh_window(rn.id)

        # Remove notes that no longer exist on Keep (deleted on another
        # device). Guard against an empty remote list — that usually
        # means the fetch failed, and we don't want to wipe everything.
        if remote_notes:
            remote_ids = {rn.id for rn in remote_notes}
            for nid in list(self._notes.keys()):
                if nid in remote_ids:
                    continue
                if nid in self._dirty:
                    # Local edit still being pushed — don't remove.
                    continue
                log.info("Note %s removed remotely; deleting locally", nid[:8])
                self._notes.pop(nid, None)
                self._visibility.pop(nid, None)
                win = self.windows.pop(nid, None)
                if win:
                    win.close()
                    win.deleteLater()
            _save_visibility(self._visibility)

        self._save_notes_to_disk()
        self._refresh_manager_if_open()

        # If new remote notes arrived (e.g. created on another device)
        # and no notes are currently visible, surface the manager so the
        # user can opt them in. We only auto-open it for *new* notes
        # that the user hasn't seen before.
        if new_remote_ids and not self._manager_dialog_open():
            visible_count = sum(1 for v in self._visibility.values() if v)
            if visible_count == 0:
                QTimer.singleShot(0, self._show_note_manager)

    def _manager_dialog_open(self) -> bool:
        dlg = getattr(self, "_manager_dlg", None)
        return dlg is not None and dlg.isVisible()

    def _push_dirty_notes(self):
        if not self.sync.is_authenticated or not self._dirty:
            return
        threading.Thread(target=self._push_dirty_worker, daemon=True).start()

    def _push_dirty_worker(self):
        for note_id in list(self._dirty):
            note = self._notes.get(note_id)
            if note:
                self.sync.push_note(note)
            self._dirty.discard(note_id)

    def _add_window_for_note(self, note: KeepNote):
        if note.id in self.windows:
            return
        win = self._create_window(note)
        self.windows[note.id] = win
        if self._visibility.get(note.id, True):
            win.show()
        QTimer.singleShot(0, self._apply_taskbar_grouping)

    def _refresh_window(self, note_id: str):
        win = self.windows.get(note_id)
        note = self._notes.get(note_id)
        if win and note:
            win._is_list = note.is_list
            win.set_dark_mode(note.dark_mode)
            if note.is_list and note.list_items:
                win._set_checklist_html(note.list_items)
            elif note.html:
                # Preserve any local rich-text formatting the user has set.
                win.set_html(note.html)
            else:
                win.set_text(note.text)
            win.set_title(note.title)
            win._apply_color(note.color_hex)

    def _manual_sync(self):
        if not self.sync.is_authenticated:
            QMessageBox.information(
                None, "KeepDesktop",
                "Not signed in. Use 'Sign in to Google Keep' first.",
            )
            return
        log.info("Manual sync requested")
        self.tray.showMessage(
            "KeepDesktop", "Syncing with Google Keep\u2026",
            QSystemTrayIcon.MessageIcon.Information, 2000,
        )
        self._full_sync()

    def _periodic_sync(self):
        if self.sync.is_authenticated:
            log.info("Periodic sync tick")
            self._full_sync()

    # ── Settings ───────────────────────────────────────────────────────

    def _toggle_autostart(self, enabled):
        self.config["autostart"] = enabled
        save_config(self.config)
        set_autostart(enabled)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_note_manager()

    # ── Updates ────────────────────────────────────────────────────────

    def _on_update_available(self, info):
        prompt_and_install(None, info)

    def _manual_update_check(self):
        if self._manual_update_in_progress:
            return
        self._manual_update_in_progress = True
        self.tray.showMessage(
            APP_NAME, "Checking for updates\u2026",
            QSystemTrayIcon.MessageIcon.Information, 1500,
        )
        checker = UpdateChecker()
        checker.update_available.connect(self._on_manual_update_available)
        checker.no_update.connect(self._on_manual_no_update)
        checker.error.connect(self._on_manual_update_error)
        # Hold a reference so it isn't GC'd before the worker finishes
        self._manual_checker = checker
        checker.check_async()

    def _on_manual_update_available(self, info):
        self._manual_update_in_progress = False
        prompt_and_install(None, info)

    def _on_manual_no_update(self):
        self._manual_update_in_progress = False
        QMessageBox.information(
            None, APP_NAME,
            f"You're running the latest version ({APP_VERSION})."
        )

    def _on_manual_update_error(self, msg):
        self._manual_update_in_progress = False
        QMessageBox.warning(
            None, APP_NAME,
            f"Couldn't check for updates:\n{msg}"
        )

    def _open_log_file(self):
        log_path = os.path.join(DATA_DIR, "keepdesktop.log")
        if not os.path.exists(log_path):
            QMessageBox.information(
                None, APP_NAME,
                f"No log file yet at:\n{log_path}",
            )
            return
        try:
            os.startfile(log_path)  # Windows: opens in default text editor
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                None, APP_NAME,
                f"Couldn't open log file:\n{exc}\n\nIt's at:\n{log_path}",
            )

    def _show_about(self):
        dlg = QDialog()
        dlg.setWindowTitle(f"About {APP_NAME}")
        dlg.setMinimumWidth(420)
        v = QVBoxLayout(dlg)
        v.setSpacing(10)

        body = QLabel(
            f"<h3>{APP_NAME} {APP_VERSION}</h3>"
            "<p>Google Keep on your Windows desktop as floating sticky notes.</p>"
            "<p>Licensed under the "
            "<a href='https://www.gnu.org/licenses/agpl-3.0.html'>"
            "GNU Affero General Public License v3.0</a>.</p>"
            f"<p><a href='https://github.com/{GITHUB_REPO}'>"
            f"github.com/{GITHUB_REPO}</a></p>"
        )
        body.setOpenExternalLinks(True)
        body.setWordWrap(True)
        v.addWidget(body)

        btn_row = QHBoxLayout()
        check_btn = QPushButton("⬆  Check for updates…")
        check_btn.clicked.connect(lambda: (dlg.accept(), self._manual_update_check()))
        btn_row.addWidget(check_btn)

        log_btn = QPushButton("📄  Open log file")
        log_btn.clicked.connect(self._open_log_file)
        btn_row.addWidget(log_btn)

        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        v.addLayout(btn_row)

        dlg.exec()

    def _quit(self):
        # Save all window geometry
        for win in self.windows.values():
            win.save_geometry()
        # Save notes to disk
        for nid, note in self._notes.items():
            win = self.windows.get(nid)
            if win:
                note.text = win.get_text()
                note.title = win.get_title()
                note.color_hex = win.color_hex
                note.dark_mode = bool(getattr(win, "dark_mode", False))
                note.html = win.get_html()
        self._save_notes_to_disk()
        # Push dirty notes if authenticated
        if self.sync.is_authenticated and self._dirty:
            for note_id in list(self._dirty):
                note = self._notes.get(note_id)
                if note:
                    self.sync.push_note(note)
        QApplication.quit()
