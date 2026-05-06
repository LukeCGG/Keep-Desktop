"""Individual sticky-note window widget."""

from PySide6.QtCore import Qt, Signal, QPoint, QSize, QEvent, QPropertyAnimation, QEasingCurve, QRect, QTimer
from PySide6.QtGui import (
    QFont, QColor, QCursor, QPainter, QPen,
    QTextCharFormat, QTextBlockFormat, QTextCursor, QAction,
    QSyntaxHighlighter, QPainterPath, QBrush,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QLabel, QLineEdit,
    QPushButton, QMenu, QSizeGrip, QGraphicsDropShadowEffect,
    QGridLayout, QToolButton, QCheckBox, QScrollArea, QFrame,
    QStyleOptionButton, QStyle, QApplication,
)

from config import (
    KEEP_COLORS, KEEP_COLORS_DARK, DEFAULT_WIDTH, DEFAULT_HEIGHT,
    MIN_WIDTH, MIN_HEIGHT, set_position, get_position,
)


# Shared light-mode menu style. Forces dark-on-white so Windows' OS-level
# dark mode probe doesn't render menus white-on-white inside our app.
_LIGHT_MENU_QSS = (
    "QMenu { background: #ffffff; color: #222; border: 1px solid #ccc;"
    "        border-radius: 6px; padding: 4px; }"
    "QMenu::item { padding: 6px 16px; border-radius: 4px; color: #222; }"
    "QMenu::item:selected { background: #e0e0e0; color: #000; }"
    "QMenu::item:disabled { color: #aaa; }"
    "QMenu::separator { height: 1px; background: #ddd; margin: 4px 6px; }"
)


def _start_native_drag(window) -> bool:
    """Hand the drag off to Windows so the user gets Aero Snap behaviour
    (drag-to-edge to half-tile, drag-to-top to maximize, etc.) on our
    frameless windows. Returns True on success.

    Disabled on translucent frameless windows (the standard NoteWindow
    case) because Windows does not show the snap preview overlay for
    layered windows — and once the OS owns the drag we never get a
    mouseRelease event to fall back to manual snap. We instead rely on
    :func:`_snap_window_to_drop_zone` which the manual drag handlers
    invoke on mouse release.
    """
    return False


def _compute_snap_target(global_pos: QPoint) -> QRect | None:
    """Return the QRect ``window`` should occupy if dropped at
    ``global_pos``, or ``None`` if no snap zone applies.

    Zones (mirrors Windows' default behaviour):
      * top edge  → maximize
      * left edge / right edge → half tile
      * each corner → quarter tile
    """
    from PySide6.QtGui import QGuiApplication
    screen = (QGuiApplication.screenAt(global_pos)
              or QGuiApplication.primaryScreen())
    if screen is None:
        return None
    avail = screen.availableGeometry()
    edge = 8           # px from the actual edge to count as "on edge"
    corner = 80        # px from the perpendicular edge to count as corner
    x, y = global_pos.x(), global_pos.y()
    on_left   = (x - avail.left())   <= edge
    on_right  = (avail.right() - x)  <= edge
    on_top    = (y - avail.top())    <= edge
    near_top    = (y - avail.top())    <= corner
    near_bottom = (avail.bottom() - y) <= corner

    half_w = avail.width() // 2
    half_h = avail.height() // 2
    if on_left and near_top:
        return QRect(avail.left(), avail.top(), half_w, half_h)
    if on_left and near_bottom:
        return QRect(avail.left(), avail.top() + half_h, half_w, half_h)
    if on_right and near_top:
        return QRect(avail.left() + half_w, avail.top(), half_w, half_h)
    if on_right and near_bottom:
        return QRect(avail.left() + half_w, avail.top() + half_h, half_w, half_h)
    if on_top:
        return QRect(avail)
    if on_left:
        return QRect(avail.left(), avail.top(), half_w, avail.height())
    if on_right:
        return QRect(avail.left() + half_w, avail.top(), half_w, avail.height())
    return None


def _snap_window_to_drop_zone(window, global_pos: QPoint) -> bool:
    """If ``global_pos`` is inside a snap zone, resize/move ``window`` to
    fill it and return True. Otherwise return False."""
    target = _compute_snap_target(global_pos)
    if target is None:
        return False
    window.setGeometry(target)
    return True


class _SnapPreview(QWidget):
    """Translucent blue rectangle shown while dragging a window near a
    snap zone, mirroring Windows' Aero Snap preview. Single shared
    instance — see :func:`_snap_preview_show` / :func:`_snap_preview_hide`.
    """

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Windows-11-style: pale blue fill, blue border, rounded corners.
        rect = self.rect().adjusted(2, 2, -2, -2)
        p.setBrush(QColor(0, 120, 215, 70))
        pen = QPen(QColor(0, 120, 215, 200))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawRoundedRect(rect, 8, 8)
        p.end()


_snap_preview_widget: "_SnapPreview | None" = None


def _snap_preview_show(target: QRect):
    """Show or move the shared snap-preview overlay to ``target``."""
    global _snap_preview_widget
    if _snap_preview_widget is None:
        _snap_preview_widget = _SnapPreview()
    w = _snap_preview_widget
    w.setGeometry(target)
    if not w.isVisible():
        w.show()
    w.raise_()


def _snap_preview_hide():
    global _snap_preview_widget
    if _snap_preview_widget is not None and _snap_preview_widget.isVisible():
        _snap_preview_widget.hide()


# ── Color Picker Popup ────────────────────────────────────────────────

class ColorPickerPopup(QWidget):
    """Grid of colored circles on a dark background for picking a note color.

    Emits ``color_chosen(light_hex, dark_mode)`` so the receiver always
    knows the canonical Keep (light) colour AND whether the user wanted
    the dark variant rendered locally.
    """
    color_chosen = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet("background: #3C4043; border-radius: 8px;")

        layout = QGridLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(10, 10, 10, 10)

        import re
        cols = 6
        # Row 0–1: light variants. Row 2–3: dark variants.
        items = list(KEEP_COLORS.items())
        for i, (name, hex_val) in enumerate(items):
            self._add_swatch(layout, i // cols, i % cols, name, hex_val,
                             False, KEEP_COLORS[name], re)
        base_row = (len(items) + cols - 1) // cols
        for i, (name, _) in enumerate(items):
            dark_val = KEEP_COLORS_DARK.get(name, "#3C4043")
            self._add_swatch(layout, base_row + i // cols, i % cols, name,
                             dark_val, True, KEEP_COLORS[name], re)

    def _add_swatch(self, layout, row, col, name, hex_val, is_dark,
                    light_hex, re_mod):
        btn = QPushButton()
        btn.setFixedSize(28, 28)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        border = "rgba(255,255,255,0.45)" if is_dark else "rgba(255,255,255,0.3)"
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {hex_val};
                border: 2px solid {border};
                border-radius: 14px;
            }}
            QPushButton:hover {{
                border: 2px solid #ffffff;
            }}
        """)
        display = re_mod.sub(r'([a-z])([A-Z])', r'\1 \2', name)
        btn.setToolTip(f"{display}{' (dark)' if is_dark else ''}")
        btn.clicked.connect(
            lambda checked, lh=light_hex, dk=is_dark: self._on_pick(lh, dk)
        )
        layout.addWidget(btn, row, col)

    def _on_pick(self, light_hex, dark_mode):
        self.color_chosen.emit(light_hex, dark_mode)
        self.close()


# ── Formatting Toolbar ─────────────────────────────────────────────────

class FormattingToolbar(QWidget):
    """Small toolbar for bold / italic / underline / strikethrough."""

    def __init__(self, text_edit, color_hex="#FFF475", parent=None):
        super().__init__(parent)
        self._text_edit = text_edit
        self._dark = False
        self.setFixedHeight(28)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(2)

        self.bold_btn = self._make_btn("B", weight=QFont.Weight.Bold)
        self.bold_btn.clicked.connect(self._toggle_bold)
        layout.addWidget(self.bold_btn)

        self.italic_btn = self._make_btn("I", italic=True)
        self.italic_btn.clicked.connect(self._toggle_italic)
        layout.addWidget(self.italic_btn)

        self.underline_btn = self._make_btn("U", underline=True)
        self.underline_btn.clicked.connect(self._toggle_underline)
        layout.addWidget(self.underline_btn)

        self.strike_btn = self._make_btn("S", strikeout=True)
        self.strike_btn.clicked.connect(self._toggle_strikethrough)
        layout.addWidget(self.strike_btn)

        # Heading dropdown (Body / Heading 1 / Heading 2). Mirrors
        # Keep web's paragraph-style picker.
        self.heading_btn = QToolButton(self)
        self.heading_btn.setText("\u00b6")  # pilcrow
        self.heading_btn.setToolTip("Paragraph style")
        self.heading_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.heading_btn.setFixedSize(30, 24)
        self.heading_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        h_menu = QMenu(self.heading_btn)
        h_menu.setStyleSheet(_LIGHT_MENU_QSS)
        for label, level in (("Body text", 0),
                             ("Heading 1", 1),
                             ("Heading 2", 2)):
            act = QAction(label, h_menu)
            act.triggered.connect(lambda _checked=False, lv=level: self._set_heading(lv))
            h_menu.addAction(act)
        self.heading_btn.setMenu(h_menu)
        layout.addWidget(self.heading_btn)

        self.clear_fmt_btn = self._make_btn("T\u02e3")
        self.clear_fmt_btn.setCheckable(False)
        self.clear_fmt_btn.setToolTip(
            "Clear formatting from the selected text (or the whole note"
            " if nothing is selected)"
        )
        self.clear_fmt_btn.clicked.connect(self._clear_formatting)
        layout.addWidget(self.clear_fmt_btn)

        layout.addStretch()

        text_edit.cursorPositionChanged.connect(self._update_states)
        self.update_color(color_hex)

    def _make_btn(self, label, weight=None, italic=False, underline=False, strikeout=False):
        btn = QPushButton(label)
        f = QFont("Segoe UI", 9)
        if weight:
            f.setWeight(weight)
        f.setItalic(italic)
        f.setUnderline(underline)
        f.setStrikeOut(strikeout)
        btn.setFont(f)
        btn.setCheckable(True)
        btn.setFixedSize(26, 24)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet("""
            QPushButton {
                border: none; border-radius: 3px;
                color: #555; background: transparent;
            }
            QPushButton:hover { background: rgba(0,0,0,0.08); }
            QPushButton:checked { background: rgba(0,0,0,0.14); }
        """)
        return btn

    def _toggle_bold(self):
        fmt = self._text_edit.currentCharFormat()
        new_w = QFont.Weight.Normal if fmt.fontWeight() >= QFont.Weight.Bold else QFont.Weight.Bold
        fmt.setFontWeight(new_w)
        self._text_edit.mergeCurrentCharFormat(fmt)

    def _toggle_italic(self):
        fmt = self._text_edit.currentCharFormat()
        fmt.setFontItalic(not fmt.fontItalic())
        self._text_edit.mergeCurrentCharFormat(fmt)

    def _toggle_underline(self):
        fmt = self._text_edit.currentCharFormat()
        fmt.setFontUnderline(not fmt.fontUnderline())
        self._text_edit.mergeCurrentCharFormat(fmt)

    def _toggle_strikethrough(self):
        fmt = self._text_edit.currentCharFormat()
        fmt.setFontStrikeOut(not fmt.fontStrikeOut())
        self._text_edit.mergeCurrentCharFormat(fmt)

    def _set_heading(self, level: int):
        """Apply heading level (0 = body, 1 = H1, 2 = H2) to every
        block touched by the current selection (or just the cursor's
        block if nothing is selected)."""
        cursor = self._text_edit.textCursor()
        # Sizes match Keep web's heading scale: H1 ~1.5x, H2 ~1.25x.
        sizes = {0: 10.0, 1: 16.0, 2: 13.0}
        target_size = sizes.get(level, 10.0)
        cursor.beginEditBlock()
        if cursor.hasSelection():
            start, end = sorted((cursor.anchor(), cursor.position()))
        else:
            start = end = cursor.position()
        # Walk every block in [start, end].
        c = QTextCursor(self._text_edit.document())
        c.setPosition(start)
        c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        while True:
            bf = QTextBlockFormat(c.blockFormat())
            bf.setHeadingLevel(level)
            c.setBlockFormat(bf)
            # Update font size of all chars in this block so the new
            # style is visible immediately (Qt won't restyle existing
            # runs from heading level alone).
            block_start = c.block().position()
            block_end = block_start + c.block().length() - 1
            cf = QTextCharFormat()
            cf.setFontPointSize(target_size)
            sel = QTextCursor(self._text_edit.document())
            sel.setPosition(block_start)
            sel.setPosition(block_end, QTextCursor.MoveMode.KeepAnchor)
            sel.mergeCharFormat(cf)
            if c.block().position() + c.block().length() - 1 >= end:
                break
            if not c.movePosition(QTextCursor.MoveOperation.NextBlock):
                break
        cursor.endEditBlock()
        self._text_edit.setFocus()
        self._update_states()

    def _clear_formatting(self):
        """Strip rich-text formatting from the selection (or whole note)."""
        cursor = self._text_edit.textCursor()
        if not cursor.hasSelection():
            cursor.select(cursor.SelectionType.Document)
        plain_fmt = QTextCharFormat()
        # setCharFormat REPLACES (rather than merges) every property,
        # so it actually wipes bold/italic/underline/strike/colour/size.
        cursor.setCharFormat(plain_fmt)

    def _update_states(self):
        fmt = self._text_edit.currentCharFormat()
        self.bold_btn.setChecked(fmt.fontWeight() >= QFont.Weight.Bold)
        self.italic_btn.setChecked(fmt.fontItalic())
        self.underline_btn.setChecked(fmt.fontUnderline())
        self.strike_btn.setChecked(fmt.fontStrikeOut())
        # Reflect current paragraph style on the dropdown label.
        level = self._text_edit.textCursor().blockFormat().headingLevel()
        self.heading_btn.setText({1: "H1", 2: "H2"}.get(level, "\u00b6"))

    def update_color(self, color_hex):
        darker = QColor(color_hex).darker(105).name()
        self.setStyleSheet(f"FormattingToolbar {{ background: {darker}; }}")
        self._restyle_buttons()

    def set_dark(self, dark: bool):
        self._dark = dark
        self._restyle_buttons()

    def _restyle_buttons(self):
        if self._dark:
            color, hover, checked = "#e8eaed", "rgba(255,255,255,0.12)", "rgba(255,255,255,0.22)"
        else:
            color, hover, checked = "#555", "rgba(0,0,0,0.08)", "rgba(0,0,0,0.14)"
        qss = (
            "QPushButton {"
            f"  border: none; border-radius: 3px; color: {color};"
            "  background: transparent;"
            "}"
            f"QPushButton:hover {{ background: {hover}; }}"
            f"QPushButton:checked {{ background: {checked}; }}"
        )
        for b in (self.bold_btn, self.italic_btn, self.underline_btn,
                  self.strike_btn, self.clear_fmt_btn):
            b.setStyleSheet(qss)
        # QToolButton needs slightly different selectors for the popup arrow.
        self.heading_btn.setStyleSheet(
            "QToolButton {"
            f"  border: none; border-radius: 3px; color: {color};"
            "  background: transparent; padding: 0 4px;"
            "}"
            f"QToolButton:hover {{ background: {hover}; }}"
            "QToolButton::menu-indicator { image: none; width: 0; }"
        )


# ── Title Bar ──────────────────────────────────────────────────────────

class DragHandle(QWidget):
    """A small grip widget (≡) used to drag the parent window around.

    Lives in the title bar where it doesn't conflict with the now-editable
    title QLineEdit.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 28)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("Drag to move note")
        self._drag_pos = None
        self._dark = False

    def set_dark(self, dark: bool):
        self._dark = dark
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(255, 255, 255, 160) if self._dark
                   else QColor(0, 0, 0, 110))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        w = self.width()
        h = self.height()
        # Three short horizontal lines, vertically centred
        cx = w // 2
        line_w = 10
        x1 = cx - line_w // 2
        x2 = cx + line_w // 2
        for dy in (-5, 0, 5):
            y = h // 2 + dy
            painter.drawLine(x1, y, x2, y)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            win = self.window()
            if _start_native_drag(win):
                # OS now owns the drag; we'll save geometry on release
                # via a moveEvent hook in the parent window.
                self._drag_pos = None
                event.accept()
                return
            self._drag_pos = event.globalPosition().toPoint() - win.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            gp = event.globalPosition().toPoint()
            self.window().move(gp - self._drag_pos)
            target = _compute_snap_target(gp)
            if target is not None:
                _snap_preview_show(target)
            else:
                _snap_preview_hide()
            event.accept()

    def mouseReleaseEvent(self, event):
        was_dragging = self._drag_pos is not None
        self._drag_pos = None
        win = self.window()
        _snap_preview_hide()
        if was_dragging:
            try:
                _snap_window_to_drop_zone(
                    win, event.globalPosition().toPoint())
            except Exception:  # noqa: BLE001
                pass
        if hasattr(win, "save_geometry"):
            win.save_geometry()


class TitleBar(QWidget):
    """Custom draggable title bar for a note window."""

    close_clicked = Signal()
    pin_toggled = Signal(bool)
    color_chosen = Signal(str, bool)  # (light_hex, dark_mode)
    delete_clicked = Signal()
    title_changed = Signal(str)
    checklist_toggled = Signal()

    def __init__(self, title="", color_hex="#FFF475", parent=None):
        super().__init__(parent)
        self.setFixedHeight(32)
        self._drag_pos = None
        self._pinned = False
        self._color_hex = color_hex
        self._dark = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)

        # Dedicated drag handle (the title field steals clicks now that
        # it's editable, so we give the user an explicit grab area).
        self.drag_handle = DragHandle(self)
        layout.addWidget(self.drag_handle)

        self.title_edit = QLineEdit(title)
        self.title_edit.setPlaceholderText("Title")
        # Show the START of the title when it overflows the available
        # width — default QLineEdit places the cursor at the end after
        # construction, which scrolls the view so the user only sees
        # the tail of long titles.
        self.title_edit.setCursorPosition(0)
        self.title_edit.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.title_edit.setStyleSheet(
            "QLineEdit { color: #222; background: transparent; border: none;"
            "            padding: 0px; font-weight: bold; }"
            "QLineEdit:focus { background: rgba(255,255,255,0.4);"
            "                  border-radius: 3px; }"
        )
        self.title_edit.textEdited.connect(self.title_changed.emit)
        # Backwards-compat alias for code that referred to title_label
        self.title_label = self.title_edit
        layout.addWidget(self.title_edit, stretch=1)

        btn_style = """
            QPushButton {
                border: none; border-radius: 3px;
                font-size: 14px; color: #666;
                padding: 2px 6px; background: transparent;
            }
            QPushButton:hover { background: rgba(0,0,0,0.1); }
        """

        self.pin_btn = QPushButton("📌")
        self.pin_btn.setToolTip("Toggle always on top")
        self.pin_btn.setStyleSheet(btn_style)
        self.pin_btn.setFixedSize(28, 28)
        self.pin_btn.clicked.connect(self._toggle_pin)
        layout.addWidget(self.pin_btn)

        self.color_btn = QPushButton("🎨")
        self.color_btn.setToolTip("Change color")
        self.color_btn.setStyleSheet(btn_style)
        self.color_btn.setFixedSize(28, 28)
        self.color_btn.clicked.connect(self._show_color_picker)
        layout.addWidget(self.color_btn)

        self.close_btn = QPushButton("✕")
        self.close_btn.setToolTip("Hide note")
        self.close_btn.setStyleSheet(btn_style)
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.clicked.connect(self.close_clicked.emit)
        layout.addWidget(self.close_btn)

        self.update_color(color_hex)

    def update_color(self, color_hex):
        self._color_hex = color_hex
        darker = QColor(color_hex).darker(110).name()
        self.setStyleSheet(f"""
            TitleBar {{
                background: {darker};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }}
        """)
        self._restyle_widgets()

    def set_dark(self, dark: bool):
        self._dark = dark
        self._restyle_widgets()
        self.drag_handle.set_dark(dark)

    def _restyle_widgets(self):
        if self._dark:
            text_color, hover, focus_bg, btn_color = (
                "#f1f3f4", "rgba(255,255,255,0.12)",
                "rgba(255,255,255,0.18)", "#dadce0",
            )
        else:
            text_color, hover, focus_bg, btn_color = (
                "#222", "rgba(0,0,0,0.10)",
                "rgba(255,255,255,0.4)", "#666",
            )
        self.title_edit.setStyleSheet(
            f"QLineEdit {{ color: {text_color}; background: transparent;"
            f"             border: none; padding: 0px; font-weight: bold; }}"
            f"QLineEdit:focus {{ background: {focus_bg};"
            f"                   border-radius: 3px; color: {text_color}; }}"
        )
        btn_qss = (
            "QPushButton {"
            f"  border: none; border-radius: 3px; font-size: 14px;"
            f"  color: {btn_color}; padding: 2px 6px; background: transparent;"
            "}"
            f"QPushButton:hover {{ background: {hover}; }}"
        )
        for b in (self.pin_btn, self.color_btn, self.close_btn):
            b.setStyleSheet(btn_qss)

    def _toggle_pin(self):
        self._pinned = not self._pinned
        self.pin_btn.setText("📍" if self._pinned else "📌")
        self.pin_toggled.emit(self._pinned)

    def _show_color_picker(self):
        from PySide6.QtWidgets import QApplication
        self._color_popup = ColorPickerPopup(self)
        self._color_popup.color_chosen.connect(self.color_chosen.emit)
        # Pre-show to settle size, then position so it stays inside the
        # current screen instead of falling off-edge near the right side.
        self._color_popup.adjustSize()
        size = self._color_popup.size()
        anchor = self.color_btn.mapToGlobal(QPoint(0, self.color_btn.height()))
        screen = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        avail = screen.availableGeometry()

        x, y = anchor.x(), anchor.y()
        # Right-align under the button if it would overflow on the right.
        if x + size.width() > avail.right():
            x = self.color_btn.mapToGlobal(
                QPoint(self.color_btn.width(), 0)
            ).x() - size.width()
        x = max(avail.left() + 4, min(x, avail.right() - size.width() - 4))
        # Flip above the button if it would overflow at the bottom.
        if y + size.height() > avail.bottom():
            y = self.color_btn.mapToGlobal(QPoint(0, 0)).y() - size.height()
        y = max(avail.top() + 4, min(y, avail.bottom() - size.height() - 4))
        self._color_popup.move(x, y)
        self._color_popup.show()

    def set_pinned(self, pinned):
        self._pinned = pinned
        self.pin_btn.setText("📍" if pinned else "📌")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            win = self.window()
            if _start_native_drag(win):
                self._drag_pos = None
                event.accept()
                return
            self._drag_pos = event.globalPosition().toPoint() - win.pos()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            gp = event.globalPosition().toPoint()
            self.window().move(gp - self._drag_pos)
            target = _compute_snap_target(gp)
            if target is not None:
                _snap_preview_show(target)
            else:
                _snap_preview_hide()

    def mouseReleaseEvent(self, event):
        was_dragging = self._drag_pos is not None
        self._drag_pos = None
        win = self.window()
        _snap_preview_hide()
        if was_dragging:
            try:
                _snap_window_to_drop_zone(
                    win, event.globalPosition().toPoint())
            except Exception:  # noqa: BLE001
                pass
        # Save position after drag
        if hasattr(win, "save_geometry"):
            win.save_geometry()


class _LinkHighlighter(QSyntaxHighlighter):
    """Visually mark URLs/emails in the note body with blue underline,
    matching the convention used by Keep web and most rich editors.

    Pure styling — does NOT change the underlying text or affect the
    HTML we round-trip to Keep. The format is reapplied on every text
    change so it tracks edits live.
    """

    # Same pattern as NoteTextEdit, kept duplicated so the highlighter
    # can be a top-level class and not depend on import order.
    # Phone alts: bare 0-prefix national numbers (AU/UK/etc, 9–12 digits),
    # international with + prefix, separator-style, and parens area-code.
    _LINK_RE = __import__("re").compile(
        r"(mailto:[^\s<>]+|tel:[+\d][\d\s.\-()]+|https?://[^\s<>]+|www\.[^\s<>]+|"
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|"
        r"(?<!\d)(?:"
        r"\+\d[\d .\-()]{7,16}\d|"
        r"\(\d{2,4}\)[ .\-]?\d{2,4}[ .\-]?\d{2,5}|"
        r"0\d{8,11}|"
        r"(?!(?:19|20)\d{6})\d{8}|"
        r"\d{2,4}[ .\-]\d{2,4}[ .\-]\d{3,5}"
        r")(?!\d))"
    )

    def __init__(self, document):
        super().__init__(document)
        self._fmt = QTextCharFormat()
        self._fmt.setForeground(QColor("#1a73e8"))  # Google's link blue
        self._fmt.setFontUnderline(True)

    def highlightBlock(self, text: str) -> None:
        for m in self._LINK_RE.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self._fmt)


class NoteTextEdit(QTextEdit):
    """Rich text editor area for the note body."""

    # URL/email/phone detection: matches http(s)://, www., bare email,
    # mailto:/tel: schemes, and phone numbers — including bare AU/UK
    # style 0-prefixed 10-digit numbers (e.g. 0478114466) and bare
    # international + numbers (e.g. +61478114466).
    _LINK_RE = __import__("re").compile(
        r"(mailto:[^\s<>]+|tel:[+\d][\d\s.\-()]+|https?://[^\s<>]+|www\.[^\s<>]+|"
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|"
        r"(?<!\d)(?:"
        r"\+\d[\d .\-()]{7,16}\d|"
        r"\(\d{2,4}\)[ .\-]?\d{2,4}[ .\-]?\d{2,5}|"
        r"0\d{8,11}|"
        r"(?!(?:19|20)\d{6})\d{8}|"
        r"\d{2,4}[ .\-]\d{2,4}[ .\-]\d{3,5}"
        r")(?!\d))"
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFont("Segoe UI", 10))
        self.setAcceptRichText(True)
        # Qt's built-in HTML renderer applies font-weight:bold to <h1>
        # and <h2> by default. Keep web does NOT — headings there are
        # only larger, not bold (unless the user actually toggles bold).
        # Override the default style sheet so our rendering matches.
        # Also flatten paragraph margins to 0 so plain newlines render
        # as single line breaks (matching Keep web's editor view) — Qt
        # otherwise inserts a full paragraph gap between every <p>.
        self.document().setDefaultStyleSheet(
            "h1, h2, h3, h4, h5, h6 { font-weight: normal; margin: 0; }"
            "p { margin: 0; padding: 0; }"
            "ul, ol { margin: 0; padding: 0; }"
            "li { margin: 0; }"
        )
        # Same for the underlying default block format (plain-text
        # paragraphs created without an explicit format).
        from PySide6.QtGui import QTextBlockFormat
        _bf = QTextBlockFormat()
        _bf.setTopMargin(0)
        _bf.setBottomMargin(0)
        _bf.setLineHeight(115.0, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cur = self.textCursor()
        cur.select(cur.SelectionType.Document)
        cur.mergeBlockFormat(_bf)

    def _apply_tight_block_format(self):
        """Re-apply zero-margin block formatting to every paragraph in
        the document. Needed after setHtml/setPlainText because Qt
        re-creates the blocks with their default (non-tight) format."""
        from PySide6.QtGui import QTextBlockFormat
        bf = QTextBlockFormat()
        bf.setTopMargin(0)
        bf.setBottomMargin(0)
        bf.setLineHeight(115.0, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cur = self.textCursor()
        cur.beginEditBlock()
        try:
            cur.select(cur.SelectionType.Document)
            cur.mergeBlockFormat(bf)
        finally:
            cur.endEditBlock()

    def setPlainText(self, text):  # type: ignore[override]
        super().setPlainText(text)
        self._apply_tight_block_format()

    def setHtml(self, html):  # type: ignore[override]
        super().setHtml(html)
        self._apply_tight_block_format()

    def set_styled_doc(self, doc):
        """Render a `keep_protocol.nested_model.StyledDoc` faithfully
        using the cursor API. Unlike setHtml, this preserves *exact*
        paragraph counts including empty paragraphs — Qt's HTML parser
        otherwise collapses empty `<p></p>` blocks, and `<p><br/></p>`
        produces an extra newline. The cursor approach round-trips
        cleanly through `toPlainText()` so push diffs aren't tainted
        by HTML-rendering artefacts (the cause of the blank-line
        ping-pong with Keep web)."""
        from PySide6.QtGui import QTextCharFormat, QTextBlockFormat, QFont
        self.clear()
        cur = self.textCursor()
        cur.beginEditBlock()
        try:
            base_block = QTextBlockFormat()
            base_block.setTopMargin(0)
            base_block.setBottomMargin(0)
            base_block.setLineHeight(
                115.0,
                QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
            )
            # Apply to the document's first (already-existing) block.
            cur.setBlockFormat(base_block)
            for i, para in enumerate(doc.paragraphs):
                if i > 0:
                    cur.insertBlock(base_block)
                # Heading sizing (no bold — Keep web matches this).
                bf = QTextBlockFormat(base_block)
                heading_size = None
                if para.heading == 1:
                    heading_size = 18
                elif para.heading == 2:
                    heading_size = 14
                cur.setBlockFormat(bf)
                for run in para.runs:
                    if not run.text:
                        continue
                    cf = QTextCharFormat()
                    if heading_size is not None:
                        cf.setFontPointSize(heading_size)
                    if run.bold:
                        cf.setFontWeight(QFont.Weight.Bold)
                    if run.italic:
                        cf.setFontItalic(True)
                    if run.underline:
                        cf.setFontUnderline(True)
                    if run.strikethrough:
                        cf.setFontStrikeOut(True)
                    cur.insertText(run.text, cf)
        finally:
            cur.endEditBlock()
        self.setStyleSheet("""
            QTextEdit {
                border: none;
                background: transparent;
                padding: 8px;
                color: #333;
                selection-color: #000;
                selection-background-color: rgba(66,133,244,0.35);
            }
            QScrollBar:vertical {
                width: 6px; background: transparent;
            }
            QScrollBar::handle:vertical {
                background: rgba(0,0,0,0.15); border-radius: 3px;
                min-height: 30px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """)
        self.setPlaceholderText("Type your note here...")
        # Required so mouseMoveEvent fires during Ctrl+hover (not just
        # while a button is held).
        self.viewport().setMouseTracking(True)
        self.setMouseTracking(True)
        # Visual styling for URLs/emails (blue + underline). Click
        # behaviour stays Ctrl+click via mousePressEvent below.
        self._link_highlighter = _LinkHighlighter(self.document())

    # ── Ctrl+click link activation ─────────────────────────────────────

    def _link_at(self, pos):
        """Return (url_string, char_start, char_end) for the URL at the
        given viewport position, or None if there isn't one."""
        cursor = self.cursorForPosition(pos)
        block = cursor.block()
        if not block.isValid():
            return None
        block_text = block.text()
        col = cursor.positionInBlock()
        for m in self._LINK_RE.finditer(block_text):
            if m.start() <= col <= m.end():
                url = m.group(0)
                # Strip common trailing punctuation that's almost
                # certainly not part of the URL (sentence-final
                # period, comma, closing bracket, etc.).
                stripped = url.rstrip(").,;:!?]\u201d\u2019")
                return (
                    stripped,
                    block.position() + m.start(),
                    block.position() + m.start() + len(stripped),
                )
        return None

    def _open_link(self, url: str) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        if url.startswith("www."):
            url = "https://" + url
        elif "@" in url and not url.startswith("mailto:") and "://" not in url:
            url = "mailto:" + url
        elif (not url.startswith(("http://", "https://", "mailto:", "tel:"))
              and "@" not in url):
            # Phone number: strip formatting whitespace/punctuation so the
            # OS handler (Skype, Teams, dialer, etc.) gets a clean number.
            digits = "".join(ch for ch in url if ch.isdigit() or ch == "+")
            if digits:
                url = "tel:" + digits
        QDesktopServices.openUrl(QUrl(url))

    def mouseMoveEvent(self, event):
        # Show the pointing-hand cursor when Ctrl is held over a URL,
        # matching the convention used by IDEs and rich-text editors.
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if self._link_at(event.pos()):
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        super().mouseMoveEvent(event)

    def keyReleaseEvent(self, event):
        # Restore I-beam when Ctrl is released so the cursor doesn't
        # stay stuck as a hand.
        if event.key() in (Qt.Key.Key_Control,):
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event):
        # Ctrl+left-click on a URL/email opens it in the system handler.
        if (event.button() == Qt.MouseButton.LeftButton
                and (event.modifiers() & Qt.KeyboardModifier.ControlModifier)):
            link = self._link_at(event.pos())
            if link is not None:
                self._open_link(link[0])
                event.accept()
                return
        if event.button() == Qt.MouseButton.LeftButton:
            cursor = self.cursorForPosition(event.pos())
            block = cursor.block()
            text = block.text()
            if text.startswith("☑") or text.startswith("☐"):
                # Only toggle if clicking near the checkbox (first ~20px)
                block_layout = self.document().documentLayout()
                block_rect = block_layout.blockBoundingRect(block)
                click_x = event.pos().x() - self.contentsRect().x() + self.horizontalScrollBar().value()
                # Checkbox char is roughly the first 24 pixels
                if click_x > 28:
                    super().mousePressEvent(event)
                    return
                # Toggle the checkbox character
                was_checked = text.startswith("☑")
                new_char = "☐" if was_checked else "☑"
                cursor.movePosition(cursor.MoveOperation.StartOfBlock)
                cursor.movePosition(cursor.MoveOperation.Right, cursor.MoveMode.KeepAnchor, 1)
                if was_checked:
                    # Was checked → now unchecked: remove strikethrough + grey
                    cursor.removeSelectedText()
                    fmt = QTextCharFormat()
                    fmt.setFontPointSize(14)
                    cursor.insertText(new_char, fmt)
                    cursor.movePosition(cursor.MoveOperation.EndOfBlock, cursor.MoveMode.KeepAnchor)
                    line_fmt = QTextCharFormat()
                    line_fmt.setFontStrikeOut(False)
                    line_fmt.setForeground(QColor("#333"))
                    cursor.mergeCharFormat(line_fmt)
                else:
                    # Was unchecked → now checked: add strikethrough + grey
                    cursor.removeSelectedText()
                    fmt = QTextCharFormat()
                    fmt.setFontPointSize(14)
                    fmt.setForeground(QColor("#888"))
                    cursor.insertText(new_char, fmt)
                    cursor.movePosition(cursor.MoveOperation.EndOfBlock, cursor.MoveMode.KeepAnchor)
                    line_fmt = QTextCharFormat()
                    line_fmt.setFontStrikeOut(True)
                    line_fmt.setForeground(QColor("#888"))
                    cursor.mergeCharFormat(line_fmt)
                # Re-sort: unchecked on top, checked on bottom
                self._resort_checklist()
                return  # Don't pass to default handler
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        # Ctrl+Shift+V → paste as plain text (matches most modern editors).
        if (event.key() == Qt.Key.Key_V
                and (event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)):
            self._paste_plain_text()
            return
        # Enter should always create a real new line. QTextEdit's default
        # Return handling inserts a rich-text paragraph block and tries to
        # carry paragraph/heading formatting forward; pressing Return twice
        # can then appear to "adjust" the previous blank line instead of
        # adding another one. Insert a literal newline instead and reset the
        # typing format to normal body text for whatever comes next.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            cursor = self.textCursor()
            block = cursor.block()
            text = block.text()
            # Auto-insert checkbox prefix on Enter in legacy glyph-list notes.
            if text.startswith("☑") or text.startswith("☐"):
                # If the current line is just an empty checkbox, remove it instead
                stripped = text.lstrip("☑☐").strip()
                if not stripped:
                    cursor.movePosition(cursor.MoveOperation.StartOfBlock)
                    cursor.movePosition(cursor.MoveOperation.EndOfBlock, cursor.MoveMode.KeepAnchor)
                    cursor.removeSelectedText()
                    cursor.deletePreviousChar()  # remove the newline
                    self.setTextCursor(cursor)
                    return
                super().keyPressEvent(event)
                # Insert checkbox prefix on the new line
                new_cursor = self.textCursor()
                fmt = QTextCharFormat()
                fmt.setFontPointSize(14)
                new_cursor.insertText("☐", fmt)
                body_fmt = QTextCharFormat()
                body_fmt.setFontStrikeOut(False)
                body_fmt.setForeground(QColor("#333"))
                new_cursor.insertText(" ", body_fmt)
                self.setTextCursor(new_cursor)
                return
            cursor.insertText("\n")
            self.setTextCursor(cursor)
            body_fmt = QTextCharFormat()
            body_fmt.setFontPointSize(10)
            body_fmt.setFontWeight(QFont.Weight.Normal)
            body_fmt.setFontItalic(False)
            body_fmt.setFontUnderline(False)
            body_fmt.setFontStrikeOut(False)
            body_fmt.setForeground(QColor("#333"))
            self.setCurrentCharFormat(body_fmt)
            self._apply_tight_block_format()
            event.accept()
            return
        super().keyPressEvent(event)

    def _resort_checklist(self):
        """Re-sort lines: unchecked items first, checked items below."""
        plain = self.toPlainText()
        lines = plain.splitlines()
        if not lines:
            return
        unchecked = []
        checked = []
        other = []
        for line in lines:
            if line.startswith("☐"):
                unchecked.append(line)
            elif line.startswith("☑"):
                checked.append(line)
            else:
                other.append(line)
        new_lines = other + unchecked + checked
        if new_lines == lines:
            return  # No reorder needed

        # Parse into items and re-render with formatting
        items = []
        for line in new_lines:
            if line.startswith("☑"):
                items.append({"text": line.lstrip("☑").strip(), "checked": True})
            elif line.startswith("☐"):
                items.append({"text": line.lstrip("☐").strip(), "checked": False})
            else:
                items.append({"text": line, "checked": False})

        # Find the parent NoteWindow to use _set_checklist_html
        win = self.parent()
        while win and not isinstance(win, NoteWindow):
            win = win.parent()
        if win:
            win._set_checklist_html(items)

    def _paste_plain_text(self):
        """Insert clipboard text without any source formatting."""
        from PySide6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        text = cb.text()
        if not text:
            return
        cursor = self.textCursor()
        # Use a fresh, empty char format so no styles leak in.
        cursor.insertText(text, QTextCharFormat())

    def insertFromMimeData(self, source):
        """Default Ctrl+V → plain text. The user can still apply formatting
        with the toolbar after pasting; this matches what most note-takers
        expect (and avoids dragging in colours/fonts from web pages)."""
        if source.hasText():
            cursor = self.textCursor()
            cursor.insertText(source.text(), QTextCharFormat())
        else:
            super().insertFromMimeData(source)

    def contextMenuEvent(self, event):
        # Build the standard menu but replace Paste with Paste (plain text).
        menu = self.createStandardContextMenu()
        menu.setStyleSheet(_LIGHT_MENU_QSS)
        for act in list(menu.actions()):
            txt = (act.text() or "").lower().replace("&", "")
            if txt.startswith("paste"):
                menu.removeAction(act)
                break
        from PySide6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        has_text = bool(cb.text())
        paste_act = menu.addAction("Paste\tCtrl+V")
        paste_act.setEnabled(has_text)
        paste_act.triggered.connect(self._paste_plain_text)
        menu.exec(event.globalPos())


class ResizeGrip(QWidget):
    """Small bottom-right resize handle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._drag_pos = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(0, 0, 0, 60))
        pen.setWidth(1)
        painter.setPen(pen)
        # Draw grip dots
        for i in range(3):
            for j in range(3):
                if i + j >= 2:
                    painter.drawEllipse(4 + i * 4, 4 + j * 4, 2, 2)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self._drag_pos = event.globalPosition().toPoint()
            win = self.window()
            new_w = max(MIN_WIDTH, win.width() + delta.x())
            new_h = max(MIN_HEIGHT, win.height() + delta.y())
            win.resize(new_w, new_h)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        win = self.window()
        if hasattr(win, "save_geometry"):
            win.save_geometry()


# ── Checklist editor (real per-item rows) ─────────────────────────────


_INDENT_PX = 22  # pixels per indent level


class _DragGripLabel(QLabel):
    """6-dot drag handle shown on the left of each checklist row.

    Mouse press starts a row-drag in the parent ``ChecklistEditor``;
    move events are forwarded so the editor can run the animation.
    """

    def __init__(self, row: "ChecklistRow"):
        super().__init__(row)
        self._row = row
        self.setFixedSize(14, 22)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to reorder")

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 0, 0, 90)))
        for col in (4, 9):
            for row in (4, 11, 18):
                p.drawEllipse(col, row, 2, 2)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            editor = self._row.editor()
            if editor is not None:
                editor.begin_drag(self._row, ev.globalPosition().toPoint())

    def mouseMoveEvent(self, ev):
        editor = self._row.editor()
        if editor is not None:
            editor.update_drag(ev.globalPosition().toPoint())

    def mouseReleaseEvent(self, _ev):
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        editor = self._row.editor()
        if editor is not None:
            editor.end_drag()


class _RowLineEdit(QLineEdit):
    """Single-line text input that bubbles up Enter / Backspace / Tab
    so the parent ``ChecklistEditor`` can grow / shrink / indent."""

    enter_pressed = Signal()
    backspace_at_start = Signal()
    tab_pressed = Signal(bool)  # True = shift held → outdent

    def event(self, ev):
        # Qt swallows Tab/Shift-Tab in QLineEdit before keyPressEvent
        # ever runs (it's used for focus chain navigation). We need to
        # intercept at `event()` to claim them as indent/outdent.
        if ev.type() == QEvent.Type.KeyPress:
            key = ev.key()
            if key == Qt.Key.Key_Tab:
                self.tab_pressed.emit(False)
                return True
            if key == Qt.Key.Key_Backtab:
                self.tab_pressed.emit(True)
                return True
        return super().event(ev)

    def keyPressEvent(self, ev):
        key = ev.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.enter_pressed.emit()
            return
        if key == Qt.Key.Key_Backspace and self.cursorPosition() == 0 and not self.hasSelectedText():
            self.backspace_at_start.emit()
            return
        super().keyPressEvent(ev)


class ChecklistRow(QFrame):
    """A single checkbox row inside :class:`ChecklistEditor`.

    Holds the data triple ``(text, checked, indent)`` plus optional
    server-side ``cbx_id`` so that diff-based pushes preserve identity
    across edits.
    """

    def __init__(self, text: str = "", checked: bool = False,
                 indent: int = 0, cbx_id: str = "",
                 editor: "ChecklistEditor | None" = None):
        super().__init__(editor)
        self._editor_ref = editor
        self._indent = max(0, min(1, int(indent)))
        self.cbx_id = cbx_id or ""
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: transparent;")

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 1, 0, 1)
        row.setSpacing(4)

        self.grip = _DragGripLabel(self)
        row.addWidget(self.grip)

        self.indent_spacer = QWidget(self)
        self.indent_spacer.setFixedWidth(self._indent * _INDENT_PX)
        row.addWidget(self.indent_spacer)

        self.checkbox = QCheckBox(self)
        self.checkbox.setChecked(bool(checked))
        # Custom indicator styling: an empty checkbox is otherwise
        # invisible against the note's coloured background. Use a
        # subtle white fill + dark border so both states are obvious.
        self.checkbox.setStyleSheet(
            "QCheckBox { background: transparent; }"
            "QCheckBox::indicator {"
            "    width: 16px; height: 16px;"
            "    border: 1.5px solid rgba(0,0,0,0.55);"
            "    border-radius: 3px;"
            "    background: rgba(255,255,255,0.85);"
            "}"
            "QCheckBox::indicator:hover {"
            "    border-color: rgba(0,0,0,0.85);"
            "    background: rgba(255,255,255,1.0);"
            "}"
            "QCheckBox::indicator:checked {"
            "    background: rgba(0,0,0,0.78);"
            "    border-color: rgba(0,0,0,0.78);"
            "    image: none;"
            "}"
        )
        self.checkbox.toggled.connect(self._on_checked)
        row.addWidget(self.checkbox)

        self.line = _RowLineEdit(self)
        self.line.setText(text or "")
        self.line.setFrame(False)
        self.line.setStyleSheet(
            "QLineEdit { background: transparent; border: none; padding: 2px; }"
        )
        self.line.setFont(QFont("Segoe UI", 10))
        self.line.textEdited.connect(self._on_text_edited)
        self.line.enter_pressed.connect(self._on_enter)
        self.line.backspace_at_start.connect(self._on_backspace_at_start)
        self.line.tab_pressed.connect(self._on_tab)
        row.addWidget(self.line, stretch=1)

        self._apply_checked_style(checked)

    # -- editor backref --------------------------------------------------
    def editor(self) -> "ChecklistEditor | None":
        return self._editor_ref

    def set_editor(self, editor: "ChecklistEditor"):
        self._editor_ref = editor
        self.setParent(editor)

    # -- indent ----------------------------------------------------------
    @property
    def indent(self) -> int:
        return self._indent

    def set_indent(self, level: int):
        level = max(0, min(int(level), 1))
        if level == self._indent:
            return
        self._indent = level
        self.indent_spacer.setFixedWidth(level * _INDENT_PX)
        editor = self.editor()
        if editor is not None:
            editor._on_changed()

    # -- text/check ------------------------------------------------------
    def text(self) -> str:
        return self.line.text()

    def is_checked(self) -> bool:
        return self.checkbox.isChecked()

    def to_dict(self) -> dict:
        return {
            "text": self.line.text(),
            "checked": self.checkbox.isChecked(),
            "indent": self._indent,
            "cbx_id": self.cbx_id,
        }

    # -- internal slots --------------------------------------------------
    def _on_checked(self, checked: bool):
        self._apply_checked_style(checked)
        editor = self.editor()
        if editor is not None:
            editor._on_row_checked(self, checked)
            editor._on_changed()

    def _on_text_edited(self, _t):
        editor = self.editor()
        if editor is not None:
            editor._on_changed()

    def _on_enter(self):
        editor = self.editor()
        if editor is not None:
            editor.insert_row_after(self)

    def _on_backspace_at_start(self):
        editor = self.editor()
        if editor is not None:
            editor.merge_into_previous(self)

    def _on_tab(self, shift: bool):
        if shift:
            self.set_indent(self._indent - 1)
        else:
            self.set_indent(self._indent + 1)

    def _apply_checked_style(self, checked: bool):
        if checked:
            self.line.setStyleSheet(
                "QLineEdit { background: transparent; border: none;"
                "            padding: 2px; color: #888;"
                "            text-decoration: line-through; }"
            )
        else:
            self.line.setStyleSheet(
                "QLineEdit { background: transparent; border: none;"
                "            padding: 2px; color: #222; }"
            )


class ChecklistEditor(QScrollArea):
    """Scrollable list of :class:`ChecklistRow` with drag-reorder + indent.

    Drop-in replacement for the old QTextEdit-based glyph checklist.
    Emits ``changed`` whenever any row's text/check/indent/order changes.
    """

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 6px; background: transparent; }"
            "QScrollBar::handle:vertical { background: rgba(0,0,0,0.18);"
            "    border-radius: 3px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
            "    height: 0; }"
        )
        self._host = QWidget()
        self._host.setStyleSheet("background: transparent;")
        self._vbox = QVBoxLayout(self._host)
        self._vbox.setContentsMargins(8, 8, 8, 8)
        self._vbox.setSpacing(2)
        self._vbox.addStretch(1)
        self.setWidget(self._host)

        self._rows: list[ChecklistRow] = []
        self._suppress_changed: bool = False
        # Drag-reorder state
        self._dragging_row: ChecklistRow | None = None
        self._drag_start_global: QPoint | None = None
        self._drag_indent_baseline_x: int = 0
        self._drag_anim: QPropertyAnimation | None = None

    # ── public API ────────────────────────────────────────────────────
    def set_items(self, items: list[dict]):
        """Replace all rows. ``items`` is a list of dicts with keys
        ``text``, ``checked`` and optionally ``indent`` and ``cbx_id``."""
        self._suppress_changed = True
        try:
            for r in self._rows:
                r.setParent(None)
                r.deleteLater()
            self._rows.clear()
            for it in items or []:
                row = ChecklistRow(
                    text=it.get("text", ""),
                    checked=bool(it.get("checked", False)),
                    indent=min(1, int(it.get("indent", 0) or 0)),
                    cbx_id=it.get("cbx_id", "") or "",
                    editor=self,
                )
                self._rows.append(row)
                self._vbox.insertWidget(self._vbox.count() - 1, row)
            if not self._rows:
                # Always show at least one empty row so the user has
                # somewhere to type — matches Keep web's empty-list view.
                self._add_blank_row()
        finally:
            self._suppress_changed = False

    def get_items(self) -> list[dict]:
        return [r.to_dict() for r in self._rows]

    def focus_first_empty(self):
        for r in self._rows:
            if not r.text():
                r.line.setFocus()
                return
        if self._rows:
            self._rows[-1].line.setFocus()

    # ── row management ────────────────────────────────────────────────
    def _add_blank_row(self):
        row = ChecklistRow(editor=self)
        self._rows.append(row)
        self._vbox.insertWidget(self._vbox.count() - 1, row)

    def insert_row_after(self, src: ChecklistRow):
        try:
            idx = self._rows.index(src)
        except ValueError:
            idx = len(self._rows) - 1
        new_row = ChecklistRow(indent=src.indent, editor=self)
        self._rows.insert(idx + 1, new_row)
        self._vbox.insertWidget(idx + 1, new_row)
        new_row.line.setFocus()
        self._on_changed()

    def merge_into_previous(self, src: ChecklistRow):
        """Backspace-at-start: merge this row's text into the previous
        row and delete this row. This is the only way to remove a row —
        there is no per-item delete button."""
        try:
            idx = self._rows.index(src)
        except ValueError:
            return
        if idx == 0:
            # First row: just outdent if possible, otherwise no-op.
            if src.indent > 0:
                src.set_indent(src.indent - 1)
            return
        prev = self._rows[idx - 1]
        carried = src.text()
        joined = prev.text() + carried
        # Place cursor at the join point on the previous row.
        cursor_pos = len(prev.text())
        prev.line.setText(joined)
        prev.line.setFocus()
        prev.line.setCursorPosition(cursor_pos)
        # Remove src
        self._rows.pop(idx)
        src.setParent(None)
        src.deleteLater()
        self._on_changed()

    # ── drag-reorder ──────────────────────────────────────────────────
    def begin_drag(self, row: ChecklistRow, global_pos: QPoint):
        if row not in self._rows:
            return
        self._dragging_row = row
        self._drag_start_global = global_pos
        self._drag_indent_baseline_x = global_pos.x()
        row.raise_()

    # Keep's API rejects nesting deeper than one level (HTTP 400). We
    # mirror that limit on the client.
    _MAX_INDENT = 1

    def _children_of(self, row: ChecklistRow) -> list[ChecklistRow]:
        """Consecutive rows whose indent is greater than ``row.indent`` and
        immediately follow ``row`` in :pyattr:`_rows`. These are the rows
        that should move with their parent during a drag."""
        try:
            idx = self._rows.index(row)
        except ValueError:
            return []
        base = row.indent
        out: list[ChecklistRow] = []
        for r in self._rows[idx + 1:]:
            if r.indent > base:
                out.append(r)
            else:
                break
        return out

    def _parent_group_bounds(self, row: ChecklistRow) -> tuple[int, int]:
        """Return (start_idx, end_idx_exclusive) of the contiguous block
        owned by the top-level parent of ``row``. For an indent-0 row
        this is the row itself plus its children. For a child row this
        is its parent's range. Used to clamp drags so children can't
        escape their parent group."""
        try:
            idx = self._rows.index(row)
        except ValueError:
            return (0, len(self._rows))
        # Walk back to the nearest indent-0 row.
        start = idx
        while start > 0 and self._rows[start].indent > 0:
            start -= 1
        end = start + 1 + len(self._children_of(self._rows[start]))
        return (start, end)

    def update_drag(self, global_pos: QPoint):
        row = self._dragging_row
        if row is None or self._drag_start_global is None:
            return

        # ── Horizontal: indent / outdent ─────────────────────────────
        dx = global_pos.x() - self._drag_indent_baseline_x
        if abs(dx) >= _INDENT_PX:
            steps = int(dx / _INDENT_PX)
            try:
                idx = self._rows.index(row)
            except ValueError:
                return
            # First row is always indent 0. Otherwise can be one deeper
            # than predecessor, capped at _MAX_INDENT.
            prev_indent = self._rows[idx - 1].indent if idx > 0 else -1
            max_indent = min(self._MAX_INDENT, prev_indent + 1)
            new_indent = max(0, min(max_indent, row.indent + steps))
            if new_indent != row.indent:
                shift = new_indent - row.indent
                self._suppress_changed = True
                try:
                    # Only carry children when changing a top-level
                    # parent's indent. A child's indent change affects
                    # only that child.
                    if row.indent == 0:
                        for child in self._children_of(row):
                            child.set_indent(
                                max(0, min(self._MAX_INDENT,
                                           child.indent + shift)))
                    row.set_indent(new_indent)
                finally:
                    self._suppress_changed = False
                self._on_changed()
            self._drag_indent_baseline_x += steps * _INDENT_PX

        # ── Vertical: reorder ────────────────────────────────────────
        dy = global_pos.y() - self._drag_start_global.y()
        if abs(dy) < max(8, row.height() // 2):
            return
        try:
            idx = self._rows.index(row)
        except ValueError:
            return

        if row.indent == 0:
            # Parent: subtree includes children; reorder against other
            # top-level blocks. Boundary: unchecked can't pass below
            # checked and vice versa.
            subtree_len = 1 + len(self._children_of(row))
            if dy > 0:
                next_idx = idx + subtree_len
                if next_idx >= len(self._rows):
                    return
                next_row = self._rows[next_idx]
                if (not row.is_checked()) and next_row.is_checked():
                    return
                next_block_size = 1 + len(self._children_of(next_row))
                self._move_block(idx, subtree_len, idx + next_block_size)
            else:
                if idx == 0:
                    return
                # Walk back to the start of the preceding top-level block.
                start = idx - 1
                while start > 0 and self._rows[start].indent > 0:
                    start -= 1
                prev_row = self._rows[start]
                if row.is_checked() and (not prev_row.is_checked()):
                    return
                self._move_block(idx, subtree_len, start)
        else:
            # Child: stays inside its parent group. Reorder among
            # sibling children only.
            grp_start, grp_end = self._parent_group_bounds(row)
            if dy > 0:
                # Find next sibling at same indent within the group.
                next_idx = None
                for j in range(idx + 1, grp_end):
                    if self._rows[j].indent == row.indent:
                        next_idx = j
                        break
                if next_idx is None:
                    return
                # Swap with next sibling (move past it; single row only).
                self._move_block(idx, 1, next_idx + 1)
            else:
                # Find previous sibling at same indent within the group
                # (must remain after the parent at grp_start).
                prev_sib = None
                for j in range(idx - 1, grp_start, -1):
                    if self._rows[j].indent == row.indent:
                        prev_sib = j
                        break
                if prev_sib is None:
                    return
                self._move_block(idx, 1, prev_sib)

        self._drag_start_global = global_pos

    def _move_block(self, src_idx: int, length: int, dst_idx: int):
        """Move ``self._rows[src_idx:src_idx+length]`` so that the block's
        new starting index is ``dst_idx``. Updates both the data list
        and the layout, and emits :pyattr:`changed`."""
        if src_idx == dst_idx or length <= 0:
            return
        block = self._rows[src_idx:src_idx + length]
        # Capture starting geometry of the lead row for the slide animation.
        lead = block[0]
        start_geom = QRect(lead.geometry())
        del self._rows[src_idx:src_idx + length]
        if dst_idx > src_idx:
            dst_idx -= length
        for i, r in enumerate(block):
            self._rows.insert(dst_idx + i, r)
        for r in block:
            self._vbox.removeWidget(r)
        for i, r in enumerate(block):
            self._vbox.insertWidget(dst_idx + i, r)
        QTimer.singleShot(0, lambda: self._animate_into_place(lead, start_geom))
        self._on_changed()

    def end_drag(self):
        self._dragging_row = None
        self._drag_start_global = None

    def _on_row_checked(self, row: "ChecklistRow", checked: bool):
        """Re-sort the list around the new check state, respecting
        parent/child grouping.

        Rules (mirrors Keep web's behaviour for indented checklists):

        * The list is split into "subtrees" — each subtree is one
          top-level (indent=0) row plus its consecutive indent>0
          children.
        * A subtree is *completed* iff every row in it is checked.
        * Completed subtrees sink to the bottom of the list as a unit.
          A parent that is checked but still has unchecked children
          stays in the active area — its children dictate its
          location, not its own check state.
        * Within an active subtree, children are also reordered so
          unchecked siblings appear before checked ones (stable).
          Children of completed subtrees keep their order.
        * The order of subtrees within each section (active /
          completed) is preserved relative to their pre-toggle order.
        """
        if not self._rows:
            return

        # 1. Build subtrees as lists of rows in their current order.
        subtrees: list[list[ChecklistRow]] = []
        for r in self._rows:
            if r.indent == 0 or not subtrees:
                subtrees.append([r])
            else:
                subtrees[-1].append(r)

        def _is_completed(sub: list[ChecklistRow]) -> bool:
            return all(x.is_checked() for x in sub)

        # 2. Reorder children within each *active* subtree so unchecked
        #    siblings rise above checked ones. Parent stays at index 0.
        for sub in subtrees:
            if len(sub) <= 1 or _is_completed(sub):
                continue
            head, kids = sub[0], sub[1:]
            kids_sorted = sorted(
                enumerate(kids),
                key=lambda pair: (pair[1].is_checked(), pair[0]),
            )
            sub[1:] = [k for _, k in kids_sorted]

        # 3. Stable partition: active subtrees first, completed last.
        active = [s for s in subtrees if not _is_completed(s)]
        done = [s for s in subtrees if _is_completed(s)]
        new_order: list[ChecklistRow] = []
        for sub in active + done:
            new_order.extend(sub)

        if new_order == self._rows:
            return

        # 4. Apply new order to data + layout. Animate the toggled row.
        start_geom = QRect(row.geometry())
        for r in self._rows:
            self._vbox.removeWidget(r)
        self._rows = new_order
        for i, r in enumerate(self._rows):
            self._vbox.insertWidget(i, r)
        QTimer.singleShot(0, lambda: self._animate_into_place(row, start_geom))

    def _swap_rows(self, i: int, j: int):
        if i == j:
            return
        # Animate the displaced row sliding into place.
        moving_to_top = j < i
        target = self._rows[j]
        start_geom = QRect(target.geometry())
        # Reorder data + layout.
        self._rows[i], self._rows[j] = self._rows[j], self._rows[i]
        # Take both rows out of the layout, then re-insert in the new
        # order. Layout indices: rows occupy 0..n-1 (the trailing
        # stretch lives at index n).
        a = self._rows[min(i, j)]
        b = self._rows[max(i, j)]
        self._vbox.removeWidget(a)
        self._vbox.removeWidget(b)
        self._vbox.insertWidget(min(i, j), a)
        self._vbox.insertWidget(max(i, j), b)
        # Animate
        QTimer.singleShot(0, lambda: self._animate_into_place(target, start_geom))
        self._on_changed()

    def _animate_into_place(self, row: ChecklistRow, from_rect: QRect):
        end_rect = QRect(row.geometry())
        if from_rect == end_rect:
            return
        if self._drag_anim is not None:
            self._drag_anim.stop()
        anim = QPropertyAnimation(row, b"geometry", self)
        anim.setDuration(140)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(from_rect)
        anim.setEndValue(end_rect)
        anim.start()
        self._drag_anim = anim

    # ── change broadcast ──────────────────────────────────────────────
    def _on_changed(self):
        if not self._suppress_changed:
            self.changed.emit()


class NoteWindow(QWidget):
    """A single floating sticky note window."""

    note_changed = Signal(str)   # note_id
    note_hidden = Signal(str)    # note_id
    note_deleted = Signal(str)   # note_id

    def __init__(self, note_id, title="", text="", html="", color_hex="#FFF475",
                 pinned=False, show_in_taskbar=False, list_items=None,
                 dark_mode=False, parent=None):
        super().__init__(parent)
        self.note_id = note_id
        self._color_hex = color_hex
        self._dark_mode = dark_mode
        self._syncing = False  # flag to suppress change signals during sync
        self._show_in_taskbar = show_in_taskbar
        self._is_list = bool(list_items)

        # Explicit per-window icon. QApplication's windowIcon usually
        # cascades, but on Windows the taskbar reads the per-window icon
        # directly — setting it here guarantees the K shows up there too.
        # We also tint the icon to match the note colour so each pinned
        # note in the taskbar is visually distinct.
        try:
            from app_icon import make_icon as _make_icon
            self.setWindowIcon(_make_icon(color_hex))
        except Exception:  # noqa: BLE001
            from PySide6.QtWidgets import QApplication
            inst = QApplication.instance()
            if inst is not None and not inst.windowIcon().isNull():
                self.setWindowIcon(inst.windowIcon())

        # Frameless window; Tool flag hides from taskbar
        flags = Qt.WindowType.FramelessWindowHint
        if not show_in_taskbar:
            flags |= Qt.WindowType.Tool
        if pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)

        # Main container (rounded rect with color)
        self.container = QWidget(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.container)

        inner = QVBoxLayout(self.container)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)

        # Title bar
        self.title_bar = TitleBar(title, color_hex, self)
        self.title_bar.close_clicked.connect(self._on_close)
        self.title_bar.pin_toggled.connect(self._on_pin_toggle)
        self.title_bar.color_chosen.connect(self._on_color_change)
        self.title_bar.title_changed.connect(self._on_title_changed)
        self.title_bar.checklist_toggled.connect(self._toggle_checklist_mode)
        self.title_bar.set_pinned(pinned)
        inner.addWidget(self.title_bar)

        # Text body
        self.text_edit = NoteTextEdit(self)
        if html and not list_items:
            self.text_edit.setHtml(html)
        else:
            self.text_edit.setPlainText(text)
        inner.addWidget(self.text_edit, stretch=1)

        # Real per-item checklist editor (used when ``_is_list`` is True).
        # Stays hidden when the note is plain-text mode.
        self.checklist_editor = ChecklistEditor(self)
        self.checklist_editor.setVisible(False)
        self.checklist_editor.changed.connect(self._on_checklist_changed)
        inner.addWidget(self.checklist_editor, stretch=1)
        if list_items:
            self._syncing = True
            self.checklist_editor.set_items(list_items)
            self._syncing = False
            self.text_edit.setVisible(False)
            self.checklist_editor.setVisible(True)

        # Formatting toolbar
        self.fmt_toolbar = FormattingToolbar(self.text_edit, color_hex, self)
        inner.addWidget(self.fmt_toolbar)
        if list_items:
            self.fmt_toolbar.setVisible(False)

        # Connect text changed AFTER setting initial content
        self.text_edit.textChanged.connect(self._on_text_changed)

        # Bottom bar with resize grip
        bottom = QHBoxLayout()
        bottom.setContentsMargins(8, 2, 2, 2)
        self.char_count = QLabel("")
        self.char_count.setStyleSheet("color: rgba(0,0,0,0.3); font-size: 9px; background: transparent;")
        bottom.addWidget(self.char_count, stretch=1)
        self.resize_grip = ResizeGrip(self)
        bottom.addWidget(self.resize_grip)
        inner.addLayout(bottom)

        # Drop shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 60))
        self.container.setGraphicsEffect(shadow)

        self._apply_color(color_hex)
        self._update_char_count()
        self._update_window_title(title)

        # Keep the OS-level window title (used by the Windows taskbar)
        # in sync with the editable note title.
        self.title_bar.title_changed.connect(self._update_window_title)

        # Restore saved position / size
        pos = get_position(note_id)
        if pos:
            self.move(pos["x"], pos["y"])
            self.resize(pos["w"], pos["h"])
        else:
            self.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT)

        # Default focus should land in the body, not the title.
        self.text_edit.setFocus()

    def _update_window_title(self, title: str):
        title = (title or "").strip()
        # Fall back to a snippet of the body if there's no title yet.
        if not title:
            body = self.text_edit.toPlainText().strip().splitlines()
            snippet = body[0][:40] if body else ""
            title = snippet or "Untitled note"
        self.setWindowTitle(f"{title} — KeepDesktop")

    def _apply_color(self, color_hex):
        self._color_hex = color_hex
        # When dark mode is on, render with the dark variant + white text;
        # the wire colour stays light so Keep keeps a clean palette.
        if self._dark_mode:
            from config import KEEP_COLORS_DARK, KEEP_COLORS
            name = next(
                (k for k, v in KEEP_COLORS.items() if v == color_hex),
                "Yellow",
            )
            bg = KEEP_COLORS_DARK.get(name, color_hex)
            text_color = "#f1f3f4"
        else:
            bg = color_hex
            text_color = "#333"
        self.container.setStyleSheet(f"""
            QWidget {{
                background: {bg};
                border-radius: 8px;
            }}
        """)
        self.text_edit.setStyleSheet(self.text_edit.styleSheet())  # ensure repaint
        # In dark mode, give the selection a brighter background AND keep
        # text white so it stays readable. In light mode the constructor
        # default (black-on-blue-tint) handles things; here we override
        # for dark.
        if self._dark_mode:
            sel_qss = (
                "selection-color: #ffffff;"
                "selection-background-color: rgba(138,180,248,0.45);"
            )
        else:
            sel_qss = (
                "selection-color: #000000;"
                "selection-background-color: rgba(66,133,244,0.35);"
            )
        self.text_edit.setStyleSheet(f"""
            QTextEdit {{
                border: none;
                background: transparent;
                padding: 8px;
                color: {text_color};
                {sel_qss}
            }}
            QScrollBar:vertical {{ width: 6px; background: transparent; }}
            QScrollBar::handle:vertical {{
                background: rgba(0,0,0,0.18); border-radius: 3px;
                min-height: 30px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)
        self.title_bar.update_color(bg)
        self.fmt_toolbar.update_color(bg)
        # Propagate dark mode so titlebar, drag handle, and toolbar
        # buttons all use light text/icons against the dark background.
        self.title_bar.set_dark(self._dark_mode)
        self.fmt_toolbar.set_dark(self._dark_mode)
        cc_color = ("rgba(255,255,255,0.45)" if self._dark_mode
                    else "rgba(0,0,0,0.3)")
        self.char_count.setStyleSheet(
            f"color: {cc_color}; font-size: 9px; background: transparent;"
        )
        # Recolour the per-window taskbar icon to match the note.
        try:
            from app_icon import make_icon as _make_icon
            self.setWindowIcon(_make_icon(bg))
        except Exception:  # noqa: BLE001
            pass

    def _on_text_changed(self):
        self._update_char_count()
        # Refresh window/taskbar title if it's body-derived
        if not (self.title_bar.title_edit.text() or "").strip():
            self._update_window_title("")
        if not self._syncing:
            self.note_changed.emit(self.note_id)

    def _on_checklist_changed(self):
        self._update_char_count()
        if not (self.title_bar.title_edit.text() or "").strip():
            self._update_window_title("")
        if not self._syncing:
            self.note_changed.emit(self.note_id)

    def _update_char_count(self):
        if self._is_list and self.checklist_editor.isVisible():
            count = sum(len(it.get("text", ""))
                        for it in self.checklist_editor.get_items())
        else:
            count = len(self.text_edit.toPlainText())
        self.char_count.setText(f"{count} chars")

    def _on_close(self):
        self.hide()
        self.note_hidden.emit(self.note_id)

    def _on_pin_toggle(self, pinned):
        flags = self.windowFlags()
        if pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()
        self.save_geometry()

    def _on_color_change(self, color_hex, dark_mode=False):
        self._dark_mode = dark_mode
        self._apply_color(color_hex)
        self.note_changed.emit(self.note_id)

    def set_dark_mode(self, dark_mode: bool):
        if dark_mode == self._dark_mode:
            return
        self._dark_mode = dark_mode
        self._apply_color(self._color_hex)

    @property
    def dark_mode(self) -> bool:
        return self._dark_mode

    def _on_title_changed(self, _text):
        if not self._syncing:
            self.note_changed.emit(self.note_id)

    def _toggle_checklist_mode(self):
        """Convert the note between plain text and checklist mode."""
        if self._is_list:
            # Checklist -> plain text: collapse rows to plain text.
            lines = []
            for it in self.checklist_editor.get_items():
                t = (it.get("text") or "").strip()
                if t:
                    lines.append(t)
            self._is_list = False
            self._syncing = True
            self.text_edit.setPlainText("\n".join(lines))
            self._syncing = False
            self.checklist_editor.setVisible(False)
            self.text_edit.setVisible(True)
            self.fmt_toolbar.setVisible(True)
        else:
            # Plain text -> checklist: each non-empty line becomes an item.
            raw = self.text_edit.toPlainText()
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if not lines:
                lines = [""]
            items = [{"text": ln, "checked": False, "indent": 0,
                      "cbx_id": ""} for ln in lines]
            self._is_list = True
            self._syncing = True
            self.checklist_editor.set_items(items)
            self._syncing = False
            self.text_edit.setVisible(False)
            self.checklist_editor.setVisible(True)
            self.fmt_toolbar.setVisible(False)
        self.note_changed.emit(self.note_id)

    @property
    def color_hex(self):
        return self._color_hex

    def get_text(self):
        return self.text_edit.toPlainText()

    def get_html(self):
        return self.text_edit.toHtml()

    def get_title(self):
        return self.title_bar.title_label.text()

    def get_list_items(self):
        """Return the current checklist items.

        When the window is in checklist mode this delegates to the
        live :class:`ChecklistEditor`. Otherwise we fall back to a
        glyph-based parse of the plain text (for older notes that
        haven't been re-rendered yet)."""
        if self._is_list and self.checklist_editor.isVisible():
            return self.checklist_editor.get_items()
        items = []
        for line in self.text_edit.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("☑ "):
                items.append({"text": line[2:], "checked": True})
            elif line.startswith("☐ "):
                items.append({"text": line[2:], "checked": False})
            elif line.startswith("☑"):
                items.append({"text": line[1:].lstrip(), "checked": True})
            elif line.startswith("☐"):
                items.append({"text": line[1:].lstrip(), "checked": False})
            else:
                items.append({"text": line, "checked": False})
        return items

    def set_list_items(self, items: list[dict]):
        """Refresh the checklist editor with new items (used when remote
        sync brings in new content). Caller is responsible for ensuring
        the window is in list mode."""
        self._syncing = True
        try:
            self.checklist_editor.set_items(items)
        finally:
            self._syncing = False

    def _set_checklist_html(self, list_items):
        """Render checkbox list items as HTML with checkbox characters.
        Unchecked items appear first, checked items below."""
        sorted_items = sorted(list_items, key=lambda x: x["checked"])
        lines = []
        for item in sorted_items:
            mark = "☑" if item["checked"] else "☐"
            text = item["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if item["checked"]:
                lines.append(
                    f'<p style="color:#888;"><span style="font-size:14px;">{mark}</span>'
                    f' <s>{text}</s></p>'
                )
            else:
                lines.append(
                    f'<p><span style="font-size:14px;">{mark}</span> {text}</p>'
                )
        self._syncing = True
        self.text_edit.setHtml("".join(lines))
        self._syncing = False

    def set_text(self, text):
        self._syncing = True
        self.text_edit.setPlainText(text)
        self._syncing = False

    def set_html(self, html):
        self._syncing = True
        self.text_edit.setHtml(html)
        self._syncing = False

    def set_title(self, title):
        self._syncing = True
        # Use setText (not textEdited path) to avoid emitting title_changed.
        # Preserve cursor if user has it focused.
        if self.title_bar.title_edit.text() != title:
            self.title_bar.title_edit.setText(title)
            # Re-anchor the view to the start so a long title shows its
            # beginning when the field isn't focused.
            if not self.title_bar.title_edit.hasFocus():
                self.title_bar.title_edit.setCursorPosition(0)
        self._syncing = False
        self._update_window_title(title)

    def save_geometry(self):
        geo = self.geometry()
        set_position(
            self.note_id, geo.x(), geo.y(), geo.width(), geo.height(),
            pinned=getattr(self.title_bar, "_pinned", False),
        )

    def moveEvent(self, event):
        super().moveEvent(event)
        # Aero-snap and other OS-driven moves never trigger our mouse
        # release handler. Persist geometry as the window settles so a
        # snap-then-quit doesn't lose the user's arrangement.
        self._schedule_geometry_save()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_geometry_save()

    def _schedule_geometry_save(self):
        # Debounce: many move/resize events fire per drag, but we only
        # need to write the final geometry to disk.
        from PySide6.QtCore import QTimer
        if not hasattr(self, "_geo_save_timer"):
            self._geo_save_timer = QTimer(self)
            self._geo_save_timer.setSingleShot(True)
            self._geo_save_timer.setInterval(400)
            self._geo_save_timer.timeout.connect(self.save_geometry)
        self._geo_save_timer.start()

    def set_taskbar_visible(self, show):
        """Toggle whether this note appears in the Windows taskbar."""
        if show == self._show_in_taskbar:
            return
        self._show_in_taskbar = show
        flags = self.windowFlags()
        was_visible = self.isVisible()
        if show:
            flags &= ~Qt.WindowType.Tool
        else:
            flags |= Qt.WindowType.Tool
        self.setWindowFlags(flags)
        if was_visible:
            self.show()

    def closeEvent(self, event):
        self.save_geometry()
        event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet(_LIGHT_MENU_QSS)
        delete_action = menu.addAction("🗑  Delete note")
        chosen = menu.exec(event.globalPos())
        if chosen == delete_action:
            self.note_deleted.emit(self.note_id)
