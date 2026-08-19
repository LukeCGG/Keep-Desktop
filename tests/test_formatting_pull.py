"""Pulling a FORMATTING-ONLY change down from the web.

Plain text can't reveal one — bold/italic/heading toggles never change a
single character — so the decision rests entirely on comparing
structured docs. Two things broke that:

  * `note.styled_doc` is cleared by AppController._on_note_changed on
    every body edit, so between a local edit and the next pull there is
    no local doc to compare against. decide_merge treated that as "our
    own push echoing back" and skipped the refresh — while still
    ADOPTING the remote content into the cache. From then on every
    later pull saw local == remote and found nothing to refresh for, so
    the window kept showing the old formatting forever.

  * "Sync now" ran an INCREMENTAL pull, which can only return what the
    cursor says is new — nothing, if the cursor already moved past it.

Qt indexes strings in UTF-16 code units while Python indexes codepoints,
so the editor's own offset arithmetic is covered here too: one emoji
earlier in a line used to drag every later link highlight one character
out of place.
"""

from __future__ import annotations

import pytest

from keep_protocol.nested_model import (
    Paragraph,
    StyleRun,
    StyledDoc,
    u16_len,
)
from sync_merge import MergeAction, decide_merge

EMOJI = chr(0x1F60E)


def R(text, **style):
    return StyleRun(text=text, **style)


def doc(*runs):
    return StyledDoc(paragraphs=[Paragraph(runs=list(runs))])


class _Note:
    """Minimal stand-in with the shape decide_merge duck-types."""

    def __init__(self, **kw):
        self.id = "n1"
        self.title = "T"
        self.text = "hello world"
        self.color_hex = "#FFF475"
        self.html = ""
        self.is_list = False
        self.list_items = []
        self.__dict__.update(kw)


PLAIN = doc(R("hello world"))
BOLDED = doc(R("hello", bold=True), R(" world"))


# ------------------------------------------------------------------
# decide_merge
# ------------------------------------------------------------------

def test_formatting_only_change_detected_via_styled_doc():
    local = _Note(styled_doc=PLAIN)
    remote = _Note(styled_doc=BOLDED)
    d = decide_merge(local=local, remote=remote, is_dirty=False,
                     user_busy=False)
    assert d.html_changed is True
    assert d.refresh_window is True


def test_formatting_only_change_detected_after_a_local_edit_cleared_styled_doc():
    """The reported failure: styled_doc was wiped by a local edit, so
    there was nothing to compare and the change was silently dropped
    from the refresh decision."""
    local = _Note()               # no styled_doc attribute at all
    remote = _Note(styled_doc=BOLDED)
    d = decide_merge(local=local, remote=remote, is_dirty=False,
                     user_busy=False, local_rendered_doc=PLAIN)
    assert d.html_changed is True
    assert d.refresh_window is True


def test_identical_formatting_does_not_trigger_a_refresh():
    """The rendered-doc fallback must not cause a refresh (and its
    scroll reset) on every no-op sync tick."""
    local = _Note()
    remote = _Note(styled_doc=PLAIN)
    d = decide_merge(local=local, remote=remote, is_dirty=False,
                     user_busy=False, local_rendered_doc=PLAIN)
    assert d.html_changed is False
    assert d.refresh_window is False


def test_notes_styled_doc_wins_over_the_rendered_doc():
    """styled_doc is the fresher of the two when both exist."""
    local = _Note(styled_doc=BOLDED)
    remote = _Note(styled_doc=BOLDED)
    d = decide_merge(local=local, remote=remote, is_dirty=False,
                     user_busy=False, local_rendered_doc=PLAIN)
    assert d.html_changed is False


def test_no_local_doc_at_all_still_skips():
    """Nothing rendered yet this run: no structured comparison is
    possible, so keep the old conservative behaviour."""
    local = _Note()
    remote = _Note(styled_doc=BOLDED)
    d = decide_merge(local=local, remote=remote, is_dirty=False,
                     user_busy=False)
    assert d.html_changed is False


def test_dirty_note_is_still_skipped_entirely():
    local = _Note()
    remote = _Note(styled_doc=BOLDED)
    d = decide_merge(local=local, remote=remote, is_dirty=True,
                     user_busy=False, local_rendered_doc=PLAIN)
    assert d.action is MergeAction.SKIP_DIRTY


# ------------------------------------------------------------------
# Qt offsets are UTF-16, Python offsets are codepoints
# ------------------------------------------------------------------

def test_link_highlight_lands_on_the_url_after_an_emoji(qapp):  # noqa: ARG001
    """QSyntaxHighlighter.setFormat indexes in UTF-16 code units; the
    regex offsets feeding it are Python codepoints. One emoji earlier in
    the line shifted the blue underline one character left."""
    from note_window import NoteTextEdit

    editor = NoteTextEdit()
    editor.setPlainText("a " + EMOJI + " see https://example.com now")
    block = editor.document().firstBlock()
    text = block.text()

    formats = block.layout().formats()
    assert formats, "the link should be highlighted"
    fmt = formats[0]
    units = text.encode("utf-16-le")
    highlighted = units[fmt.start * 2:(fmt.start + fmt.length) * 2].decode(
        "utf-16-le", "replace")
    assert highlighted == "https://example.com"


def test_link_highlight_unaffected_without_astral_characters(qapp):  # noqa: ARG001
    from note_window import NoteTextEdit

    editor = NoteTextEdit()
    editor.setPlainText("see https://example.com now")
    block = editor.document().firstBlock()
    text = block.text()
    fmt = block.layout().formats()[0]
    units = text.encode("utf-16-le")
    highlighted = units[fmt.start * 2:(fmt.start + fmt.length) * 2].decode(
        "utf-16-le", "replace")
    assert highlighted == "https://example.com"


def test_link_at_returns_utf16_offsets_after_an_emoji(qapp):  # noqa: ARG001
    """_link_at mixes cursor offsets (UTF-16) with regex offsets
    (codepoints); the span it returns is used to build a QTextCursor,
    so it must be in Qt's units.

    The viewport hit-test is stubbed out — offscreen widgets have no
    real layout to hit — leaving exactly the offset arithmetic under
    test.
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QTextCursor
    from note_window import NoteTextEdit

    editor = NoteTextEdit()
    text = "a " + EMOJI + " see https://example.com now"
    editor.setPlainText(text)
    block = editor.document().firstBlock()
    cp_start = text.index("https")
    expected_start = block.position() + u16_len(text[:cp_start])

    cur = QTextCursor(editor.document())
    cur.setPosition(expected_start)
    editor.cursorForPosition = lambda _pos: cur      # stub the hit-test

    hit = editor._link_at(QPoint(0, 0))
    assert hit is not None, "the URL should be found"
    url, start, end = hit
    assert url == "https://example.com"
    assert start == expected_start
    assert end == start + u16_len("https://example.com")

    # And the span must actually select the URL in the document.
    sel = QTextCursor(editor.document())
    sel.setPosition(start)
    sel.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    assert sel.selectedText() == "https://example.com"


@pytest.mark.parametrize("prefix", ["", "x", EMOJI, EMOJI + EMOJI, "é"])
def test_link_span_matches_the_document_for_any_prefix(qapp, prefix):  # noqa: ARG001
    from note_window import NoteTextEdit
    from PySide6.QtGui import QTextCursor

    editor = NoteTextEdit()
    editor.setPlainText(prefix + " https://example.com")
    block = editor.document().firstBlock()
    text = block.text()
    cp_start = text.index("https")
    start = block.position() + u16_len(text[:cp_start])

    cur = QTextCursor(editor.document())
    cur.setPosition(start)
    cur.setPosition(start + u16_len("https://example.com"),
                    QTextCursor.MoveMode.KeepAnchor)
    assert cur.selectedText() == "https://example.com"
