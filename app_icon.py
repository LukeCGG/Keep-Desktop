"""Shared application icon — the yellow rounded "K" sticky note.

Used by the tray, all windows/dialogs, and (via icon.ico) the Windows EXE.
"""

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QPainterPath, QPen,
)


# Sizes baked into the QIcon so Qt picks the closest one for any context
# (tray = 16/22/32, taskbar = 32/48, Alt-Tab/About = 64/128/256).
_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _render_pixmap(size: int, bg_color: str = "#FFF475",
                   border_color: str = "#E0D85A",
                   k_color: str = "#555") -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    # Scale geometry from the original 64px design.
    s = size / 64.0
    margin = max(1.0, 4 * s)
    radius = max(2.0, 12 * s)
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)

    # Sticky-note background.
    painter.setBrush(QColor(bg_color))
    painter.setPen(QColor(border_color))
    painter.drawRoundedRect(rect, radius, radius)

    # Subtle notepad horizontal rules, clipped to the rounded rect.
    line_pen = QPen(QColor(85, 85, 85, 55))   # dark grey, ~22% alpha
    line_pen.setWidthF(max(0.5, 0.75 * s))
    painter.setPen(line_pen)
    clip = QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    painter.save()
    painter.setClipPath(clip)
    # Always draw the same number of evenly-spaced rules regardless of
    # output size, so the design is visually identical at 16px and 256px.
    inset = 6 * s                             # left/right padding inside the note
    line_count = 5
    spacing = rect.height() / (line_count + 1)
    for i in range(1, line_count + 1):
        y = rect.top() + spacing * i
        painter.drawLine(
            QPointF(rect.left() + inset, y),
            QPointF(rect.right() - inset, y),
        )
    painter.restore()

    # The "K" — drawn as a vector path so its bounding rect is reliable
    # regardless of platform/font-rasterizer state (matters on CI / offscreen).
    # Try the real font first; fall back to a hand-drawn geometric K if Qt
    # has no fonts available (e.g. the offscreen platform plugin) and is
    # returning a placeholder square instead of a real glyph outline.
    font = QFont("Segoe UI", max(6, round(34 * s)), QFont.Weight.Bold)
    path = _glyph_path(font, "K")
    if path is None:
        font = QFont()
        font.setBold(True)
        font.setPointSizeF(max(6.0, 34 * s))
        path = _glyph_path(font, "K")

    if path is not None:
        br = path.boundingRect()
        path.translate(
            rect.center().x() - br.center().x(),
            rect.center().y() - br.center().y(),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(k_color))
        painter.drawPath(path)
    else:
        _draw_geometric_k(painter, rect, s, QColor(k_color))

    painter.end()
    return pix


def _glyph_path(font: QFont, char: str) -> QPainterPath | None:
    """Return a path for ``char`` rendered with ``font``, or None if Qt's
    font system can only produce a placeholder rectangle (which happens
    when no real font files are available, e.g. offscreen mode on CI).
    """
    path = QPainterPath()
    path.addText(0.0, 0.0, font, char)
    if path.isEmpty():
        return None
    # A real glyph outline has many path elements (lines + curves). Qt's
    # missing-glyph placeholder is a simple rectangle (~5 elements).
    if path.elementCount() < 10:
        return None
    return path


def _draw_geometric_k(painter: QPainter, rect: QRectF, s: float,
                      color: QColor = None) -> None:
    """Render the letter K from primitive lines, no font required."""
    pen = QPen(color or QColor("#555"))
    pen.setWidthF(max(2.0, 6 * s))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    cx, cy = rect.center().x(), rect.center().y()
    half_h = rect.height() * 0.28
    stem_x = cx - rect.width() * 0.14
    arm_x = cx + rect.width() * 0.20

    painter.drawLine(int(stem_x), int(cy - half_h),
                     int(stem_x), int(cy + half_h))
    painter.drawLine(int(stem_x), int(cy),
                     int(arm_x), int(cy - half_h))
    painter.drawLine(int(stem_x), int(cy),
                     int(arm_x), int(cy + half_h))


def make_icon(color_hex: str | None = None) -> QIcon:
    """Return a QIcon containing the K note rendered at multiple sizes.

    If ``color_hex`` is given, the sticky-note background is tinted to
    that colour (used by per-note taskbar icons). The K text colour is
    auto-chosen for contrast.
    """
    if color_hex is None:
        bg, border, k = "#FFF475", "#E0D85A", "#555"
    else:
        bg = color_hex
        c = QColor(color_hex)
        border = c.darker(115).name()
        # Pick black or white "K" depending on background luminance.
        # Standard relative luminance approximation.
        lum = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255
        k = "#222" if lum > 0.55 else "#f1f3f4"
    icon = QIcon()
    for s in _ICON_SIZES:
        icon.addPixmap(_render_pixmap(s, bg, border, k))
    return icon


def save_ico(path: str) -> None:
    """Write a 256x256 .ico file at ``path`` for use as the Windows EXE icon."""
    pix = _render_pixmap(256)
    if not pix.save(path, "ICO"):
        raise RuntimeError(f"Failed to save ICO file to {path}")
