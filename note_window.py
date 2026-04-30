"""Individual sticky-note window widget."""

from PySide6.QtCore import Qt, Signal, QPoint, QSize, QEvent
from PySide6.QtGui import QFont, QColor, QCursor, QPainter, QPen, QTextCharFormat
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QLabel, QLineEdit,
    QPushButton, QMenu, QSizeGrip, QGraphicsDropShadowEffect,
    QGridLayout,
)

from config import (
    KEEP_COLORS, DEFAULT_WIDTH, DEFAULT_HEIGHT, MIN_WIDTH, MIN_HEIGHT,
    set_position, get_position,
)


# ── Color Picker Popup ────────────────────────────────────────────────

class ColorPickerPopup(QWidget):
    """Grid of colored circles on a dark background for picking a note color."""
    color_chosen = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet("background: #3C4043; border-radius: 8px;")

        layout = QGridLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(10, 10, 10, 10)

        colors = list(KEEP_COLORS.items())
        cols = 4
        for i, (name, hex_val) in enumerate(colors):
            btn = QPushButton()
            btn.setFixedSize(30, 30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {hex_val};
                    border: 2px solid rgba(255,255,255,0.3);
                    border-radius: 15px;
                }}
                QPushButton:hover {{
                    border: 2px solid #ffffff;
                }}
            """)
            # Add space before capitals for tooltip: "DarkBlue" → "Dark Blue"
            import re
            display_name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
            btn.setToolTip(display_name)
            btn.clicked.connect(lambda checked, hv=hex_val: self._on_pick(hv))
            layout.addWidget(btn, i // cols, i % cols)

    def _on_pick(self, hex_val):
        self.color_chosen.emit(hex_val)
        self.close()


# ── Formatting Toolbar ─────────────────────────────────────────────────

class FormattingToolbar(QWidget):
    """Small toolbar for bold / italic / underline / strikethrough."""

    def __init__(self, text_edit, color_hex="#FFF475", parent=None):
        super().__init__(parent)
        self._text_edit = text_edit
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

    def _update_states(self):
        fmt = self._text_edit.currentCharFormat()
        self.bold_btn.setChecked(fmt.fontWeight() >= QFont.Weight.Bold)
        self.italic_btn.setChecked(fmt.fontItalic())
        self.underline_btn.setChecked(fmt.fontUnderline())
        self.strike_btn.setChecked(fmt.fontStrikeOut())

    def update_color(self, color_hex):
        darker = QColor(color_hex).darker(105).name()
        self.setStyleSheet(f"FormattingToolbar {{ background: {darker}; }}")


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

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(0, 0, 0, 110))
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
            self._drag_pos = event.globalPosition().toPoint() - self.window().pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, _event):
        self._drag_pos = None
        win = self.window()
        if hasattr(win, "save_geometry"):
            win.save_geometry()


class TitleBar(QWidget):
    """Custom draggable title bar for a note window."""

    close_clicked = Signal()
    pin_toggled = Signal(bool)
    color_chosen = Signal(str)
    delete_clicked = Signal()
    title_changed = Signal(str)
    checklist_toggled = Signal()

    def __init__(self, title="", color_hex="#FFF475", parent=None):
        super().__init__(parent)
        self.setFixedHeight(32)
        self._drag_pos = None
        self._pinned = False
        self._color_hex = color_hex

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)

        # Dedicated drag handle (the title field steals clicks now that
        # it's editable, so we give the user an explicit grab area).
        self.drag_handle = DragHandle(self)
        layout.addWidget(self.drag_handle)

        self.title_edit = QLineEdit(title)
        self.title_edit.setPlaceholderText("Title")
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

        self.checklist_btn = QPushButton("☑")
        self.checklist_btn.setToolTip("Convert to / from checklist")
        self.checklist_btn.setStyleSheet(btn_style)
        self.checklist_btn.setFixedSize(28, 28)
        self.checklist_btn.clicked.connect(self.checklist_toggled.emit)
        layout.addWidget(self.checklist_btn)

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

    def _toggle_pin(self):
        self._pinned = not self._pinned
        self.pin_btn.setText("📍" if self._pinned else "📌")
        self.pin_toggled.emit(self._pinned)

    def _show_color_picker(self):
        self._color_popup = ColorPickerPopup(self)
        self._color_popup.color_chosen.connect(self.color_chosen.emit)
        pos = self.color_btn.mapToGlobal(QPoint(0, self.color_btn.height()))
        self._color_popup.move(pos)
        self._color_popup.show()

    def set_pinned(self, pinned):
        self._pinned = pinned
        self.pin_btn.setText("📍" if pinned else "📌")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.window().pos()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        # Save position after drag
        win = self.window()
        if hasattr(win, "save_geometry"):
            win.save_geometry()


class NoteTextEdit(QTextEdit):
    """Rich text editor area for the note body."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFont("Segoe UI", 10))
        self.setAcceptRichText(True)
        self.setStyleSheet("""
            QTextEdit {
                border: none;
                background: transparent;
                padding: 8px;
                color: #333;
                selection-background-color: rgba(0,0,0,0.15);
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

    def mousePressEvent(self, event):
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
        # Auto-insert checkbox prefix on Enter in list notes
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            cursor = self.textCursor()
            block = cursor.block()
            text = block.text()
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


class NoteWindow(QWidget):
    """A single floating sticky note window."""

    note_changed = Signal(str)   # note_id
    note_hidden = Signal(str)    # note_id
    note_deleted = Signal(str)   # note_id

    def __init__(self, note_id, title="", text="", html="", color_hex="#FFF475",
                 pinned=False, show_in_taskbar=False, list_items=None, parent=None):
        super().__init__(parent)
        self.note_id = note_id
        self._color_hex = color_hex
        self._syncing = False  # flag to suppress change signals during sync
        self._show_in_taskbar = show_in_taskbar
        self._is_list = bool(list_items)

        # Explicit per-window icon. QApplication's windowIcon usually
        # cascades, but on Windows the taskbar reads the per-window icon
        # directly — setting it here guarantees the K shows up there too.
        from PySide6.QtWidgets import QApplication
        app_icon = QApplication.instance().windowIcon() if QApplication.instance() else None
        if app_icon is not None and not app_icon.isNull():
            self.setWindowIcon(app_icon)

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
        if list_items:
            self._set_checklist_html(list_items)
        elif html:
            self.text_edit.setHtml(html)
        else:
            self.text_edit.setPlainText(text)
        inner.addWidget(self.text_edit, stretch=1)

        # Formatting toolbar
        self.fmt_toolbar = FormattingToolbar(self.text_edit, color_hex, self)
        inner.addWidget(self.fmt_toolbar)

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
        self.container.setStyleSheet(f"""
            QWidget {{
                background: {color_hex};
                border-radius: 8px;
            }}
        """)
        self.title_bar.update_color(color_hex)
        self.fmt_toolbar.update_color(color_hex)

    def _on_text_changed(self):
        self._update_char_count()
        # Refresh window/taskbar title if it's body-derived
        if not (self.title_bar.title_edit.text() or "").strip():
            self._update_window_title("")
        if not self._syncing:
            self.note_changed.emit(self.note_id)

    def _update_char_count(self):
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

    def _on_color_change(self, color_hex):
        self._apply_color(color_hex)
        self.note_changed.emit(self.note_id)

    def _on_title_changed(self, _text):
        if not self._syncing:
            self.note_changed.emit(self.note_id)

    def _toggle_checklist_mode(self):
        """Convert the note between plain text and checklist mode."""
        if self._is_list:
            # Checklist -> plain text: strip the checkbox prefixes.
            lines = []
            for line in self.text_edit.toPlainText().splitlines():
                stripped = line.lstrip("\u2611\u2610 ").rstrip()
                lines.append(stripped)
            self._is_list = False
            self._syncing = True
            self.text_edit.setPlainText("\n".join(lines))
            self._syncing = False
        else:
            # Plain text -> checklist: each non-empty line becomes an item.
            raw = self.text_edit.toPlainText()
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if not lines:
                lines = [""]
            items = [{"text": ln, "checked": False} for ln in lines]
            self._is_list = True
            self._set_checklist_html(items)
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
        """Parse checkbox items from the text content. Returns list of {text, checked}."""
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
        self._syncing = False
        self._update_window_title(title)

    def save_geometry(self):
        geo = self.geometry()
        set_position(self.note_id, geo.x(), geo.y(), geo.width(), geo.height())

    def moveEvent(self, event):
        super().moveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)

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
        menu.setStyleSheet("""
            QMenu { background: #fff; border: 1px solid #ccc; border-radius: 6px; padding: 4px; }
            QMenu::item { padding: 6px 16px; border-radius: 4px; }
            QMenu::item:selected { background: #e0e0e0; }
        """)
        delete_action = menu.addAction("🗑  Delete note")
        chosen = menu.exec(event.globalPos())
        if chosen == delete_action:
            self.note_deleted.emit(self.note_id)
