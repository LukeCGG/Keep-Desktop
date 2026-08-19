"""Tests for the text-side of keep_protocol.nested_model.

Covers:
  - StyledDoc construction + plain_text projection.
  - encode_text_diff producing minimal is/ds/replace ops.
  - encode_text_diff identity case (no ops).
  - encode_text_diff requires an sct_id on at least one doc.
  - decode_chunks empty / single op.
"""

from __future__ import annotations

import pytest

from keep_protocol.nested_model import (
    Paragraph,
    StyleRun,
    StyledDoc,
    decode_chunks,
    encode_text_diff,
    styled_doc_from_dict,
    styled_doc_to_dict,
)


def _doc(text: str, sct_id: str = "sct.test") -> StyledDoc:
    """Build a single-paragraph StyledDoc with no styling."""
    runs = [StyleRun(text=text)] if text else []
    return StyledDoc(paragraphs=[Paragraph(runs=runs)], sct_id=sct_id)


# ---------------------------------------------------------------- decode

def test_decode_empty_chunks_returns_empty_doc():
    doc = decode_chunks([])
    assert doc.plain_text == ""
    # decode_chunks always emits at least one (possibly-empty) paragraph
    # so QTextDocument has somewhere to render an empty doc.
    assert all(p.text == "" for p in doc.paragraphs)


def test_decode_blank_chunk_skipped():
    doc = decode_chunks(["", None])  # type: ignore[list-item]
    assert doc.plain_text == ""


# ---------------------------------------------------------------- encode_text_diff

def test_text_diff_identity_returns_no_ops():
    a = _doc("hello world")
    b = _doc("hello world")
    assert encode_text_diff(a, b) == []


def test_text_diff_pure_insert_emits_single_is_op():
    old = _doc("Hello world")
    new = _doc("Hello there world")
    ops = encode_text_diff(old, new)
    # Filter out style ops; the bare insert should produce exactly one
    # 'is' op at position 7 (after "Hello ").
    is_ops = [
        op for op in ops
        if isinstance(op, list) and len(op) >= 3
        and isinstance(op[2], dict) and op[2].get("ty") == "is"
    ]
    assert len(is_ops) == 1, f"expected 1 insert, got: {ops}"
    body = is_ops[0][2]
    assert body["s"] == "there "
    assert body["ibi"] == 7


def test_text_diff_pure_delete_emits_single_ds_op():
    old = _doc("Hello cruel world")
    new = _doc("Hello world")
    ops = encode_text_diff(old, new)
    ds_ops = [
        op for op in ops
        if isinstance(op, list) and len(op) >= 3
        and isinstance(op[2], dict) and op[2].get("ty") == "ds"
    ]
    assert len(ds_ops) == 1, f"expected 1 delete, got: {ops}"
    body = ds_ops[0][2]
    # "cruel " is at indices 6..12 (1-based si=7, ei=12).
    assert body["si"] == 7
    assert body["ei"] == 12


def test_text_diff_replace_emits_paired_ds_then_is():
    old = _doc("foo bar baz")
    new = _doc("foo qux baz")
    ops = encode_text_diff(old, new)
    types = [
        op[2].get("ty") for op in ops
        if isinstance(op, list) and len(op) >= 3 and isinstance(op[2], dict)
    ]
    # Must contain both a ds and an is for the middle word.
    assert "ds" in types and "is" in types


def test_text_diff_targets_correct_sct():
    old = _doc("a", sct_id="sct.AAA")
    new = _doc("ab", sct_id="sct.AAA")
    ops = encode_text_diff(old, new)
    # Every op must carry the same sct anchor.
    for op in ops:
        assert isinstance(op, list) and len(op) >= 2
        target = op[1]
        # target shape: ["text", 1, sct_id]
        assert target[0] == "text"
        assert target[2] == "sct.AAA"


def test_text_diff_emits_style_op_for_untouched_paragraph_restyled():
    """Regression: a pure formatting change (no text edit) on a
    paragraph that ISN'T where the SAME push also edited text used to
    be silently dropped -- only headings were exempted from the
    "must fall inside a text-changed range" gate; run-level bold/
    italic/underline/strikethrough was not. Bolding one paragraph
    while typing in another, in the same autosave, lost the bold on
    the next pull."""
    old = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="hello")]),
        Paragraph(runs=[StyleRun(text="world")]),
    ])
    new = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="helloX")]),  # text edit here
        Paragraph(runs=[StyleRun(text="world", bold=True)]),  # style-only here
    ])
    ops = encode_text_diff(old, new)
    bold_ops = [
        op for op in ops
        if op[2].get("ty") == "as" and op[2].get("st") == "text"
        and op[2].get("sm", {}).get("ts_bd") is True
    ]
    assert bold_ops, "bold change on the untouched paragraph must not be dropped"


def test_text_diff_heading_clear_detected_despite_paragraph_count_change():
    """Regression: heading-clear detection used to compare
    old_doc.paragraphs[p_idx] against doc.paragraphs[p_idx] by raw
    index, and skipped clear-detection for the WHOLE document
    whenever paragraph counts merely differed elsewhere -- so
    clearing a heading while ALSO adding an unrelated new paragraph
    in the same push silently kept the old heading server-side."""
    old = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Title")], heading=1, heading_id="h.abc"),
        Paragraph(runs=[StyleRun(text="body")]),
    ])
    new = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Title")], heading=0),  # cleared
        Paragraph(runs=[StyleRun(text="body")]),
        Paragraph(runs=[StyleRun(text="new para")]),  # count changed
    ])
    ops = encode_text_diff(old, new)
    clear_ops = [
        op for op in ops
        if op[2].get("ty") == "as" and op[2].get("st") == "paragraph"
        and op[2].get("sm", {}).get("ps_hd") == 0
    ]
    assert clear_ops, "heading clear must not be dropped by an unrelated paragraph-count change"


def test_text_diff_reuses_heading_id_instead_of_reminting():
    """Regression: html_to_styled_doc (the editor -> push path) never
    populates Paragraph.heading_id, so the old code's `para.heading_id
    or _mint_heading_id()` minted a BRAND NEW heading anchor id on
    every single push that touched any text in a note containing a
    heading -- even though the heading itself never changed. Must
    reuse the id already known server-side (from old_doc) instead."""
    old = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Title")], heading=1, heading_id="h.abc123"),
        Paragraph(runs=[StyleRun(text="body")]),
    ])
    new = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Title")], heading=1, heading_id=None),
        Paragraph(runs=[StyleRun(text="bodyX")]),
    ])
    ops = encode_text_diff(old, new)
    heading_ops = [
        op for op in ops
        if op[2].get("ty") == "as" and op[2].get("st") == "paragraph"
        and "ps_hd" in op[2].get("sm", {})
    ]
    assert heading_ops, "expected a heading op even though the heading itself is unchanged"
    assert heading_ops[0][2]["sm"]["ps_hdid"] == "h.abc123"


def test_text_diff_requires_sct_id():
    old = StyledDoc(paragraphs=[Paragraph(runs=[StyleRun(text="x")])])
    new = StyledDoc(paragraphs=[Paragraph(runs=[StyleRun(text="xy")])])
    with pytest.raises(ValueError):
        encode_text_diff(old, new)


def test_text_diff_handles_complete_rewrite():
    old = _doc("aaa")
    new = _doc("bbb")
    ops = encode_text_diff(old, new)
    # Must NOT be empty (the docs differ).
    assert ops, "complete rewrite should produce ops"
    # Must contain a delete and an insert.
    types = [
        op[2].get("ty") for op in ops
        if isinstance(op, list) and len(op) >= 3 and isinstance(op[2], dict)
    ]
    assert "ds" in types
    assert "is" in types


def test_text_diff_back_to_front_walk_preserves_offsets():
    """Two inserts in the same diff must each use the OLD-side offset.

    Walking back-to-front means later ops don't shift earlier ones.
    """
    old = _doc("AC")
    new = _doc("ABCD")
    ops = encode_text_diff(old, new)
    is_ops = [
        op for op in ops
        if isinstance(op, list) and len(op) >= 3
        and isinstance(op[2], dict) and op[2].get("ty") == "is"
    ]
    # Should be two inserts: "B" between A and C, "D" at the end.
    assert len(is_ops) == 2
    # Recover (text, position) pairs.
    pairs = sorted((o[2]["s"], o[2]["ibi"]) for o in is_ops)
    assert pairs == [("B", 2), ("D", 3)]


# ---------------------------------------------------------- styled_doc persistence

def test_styled_doc_json_round_trip_preserves_all_fields():
    """Regression: styled_doc is a dynamically-attached (non-dataclass)
    KeepNote attribute that was never persisted to the disk cache,
    meaning it was absent on every single app restart -- not just
    after a fresh local edit. sync_merge.py's decide_merge uses
    "local has no styled_doc" as a signal for "this is our own push
    echoing back, suppress the refresh" (see its local_doc-is-None
    branch); with styled_doc never surviving a restart, that
    heuristic misfired on literally every note's first sync after
    launch, silently swallowing any concurrent web restyle."""
    import json

    doc = StyledDoc(sct_id="sct.x", revision="5", paragraphs=[
        Paragraph(runs=[StyleRun(text="Heading", bold=True)], heading=1, heading_id="h.abc"),
        Paragraph(runs=[
            StyleRun(text="body ", italic=True),
            StyleRun(text="text", underline=True),
        ]),
        Paragraph(runs=[]),  # blank paragraph
    ])

    # Must be JSON-serializable (this is what actually hits notes.json).
    d = json.loads(json.dumps(styled_doc_to_dict(doc)))
    doc2 = styled_doc_from_dict(d)

    assert doc2.sct_id == "sct.x"
    assert doc2.revision == "5"
    assert len(doc2.paragraphs) == 3
    assert doc2.paragraphs[0].heading == 1
    assert doc2.paragraphs[0].heading_id == "h.abc"
    assert doc2.paragraphs[0].runs[0].bold is True
    assert doc2.paragraphs[1].runs[0].italic is True
    assert doc2.paragraphs[1].runs[1].underline is True
    assert doc2.paragraphs[2].runs == []
    assert doc2.plain_text == doc.plain_text


def test_styled_doc_from_dict_degrades_gracefully_on_bad_input():
    """A corrupt or old-format cache entry must fall back to "no
    baseline yet" (the pre-existing behavior) rather than crashing
    the whole disk-cache load."""
    assert styled_doc_from_dict(None) is None
    assert styled_doc_from_dict("not a dict") is None  # type: ignore[arg-type]
    assert styled_doc_from_dict({"paragraphs": [{"heading": 0, "runs": []}]}) is not None
