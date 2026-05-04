"""Tests for keep_sync_v2 safety guards.

These exercise the defensive paths added in v2.0.0:

  - empty/whitespace id refused
  - missing-on-server schedules a resync and returns False
  - server-trashed/deleted note returns True without pushing
  - empty local list refuses to wipe a populated server list
  - empty local text+title refuses to wipe a populated server note
  - delete_note is idempotent on already-trashed server state

We mock KeepClient so the tests run offline and deterministically.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import pytest

from keep_sync import KeepNote
from keep_sync_v2 import KeepSyncV2
from keep_protocol.client import KeepError
from keep_protocol.models import Note as ServerNote


def _server_note(
    note_id: str = "note.123",
    *,
    type: str = "NOTE",
    sct_id: Optional[str] = "sct.abc",
    is_trashed: bool = False,
    is_deleted: bool = False,
    title: str = "",
    indexable_text: str = "",
    chunks: Optional[list[str]] = None,
) -> ServerNote:
    """Build a minimal ServerNote that satisfies the push paths we test."""
    return ServerNote(
        id=note_id,
        server_id=note_id,
        type=type,
        title=title,
        text=indexable_text,
        color="DEFAULT",
        is_archived=False,
        is_pinned=False,
        is_trashed=is_trashed,
        is_deleted=is_deleted,
        created=None,
        updated=None,
        user_edited=None,
        sort_value=0,
        base_version="0",
        sct_id=sct_id,
        serialized_chunks=chunks or [],
        nested_revision="0",
        indexable_text=indexable_text,
        raw={
            "id": note_id,
            "kind": "notes#node",
            "parentId": "root",
            "type": type,
            "trashState": int(is_trashed),
            "deletionState": int(is_deleted),
            "isPinned": False,
            "title": title,
        },
    )


@pytest.fixture
def sync():
    s = KeepSyncV2()
    # Bypass real auth.
    s._authenticated = True
    s._client = MagicMock()
    s._client.notes = {}
    # `_client.sync()` is called inside push_note's pre-push refresh —
    # make it a no-op so we can inject server state via _server_notes.
    s._client.sync.return_value = None
    return s


# ---------------------------------------------------------------- push_note

def test_push_note_refuses_empty_id(sync):
    note = KeepNote(id="", text="hi")
    assert sync.push_note(note) is False


def test_push_note_refuses_whitespace_id(sync):
    note = KeepNote(id="   ", text="hi")
    assert sync.push_note(note) is False


def test_push_note_missing_on_server_returns_false_and_schedules_resync(sync):
    note = KeepNote(id="note.unknown", text="hi")
    # _server_notes empty → server is None.
    assert sync.push_note(note) is False
    assert "note.unknown" in sync._force_full_resync_for
    # Did NOT call any update method.
    sync._client.update_text_diff.assert_not_called()


def test_push_note_trashed_server_side_returns_true_without_push(sync):
    server = _server_note("note.t1", is_trashed=True, indexable_text="x")
    sync._server_notes["note.t1"] = server
    note = KeepNote(id="note.t1", text="local edit")
    # True == "consider it handled, drop from _dirty".
    assert sync.push_note(note) is True
    sync._client.update_text_diff.assert_not_called()


def test_push_note_deleted_server_side_returns_true_without_push(sync):
    server = _server_note("note.d1", is_deleted=True, indexable_text="x")
    sync._server_notes["note.d1"] = server
    note = KeepNote(id="note.d1", text="local edit")
    assert sync.push_note(note) is True
    sync._client.update_text_diff.assert_not_called()


def test_push_note_refuses_empty_local_against_populated_server(sync):
    """Empty local text + title vs populated server = UI glitch, refuse.

    Set base_text == server text so neither remote-changed nor 3-way
    merge fires; the only thing happening is the user clearing the
    note locally. That's the UI-glitch case the guard exists to catch.
    """
    server = _server_note("note.x", indexable_text="server has stuff")
    sync._server_notes["note.x"] = server
    sync._base_text["note.x"] = "server has stuff"   # in sync with server
    note = KeepNote(id="note.x", title="", text="", html="")
    result = sync.push_note(note)
    assert result is False
    sync._client.update_text_diff.assert_not_called()


def test_push_note_allows_empty_local_when_server_also_empty(sync):
    """Pushing an empty note to an empty server is fine — no-op diff."""
    server = _server_note("note.y", indexable_text="")
    sync._server_notes["note.y"] = server
    note = KeepNote(id="note.y", title="", text="")
    # Patch html_to_styled_doc to return an empty StyledDoc-like.
    sync._client.update_text_diff.return_value = {}
    # Should not be refused. (May still no-op; we don't care about the
    # exact value, just that the safety guard doesn't bail.)
    result = sync.push_note(note)
    # Either True (no-op succeeded) or some truthy result; importantly
    # NOT the False that the empty-server-content refusal would emit.
    # The actual call to update_text_diff is expected.
    assert result is not False or sync._client.update_text_diff.called


def test_push_note_legacy_text_refuses_empty_against_populated_server(sync):
    """Same guard for the no-sct (legacy) branch."""
    server = _server_note("note.l", sct_id=None, indexable_text="legacy stuff")
    sync._server_notes["note.l"] = server
    note = KeepNote(id="note.l", title="", text="")
    result = sync.push_note(note)
    assert result is False
    sync._client.update_note_legacy_text.assert_not_called()


# ---------------------------------------------------------------- _push_list

def test_push_list_refuses_empty_local_against_populated_server(sync):
    server = _server_note("note.list1", type="LIST", sct_id="sct.list")
    sync._server_notes["note.list1"] = server
    # Server reports 3 items.
    sync._client.get_checkboxes.return_value = [
        MagicMock(cbx_id="cbx.a", text="A", checked=False, position=(0,)),
        MagicMock(cbx_id="cbx.b", text="B", checked=False, position=(1,)),
        MagicMock(cbx_id="cbx.c", text="C", checked=False, position=(2,)),
    ]
    # Local: empty list, no items.
    note = KeepNote(id="note.list1", is_list=True, list_items=[], text="")
    result = sync.push_note(note)
    assert result is False
    sync._client.update_list_diff.assert_not_called()


def test_push_list_falls_through_to_legacy_when_server_lacks_sct(sync):
    """Server has no sct anchor → legacy text fallback (warn-only)."""
    server = _server_note("note.l2", type="LIST", sct_id=None)
    sync._server_notes["note.l2"] = server
    sync._client.update_note_legacy_text.return_value = {}
    note = KeepNote(
        id="note.l2", is_list=True,
        text="☐ buy milk\n☑ done",
        list_items=[
            {"text": "buy milk", "checked": False},
            {"text": "done", "checked": True},
        ],
    )
    result = sync.push_note(note)
    assert result is True
    sync._client.update_note_legacy_text.assert_called_once()
    sync._client.update_list_diff.assert_not_called()


# ---------------------------------------------------------------- delete_note

def test_delete_note_idempotent_on_already_trashed(sync):
    server = _server_note("note.gone", is_trashed=True)
    sync._server_notes["note.gone"] = server
    sync.delete_note("note.gone")
    sync._client.trash_note.assert_not_called()
    # Local cache entries cleared.
    assert "note.gone" not in sync._server_notes


def test_delete_note_handles_server_missing_without_crash(sync):
    sync._client.notes = {}      # no server-side notes
    # Should NOT raise even though the note doesn't exist.
    sync.delete_note("note.never_existed")
    sync._client.trash_note.assert_not_called()


def test_delete_note_normal_path_calls_trash(sync):
    server = _server_note("note.live")
    sync._server_notes["note.live"] = server
    sync._client.trash_note.return_value = {}
    sync.delete_note("note.live")
    sync._client.trash_note.assert_called_once_with(server)
    assert "note.live" not in sync._server_notes


# ---------------------------------------------------------------- create_note

def test_create_note_does_not_send_tasks_field(sync):
    """A regression: create_note used to pass `tasks=[]`, which caused
    the server to treat the brand-new note as a LIST type, breaking the
    very first edit. Verify we don't send it."""
    fake = _server_note("note.new")
    sync._client.create_note.return_value = fake
    sync.create_note(title="t", text="hello", color_hex="#FFF475")
    call_kwargs = sync._client.create_note.call_args.kwargs
    assert "tasks" not in call_kwargs


# ---------------------------------------------------------------- routing

def test_push_note_routes_list_when_local_is_list_even_if_server_says_note(sync):
    """A brand-new note that the user toggled to checklist mode locally
    should still route through the list path."""
    server = _server_note("note.local_list", type="NOTE", sct_id=None)
    sync._server_notes["note.local_list"] = server
    sync._client.update_note_legacy_text.return_value = {}
    note = KeepNote(
        id="note.local_list",
        is_list=True,
        list_items=[{"text": "a", "checked": False}],
        text="☐ a",
    )
    sync.push_note(note)
    # Should NOT have called the text diff path.
    sync._client.update_text_diff.assert_not_called()
