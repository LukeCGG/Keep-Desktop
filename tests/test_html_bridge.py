"""Tests for the HTML <-> StyledDoc bridge in keep_sync_v2.

Requires Qt (uses QTextDocument under the hood).
"""

from __future__ import annotations


def test_plain_text_round_trip(qapp):  # noqa: ARG001
    from keep_sync_v2 import html_to_styled_doc
    doc = html_to_styled_doc("hello world", sct_id="sct.x")
    assert doc.plain_text == "hello world"
    assert doc.sct_id == "sct.x"


def test_empty_html_returns_empty_doc(qapp):  # noqa: ARG001
    from keep_sync_v2 import html_to_styled_doc
    doc = html_to_styled_doc("", sct_id="sct.x")
    assert doc.paragraphs == []
    assert doc.sct_id == "sct.x"


def test_bold_run_detected(qapp):  # noqa: ARG001
    from keep_sync_v2 import html_to_styled_doc
    doc = html_to_styled_doc("<p>before <b>BOLD</b> after</p>", sct_id="sct.x")
    flat = [
        (run.text, run.bold)
        for p in doc.paragraphs for run in p.runs
    ]
    # The "BOLD" run must be marked bold; surrounding runs must not.
    bold_runs = [t for t, b in flat if b]
    nonbold_runs = [t for t, b in flat if not b]
    assert any("BOLD" in t for t in bold_runs)
    assert any("before" in t for t in nonbold_runs)


def test_italic_run_detected(qapp):  # noqa: ARG001
    from keep_sync_v2 import html_to_styled_doc
    doc = html_to_styled_doc("<p><i>slanted</i></p>", sct_id="sct.x")
    runs = [r for p in doc.paragraphs for r in p.runs]
    assert any(r.italic for r in runs)


def test_paragraph_split(qapp):  # noqa: ARG001
    from keep_sync_v2 import html_to_styled_doc
    doc = html_to_styled_doc("<p>one</p><p>two</p>", sct_id="sct.x")
    texts = [p.text for p in doc.paragraphs]
    assert texts == ["one", "two"]


def test_heading_level_extracted(qapp):  # noqa: ARG001
    from keep_sync_v2 import html_to_styled_doc
    doc = html_to_styled_doc("<h1>title</h1><p>body</p>", sct_id="sct.x")
    assert doc.paragraphs[0].heading == 1
    assert doc.paragraphs[1].heading == 0


def test_heading_default_bold_is_stripped(qapp):  # noqa: ARG001
    """Qt makes <h1> bold by default; our parser must strip that
    presentation-bold or we'd round-trip headings as bold-everywhere."""
    from keep_sync_v2 import html_to_styled_doc
    doc = html_to_styled_doc("<h1>plain heading</h1>", sct_id="sct.x")
    runs = doc.paragraphs[0].runs
    assert runs and not any(r.bold for r in runs)


def test_heading_explicit_whole_heading_bold_is_preserved(qapp):  # noqa: ARG001
    """Regression: an EARLIER heuristic here stripped bold from a
    heading whenever every run in it was bold, reasoning that this
    must be Qt's own auto-bold-headings presentation rather than real
    user intent. That heuristic predated (and became wrong once we
    added) the defaultStyleSheet override above, which already makes
    fragment.charFormat().fontWeight() honest -- so a user who
    selects a WHOLE heading and explicitly toggles bold on (making
    every run in it bold, indistinguishable from the old heuristic's
    trigger condition) had that genuine edit silently discarded on
    every push: the heading round-tripped back as un-bold."""
    from keep_sync_v2 import html_to_styled_doc
    doc = html_to_styled_doc("<h1><b>Whole Heading Bolded</b></h1>", sct_id="sct.x")
    runs = doc.paragraphs[0].runs
    assert runs and all(r.bold for r in runs), (
        "explicit whole-heading bold must survive the HTML round-trip"
    )


def test_heading_partial_bold_still_preserved(qapp):  # noqa: ARG001
    """Sanity check alongside the whole-heading case above: a heading
    with only PART of its text bolded must keep exactly that split."""
    from keep_sync_v2 import html_to_styled_doc
    doc = html_to_styled_doc(
        "<h1>plain <b>bold part</b></h1>", sct_id="sct.x",
    )
    runs = doc.paragraphs[0].runs
    bold_texts = [r.text for r in runs if r.bold]
    nonbold_texts = [r.text for r in runs if not r.bold]
    assert any("bold part" in t for t in bold_texts)
    assert any("plain" in t for t in nonbold_texts)


def test_to_html_empty_paragraph_survives_round_trip(qapp):  # noqa: ARG001
    """Regression: to_html() used to render an empty paragraph as bare
    <p></p>/<h1></h1>/<h2></h2>. Qt's own HTML parser silently drops
    truly empty block elements when re-parsing via html_to_styled_doc
    (QTextDocument.setHtml), so a StyledDoc round-tripped through
    to_html() -> html_to_styled_doc() lost every blank paragraph
    (deliberate blank lines between sections) — and if that lossy
    doc's text ever got reused as input to a later merge, the blank
    lines vanished from what actually got pushed to the server. Qt's
    own toHtml() marks blank lines with -qt-paragraph-type:empty so
    they survive re-parsing; to_html() must do the same."""
    from keep_sync_v2 import html_to_styled_doc
    from keep_protocol.nested_model import StyledDoc, Paragraph, StyleRun, to_html

    doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Title")], heading=1),
        Paragraph(runs=[], heading=0),  # blank line
        Paragraph(runs=[StyleRun(text="Section A")], heading=2),
        Paragraph(runs=[StyleRun(text="body A")], heading=0),
        Paragraph(runs=[], heading=0),  # blank line
        Paragraph(runs=[StyleRun(text="Section B")], heading=2),
        Paragraph(runs=[StyleRun(text="body B")], heading=0),
    ])

    roundtripped = html_to_styled_doc(to_html(doc), sct_id="sct.x")

    assert len(roundtripped.paragraphs) == len(doc.paragraphs)
    assert roundtripped.plain_text == doc.plain_text
    assert [p.heading for p in roundtripped.paragraphs] == [1, 0, 2, 0, 0, 2, 0]

def test_adjacent_identically_styled_runs_are_coalesced(qapp):  # noqa: ARG001
    """Qt splits text into fragments on attributes we deliberately do
    not model — font point size above all, which set_styled_doc stamps
    on every run to size headings. Merge a heading line into the body
    line below it and the result arrives here as two fragments whose
    MODELLED styling ("no bold, no italic, no underline, no strike") is
    identical.

    Left split, that purely cosmetic difference made _styles_equal
    report "formatting changed" against the server's own merged form,
    so encode_text_diff skipped its no-op early-return and pushed `as`
    ops — plus a freshly minted ps_hdid — on every 30-second sync tick
    for a note nobody had touched.
    """
    from keep_sync_v2 import html_to_styled_doc

    doc = html_to_styled_doc(
        '<p><span style="font-size:14pt;">one</span>'
        '<span style="font-size:10pt;">a </span></p>',
        sct_id="sct.x",
    )
    runs = doc.paragraphs[0].runs
    assert [r.text for r in runs] == ["onea "], (
        "identically-styled adjacent runs must be merged"
    )


def test_coalescing_keeps_genuinely_different_runs_apart(qapp):  # noqa: ARG001
    """Sanity check alongside the merge above: real style boundaries
    must survive coalescing."""
    from keep_sync_v2 import html_to_styled_doc

    doc = html_to_styled_doc("<p>plain <b>bold</b> plain</p>", sct_id="sct.x")
    runs = [r for r in doc.paragraphs[0].runs if r.text]
    assert [r.bold for r in runs] == [False, True, False]


def test_styled_doc_widget_round_trip_emits_no_ops(qapp):  # noqa: ARG001
    """The full display path: render a server doc into a real note
    widget, change NOTHING, read it back and diff. A note the user
    never touched must produce zero ops — anything else is a spurious
    write to Keep on every sync tick."""
    from keep_protocol.nested_model import (
        Paragraph, StyleRun, StyledDoc, encode_text_diff,
    )
    from keep_sync_v2 import html_to_styled_doc
    from note_window import NoteTextEdit

    server = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Title")], heading=1),
        Paragraph(runs=[], heading=0),
        Paragraph(runs=[StyleRun(text="bold", bold=True),
                        StyleRun(text=" and "),
                        StyleRun(text="italic", italic=True)], heading=0),
        Paragraph(runs=[StyleRun(text="Section")], heading=2),
    ])

    editor = NoteTextEdit()
    editor.set_styled_doc(server)
    read_back = html_to_styled_doc(editor.toHtml(), sct_id="sct.x")

    assert read_back.plain_text == server.plain_text
    assert [p.heading for p in read_back.paragraphs] == [1, 0, 0, 2]
    assert encode_text_diff(server, read_back) == []

