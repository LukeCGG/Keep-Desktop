"""System-tray icon and main application controller."""

import logging
import os
import time
import uuid
import threading
from functools import partial

from PySide6.QtCore import Qt, QTimer, Slot, Signal, QObject, QUrl
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction, QFont
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QDialog,
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QFrame, QScrollArea, QWidget, QCheckBox, QSizePolicy,
    QTabWidget, QComboBox,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile

from config import (
    load_config, save_config, set_autostart, is_autostart_enabled, save_token,
    SYNC_INTERVAL_MS, DATA_DIR, load_token,
    load_json, save_json, APP_NAME, APP_VERSION, GITHUB_REPO,
)
from app_icon import make_icon as _make_icon
import note_window
from note_window import NoteWindow
from keep_sync import KeepSync, KeepNote
from updater import UpdateChecker, prompt_and_install

log = logging.getLogger(__name__)

VISIBILITY_FILE = os.path.join(DATA_DIR, "visibility.json")

# How many periodic sync ticks between full resyncs. A full resync is
# the only thing that reliably surfaces a formatting-only web edit (see
# _periodic_sync), so run it often enough to feel responsive while any
# note is on screen, and rarely when nothing is.
_FULL_RESYNC_TICKS_ACTIVE = 4      # ~2 minutes at the 30s interval
_FULL_RESYNC_TICKS_IDLE = 20       # ~10 minutes


def _set_window_app_id(window, app_id: str) -> None:
    """Set per-window AppUserModelID via IPropertyStore so Windows can
    group (or refuse to group) this HWND in the taskbar.

    No-op on non-Windows. Failures raise OSError so the caller can log
    the HRESULT.

    IMPORTANT: changing the AppUserModelID of a window that is already
    visible has NO effect on its existing taskbar button. The window
    must be hidden, the property changed, and the window shown again.
    """
    import sys
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    hwnd = int(window.winId())

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32),
                    ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16),
                    ("Data4", ctypes.c_ubyte * 8)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]

    # PROPVARIANT is 24 bytes on x64. We only need the LPWSTR variant;
    # use a c_void_p for the union slot so InitPropVariantFromString /
    # PropVariantClear can manage the buffer via CoTaskMemAlloc/Free.
    class PROPVARIANT(ctypes.Structure):
        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("wReserved1", ctypes.c_ushort),
            ("wReserved2", ctypes.c_ushort),
            ("wReserved3", ctypes.c_ushort),
            ("data1", ctypes.c_void_p),   # union slot 1 (pwszVal etc.)
            ("data2", ctypes.c_void_p),   # union slot 2 (padding)
        ]

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

    # Build a VT_LPWSTR PROPVARIANT manually instead of relying on
    # propsys!InitPropVariantFromString — that export is missing on
    # some Windows builds / ctypes views (we hit "function not found"
    # in the wild). The shape is well-defined: vt=31 (VT_LPWSTR) and
    # the union slot holds a pointer to a wide string allocated with
    # CoTaskMemAlloc. PropVariantClear will CoTaskMemFree it for us.
    VT_LPWSTR = 31
    CoTaskMemAlloc = ole32.CoTaskMemAlloc
    CoTaskMemAlloc.argtypes = [ctypes.c_size_t]
    CoTaskMemAlloc.restype = ctypes.c_void_p

    PropVariantClear = ole32.PropVariantClear
    PropVariantClear.argtypes = [ctypes.POINTER(PROPVARIANT)]
    PropVariantClear.restype = ctypes.c_long

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
        raise OSError(
            f"SHGetPropertyStoreForWindow hwnd=0x{hwnd:X} "
            f"hr=0x{hr & 0xFFFFFFFF:08X}"
        )

    # IPropertyStore vtable: 0=QueryInterface 1=AddRef 2=Release
    # 3=GetCount 4=GetAt 5=GetValue 6=SetValue 7=Commit
    vtbl_ptr = ctypes.cast(pStore, ctypes.POINTER(ctypes.c_void_p))[0]
    vtbl = ctypes.cast(vtbl_ptr, ctypes.POINTER(ctypes.c_void_p))
    SetValue = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p,
        ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT),
    )(vtbl[6])
    Commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[7])
    Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])

    pv = PROPVARIANT()
    try:
        # Allocate a CoTaskMem-owned wide-char copy of app_id (with NUL),
        # then populate the PROPVARIANT manually.
        nbytes = (len(app_id) + 1) * ctypes.sizeof(ctypes.c_wchar)
        buf = CoTaskMemAlloc(nbytes)
        if not buf:
            raise OSError("CoTaskMemAlloc failed")
        ctypes.memmove(
            buf,
            ctypes.create_unicode_buffer(app_id),
            nbytes,
        )
        pv.vt = VT_LPWSTR
        pv.data1 = buf
        hr = SetValue(pStore, ctypes.byref(PKEY_AppUserModel_ID),
                      ctypes.byref(pv))
        if hr != 0:
            raise OSError(
                f"IPropertyStore::SetValue hr=0x{hr & 0xFFFFFFFF:08X}"
            )
        hr = Commit(pStore)
        if hr != 0:
            raise OSError(
                f"IPropertyStore::Commit hr=0x{hr & 0xFFFFFFFF:08X}"
            )
        log.debug("AppUserModelID set on hwnd=0x%X -> %r", hwnd, app_id)
    finally:
        PropVariantClear(ctypes.byref(pv))
        Release(pStore)


def _force_foreground(window) -> None:
    """Force ``window`` into the Windows foreground.

    SetForegroundWindow is normally blocked when our process doesn't
    own the active foreground (which is the case when called from a
    tray-icon callback). We combine three workarounds, each of which
    independently bypasses the restriction in different scenarios:

      1. Synthesise an ALT keystroke. Windows treats any pending input
         from our thread as an "active" signal and lifts the lock.
      2. AttachThreadInput to the foreground thread's input queue, so
         our SetForegroundWindow looks like it came from that thread.
      3. SystemParametersInfo SPI_SETFOREGROUNDLOCKTIMEOUT = 0 around
         the call, then restore the previous timeout.

    No-op on non-Windows or if the underlying APIs fail.
    """
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(window.winId())

        # Workaround 1: ALT keystroke — unblocks SetForegroundWindow.
        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

        # Workaround 3: relax the foreground-lock timeout so other
        # processes can't race us. We don't bother restoring it —
        # leaving it at 0 only affects our own process and Windows
        # resets it on the next foreground change anyway.
        SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
        SPIF_SENDCHANGE = 0x0002
        user32.SystemParametersInfoW(
            SPI_SETFOREGROUNDLOCKTIMEOUT, 0, 0, SPIF_SENDCHANGE,
        )

        # Workaround 2: attach input queues, then promote the window.
        fg = user32.GetForegroundWindow()
        our_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_tid and fg_tid != our_tid:
            attached = bool(user32.AttachThreadInput(fg_tid, our_tid, True))
        try:
            # If minimised, restore first — SetForegroundWindow on a
            # minimised window leaves it minimised.
            SW_RESTORE = 9
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
            user32.SetActiveWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_tid, our_tid, False)
    except Exception:  # noqa: BLE001
        pass


def _load_visibility() -> dict:
    return load_json(VISIBILITY_FILE, {})


def _save_visibility(vis: dict):
    save_json(VISIBILITY_FILE, vis)


def _escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Step granularity for new sort values — matches Keep web's typical
# spacing. Big enough that the midpoint between two adjacent values
# always has room for further inserts without collision.
_REORDER_STEP = 1 << 22  # ~4M


def _compute_new_sort_value(ordered: list, note) -> int:
    """Pick a new ``sortValue`` for ``note`` so Keep web mirrors the
    desktop order in ``ordered`` (left-to-right == top-to-bottom).

    The bug we're guarding against here: ``ordered`` is keyed by
    ``local_order``, which can disagree with each note's ``sort_key``
    after a push failure or sync race. So the immediate visual
    neighbour in ``ordered`` is NOT always the right anchor — for
    edge moves (top / bottom) we need to dominate / be dominated by
    every other note's sort_key, not just the neighbour. Otherwise
    Keep web only sees a one-step move when the user asked for
    "move to top" or "move to bottom".

    Pure function for testability; no side effects.
    """
    def _sv(n):
        try:
            return int(getattr(n, "sort_key", 0) or 0)
        except (TypeError, ValueError):
            return 0

    try:
        new_idx = ordered.index(note)
    except ValueError:
        return _sv(note) or _REORDER_STEP

    above = ordered[new_idx - 1] if new_idx > 0 else None
    below = ordered[new_idx + 1] if new_idx < len(ordered) - 1 else None
    other_svs = [_sv(n) for n in ordered if n is not note]

    if above is None and below is not None:
        # "Move to top" — must beat every other sort_key on the wire,
        # not just the visual neighbour.
        return (max(other_svs) if other_svs else 0) + _REORDER_STEP
    if below is None and above is not None:
        # "Move to bottom" — symmetric.
        return (min(other_svs) if other_svs else 0) - _REORDER_STEP
    if above is not None and below is not None:
        a, b = _sv(above), _sv(below)
        if a == b:
            return a - 1  # tiebreak
        new_sv = (a + b) // 2
        if new_sv == a or new_sv == b:
            return a - 1
        return new_sv
    # Single-note list: keep whatever sort_key it had.
    return _sv(note) or _REORDER_STEP


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

class _NoWheelComboBox(QComboBox):
    """QComboBox that ignores mouse-wheel scrolling.

    Qt's default QComboBox changes selection on every wheel tick even
    without the dropdown open — idly scrolling a Settings page while
    the cursor happens to pass over this combo silently changes the
    value. Used for the font-scale picker, where each change triggers
    a disk write and a full re-render of every open note window."""

    def wheelEvent(self, event):
        event.ignore()


class NoteManagerDialog(QDialog):
    """Modal to manage which notes appear on the desktop."""

    note_deleted = Signal(str)  # emitted immediately on confirmed delete
    note_create_requested = Signal()  # emitted when user clicks "New note"
    visibility_changed = Signal(str, bool)  # note_id, is_visible (live)
    checklist_toggle_requested = Signal(str)  # note_id
    pin_toggle_requested = Signal(str)        # note_id (Keep is_pinned)
    reorder_requested = Signal(str, str)      # note_id, action: top|up|down|bottom
    font_scale_changed = Signal(float)        # new global font scale

    # Presets offered in the Settings tab's font-scale picker.
    _FONT_SCALE_PRESETS = [0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]

    def __init__(self, notes: dict, visibility: dict, font_scale: float = 1.0, parent=None):
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

        tabs = QTabWidget(self)
        layout.addWidget(tabs, stretch=1)

        notes_page = QWidget()
        notes_layout = QVBoxLayout(notes_page)
        notes_layout.setSpacing(8)

        header = QLabel("<b>Choose which notes to show on your desktop:</b>")
        notes_layout.addWidget(header)

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
        notes_layout.addLayout(search_row)

        # Quick-select row
        quick_row = QHBoxLayout()
        sel_all = QPushButton("Select All")
        sel_all.clicked.connect(lambda: self._set_all(True))
        quick_row.addWidget(sel_all)
        sel_none = QPushButton("Select None")
        sel_none.clicked.connect(lambda: self._set_all(False))
        quick_row.addWidget(sel_none)
        quick_row.addStretch()
        notes_layout.addLayout(quick_row)

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
        notes_layout.addWidget(scroll, stretch=1)

        tabs.addTab(notes_page, "Notes")
        tabs.addTab(self._build_settings_page(font_scale), "Settings")

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

    # ── Settings tab ─────────────────────────────────────────────────

    def _build_settings_page(self, font_scale: float) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setSpacing(8)
        page_layout.setContentsMargins(12, 12, 12, 12)

        label = QLabel("<b>Text size</b>")
        page_layout.addWidget(label)
        hint = QLabel(
            "Scales the body, heading, and toolbar text in every note "
            "window — turn it up on a 4K display, or down to fit more "
            "on screen. Applies immediately to open notes."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 11px;")
        page_layout.addWidget(hint)

        row = QHBoxLayout()
        row.addWidget(QLabel("Note text size:"))
        self._font_scale_combo = _NoWheelComboBox()
        closest = min(
            self._FONT_SCALE_PRESETS, key=lambda p: abs(p - font_scale)
        )
        for preset in self._FONT_SCALE_PRESETS:
            self._font_scale_combo.addItem(f"{round(preset * 100)}%", preset)
        self._font_scale_combo.setCurrentIndex(
            self._FONT_SCALE_PRESETS.index(closest)
        )
        self._font_scale_combo.currentIndexChanged.connect(
            self._on_font_scale_selected
        )
        row.addWidget(self._font_scale_combo)
        row.addStretch()
        page_layout.addLayout(row)
        page_layout.addStretch()
        return page

    def _on_font_scale_selected(self, index: int):
        scale = self._font_scale_combo.itemData(index)
        if scale is not None:
            self.font_scale_changed.emit(float(scale))

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
                # User-imposed order wins (small ascending integers).
                # Otherwise fall back to Keep's sortValue, descending
                # — so notes appear in the same order as keep.google.com.
                (0, n.local_order) if n.local_order
                else (1, -int(n.sort_key or 0)),
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
        if note is not None:
            pin_label = ("📌  Unpin from top of Keep"
                         if getattr(note, "pinned", False)
                         else "📌  Pin to top of Keep")
        else:
            pin_label = "📌  Toggle Keep pin"
        act_pin = menu.addAction(pin_label)
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
        elif chosen is act_pin:
            self.pin_toggle_requested.emit(note_id)
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
    _note_merged_during_push = Signal(str)  # note_id

    def __init__(self):
        super().__init__()
        self.config = load_config()
        # Apply the saved text-size preference before any NoteWindow is
        # built so freshly-opened notes render at the right scale from
        # the start (not just ones open when the setting later changes).
        note_window.set_font_scale(self.config.get("font_scale", 1.0))
        # Reconcile the autostart flag with reality. The Inno Setup
        # installer creates a Startup-folder shortcut directly without
        # touching our config, so a fresh install would otherwise show
        # the in-app toggle as OFF even though Windows IS configured to
        # launch us at login. Source of truth is the .lnk on disk.
        real_autostart = is_autostart_enabled()
        if self.config.get("autostart", False) != real_autostart:
            log.info(
                "autostart: reconciling config (%s) with disk state (%s)",
                self.config.get("autostart", False), real_autostart,
            )
            self.config["autostart"] = real_autostart
            save_config(self.config)
        # Pick sync backend. v2 (keep_protocol) supports docs-nestedModel
        # formatting and won't corrupt Keep web's state. v1 (gkeepapi)
        # is kept as a fallback for one release; see config.py.
        if self.config.get("keep_protocol_v2", True):
            try:
                from keep_sync_v2 import KeepSyncV2
                self.sync = KeepSyncV2()
                log.info("using KeepSyncV2 (keep_protocol)")
            except Exception as exc:  # noqa: BLE001
                log.error("KeepSyncV2 init failed, falling back to v1: %s", exc)
                self.sync = KeepSync()
        else:
            self.sync = KeepSync()
            log.info("using KeepSync v1 (gkeepapi)")
        self.windows: dict[str, NoteWindow] = {}
        self._notes: dict[str, KeepNote] = {}
        self._dirty: set[str] = set()
        # Notes whose CACHE (self._notes[id].text/.html/.styled_doc)
        # was just updated -- by push_note's merge result, or by a
        # remote pull the user was too busy to have applied to their
        # widget immediately -- but whose WIDGET hasn't been confirmed
        # refreshed to match yet (the refresh is deferred: queued via
        # a cross-thread signal for the push case, or retried on a
        # timer until idle for the busy-pull case). Included in
        # hold_baseline_for alongside self._dirty and busy_ids so
        # KeepSyncV2's fetch_notes() doesn't advance _base_text/
        # _base_doc to the server's latest before the widget has
        # actually caught up -- a note in this gap is neither dirty
        # nor busy, so without tracking it explicitly here, the very
        # next periodic sync cycle's fetch would treat the widget's
        # (still-stale) content as if it were the confirmed baseline,
        # and the next local edit would silently revert the
        # just-merged/just-pulled content right back off the server.
        self._pending_widget_refresh: set[str] = set()
        # Per-note record of the StyledDoc actually rendered into the
        # window. decide_merge needs a local structured view to spot a
        # formatting-only remote change (plain text can't reveal one),
        # and note.styled_doc can't serve: _on_note_changed clears it on
        # every body edit, so between a local edit and the next pull it
        # is simply absent -- and a formatting-only web change landing
        # in that gap got written into the cache but never rendered,
        # after which no later pull ever saw a difference to refresh
        # for. What the window is SHOWING is the honest thing to
        # compare against, and it survives local edits.
        self._rendered_doc: dict = {}
        # Periodic-sync counter driving the adaptive full-resync
        # cadence in _periodic_sync.
        self._tick_count = 0
        # Re-entrancy guard shared between _push_dirty_notes (the
        # debounced post-edit push) and _sync_worker's own push phase
        # -- deliberately separate from _sync_running, which guards
        # _full_sync's entire push+pull cycle. See _push_dirty_notes'
        # own comment for why conflating the two starves the periodic
        # pull (the only path that brings in web edits) during active
        # typing, when the debounced push fires far more often than
        # the 30s periodic tick.
        self._push_running = False
        # Notes the user just deleted locally for which the threaded
        # `sync.delete_note` trash POST hasn't yet completed. We must
        # ignore these in `_apply_remote_notes`, otherwise a periodic
        # sync that races ahead of the trash request will see the note
        # still untrashed on the server and re-add it locally — which
        # then reappears in the manager after relaunch.
        self._pending_deletes: set[str] = set()
        # Notes that have gone missing from a single sync response.
        # We require them to be missing for ``STRIKES_TO_DELETE``
        # consecutive syncs before treating it as a real remote
        # deletion — otherwise a transient sync that returns fewer
        # notes than usual (server hiccup, push response that only
        # echoes the changed node, etc.) wipes notes that quietly
        # come back next cycle, producing visible churn in the
        # manager dialog. Maps note_id -> consecutive miss count.
        self._missing_strikes: dict[str, int] = {}
        # Per-note timestamp of the last user edit. Used to decide
        # whether a focused note window is "actively being edited" or
        # just sitting open — if it's been idle long enough we let
        # remote refreshes through so web-side changes appear without
        # the user having to close and reopen the note.
        self._last_edit_time: dict[str, float] = {}
        # How long after the last keystroke we still treat a note as
        # "being edited" and refuse to overwrite it from a remote pull.
        self._edit_idle_seconds = 15.0
        self._visibility = _load_visibility()
        self._show_in_taskbar = self.config.get("show_in_taskbar", False)

        # Marshal remote-note results from worker thread back to GUI thread
        self._remote_notes_ready.connect(self._apply_remote_notes)
        self._note_merged_during_push.connect(self._refresh_window_when_idle)

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
        # Read the live disk state so the menu always reflects whether
        # the Startup-folder shortcut actually exists, even if the
        # installer or another app touched it behind our back.
        autostart_action.setChecked(is_autostart_enabled())
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
        from keep_protocol.nested_model import styled_doc_to_dict
        data = {}
        for nid, note in self._notes.items():
            entry = {
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
            # Persisting styled_doc means the very first sync after
            # launch has a real baseline to structurally compare
            # against, instead of "no styled_doc yet" being
            # indistinguishable from "we just pushed our own edit" --
            # see decide_merge's local_doc-is-None handling in
            # sync_merge.py, and styled_doc_to_dict's own docstring.
            sdoc = getattr(note, "styled_doc", None)
            if sdoc is not None:
                entry["styled_doc"] = styled_doc_to_dict(sdoc)
            data[nid] = entry
        save_json(NOTES_FILE, data)

    def _load_notes_from_disk(self):
        from config import NOTES_FILE
        from keep_protocol.nested_model import styled_doc_from_dict
        data = load_json(NOTES_FILE, {})
        log.info("Loading %d notes from disk cache (%s)",
                 len(data), NOTES_FILE)
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
            sdoc = styled_doc_from_dict(d.get("styled_doc"))
            if sdoc is not None:
                note.styled_doc = sdoc  # type: ignore[attr-defined]
            self._notes[nid] = note
            if nid not in self._visibility:
                self._visibility[nid] = True

    def _show_persisted_notes(self):
        for note_id, note in self._notes.items():
            if self._visibility.get(note_id, True):
                win = self._create_window(note)
                self.windows[note_id] = win
                # Show first so Qt has a chance to attach window icon
                # to the HWND. Then _apply_taskbar_grouping below will
                # hide/show again to register the taskbar button under
                # the right AppUserModelID — and the icon comes along
                # for the ride. Doing the AppID set BEFORE first show
                # left the taskbar button with no icon.
                win.show()
        # Now that all initial windows have HWNDs and icons, apply the
        # grouping. This hides + reshows each window which is the only
        # supported way to change a window's taskbar AppID at runtime.
        QTimer.singleShot(0, self._apply_taskbar_grouping)

    # ── Note management ────────────────────────────────────────────────

    def _create_window(self, note: KeepNote) -> NoteWindow:
        from config import get_position
        pos = get_position(note.id) or {}
        was_pinned = bool(pos.get("pinned", False))
        win = NoteWindow(
            note_id=note.id,
            title=note.title,
            text=note.text,
            html=note.html,
            color_hex=note.color_hex,
            pinned=was_pinned,
            show_in_taskbar=self._show_in_taskbar,
            list_items=note.list_items if note.is_list else None,
            dark_mode=note.dark_mode,
        )
        # If the v2 fetch attached a fresh StyledDoc, render via the
        # cursor API for faithful empty-line preservation.
        sdoc = getattr(note, "styled_doc", None)
        if sdoc is not None and not note.is_list:
            win._syncing = True
            try:
                win.text_edit.set_styled_doc(sdoc)
            finally:
                # Deferred clear — the highlighter's late textChanged
                # must not read as a user edit (see end_sync_render).
                win.end_sync_render()
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
        QTimer.singleShot(0, lambda: self._regroup_window(note_id))

        if self.sync.is_authenticated:
            threading.Thread(target=self._push_new_note, args=(note,), daemon=True).start()

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
            text_before, html_before = note.text, note.html
            note.text = win.get_text()
            note.title = win.get_title()
            note.color_hex = win.color_hex
            note.dark_mode = bool(getattr(win, "dark_mode", False))
            note.html = win.get_html()
            # A local edit to the BODY invalidates any cached server-
            # decoded StyledDoc — drop it so a refresh doesn't
            # overwrite the user's in-flight typing with stale render.
            # Gated on the body actually changing (not e.g. a title-
            # only or colour-only edit, which also routes through
            # this same handler): styled_doc absence is also how
            # sync_merge.py's decide_merge tells "local just echoed
            # its own push back" apart from "a genuine concurrent web
            # restyle arrived" (see its local_doc-is-None branch) --
            # clearing it for edits that never touched the body made
            # that heuristic wrong far more often than intended,
            # silently swallowing a concurrent web restyle whenever it
            # landed in the same cycle as an unrelated title/colour
            # edit.
            if (note.text != text_before or note.html != html_before) and hasattr(note, "styled_doc"):
                try:
                    delattr(note, "styled_doc")
                except AttributeError:
                    pass
            # The window may have been toggled in/out of checklist mode
            # by the user; pick that up so it persists.
            note.is_list = bool(getattr(win, "_is_list", False))
            if note.is_list:
                note.list_items = win.get_list_items()
            else:
                note.list_items = []
            self._dirty.add(note_id)
            self._last_edit_time[note_id] = time.monotonic()
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
        self._rendered_doc.pop(note_id, None)
        self._visibility.pop(note_id, None)
        self._dirty.discard(note_id)
        _save_visibility(self._visibility)
        self._save_notes_to_disk()
        if win:
            win.close()
            win.deleteLater()
        if note and self.sync.is_authenticated:
            self._pending_deletes.add(note_id)
            threading.Thread(
                target=self._delete_worker, args=(note_id,), daemon=True
            ).start()
        self._refresh_manager_if_open()

    def _delete_worker(self, note_id: str):
        try:
            self.sync.delete_note(note_id)
        except Exception:  # noqa: BLE001
            log.exception("delete_note crashed for %s", note_id[:8])
        finally:
            # Clear the guard regardless — if the trash POST genuinely
            # failed the user can re-delete; we don't want to wedge the
            # note in a never-syncs state.
            self._pending_deletes.discard(note_id)

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

    def _toggle_note_pin(self, note_id: str):
        """Toggle the Keep ``isPinned`` flag and sync."""
        note = self._notes.get(note_id)
        if note is None:
            return
        note.pinned = not bool(getattr(note, "pinned", False))
        self._save_notes_to_disk()
        self._refresh_manager_if_open()
        if self.sync.is_authenticated:
            new_pinned = bool(note.pinned)
            threading.Thread(
                target=lambda: self.sync.push_metadata(
                    note, is_pinned=new_pinned,
                ),
                daemon=True,
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
                (0, n.local_order) if n.local_order
                else (1, -int(n.sort_key or 0)),
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
        new_sv = _compute_new_sort_value(ordered, note)
        note.sort_key = new_sv
        self._save_notes_to_disk()
        self._refresh_manager_if_open()
        if self.sync.is_authenticated:
            threading.Thread(
                target=lambda: self.sync.push_metadata(
                    note, sort_value=new_sv,
                ),
                daemon=True,
            ).start()

    # ── Note Manager ───────────────────────────────────────────────────

    def _show_note_manager(self):
        # If already open, just bring it forward instead of opening a duplicate
        existing = getattr(self, "_manager_dlg", None)
        if existing is not None:
            try:
                existing.refresh(self._notes, self._visibility)
                if existing.isMinimized():
                    existing.setWindowState(
                        existing.windowState() & ~Qt.WindowState.WindowMinimized
                        | Qt.WindowState.WindowActive
                    )
                if not existing.isVisible():
                    existing.show()
                existing.raise_()
                existing.activateWindow()
                _force_foreground(existing)
                # Sometimes Windows promotes a different window between
                # our show() and SetForegroundWindow(); a deferred retry
                # after the next event-loop tick catches that race.
                QTimer.singleShot(50, lambda: _force_foreground(existing))
                return
            except RuntimeError:
                # Dialog was deleted from under us — fall through to recreate.
                self._manager_dlg = None
        dlg = NoteManagerDialog(
            self._notes, self._visibility,
            font_scale=self.config.get("font_scale", 1.0),
        )
        dlg.setModal(False)
        dlg.note_deleted.connect(self._on_note_deleted)
        dlg.note_create_requested.connect(self._new_note)
        dlg.visibility_changed.connect(self._on_manager_visibility_changed)
        dlg.checklist_toggle_requested.connect(self._toggle_note_checklist)
        dlg.pin_toggle_requested.connect(self._toggle_note_pin)
        dlg.reorder_requested.connect(self._reorder_note)
        dlg.font_scale_changed.connect(self._on_font_scale_changed)
        dlg.accepted.connect(lambda: self._on_manager_accepted(dlg))
        dlg.rejected.connect(lambda: self._on_manager_closed())
        dlg.finished.connect(lambda _r: self._on_manager_closed())
        dlg.show()
        self._manager_dlg = dlg  # prevent GC
        dlg.raise_()
        dlg.activateWindow()
        _force_foreground(dlg)
        QTimer.singleShot(50, lambda: _force_foreground(dlg))

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

    def _on_font_scale_changed(self, scale: float):
        self.config["font_scale"] = scale
        save_config(self.config)
        note_window.set_font_scale(scale)
        for win in self.windows.values():
            win.refresh_font_scale()

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
                # If the window already exists & is visible, leave its
                # z-order alone — we don't want closing the manager to
                # punch every note window to the front.
                elif not win.isVisible():
                    win.show()
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

    def _grouping_app_id_for(self, note_id: str) -> str:
        from config import APP_NAME, APP_VERSION
        base = f"LukeCGG.{APP_NAME}.{APP_VERSION}"
        if self.config.get("group_in_taskbar", True):
            return base
        # Stable per-note id so taskbar pins survive restarts.
        return f"{base}.note.{note_id[:12]}"

    def _regroup_window(self, note_id: str) -> None:
        """Force-rebuild one window's taskbar button under the current
        AppUserModelID.

        Just calling hide()/setAppID()/show() is NOT enough on Windows:
        the shell remembers the HWND's original taskbar group and
        keeps the button in that slot. The reliable workaround is to
        destroy the underlying native HWND so the next show() creates
        a fresh one — Windows treats it as a new window and registers
        it under whatever AppUserModelID is current at that moment.

        We also need to restore geometry, because destroy() drops it.
        """
        win = self.windows.get(note_id)
        if win is None:
            return
        was_visible = win.isVisible()
        # Snapshot state we need to restore after recreating the HWND.
        geom = win.geometry()
        # Save any in-flight geometry so a follow-on save doesn't lose it.
        try:
            win.save_geometry()
        except Exception:  # noqa: BLE001
            pass

        new_app_id = self._grouping_app_id_for(note_id)

        if was_visible:
            win.hide()
        # Drop the native window. Qt will lazily recreate it on show().
        # create() forces it now so we have a winId() to attach the
        # AppUserModelID to BEFORE the window becomes visible (and is
        # registered with the taskbar).
        try:
            win.destroy(destroyWindow=True, destroySubWindows=False)
            win.create()
        except Exception as exc:  # noqa: BLE001
            log.warning("HWND recreate failed for %s: %s", note_id, exc)

        try:
            _set_window_app_id(win, new_app_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Couldn't set per-window app id for %s: %s",
                        note_id, exc)

        if was_visible:
            win.setGeometry(geom)
            win.show()
            # Re-assert AppID after show — some Qt builds reset window
            # properties during the platform-window re-attach.
            try:
                _set_window_app_id(win, new_app_id)
            except Exception:  # noqa: BLE001
                pass

    def _apply_taskbar_grouping(self):
        """Rebuild the taskbar button for every visible note under the
        current grouping setting.
        """
        for nid in list(self.windows.keys()):
            self._regroup_window(nid)

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
            # Don't auto-open the manager on launch — it's noisy when
            # KeepDesktop is configured to start with Windows. The user
            # can open it from the tray any time.
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
            # Don't auto-open the manager on first install — the tray
            # message below tells the user where to find it.
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

    def _full_sync(self, force_resync: bool = False):
        """Run one push+pull cycle in the background.

        `force_resync` promotes the pull to a full resync. The periodic
        tick leaves it False (an incremental delta is all it needs);
        user-triggered "Sync now" passes True, because the whole point
        of pressing it is "I know something changed over there, go and
        get it" — and an incremental pull can only ever return what the
        cursor says is new, which is exactly nothing when the cursor
        has already moved past the change.
        """
        if not self.sync.is_authenticated:
            return
        # Re-entrancy guard: a slow sync (large pull, retry, etc) can
        # still be running when the next timer tick fires. Stacking
        # them double-pushes dirty notes and can race the cache.
        if getattr(self, "_sync_running", False):
            log.debug("_full_sync: previous sync still running; skipping")
            return
        self._sync_running = True
        # _is_note_busy() reads QWidget.hasFocus(), which must stay on
        # the GUI thread — compute it here, before handing off to the
        # background thread, rather than inside _sync_worker.
        busy_ids = {nid for nid in self.windows if self._is_note_busy(nid)}
        threading.Thread(target=self._sync_worker,
                         args=(busy_ids, force_resync), daemon=True).start()

    def _sync_worker(self, busy_ids: set, force_resync: bool = False):
        try:
            # Push dirty notes first — via the shared, race-safe
            # helper (see _push_one_dirty_note) so a note edited while
            # ITS push is in flight isn't silently marked clean here
            # too. This loop used to have its own inline copy of the
            # push logic, predating that fix, so the exact race it
            # closes was still fully reachable every 30s via this
            # periodic/manual sync path even after the debounce path
            # (_push_dirty_worker) was fixed.
            #
            # _push_running (NOT _sync_running) guards just this push
            # phase against _push_dirty_notes' own background thread
            # touching the same self._dirty set concurrently — see
            # _push_running's own comment for why this must be a
            # SEPARATE flag from _sync_running, scoped to end before
            # the pull below starts.
            self._push_running = True
            try:
                for note_id in list(self._dirty):
                    self._push_one_dirty_note(note_id)
            finally:
                self._push_running = False

            # Pull remote notes. Notes that are still dirty (push
            # above didn't clear them), whose window is busy, or whose
            # cache was just updated but hasn't reached the widget yet
            # (self._pending_widget_refresh -- a merge-during-push
            # result queued for the main thread, or a busy-deferred
            # remote pull still waiting for idle) won't have this
            # fetch's content applied to their widget right away (see
            # _apply_remote_notes) — hold their push-merge baseline
            # back too, or a genuine concurrent web restyle would look
            # identical to "no remote change" once the editor finally
            # catches up, and the next local push would silently
            # overwrite it right back off the server.
            try:
                remote_notes = self.sync.fetch_notes(
                    force_resync=force_resync,
                    hold_baseline_for=(
                        self._dirty | busy_ids | self._pending_widget_refresh
                    ),
                )
            except Exception:  # noqa: BLE001
                log.exception("fetch_notes crashed; skipping pull this cycle")
                remote_notes = []
            # Hand back to main thread via signal (QTimer.singleShot from a
            # worker thread is unreliable / silently dropped).
            log.info("Sync worker emitting %d remote notes to main thread",
                     len(remote_notes))
            self._remote_notes_ready.emit(remote_notes)
        finally:
            self._sync_running = False

    def _apply_remote_notes(self, remote_notes: list):
        """Apply remote note data on the main thread."""
        from sync_merge import decide_merge, MergeAction
        log.info("Applying %d remote notes (local dirty=%s)",
                 len(remote_notes), [d[:8] for d in self._dirty])
        new_remote_ids: list[str] = []
        for rn in remote_notes:
            if rn.id in self._pending_deletes:
                # Local delete still in flight — don't resurrect.
                continue
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
                # Update sort/pin info always — these aren't user-edit
                # surfaces so they can't conflict with typing.
                existing.sort_key = rn.sort_key
                existing.pinned = rn.pinned
                # Compute merge decision with the user's editing state.
                idle_for = time.monotonic() - self._last_edit_time.get(rn.id, 0.0)
                user_busy = self._is_note_busy(rn.id)
                decision = decide_merge(
                    local=existing,
                    remote=rn,
                    is_dirty=(rn.id in self._dirty),
                    user_busy=user_busy,
                    local_rendered_doc=self._rendered_doc.get(rn.id),
                )

                if decision.action is MergeAction.SKIP_DIRTY:
                    log.info("Skipping remote update for %s (locally dirty)", rn.id[:8])
                    continue

                # Diagnostics
                changes = []
                if decision.color_changed:
                    changes.append(f"color {existing.color_hex}->{rn.color_hex}")
                if decision.title_changed:
                    changes.append("title")
                if decision.text_changed:
                    changes.append("text")
                if existing.is_list != rn.is_list:
                    changes.append(f"is_list {existing.is_list}->{rn.is_list}")
                if existing.list_items != rn.list_items:
                    changes.append(
                        f"list_items ({len(existing.list_items)}->{len(rn.list_items)})"
                    )
                if decision.html_changed:
                    changes.append("formatting")
                if changes:
                    log.info("Note %s changed: %s", rn.id[:8], ", ".join(changes))

                if decision.action is MergeAction.PRESERVE_LOCAL_BODY:
                    log.warning(
                        "Note %s: remote text empty but local non-empty "
                        "(%d chars). Skipping body overwrite.",
                        rn.id[:8], len(existing.text or ""),
                    )
                    # Still adopt safe metadata (title, colour, pin).
                    # dark_mode is a per-device rendering preference, not
                    # part of Keep's data model — never let a remote
                    # colour change override it (see ADOPT_REMOTE below).
                    existing.title = rn.title
                    existing.color_hex = rn.color_hex
                    continue

                # ADOPT_REMOTE — full body update.
                existing.text = rn.text
                if decision.text_changed:
                    existing.html = ""  # text changed remotely — stale html
                # Only adopt remote html when text is non-empty (or both are empty).
                if rn.html and rn.text:
                    existing.html = rn.html
                # Carry the freshly-decoded StyledDoc across so the
                # cursor renderer (preserves empty paragraphs) is
                # used on refresh. Without this, _refresh_window
                # falls back to note.html and HTML round-trip
                # corrupts blank lines.
                rn_doc = getattr(rn, "styled_doc", None)
                if rn_doc is not None:
                    existing.styled_doc = rn_doc  # type: ignore[attr-defined]
                elif hasattr(existing, "styled_doc"):
                    # Remote no longer has a snapshot doc (e.g. legacy
                    # note); drop the stale one.
                    try:
                        delattr(existing, "styled_doc")
                    except AttributeError:
                        pass
                existing.title = rn.title
                # dark_mode is a per-device rendering preference — it's
                # never sent to Keep (the wire colour is always the light
                # hex, see config.KEEP_COLORS_DARK), so a remote colour
                # change (from the web, or from this same note open on
                # another computer) must not touch it. Forcing it off
                # here used to reset every other open instance of this
                # note back to its light variant on the very next pull.
                existing.color_hex = rn.color_hex
                existing.list_items = rn.list_items
                existing.is_list = rn.is_list
                if user_busy:
                    # The cache (existing.*) is already updated above,
                    # but the open window can't be safely re-rendered
                    # over active typing. Without a retry, this remote
                    # change is never shown: decide_merge() compares
                    # against `existing`, which now already matches
                    # `rn`, so no future pull ever sees a difference to
                    # refresh for again — and if the user's next
                    # keystroke re-derives note.text/.html from the
                    # (still stale) widget before that, their push
                    # would silently overwrite this remote change right
                    # back off the server. Retry once idle instead of
                    # dropping it.
                    log.info(
                        "Deferring refresh for %s (user editing, idle %.1fs)",
                        rn.id[:8], idle_for,
                    )
                    self._pending_widget_refresh.add(rn.id)
                    self._refresh_window_when_idle(rn.id)
                elif decision.refresh_window:
                    log.info("Refreshing window %s", rn.id[:8])
                    self._refresh_window(rn.id)

        # Remove notes that no longer exist on Keep (deleted on another
        # device). Guard against an empty remote list — that usually
        # means the fetch failed, and we don't want to wipe everything.
        # Also defer single-cycle absences (a metadata push response or
        # incremental sync sometimes echoes fewer nodes than usual)
        # via ``_missing_strikes`` so we don't flicker notes out of the
        # manager only to immediately re-add them next cycle.
        STRIKES_TO_DELETE = 2
        if remote_notes:
            remote_ids = {rn.id for rn in remote_notes}
            for nid in list(self._notes.keys()):
                if nid in remote_ids:
                    self._missing_strikes.pop(nid, None)
                    continue
                if nid in self._dirty:
                    # Local edit still being pushed — don't remove.
                    continue
                # Real Keep server ids are ``timestamp.user`` and
                # always contain a dot. Anything else is a local UUID
                # that hasn't been promoted to a server id yet (e.g.
                # _push_new_note hasn't completed) — those won't ever
                # show up in remote_ids by construction.
                if "." not in nid:
                    continue
                strikes = self._missing_strikes.get(nid, 0) + 1
                self._missing_strikes[nid] = strikes
                if strikes < STRIKES_TO_DELETE:
                    log.info(
                        "Note %s missing from sync (strike %d/%d); "
                        "deferring delete",
                        nid[:8], strikes, STRIKES_TO_DELETE,
                    )
                    continue
                log.info("Note %s removed remotely; deleting locally", nid[:8])
                self._missing_strikes.pop(nid, None)
                self._notes.pop(nid, None)
                self._rendered_doc.pop(nid, None)
                self._visibility.pop(nid, None)
                win = self.windows.pop(nid, None)
                if win:
                    win.close()
                    win.deleteLater()
            _save_visibility(self._visibility)

        self._save_notes_to_disk()
        self._refresh_manager_if_open()

        # We deliberately do NOT auto-open the manager when remote sync
        # brings in new notes — it would pop up unexpectedly during
        # background syncs and on every launch. The tray notification
        # below covers the discoverability case for the first sync.

    def _manager_dialog_open(self) -> bool:
        dlg = getattr(self, "_manager_dlg", None)
        return dlg is not None and dlg.isVisible()

    def _push_dirty_notes(self):
        if not self.sync.is_authenticated or not self._dirty:
            return
        # Dedicated re-entrancy guard (_push_running), deliberately
        # NOT _sync_running: this (the debounced post-edit push) and
        # _sync_worker's own "push dirty notes first" step both call
        # the same _push_one_dirty_note for whatever's currently in
        # self._dirty, from two INDEPENDENT background threads with no
        # mutual exclusion between them -- redundant concurrent pushes
        # of the same note_id would race on app_controller-level
        # bookkeeping (dirty-clearing, _pending_widget_refresh,
        # _note_merged_during_push emission) that KeepSyncV2's own
        # internal lock doesn't cover.
        #
        # _sync_running was tried here first and reverted: _full_sync
        # guards its ENTIRE cycle (push AND pull) with it, so shared
        # would mean a debounced push in flight -- which fires on
        # every edit during active typing, far more often than the
        # 30s periodic tick -- blocks that periodic cycle's pull
        # phase too, starving fetch_notes() during exactly the
        # "actively editing while a web change also landed" scenario
        # this whole push/pull split exists to handle correctly. This
        # flag only ever gates the push phase (see _sync_worker's own
        # scoped use of it), never the pull.
        if getattr(self, "_push_running", False):
            log.debug("_push_dirty_notes: a push is already running; skipping")
            return
        self._push_running = True
        threading.Thread(target=self._push_dirty_worker, daemon=True).start()

    def _push_dirty_worker(self):
        try:
            for note_id in list(self._dirty):
                self._push_one_dirty_note(note_id)
        finally:
            self._push_running = False

    def _push_one_dirty_note(self, note_id: str):
        """Push a single dirty note, race-safely. Caller must be on a
        background thread (this blocks on network I/O via push_note).

        Shared by both push triggers — the debounced post-edit push
        AND the periodic/manual full-sync's own "push dirty notes
        first" step — so the mid-push race protection below applies
        regardless of which one happens to fire. It used to live only
        in the debounce path; the periodic-sync path had its own
        unguarded copy of this same loop, so the exact race this
        method exists to prevent was still fully reachable every 30s
        via that path even after the debounce path was fixed.
        """
        note = self._notes.get(note_id)
        if not note:
            self._dirty.discard(note_id)
            return
        # push_note's 3-way / format-preserving merge paths can
        # rewrite note.text/.html in place to fold in a concurrent
        # remote edit — from THIS (background) thread. The open
        # window has no idea that happened and keeps showing only
        # what the user typed; worse, the user's very next keystroke
        # re-derives note.text from the (stale) widget and clobbers
        # the merge before it's ever seen. Detect the mutation and
        # ask the GUI thread to refresh the window so the merge
        # actually reaches the screen.
        text_before, html_before = note.text, note.html
        # push_note is network-bound (it does its own full resync
        # before writing) and can easily take several seconds, all on
        # this background thread, while _on_note_changed keeps
        # mutating this SAME note object from the main thread on
        # every keystroke. If the user types more mid-push, what
        # we're about to send doesn't include that edit.
        push_started_at = time.monotonic()
        try:
            ok = self.sync.push_note(note)
        except Exception:  # noqa: BLE001
            log.exception("push_note crashed for %s", note_id[:8])
            ok = False
        if ok is not False:
            edited_during_push = (
                self._last_edit_time.get(note_id, 0.0) > push_started_at
            )
            if edited_during_push:
                # Don't clear dirty — the edit that landed while this
                # push was in flight was never sent. Without this
                # check we'd mark the note clean anyway, and since
                # nothing else ever re-dirties a note on its own, that
                # edit would silently never sync: no further push
                # would carry it, and the next periodic pull (no
                # longer blocked by SKIP_DIRTY, since dirty was
                # cleared) would overwrite the cached text with the
                # server's older copy. Leave it dirty so the next push
                # cycle sends the CURRENT widget content instead.
                log.info(
                    "v2 push: %s edited again mid-push; keeping "
                    "dirty for another push cycle", note_id[:8],
                )
            else:
                self._dirty.discard(note_id)
            if note.text != text_before or note.html != html_before:
                # Mark BEFORE emitting: the signal is queued to the
                # main thread's event loop and may not be processed
                # for a while, but a periodic sync's fetch_notes()
                # (running on ANOTHER background thread) could fire in
                # that gap -- see _pending_widget_refresh's own
                # comment for why that must not advance this note's
                # baseline yet.
                self._pending_widget_refresh.add(note_id)
                self._note_merged_during_push.emit(note_id)

    def _refresh_window_when_idle(self, note_id: str):
        """Render self._notes[note_id]'s current content into its open
        window, retrying later if the user is busy right now.

        Shared by two callers that both adopt new content into the
        note cache (push_note's merge result, or a remote pull) without
        being able to touch the widget immediately: stomping active
        typing with a snapshot computed before the user's last few
        keystrokes would lose those keystrokes. The cache is already
        correct by the time this is called — whenever the refresh
        actually lands is still correct, just possibly delayed.
        """
        win = self.windows.get(note_id)
        note = self._notes.get(note_id)
        if not win or not note:
            return
        if self._is_note_busy(note_id):
            QTimer.singleShot(
                int(self._edit_idle_seconds * 1000),
                lambda: self._refresh_window_when_idle(note_id),
            )
            return
        self._refresh_window(note_id)

    def _add_window_for_note(self, note: KeepNote):
        if note.id in self.windows:
            return
        win = self._create_window(note)
        self.windows[note.id] = win
        if self._visibility.get(note.id, True):
            win.show()
            # show()-then-grouping so the icon attaches first.
            QTimer.singleShot(0, lambda: self._regroup_window(note.id))

    def _refresh_window(self, note_id: str):
        win = self.windows.get(note_id)
        note = self._notes.get(note_id)
        if win and note:
            # set_styled_doc/set_html/set_text below all clear() and
            # rebuild the document from scratch, which resets the
            # scrollbar to the top — jarring if the user was reading
            # further down when a sync (even a no-op one that just
            # re-confirms unchanged content) happened to land.
            # Restore the scroll position afterward; deferred one
            # event-loop tick so the widget's layout — and thus its
            # scrollbar range — has settled onto the new content
            # first (a synchronous restore can land before Qt has
            # recomputed the range for the new document length).
            scrollbar = win.text_edit.verticalScrollBar()
            saved_scroll = scrollbar.value()
            if saved_scroll:
                def _restore_scroll(sb=scrollbar, v=saved_scroll):
                    # The note (or its window) can be closed/deleted
                    # in the gap between scheduling this and the next
                    # event-loop tick actually running it (e.g. the
                    # user closes the note right after a remote pull
                    # triggers this refresh) -- sb would then be a
                    # dangling wrapper around an already-destroyed Qt
                    # C++ scrollbar, and touching it raises
                    # "wrapped C/C++ object ... has been deleted"
                    # from inside a timer callback.
                    try:
                        sb.setValue(v)
                    except RuntimeError:
                        pass
                QTimer.singleShot(0, _restore_scroll)
            win._is_list = note.is_list
            win.set_dark_mode(note.dark_mode)
            if note.is_list and note.list_items:
                # Real ChecklistEditor path — reflect remote items.
                win.set_list_items(note.list_items)
                win.text_edit.setVisible(False)
                win.checklist_editor.setVisible(True)
                win.fmt_toolbar.setVisible(False)
            elif getattr(note, "styled_doc", None) is not None:
                # Cursor-based render preserves empty paragraphs and
                # avoids HTML round-trip artefacts (NBSP injection,
                # line ping-pong with Keep web).
                win._syncing = True
                try:
                    win.text_edit.set_styled_doc(note.styled_doc)
                finally:
                    # Deferred clear — the highlighter's late
                    # textChanged must not read as a user edit.
                    win.end_sync_render()
                self._rendered_doc[note_id] = note.styled_doc
                win.checklist_editor.setVisible(False)
                win.text_edit.setVisible(True)
                win.fmt_toolbar.setVisible(True)
            elif note.html:
                # Preserve any local rich-text formatting the user has set.
                win.set_html(note.html)
                win.checklist_editor.setVisible(False)
                win.text_edit.setVisible(True)
                win.fmt_toolbar.setVisible(True)
            else:
                win.set_text(note.text)
                win.checklist_editor.setVisible(False)
                win.text_edit.setVisible(True)
                win.fmt_toolbar.setVisible(True)
            win.set_title(note.title)
            win._apply_color(note.color_hex)
            # The widget now actually reflects the cache -- see
            # _pending_widget_refresh's own comment for why a periodic
            # sync's fetch_notes() must not advance this note's
            # baseline before this point. Cleared at the END (not the
            # top of this method) so an exception partway through the
            # render above leaves the note correctly still marked
            # pending rather than wrongly treated as caught up.
            self._pending_widget_refresh.discard(note_id)

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
        # User-triggered Sync Now is also a request to realign with
        # Keep — drop any locally-imposed order so the manager mirrors
        # whatever order Keep currently reports (its sortValue field).
        # Per-note moves the user makes after this will re-establish a
        # local override via _reorder_note (which now also pushes the
        # new sortValue back to Keep).
        cleared = 0
        for n in self._notes.values():
            if n.local_order:
                n.local_order = 0
                cleared += 1
        if cleared:
            log.info("Manual sync: cleared local_order on %d notes", cleared)
            self._save_notes_to_disk()
            self._refresh_manager_if_open()
        self._full_sync(force_resync=True)

    def _periodic_sync(self):
        """Timer-driven sync.

        Most ticks are incremental — that is all a text edit needs. But
        a FORMATTING-ONLY change on the web (bold, italic, a heading)
        moves no text at all: it lives entirely in the note's
        docs-nestedModel snapshot, and if a delta bumps the revision
        without re-echoing that snapshot there is nothing in the
        response to compare against. Only a full resync surfaces it.

        So fold one in periodically, and pick the cadence by whether the
        user can actually see anything: with note windows open a stale
        note is visibly wrong, so check every few minutes; with
        everything closed there is nothing to be wrong on screen, so
        back off and keep the request count down. This is the cheap
        version of "only check open notes" — the pull is one bulk
        request either way, so what is worth tuning is how OFTEN it
        runs, not which notes it covers.
        """
        if not self.sync.is_authenticated:
            return
        self._tick_count += 1
        anything_visible = any(
            w.isVisible() for w in self.windows.values()
        )
        every = (_FULL_RESYNC_TICKS_ACTIVE if anything_visible
                 else _FULL_RESYNC_TICKS_IDLE)
        force = (self._tick_count % every) == 0
        if force:
            log.info("Periodic sync tick (full resync; %s)",
                     "windows open" if anything_visible else "all closed")
        else:
            log.info("Periodic sync tick")
        self._full_sync(force_resync=force)

    def _toggle_autostart(self, enabled):
        ok = set_autostart(enabled)
        if not ok:
            log.warning("set_autostart(%s) failed", enabled)
        # Re-read the actual on-disk state so config.json never lies
        # about what Windows will do at login.
        actual = is_autostart_enabled()
        self.config["autostart"] = actual
        save_config(self.config)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_note_manager()

    # ── Updates ────────────────────────────────────────────────────────

    def _on_update_available(self, info):
        if self._any_window_busy():
            # Don't pop a modal over someone's typing — Qt hands the
            # very next keypress to the new dialog's focused button, so
            # a keystroke meant for the note (Enter/Space) can silently
            # trigger "Update now" instead. Retry shortly instead.
            QTimer.singleShot(10000, lambda: self._on_update_available(info))
            return
        if getattr(self, "_update_prompt_open", False):
            # The startup check's retry and a manual "Check for
            # updates" click can both resolve around the same time —
            # don't stack a second modal on top of one already showing.
            return
        self._update_prompt_open = True
        try:
            prompt_and_install(None, info)
        finally:
            self._update_prompt_open = False

    def _is_note_busy(self, note_id: str) -> bool:
        """True if the user appears to be actively engaged with this
        note's window right now — recently edited it AND either the
        body or the title field currently has keyboard focus. Used to
        avoid interrupting in-progress editing with a sync-driven
        refresh, a merge result, or an update prompt.

        Checks the title field too, not just the body: a user typing
        a title is just as "busy" as one typing body text, but only
        the body was checked here originally — narrower than the
        other two busy-checks in this class started requiring."""
        win = self.windows.get(note_id)
        if not win:
            return False
        idle_for = time.monotonic() - self._last_edit_time.get(note_id, 0.0)
        if idle_for >= self._edit_idle_seconds:
            return False
        title_edit = getattr(win.title_bar, "title_edit", None)
        return bool(
            win.text_edit.hasFocus()
            or (title_edit is not None and title_edit.hasFocus())
        )

    def _any_window_busy(self) -> bool:
        return any(self._is_note_busy(note_id) for note_id in self.windows)

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
        # Route through the same busy-check/defer as the startup check —
        # the network check this follows can take a few seconds, long
        # enough for the user to have started typing since they clicked
        # "Check for updates".
        self._on_update_available(info)

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
