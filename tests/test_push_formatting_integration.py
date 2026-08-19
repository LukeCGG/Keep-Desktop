"""App-level checks that a formatting edit survives the whole push path.

The unit tests in test_formatting_push_regressions.py pin the encoder
(StyledDoc in, ops out). These go one level up and drive
``KeepSyncV2.push_note`` with a mocked client, starting from HTML that a
REAL ``NoteTextEdit`` produced — because the widget is where the two
lossy conversions live (StyledDoc -> widget -> Qt HTML -> StyledDoc),
and a bug in either direction looks exactly like "the app isn't sending
my formatting to the web".
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from keep_protocol.models import Note as ServerNote
from keep_protocol.nested_model import (
    Paragraph,
    StyleRun,
    StyledDoc,
    decode_chunks,
    encode_doc,
    to_html,
)
from keep_sync import KeepNote
from keep_sync_v2 import KeepSyncV2

SCT = "sct.abc"


def _server_note(doc: StyledDoc) -> ServerNote:
    doc.sct_id = SCT
    chunks = [json.dumps(encode_doc(doc))]
    return ServerNote(
        id="n1", server_id="n1", type="NOTE", title="", text=doc.plain_text,
        color="DEFAULT", is_archived=False, is_pinned=False,
        is_trashed=False, is_deleted=False, created=None, updated=None,
        user_edited=None, sort_value=0, base_version="0", sct_id=SCT,
        serialized_chunks=chunks, nested_revision="0",
        indexable_text=doc.plain_text,
        raw={"id": "n1", "kind": "notes#node", "parentId": "root",
             "type": "NOTE", "trashState": 0, "deletionState": 0,
             "isPinned": False, "title": ""},
    )


@pytest.fixture
def sync():
    s = KeepSyncV2()
    s._authenticated = True
    s._client = MagicMock()
    s._client.notes = {}
    s._client.sync.return_value = None
    s._client.pop_stale_snapshot_ids.return_value = set()
    s._client.is_snapshot_stale.return_value = False
    return s


def _push(sync, server_doc: StyledDoc, html: str, text: str):
    """Seed the server + baseline, push `html`, return the pushed doc."""
    server = _server_note(server_doc)
    sync._server_notes["n1"] = server
    sync._base_text["n1"] = server_doc.plain_text
    sync._base_doc["n1"] = decode_chunks(server.serialized_chunks)
    sync.push_note(KeepNote(id="n1", text=text, html=html,
                            color_hex="#FFF475"))
    calls = sync._client.update_text_diff.call_args_list
    return calls[0][0][1] if calls else None


def _rendered(doc: StyledDoc):
    """Render `doc` into a real note widget; return (widget, html, text)."""
    from note_window import NoteTextEdit

    editor = NoteTextEdit()
    editor.set_styled_doc(doc)
    return editor, editor.toHtml(), editor.toPlainText()


def _shape(doc: StyledDoc) -> list:
    return [(p.heading, [(r.text, r.style_tuple()) for r in p.runs if r.text])
            for p in doc.paragraphs]


SAMPLE = [
    Paragraph(runs=[StyleRun(text="Title")], heading=1),
    Paragraph(runs=[], heading=0),
    Paragraph(runs=[StyleRun(text="bold", bold=True),
                    StyleRun(text=" tail")], heading=0),
    Paragraph(runs=[StyleRun(text="Section")], heading=2),
]


def _sample() -> StyledDoc:
    import copy
    return StyledDoc(paragraphs=copy.deepcopy(SAMPLE))


def test_untouched_note_pushes_nothing(sync, qapp):  # noqa: ARG001
    """Open a note, touch nothing, let the sync tick fire.

    Rendering a doc into the widget stamps a font size on every run to
    size headings, and Qt then splits its text into fragments on that
    size. Read back, those fragments used to look like a formatting
    change against the server's own copy, so every 30-second tick
    pushed `as` ops (and minted a fresh ps_hdid) for a note nobody had
    edited — bumping userEdited and racing any real web edit.
    """
    doc = _sample()
    _editor, html, text = _rendered(doc)
    assert _push(sync, doc, html, text) is None
    sync._client.update_text_diff.assert_not_called()


def test_bold_plus_typing_in_one_cycle_reaches_the_server(sync, qapp):  # noqa: ARG001
    """Bold a word and keep typing; one autosave carries both."""
    from PySide6.QtGui import QFont, QTextCharFormat, QTextCursor

    doc = StyledDoc(paragraphs=[
        Paragraph(runs=[StyleRun(text="hello world")], heading=0)])
    editor, _html, _text = _rendered(doc)

    cur = editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    editor.setTextCursor(cur)
    editor.insertPlainText("!")
    cur = editor.textCursor()
    cur.setPosition(0)
    cur.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
    fmt = QTextCharFormat()
    fmt.setFontWeight(QFont.Weight.Bold)
    cur.mergeCharFormat(fmt)

    pushed = _push(sync, doc, editor.toHtml(), editor.toPlainText())
    assert pushed is not None, "a formatting change must be pushed"
    assert _shape(pushed) == [(0, [
        ("hello", (True, False, False, False)),
        (" world!", (False, False, False, False)),
    ])]


def test_heading_cleared_while_typing_in_it_reaches_the_server(sync, qapp):  # noqa: ARG001
    from note_window import FormattingToolbar
    from PySide6.QtGui import QTextCursor

    doc = StyledDoc(paragraphs=[
        Paragraph(runs=[StyleRun(text="Title")], heading=1),
        Paragraph(runs=[StyleRun(text="body")], heading=0),
    ])
    editor, _html, _text = _rendered(doc)
    toolbar = FormattingToolbar(editor)

    cur = editor.textCursor()
    cur.setPosition(5)
    editor.setTextCursor(cur)
    editor.insertPlainText("X")
    cur = editor.textCursor()
    cur.setPosition(2)
    editor.setTextCursor(cur)
    toolbar._set_heading(0)

    pushed = _push(sync, doc, editor.toHtml(), editor.toPlainText())
    assert pushed is not None
    assert [p.heading for p in pushed.paragraphs] == [0, 0]
    assert pushed.plain_text == "TitleX\nbody"


def test_backspace_merge_pushes_the_heading_the_editor_shows(sync, qapp):  # noqa: ARG001
    """Backspacing a heading line into the body line above it merges the
    two. The editor keeps the FIRST block's format (body), but the
    server would inherit the SECOND paragraph's heading unless the push
    clears it explicitly."""
    from PySide6.QtGui import QTextCursor

    doc = _sample()
    editor, _html, _text = _rendered(doc)
    block = editor.document().findBlockByNumber(3)
    cur = editor.textCursor()
    cur.setPosition(block.position() - 1)
    cur.setPosition(block.position(), QTextCursor.MoveMode.KeepAnchor)
    cur.removeSelectedText()

    pushed = _push(sync, doc, editor.toHtml(), editor.toPlainText())
    assert pushed is not None
    widget_headings = [
        editor.document().findBlockByNumber(i).blockFormat().headingLevel()
        for i in range(editor.document().blockCount())
    ]
    assert [p.heading for p in pushed.paragraphs] == widget_headings
    assert pushed.paragraphs[-1].heading == 0


def test_formatting_only_change_is_not_skipped_as_a_no_op(sync, qapp):  # noqa: ARG001
    """Plain text is identical, so only the styling can reveal the edit."""
    server_doc = StyledDoc(paragraphs=[
        Paragraph(runs=[StyleRun(text="hello world")], heading=0)])
    local = StyledDoc(paragraphs=[Paragraph(runs=[
        StyleRun(text="hello", bold=True), StyleRun(text=" world")])])

    pushed = _push(sync, server_doc, to_html(local), "hello world")
    assert pushed is not None
    assert _shape(pushed) == _shape(local)
