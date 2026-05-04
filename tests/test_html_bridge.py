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
