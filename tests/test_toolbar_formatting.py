"""Formatting toolbar behaviour.

"Remove formatting" had one bug with two visible symptoms:

  * with nothing selected it fell back to selecting the WHOLE document,
    so a mis-click wiped every style in the note; and
  * it used setCharFormat(), which REPLACES every property rather than
    merging — including the font point size that carries heading
    sizing. That left headings at point size 0 (unset), so Qt drew them
    all at the widget's default body size and an H1 became
    indistinguishable from an H2.
"""

from __future__ import annotations

import pytest

from keep_protocol.nested_model import Paragraph, StyledDoc, StyleRun


def R(text, **style):
    return StyleRun(text=text, **style)


def _editor_with(doc):
    from note_window import FormattingToolbar, NoteTextEdit

    editor = NoteTextEdit()
    editor.set_styled_doc(doc)
    return editor, FormattingToolbar(editor)


def _runs(editor):
    from keep_sync_v2 import html_to_styled_doc

    parsed = html_to_styled_doc(editor.toHtml())
    return [(r.text, r.bold, r.italic, r.underline, r.strikethrough)
            for p in parsed.paragraphs for r in p.runs]


def _sizes(editor):
    """(heading level, first-fragment point size) per block."""
    out = []
    block = editor.document().begin()
    while block.isValid():
        pt = None
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.text():
                pt = frag.charFormat().fontPointSize()
                break
            it += 1
        out.append((block.blockFormat().headingLevel(), pt))
        block = block.next()
    return out


STYLED = StyledDoc(paragraphs=[Paragraph(runs=[
    R("keep", bold=True), R(" and "), R("me", italic=True)])])

HEADINGS = StyledDoc(paragraphs=[
    Paragraph(runs=[R("Head one")], heading=1),
    Paragraph(runs=[R("Head two")], heading=2),
    Paragraph(runs=[R("body")], heading=0),
])


# ------------------------------------------------------------------
# Scope
# ------------------------------------------------------------------

def test_clear_formatting_does_nothing_without_a_selection(qapp):  # noqa: ARG001
    editor, toolbar = _editor_with(STYLED)
    before = _runs(editor)
    toolbar._clear_formatting()
    assert _runs(editor) == before


def test_clear_formatting_only_touches_the_selection(qapp):  # noqa: ARG001
    from PySide6.QtGui import QTextCursor

    editor, toolbar = _editor_with(STYLED)
    cur = editor.textCursor()
    cur.setPosition(0)
    cur.setPosition(4, QTextCursor.MoveMode.KeepAnchor)   # "keep"
    editor.setTextCursor(cur)
    toolbar._clear_formatting()

    runs = _runs(editor)
    assert not any(bold for _t, bold, _i, _u, _s in runs), "bold must be gone"
    italics = [t for t, _b, ital, _u, _s in runs if ital]
    assert italics == ["me"], "the unselected italic run must survive"


def test_clear_formatting_over_everything_still_clears_everything(qapp):  # noqa: ARG001
    from PySide6.QtGui import QTextCursor

    editor, toolbar = _editor_with(STYLED)
    cur = editor.textCursor()
    cur.select(QTextCursor.SelectionType.Document)
    editor.setTextCursor(cur)
    toolbar._clear_formatting()

    runs = _runs(editor)
    assert all(not b and not i and not u and not s
               for _t, b, i, u, s in runs)


@pytest.mark.parametrize("style", ["bold", "italic", "underline",
                                   "strikethrough"])
def test_clear_formatting_removes_each_style(qapp, style):  # noqa: ARG001
    from PySide6.QtGui import QTextCursor

    doc = StyledDoc(paragraphs=[Paragraph(runs=[R("word", **{style: True})])])
    editor, toolbar = _editor_with(doc)
    cur = editor.textCursor()
    cur.select(QTextCursor.SelectionType.Document)
    editor.setTextCursor(cur)
    toolbar._clear_formatting()
    assert _runs(editor) == [("word", False, False, False, False)]


# ------------------------------------------------------------------
# Heading sizes
# ------------------------------------------------------------------

def test_heading_sizes_differ(qapp):  # noqa: ARG001
    from note_window import BASE_BODY_PT, BASE_H1_PT, BASE_H2_PT, _scaled_pt

    editor, _toolbar = _editor_with(HEADINGS)
    assert _sizes(editor) == [
        (1, _scaled_pt(BASE_H1_PT)),
        (2, _scaled_pt(BASE_H2_PT)),
        (0, _scaled_pt(BASE_BODY_PT)),
    ]


def test_clear_formatting_preserves_heading_sizes(qapp):  # noqa: ARG001
    """The H1-looks-like-H2 report: wiping the point size left every
    heading unset, so they all rendered at the default body size."""
    from PySide6.QtGui import QTextCursor
    from note_window import BASE_H1_PT, BASE_H2_PT, _scaled_pt

    editor, toolbar = _editor_with(HEADINGS)
    cur = editor.textCursor()
    cur.select(QTextCursor.SelectionType.Document)
    editor.setTextCursor(cur)
    toolbar._clear_formatting()

    sizes = _sizes(editor)
    assert sizes[0] == (1, _scaled_pt(BASE_H1_PT))
    assert sizes[1] == (2, _scaled_pt(BASE_H2_PT))
    assert sizes[0][1] != sizes[1][1], "H1 and H2 must not render alike"


def test_heading_sizes_survive_a_font_scale_change(qapp):  # noqa: ARG001
    import note_window
    from note_window import BASE_H1_PT, BASE_H2_PT, _scaled_pt

    note_window.set_font_scale(1.25)
    try:
        editor, _toolbar = _editor_with(HEADINGS)
        sizes = _sizes(editor)
        assert sizes[0] == (1, _scaled_pt(BASE_H1_PT))
        assert sizes[1] == (2, _scaled_pt(BASE_H2_PT))
        assert sizes[0][1] != sizes[1][1]
    finally:
        note_window.set_font_scale(1.0)


def test_html_render_path_also_sizes_headings_apart(qapp):  # noqa: ARG001
    """The boot-time setHtml path relies on _apply_tight_block_format to
    override Qt's own heading scale."""
    from keep_protocol.nested_model import to_html
    from note_window import BASE_H1_PT, BASE_H2_PT, NoteTextEdit, _scaled_pt

    editor = NoteTextEdit()
    editor.setHtml(to_html(HEADINGS))
    sizes = _sizes(editor)
    assert sizes[0] == (1, _scaled_pt(BASE_H1_PT))
    assert sizes[1] == (2, _scaled_pt(BASE_H2_PT))


def test_clear_formatting_does_not_change_heading_levels(qapp):  # noqa: ARG001
    """It clears character styling, not paragraph structure."""
    from PySide6.QtGui import QTextCursor

    editor, toolbar = _editor_with(HEADINGS)
    cur = editor.textCursor()
    cur.select(QTextCursor.SelectionType.Document)
    editor.setTextCursor(cur)
    toolbar._clear_formatting()
    assert [lvl for lvl, _pt in _sizes(editor)] == [1, 2, 0]
