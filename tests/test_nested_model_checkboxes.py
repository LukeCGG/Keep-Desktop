"""Tests for the checklist-side of keep_protocol.nested_model.

Covers:
  - decode_checkboxes from realistic chunk strings.
  - decode_checkboxes unwraps docs-mlti envelopes.
  - encode_list_diff identity → [].
  - encode_list_diff add / remove / toggle / move / text-edit.
  - encode_list_diff mints cbx ids for fresh items.
"""

from __future__ import annotations

import json

from keep_protocol.nested_model import (
    CheckboxItem,
    decode_checkboxes,
    encode_list_diff,
)


SCT = "sct.list1"


def _ops_to_chunk(ops):
    """Pack a list of ops into a single serializedChunks-style string."""
    return [json.dumps(ops)]


def _bootstrap_chunk():
    """The sct-add op every cbx-typed list begins with."""
    return ["sct-add", "0", SCT, "cbx"]


# ---------------------------------------------------------------- decode

def test_decode_empty_returns_empty_list():
    items, sct = decode_checkboxes([])
    assert items == []
    assert sct is None


def test_decode_single_unchecked_item():
    chunks = _ops_to_chunk([
        _bootstrap_chunk(),
        ["cbx-add", SCT, "cbx.aaa", [0]],
        ["docs-nestedModel", ["text", 0, SCT, "cbx.aaa"],
         {"ty": "is", "ibi": 1, "s": "Buy milk"}],
    ])
    items, sct = decode_checkboxes(chunks)
    assert sct == SCT
    assert len(items) == 1
    assert items[0].cbx_id == "cbx.aaa"
    assert items[0].text == "Buy milk"
    assert items[0].checked is False


def test_decode_checked_item():
    chunks = _ops_to_chunk([
        _bootstrap_chunk(),
        ["cbx-add", SCT, "cbx.bbb", [0]],
        ["docs-nestedModel", ["text", 0, SCT, "cbx.bbb"],
         {"ty": "is", "ibi": 1, "s": "Done"}],
        ["cbx-p", SCT, "cbx.bbb", [], ["cb:ck", True]],
    ])
    items, _ = decode_checkboxes(chunks)
    assert len(items) == 1
    assert items[0].checked is True


def test_decode_handles_docs_mlti_wrapper():
    """A real Keep snapshot wraps multi-op chunks in docs-mlti."""
    chunks = _ops_to_chunk([
        _bootstrap_chunk(),
        ["docs-mlti", [
            ["cbx-add", SCT, "cbx.x", [0]],
            ["docs-nestedModel", ["text", 0, SCT, "cbx.x"],
             {"ty": "is", "ibi": 1, "s": "Hello"}],
        ]],
    ])
    items, _ = decode_checkboxes(chunks)
    assert len(items) == 1
    assert items[0].text == "Hello"


def test_decode_skips_removed_items():
    chunks = _ops_to_chunk([
        _bootstrap_chunk(),
        ["cbx-add", SCT, "cbx.a", [0]],
        ["docs-nestedModel", ["text", 0, SCT, "cbx.a"],
         {"ty": "is", "ibi": 1, "s": "A"}],
        ["cbx-add", SCT, "cbx.b", [1]],
        ["docs-nestedModel", ["text", 0, SCT, "cbx.b"],
         {"ty": "is", "ibi": 1, "s": "B"}],
        ["cbx-rm", SCT, "cbx.a", [0], 0],
    ])
    items, _ = decode_checkboxes(chunks)
    assert [it.cbx_id for it in items] == ["cbx.b"]


def test_decode_text_delete_op():
    chunks = _ops_to_chunk([
        _bootstrap_chunk(),
        ["cbx-add", SCT, "cbx.c", [0]],
        ["docs-nestedModel", ["text", 0, SCT, "cbx.c"],
         {"ty": "is", "ibi": 1, "s": "Hello world"}],
        ["docs-nestedModel", ["text", 0, SCT, "cbx.c"],
         {"ty": "ds", "si": 6, "ei": 12}],   # delete " world"
    ])
    items, _ = decode_checkboxes(chunks)
    assert items[0].text == "Hello"


def test_decode_with_explicit_sct_filter():
    """Passing list_sct_id should ignore ops on other anchors."""
    chunks = _ops_to_chunk([
        ["sct-add", "0", "sct.OTHER", "cbx"],
        ["cbx-add", "sct.OTHER", "cbx.skip", [0]],
        ["sct-add", "0", SCT, "cbx"],
        ["cbx-add", SCT, "cbx.keep", [0]],
        ["docs-nestedModel", ["text", 0, SCT, "cbx.keep"],
         {"ty": "is", "ibi": 1, "s": "kept"}],
    ])
    items, sct = decode_checkboxes(chunks, list_sct_id=SCT)
    assert sct == SCT
    ids = {it.cbx_id for it in items}
    assert "cbx.keep" in ids
    assert "cbx.skip" not in ids


# ---------------------------------------------------------------- encode_list_diff

def _item(cbx_id, text, checked=False, position=(0,)):
    return CheckboxItem(
        cbx_id=cbx_id, text=text, checked=checked, position=position,
    )


def test_list_diff_identity_no_ops():
    items = [_item("cbx.a", "x", position=(0,))]
    assert encode_list_diff(SCT, items, items) == []


def test_list_diff_text_edit_emits_per_row_text_ops():
    old = [_item("cbx.a", "Hello world", position=(0,))]
    new = [_item("cbx.a", "Hello there world", position=(0,))]
    ops = encode_list_diff(SCT, old, new)
    # Should ONLY include text ops on cbx.a (no cbx-rm, no cbx-add).
    heads = [op[0] for op in ops]
    assert "cbx-rm" not in heads
    assert "cbx-add" not in heads
    is_ops = [
        op for op in ops
        if op[0] == "docs-nestedModel"
        and isinstance(op[2], dict) and op[2].get("ty") == "is"
    ]
    assert len(is_ops) == 1
    assert is_ops[0][2]["s"] == "there "
    assert is_ops[0][1] == ["text", 0, SCT, "cbx.a"]


def test_list_diff_check_toggle_only():
    old = [_item("cbx.a", "task", checked=False, position=(0,))]
    new = [_item("cbx.a", "task", checked=True, position=(0,))]
    ops = encode_list_diff(SCT, old, new)
    assert len(ops) == 1
    assert ops[0][0] == "cbx-p"
    assert ops[0][4] == ["cb:ck", True]


def test_list_diff_remove():
    old = [_item("cbx.a", "x", position=(0,))]
    new: list[CheckboxItem] = []
    ops = encode_list_diff(SCT, old, new)
    assert len(ops) == 1
    assert ops[0][0] == "cbx-rm"
    assert ops[0][2] == "cbx.a"


def test_list_diff_add_mints_id_when_missing():
    old: list[CheckboxItem] = []
    new = [_item("", "Fresh", position=(0,))]
    ops = encode_list_diff(SCT, old, new)
    add_ops = [o for o in ops if o[0] == "cbx-add"]
    assert len(add_ops) == 1
    minted = add_ops[0][2]
    assert isinstance(minted, str) and minted.startswith("cbx.")
    assert len(minted) > len("cbx.")


def test_list_diff_add_with_explicit_id_preserves_it():
    old: list[CheckboxItem] = []
    new = [_item("cbx.preset", "Fresh", position=(0,))]
    ops = encode_list_diff(SCT, old, new)
    add_ops = [o for o in ops if o[0] == "cbx-add"]
    assert add_ops[0][2] == "cbx.preset"


def test_list_diff_move_emits_cbx_mv():
    # Two items swapped: encoder should emit a single cbx-mv that
    # moves item B from index 1 to index 0 (sequential semantics —
    # no need to also move A, the server shifts it implicitly).
    old = [
        _item("cbx.a", "x", position=(0,)),
        _item("cbx.b", "y", position=(1,)),
    ]
    new = [
        _item("cbx.b", "y", position=(0,)),
        _item("cbx.a", "x", position=(1,)),
    ]
    ops = encode_list_diff(SCT, old, new)
    mv_ops = [o for o in ops if o[0] == "cbx-mv"]
    assert len(mv_ops) == 1
    assert mv_ops[0][2] == [1]
    assert mv_ops[0][3] == [0]


def test_list_diff_complex_mixed_change():
    """Edit one row, remove another, add a third — all in one diff."""
    old = [
        _item("cbx.a", "keep me", position=(0,)),
        _item("cbx.b", "remove me", position=(1,)),
    ]
    new = [
        _item("cbx.a", "keep me edited", position=(0,)),
        _item("", "added", position=(1,)),
    ]
    ops = encode_list_diff(SCT, old, new)
    heads = [o[0] for o in ops]
    assert "cbx-rm" in heads     # cbx.b removed
    assert "cbx-add" in heads    # new row
    # Must contain a docs-nestedModel text op for cbx.a.
    text_ops = [
        o for o in ops
        if o[0] == "docs-nestedModel" and o[1][3] == "cbx.a"
    ]
    assert text_ops
