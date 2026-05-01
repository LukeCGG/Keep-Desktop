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
