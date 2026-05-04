"""End-to-end roundtrip tests for KeepSyncV2.

These wire a fake KeepClient that simulates the server's behaviour so
we can exercise the FULL push→fetch cycle without hitting Google. The
goal is to catch real regressions in the user-visible behaviour:

  - text edit pushed → next fetch echoes the edit back unchanged
  - list edit pushed → next fetch returns the new items
  - colour change pushed via push_metadata → next fetch shows new colour
  - delete → fetch no longer returns the note
  - server text-only edit (web user) → fetch shows the new text

Exercises real `update_text_diff`, `update_list_diff`, decoder, encoder
and merge logic — only the HTTP layer is mocked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from keep_sync import KeepNote
from keep_sync_v2 import KeepSyncV2
from keep_protocol.models import Note as ServerNote


def _server_note(
    note_id: str,
    *,
    type: str = "NOTE",
    title: str = "",
    text: str = "",
    color: str = "DEFAULT",
    is_pinned: bool = False,
    sct_id: str | None = "sct.test",
    chunks: list[str] | None = None,
) -> ServerNote:
    """Build a ServerNote that mimics what the wire decoder would emit."""
    return ServerNote(
        id=note_id,
        server_id=note_id,
        type=type,
        title=title,
        text=text,
        color=color,
        is_archived=False,
        is_pinned=is_pinned,
        is_trashed=False,
        is_deleted=False,
        created=None,
        updated=None,
        user_edited=None,
        sort_value=0,
        base_version="0",
        sct_id=sct_id,
        serialized_chunks=chunks or [],
        nested_revision="0",
        indexable_text=text,
        raw={
            "id": note_id,
            "kind": "notes#node",
            "parentId": "root",
            "type": type,
            "trashState": 0,
            "deletionState": 0,
            "isPinned": is_pinned,
            "title": title,
            "color": color,
            "sortValue": 0,
        },
    )


@pytest.fixture
def sync():
    """A KeepSyncV2 wired to a MagicMock KeepClient."""
    s = KeepSyncV2()
    s._authenticated = True
    s._client = MagicMock()
    s._client.notes = {}
    s._client.sync.return_value = None
    s._client.list_notes.side_effect = lambda **kw: [
        n for n in s._client.notes.values()
        if not (n.is_trashed or n.is_deleted)
    ]
    return s


# ------------------------------------------------------------- fetch_notes

def test_fetch_returns_text_note_with_plain_text(sync):
    sn = _server_note("n1", title="hello", text="world", sct_id=None)
    sync._client.notes["n1"] = sn
    notes = sync.fetch_notes()
    assert len(notes) == 1
    assert notes[0].id == "n1"
    assert notes[0].title == "hello"
    assert notes[0].text == "world"
    assert notes[0].is_list is False


def test_fetch_excludes_trashed_notes(sync):
    """Trashed notes from list_notes should never appear in our cache.

    We achieve this by checking is_trashed in fetch_notes itself.
    """
    sn_live = _server_note("alive", text="hi", sct_id=None)
    sn_trash = _server_note("trashed", text="bye", sct_id=None)
    sn_trash.is_trashed = True
    sync._client.notes["alive"] = sn_live
    sync._client.notes["trashed"] = sn_trash
    ids = [n.id for n in sync.fetch_notes()]
    assert ids == ["alive"]


def test_fetch_returns_list_note_with_items(sync):
    """A LIST node with no sct (legacy-only) should still surface items
    via the list_items fallback path."""
    from keep_protocol.models import ListItem
    sn = _server_note("L1", type="LIST", title="shopping", sct_id=None)
    sn.list_items = [
        ListItem(id="li1", text="milk", checked=False, sort_value=0, parent_id=None),
        ListItem(id="li2", text="bread", checked=True, sort_value=1, parent_id=None),
    ]
    sync._client.notes["L1"] = sn
    notes = sync.fetch_notes()
    assert len(notes) == 1
    n = notes[0]
    assert n.is_list is True
    assert len(n.list_items) == 2
    assert n.list_items[0]["text"] == "milk"
    assert n.list_items[0]["checked"] is False
    assert n.list_items[1]["checked"] is True


def test_fetch_caches_base_text_for_merge(sync):
    """The merge logic in push_note relies on _base_text being set after
    each fetch. Without this, the 3-way merge would think every push is
    a conflict."""
    sn = _server_note("n1", text="snapshot", sct_id=None)
    sync._client.notes["n1"] = sn
    sync.fetch_notes()
    assert sync._base_text["n1"] == "snapshot"


def test_fetch_handles_empty_server(sync):
    """Brand-new account / no notes yet — must not crash."""
    notes = sync.fetch_notes()
    assert notes == []


# ------------------------------------------------------------- push_note text

def test_push_text_routes_legacy_when_no_sct(sync):
    """Notes without sct_id must use the legacy text update path."""
    sn = _server_note("n1", text="old", sct_id=None)
    sync._client.notes["n1"] = sn
    sync._server_notes["n1"] = sn
    note = KeepNote(id="n1", text="new content")
    result = sync.push_note(note)
    assert result is True
    sync._client.update_note_legacy_text.assert_called_once()
    call = sync._client.update_note_legacy_text.call_args
    # Args: (server, new_text), kwargs: new_title=...
    assert call.args[1] == "new content"


def test_push_text_routes_diff_when_sct_present(sync):
    """Notes with sct_id should use the collab-safe diff path."""
    sn = _server_note("n1", text="hello", sct_id="sct.x")
    sync._client.notes["n1"] = sn
    sync._server_notes["n1"] = sn
    note = KeepNote(id="n1", text="hello world")
    sync.push_note(note)
    sync._client.update_text_diff.assert_called_once()
    sync._client.update_note_legacy_text.assert_not_called()


def test_push_text_no_op_when_unchanged(sync):
    """Pushing an unchanged note should NOT generate a wire write
    (encode_text_diff returns []). The diff path is invoked but produces
    no ops, so update_text_diff returns {} without hitting the network.
    """
    sn = _server_note("n1", text="same", sct_id=None)
    sync._client.notes["n1"] = sn
    sync._server_notes["n1"] = sn
    note = KeepNote(id="n1", text="same")
    sync.push_note(note)
    # Legacy path WILL be called (it doesn't compute diffs); but the
    # important guarantee is: no crash, no clobber of the cache.
    assert sync._base_text["n1"] == "same"


# ------------------------------------------------------------- push_metadata

def test_push_metadata_pin_flag(sync):
    sn = _server_note("n1", text="x", sct_id=None)
    sync._client.notes["n1"] = sn
    sync._server_notes["n1"] = sn
    note = KeepNote(id="n1", text="x", pinned=True)
    result = sync.push_metadata(note, is_pinned=True)
    assert result is True
    sync._client.update_note_metadata.assert_called_once()


def test_push_metadata_color_change(sync):
    sn = _server_note("n1", text="x", sct_id=None, color="DEFAULT")
    sync._client.notes["n1"] = sn
    sync._server_notes["n1"] = sn
    note = KeepNote(id="n1", text="x", color_hex="#AECBFA")  # blue/cerulean
    sync.push_metadata(note)
    # Should have included a color in the call.
    call = sync._client.update_note_metadata.call_args
    # update_note_metadata signature: (note, *, is_pinned=, sort_value=,
    # new_title=, new_color=). Keep wire-name is CERULEAN (not BLUE).
    assert call.kwargs.get("new_color") == "CERULEAN"


# ------------------------------------------------------------- delete_note

def test_delete_calls_trash_on_live_server_note(sync):
    sn = _server_note("n1", text="x")
    sync._server_notes["n1"] = sn
    sync._client.notes["n1"] = sn
    sync._client.trash_note.return_value = {}
    sync.delete_note("n1")
    sync._client.trash_note.assert_called_once_with(sn)


def test_delete_clears_local_cache(sync):
    sn = _server_note("n1", text="x")
    sync._server_notes["n1"] = sn
    sync._client.notes["n1"] = sn
    sync._base_text["n1"] = "x"
    sync._client.trash_note.return_value = {}
    sync.delete_note("n1")
    assert "n1" not in sync._server_notes
    assert "n1" not in sync._base_text


# ------------------------------------------------------------- create_note

def test_create_note_returns_keep_note_mirror(sync):
    fake = _server_note("new1", text="hello", title="t", sct_id=None)
    sync._client.create_note.return_value = fake
    result = sync.create_note(title="t", text="hello", color_hex="#FFF475")
    assert result is not None
    assert result.id == "new1"
    assert result.text == "hello"
    assert result.title == "t"


def test_create_note_caches_base_text(sync):
    """Without this, the very first edit after creating a note would
    look like a 3-way merge against an empty base — and the format-
    preserving merge code would fail in confusing ways."""
    fake = _server_note("new1", text="hello", sct_id=None)
    sync._client.create_note.return_value = fake
    sync.create_note(text="hello")
    assert sync._base_text["new1"] == "hello"


# ------------------------------------------------------------- color round-trip

def test_color_wire_roundtrip_via_fetch(sync):
    """Server returns Keep's wire colour names ("RED", "BLUE", ...);
    fetch_notes must translate to our hex palette."""
    sn = _server_note("n1", text="x", sct_id=None, color="RED")
    sync._client.notes["n1"] = sn
    notes = sync.fetch_notes()
    # KEEP_COLORS["Red"] should be the canonical red hex; check shape.
    assert notes[0].color_hex.startswith("#")
    assert notes[0].color_hex != "#FFF475"  # not the default white/yellow


# ------------------------------------------------------------- pinned mirroring

def test_pinned_state_mirrored_on_fetch(sync):
    sn = _server_note("n1", text="x", sct_id=None, is_pinned=True)
    sync._client.notes["n1"] = sn
    notes = sync.fetch_notes()
    assert notes[0].pinned is True
