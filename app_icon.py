"""Shared application icon — the yellow rounded "K" sticky note.

Used by the tray, all windows/dialogs, and (via icon.ico) the Windows EXE.
"""

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QFontMetricsF, QPainterPath, QPen,
)


# Sizes baked into the QIcon so Qt picks the closest one for any context
# (tray = 16/22/32, taskbar = 32/48, Alt-Tab/About = 64/128/256).
_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _render_pixmap(size: int) -> QPixmap:
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
    painter.setBrush(QColor("#FFF475"))
    painter.setPen(QColor("#E0D85A"))
    painter.drawRoundedRect(rect, radius, radius)

    # Subtle notepad horizontal rules, clipped to the rounded rect.
    line_pen = QPen(QColor(85, 85, 85, 55))   # dark grey, ~22% alpha
    line_pen.setWidthF(max(0.5, 0.75 * s))
    painter.setPen(line_pen)
    clip = QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    painter.save()
    painter.setClipPath(clip)
    spacing = 10 * s                          # ~10px gap on the 64px design
    inset = 6 * s                             # left/right padding inside the note
    y = rect.top() + spacing * 1.4
    while y < rect.bottom() - spacing * 0.4:
        painter.drawLine(
            QRectF(rect.left() + inset, y, rect.width() - 2 * inset, 0).topLeft(),
            QRectF(rect.left() + inset, y, rect.width() - 2 * inset, 0).topRight(),
        )
        y += spacing
    painter.restore()

    # The "K", visually centered using the glyph's tight bounding rect.
    font = QFont("Segoe UI", max(6, round(34 * s)), QFont.Weight.Bold)
    painter.setFont(font)
    painter.setPen(QColor("#555"))
    fm = QFontMetricsF(font)
    tight = fm.tightBoundingRect("K")
    tx = rect.center().x() - tight.center().x()
    ty = rect.center().y() - tight.center().y()
    painter.drawText(int(round(tx)), int(round(ty)), "K")
    painter.end()
    return pix


def make_icon() -> QIcon:
    """Return a QIcon containing the K note rendered at multiple sizes."""
    icon = QIcon()
    for s in _ICON_SIZES:
        icon.addPixmap(_render_pixmap(s))
    return icon


def save_ico(path: str) -> None:
    """Write a 256x256 .ico file at ``path`` for use as the Windows EXE icon."""
    pix = _render_pixmap(256)
    if not pix.save(path, "ICO"):
        raise RuntimeError(f"Failed to save ICO file to {path}")
