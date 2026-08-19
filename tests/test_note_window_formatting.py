"""Tests for NoteWindow's FormattingToolbar toggle methods and the
NoteTextEdit link/phone-number highlighter.

Uses the shared `qapp` fixture (conftest.py) — a full QApplication,
needed here since NoteWindow is a real QWidget.
"""

from __future__ import annotations

import pytest


def _make_note_window(qapp):
    from note_window import NoteWindow
    win = NoteWindow(note_id="fmt-test", title="T", text="")
    win.show()
    qapp.processEvents()
    return win


def _fragment_formats(win, block_index):
    """List (text, charFormat) for every fragment in a block."""
    block = win.text_edit.document().findBlockByNumber(block_index)
    out = []
    it = block.begin()
    while not it.atEnd():
        frag = it.fragment()
        if frag.isValid() and frag.text():
            out.append((frag.text(), frag.charFormat()))
        it += 1
    return out


def test_toggle_underline_across_heading_and_body_does_not_leak_font_size(qapp):
    """Regression: selecting from body text UP INTO a heading (so the
    cursor's own position ends inside the heading, not the anchor) and
    toggling underline used to leak the heading's FULL character
    format — including its larger font size — onto the WHOLE
    selection via mergeCurrentCharFormat, not just the toggled
    underline property. The body text visually grew to the heading's
    point size, which read to the user as "the body got bolded"
    (a single formatting-toolbar click affecting an unrelated
    property it was never meant to touch)."""
    from PySide6.QtGui import QTextCursor

    win = _make_note_window(qapp)
    cur = win.text_edit.textCursor()
    cur.insertText("Section Heading")
    cur.insertBlock()
    cur.insertText("Body text line")
    win.text_edit.setTextCursor(cur)
    qapp.processEvents()

    doc = win.text_edit.document()
    block0 = doc.findBlockByNumber(0)
    c = QTextCursor(doc)
    c.setPosition(block0.position())
    win.text_edit.setTextCursor(c)
    win.fmt_toolbar._set_heading(2)
    qapp.processEvents()

    body_size_before = _fragment_formats(win, 1)[0][1].fontPointSize()

    # Drag UP: anchor in the body, cursor ends in the heading.
    block0 = doc.findBlockByNumber(0)
    block1 = doc.findBlockByNumber(1)
    sel = QTextCursor(doc)
    sel.setPosition(block1.position() + 4)
    sel.setPosition(block0.position() + 8, QTextCursor.MoveMode.KeepAnchor)
    win.text_edit.setTextCursor(sel)
    win.fmt_toolbar._toggle_underline()
    qapp.processEvents()

    body_fragments = _fragment_formats(win, 1)
    body_frag = next(text_fmt for text_fmt in body_fragments if "Body" in text_fmt[0])
    assert body_frag[1].fontUnderline() is True, "underline should still apply to 'Body'"
    assert body_frag[1].fontPointSize() == body_size_before, (
        f"'Body' text's font size changed from {body_size_before} to "
        f"{body_frag[1].fontPointSize()} -- the heading's size leaked "
        f"onto it via the underline toggle"
    )
    assert body_frag[1].fontWeight() < 600, (
        "'Body' text should not have become bold as a side effect"
    )


@pytest.mark.parametrize("toggle_method,getter", [
    ("_toggle_bold", lambda fmt: fmt.fontWeight() >= 600),
    ("_toggle_italic", lambda fmt: fmt.fontItalic()),
    ("_toggle_underline", lambda fmt: fmt.fontUnderline()),
    ("_toggle_strikethrough", lambda fmt: fmt.fontStrikeOut()),
])
def test_toggle_only_affects_its_own_property(qapp, toggle_method, getter):
    """Each toggle method must flip ONLY its own property across a
    mixed-size selection, leaving font size (and every other
    property) on each character untouched."""
    from PySide6.QtGui import QTextCursor

    win = _make_note_window(qapp)
    cur = win.text_edit.textCursor()
    cur.insertText("Big Heading")
    cur.insertBlock()
    cur.insertText("small body")
    win.text_edit.setTextCursor(cur)
    qapp.processEvents()

    doc = win.text_edit.document()
    block0 = doc.findBlockByNumber(0)
    c = QTextCursor(doc)
    c.setPosition(block0.position())
    win.text_edit.setTextCursor(c)
    win.fmt_toolbar._set_heading(1)
    qapp.processEvents()

    body_size_before = _fragment_formats(win, 1)[0][1].fontPointSize()

    block0 = doc.findBlockByNumber(0)
    block1 = doc.findBlockByNumber(1)
    sel = QTextCursor(doc)
    sel.setPosition(block1.position() + 3)
    sel.setPosition(block0.position() + 4, QTextCursor.MoveMode.KeepAnchor)
    win.text_edit.setTextCursor(sel)
    getattr(win.fmt_toolbar, toggle_method)()
    qapp.processEvents()

    body_frag = _fragment_formats(win, 1)[0]
    assert getter(body_frag[1]) is True
    assert body_frag[1].fontPointSize() == body_size_before, (
        f"{toggle_method} leaked the heading's font size onto the body text"
    )


def _link_highlight_ranges(win, block_index):
    """QSyntaxHighlighter applies formatting via the block's text
    LAYOUT (an overlay), NOT the document's own stored character
    format — frag.charFormat() never reflects it. This is the correct
    way to check what the highlighter actually applied. Extracts the
    needed values immediately — the QTextCharFormat wrapper objects
    inside QTextLayout.FormatRange don't outlive this call."""
    block = win.text_edit.document().findBlockByNumber(block_index)
    layout = block.layout()
    if layout is None:
        return []
    return [
        (f.start, f.length, f.format.fontUnderline(), f.format.foreground().color().name())
        for f in layout.formats()
    ]


def test_boot_time_html_load_matches_sync_driven_heading_size(qapp):
    """Regression: a note's very first render (boot, loading html=
    to_html(doc) from the disk cache before any styled_doc exists)
    used setHtml, whose Qt HTML importer sizes <h1>/<h2> with its own
    built-in heading scale -- not the app's BASE_H1_PT/BASE_H2_PT.
    The very next sync-driven refresh (set_styled_doc, which sets an
    explicit point size) then made the heading visibly shrink. Both
    render paths must agree from the start."""
    from note_window import NoteWindow, BASE_H1_PT, BASE_H2_PT, _scaled_pt
    from keep_protocol.nested_model import StyledDoc, Paragraph, StyleRun, to_html

    doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Title")], heading=1),
        Paragraph(runs=[StyleRun(text="Section")], heading=2),
    ])

    win = NoteWindow(note_id="boot-size-test", title="T",
                      html=to_html(doc), text=doc.plain_text)
    win.show()
    qapp.processEvents()

    h1_frag = _fragment_formats(win, 0)[0][1]
    h2_frag = _fragment_formats(win, 1)[0][1]
    assert h1_frag.fontPointSize() == _scaled_pt(BASE_H1_PT)
    assert h2_frag.fontPointSize() == _scaled_pt(BASE_H2_PT)


def test_phone_number_linkified_immediately_on_construction(qapp):
    """Regression: the phone/URL syntax highlighter (_link_highlighter)
    used to be constructed as the accidental tail end of
    set_styled_doc()'s body (a structural mis-nesting — it read as
    part of __init__ but was actually indented one level too deep,
    inside set_styled_doc). NoteWindow's INITIAL load uses
    setPlainText/setHtml, not set_styled_doc, so the highlighter was
    never created at all until the first sync-driven refresh happened
    to call set_styled_doc — meaning phone numbers and links showed
    as plain text on boot and only lit up after a sync eventually
    triggered a refresh, never immediately."""
    from note_window import NoteWindow

    win = NoteWindow(note_id="linkify-test", title="T", text="Call me: 0478114466")
    win.show()
    qapp.processEvents()

    assert hasattr(win.text_edit, "_link_highlighter"), (
        "the link highlighter must exist right after construction, "
        "before any sync/refresh"
    )
    ranges = _link_highlight_ranges(win, 0)
    assert ranges, "no highlight ranges found -- phone number was not linkified"
    start, length, underline, color = ranges[0]
    highlighted_text = win.text_edit.toPlainText()[start:start + length]
    assert highlighted_text == "0478114466"
    assert underline is True
    assert color == "#1a73e8"


def test_set_title_does_not_prematurely_clear_syncing_flag(qapp):
    """Regression: set_title() used to set self._syncing = False
    SYNCHRONOUSLY at its own end, instead of via the deferred
    end_sync_render()/_clear_syncing pattern every other sync-driven
    render uses. _refresh_window calls set_title() immediately after
    a guarded content render (set_styled_doc/set_html/set_text),
    which relies on _syncing staying True until the link/phone
    highlighter's late textChanged (fired one event-loop tick after
    the render call returns) has had its chance to be suppressed. A
    synchronous clear in set_title reopened that window early: the
    late signal then landed with _syncing already False and was
    misread as a real user edit -- re-marking the note dirty and
    dropping its just-adopted styled_doc cache even though the user
    never touched the note, purely because a sync-driven refresh
    happened to also update the title in the same cycle (which it
    always does)."""
    from keep_protocol.nested_model import StyledDoc, Paragraph, StyleRun

    win = _make_note_window(qapp)
    doc = StyledDoc(sct_id="sct.x", paragraphs=[Paragraph(runs=[StyleRun(text="hello")])])

    # Reproduce _refresh_window's exact sequence: a guarded content
    # render followed immediately by set_title.
    win._syncing = True
    try:
        win.text_edit.set_styled_doc(doc)
    finally:
        win.end_sync_render()
    win.set_title("New Title")

    assert win._syncing is True, (
        "set_title must not clear _syncing synchronously -- it must "
        "stay True until the deferred clear fires on the next tick"
    )

    qapp.processEvents()
    assert win._syncing is False, (
        "the deferred clear must still eventually run"
    )
