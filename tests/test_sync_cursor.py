"""The incremental cursor, and why a web edit could go missing forever.

`KeepClient.target_version` is the cursor meaning "we have processed
every change up to this version". Send it and the server replies with
what changed since — so anything the cursor is already past is never
mentioned again, correctly, because the server considers it delivered.

Two bugs conspired to make that fatal rather than merely subtle:

  * every WRITE adopted its response's `toVersion`, even though a write
    response is a single unpaginated payload that echoes whatever the
    server chose to include (only `sync()` loops until `truncated` is
    false); and
  * write responses merged those echoed nodes with a bare
    `dict.update()`, which overwrites `serverChanges` wholesale — and
    never flagged a compact delta in `_stale_snapshot_ids`, so
    `fetch_notes` never scheduled the full resync that repairs it.

Together: push once while a note is also being edited on the web, and
that note freezes at its stale content through every periodic poll and
every manual "Sync now" — both incremental — until the app is restarted.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from keep_protocol.client import KeepClient


def _node(nid="n1", **over):
    node = {
        "id": nid,
        "kind": "notes#node",
        "parentId": "root",
        "type": "NOTE",
        "trashState": 0,
        "deletionState": 0,
        "isPinned": False,
        "isArchived": False,
        "title": "T",
        "text": "hello",
        "indexableText": "hello",
        "color": "DEFAULT",
        "sortValue": 0,
        "baseVersion": "0",
        "serverId": nid,
        "timestamps": {"kind": "notes#timestamps"},
        "serverChanges": {"snapshot": {
            "revision": "10",
            "serializedChunks": ['[["sct-add",0,"sct.a","txt"],'
                                 '["docs-nestedModel",["text",1,"sct.a"],'
                                 '{"ibi":1,"s":"hello","ty":"is"}]]'],
        }},
    }
    node.update(over)
    return node


@pytest.fixture
def client():
    c = KeepClient.__new__(KeepClient)
    c.notes = {}
    c.target_version = None
    c._stale_snapshot_ids = set()
    c._client_revision = {}
    c._next_request_id = {}
    c._pending_first_cbx = {}
    c._bundle_session_id = "s1"
    c._session_id = "sess1"
    return c


def _seed(client):
    client._merge_node_delta("n1", _node())
    client.target_version = "100"
    assert client.notes["n1"].serialized_chunks


# ------------------------------------------------------------------
# _merge_node_delta
# ------------------------------------------------------------------

def test_compact_delta_keeps_chunks_and_flags_stale(client):
    """Revision bumped, chunks not re-echoed: keep what we have and
    flag the note so fetch_notes schedules a repairing full resync."""
    _seed(client)
    client._merge_node_delta("n1", {
        "id": "n1", "type": "NOTE",
        "serverChanges": {"snapshot": {"revision": "11"}},
    })
    assert client.notes["n1"].serialized_chunks, "chunks must survive"
    assert client.notes["n1"].nested_revision == "11"
    assert "n1" in client._stale_snapshot_ids


def test_degenerate_serverchanges_does_not_wipe_chunks(client):
    """serverChanges present but useless (no revision, no chunks) — a
    bare dict.update() would blow the snapshot away."""
    _seed(client)
    client._merge_node_delta("n1", {
        "id": "n1", "type": "NOTE", "serverChanges": {},
    })
    assert client.notes["n1"].serialized_chunks


def test_absent_serverchanges_keeps_previous_snapshot(client):
    _seed(client)
    client._merge_node_delta("n1", {"id": "n1", "type": "NOTE",
                                    "title": "renamed"})
    assert client.notes["n1"].serialized_chunks
    assert client.notes["n1"].title == "renamed"


def test_real_chunks_are_adopted_and_clear_the_stale_flag(client):
    _seed(client)
    client._stale_snapshot_ids.add("n1")
    client._merge_node_delta("n1", {
        "id": "n1", "type": "NOTE",
        "serverChanges": {"snapshot": {
            "revision": "12",
            "serializedChunks": ['[["sct-add",0,"sct.a","txt"],'
                                 '["docs-nestedModel",["text",1,"sct.a"],'
                                 '{"ibi":1,"s":"hello world","ty":"is"}]]'],
        }},
    })
    assert client.notes["n1"].nested_revision == "12"
    assert "n1" not in client._stale_snapshot_ids
    assert "hello world" in client.notes["n1"].serialized_chunks[0]


def test_authoritative_empty_snapshot_is_adopted(client):
    """serializedChunks: [] means the body really was emptied — that is
    structurally identical to "field omitted" under a truthy check, so
    it must be distinguished with `is not None`."""
    _seed(client)
    client._merge_node_delta("n1", {
        "id": "n1", "type": "NOTE",
        "serverChanges": {"snapshot": {"revision": "13",
                                       "serializedChunks": []}},
    })
    assert client.notes["n1"].serialized_chunks == []


def test_unknown_node_is_added_fresh(client):
    client._merge_node_delta("n2", _node("n2"))
    assert "n2" in client.notes


# ------------------------------------------------------------------
# The cursor
# ------------------------------------------------------------------

def _write_response():
    return {"toVersion": "999", "nodes": [
        {"id": "n1", "type": "NOTE",
         "serverChanges": {"snapshot": {"revision": "11"}}},
    ]}


@pytest.mark.parametrize("call", [
    lambda c, n: c.update_note_metadata(n, new_title="x"),
    lambda c, n: c.update_note_legacy_text(n, "new body"),
])
def test_a_write_does_not_advance_the_cursor(client, call):
    """Only sync() may move the cursor: it is the only path that pages
    through `truncated` responses until the delta is exhausted."""
    _seed(client)
    note = client.notes["n1"]
    with patch.object(client, "_post", return_value=_write_response()):
        call(client, note)
    assert client.target_version == "100", (
        "a write response must not move the incremental cursor"
    )


def test_a_write_still_merges_echoed_nodes_carefully(client):
    _seed(client)
    note = client.notes["n1"]
    with patch.object(client, "_post", return_value=_write_response()):
        client.update_note_metadata(note, new_title="x")
    # Compact delta echoed by the write: chunks kept, note flagged so
    # the next fetch repairs it.
    assert client.notes["n1"].serialized_chunks
    assert "n1" in client._stale_snapshot_ids


def test_sync_does_advance_the_cursor(client):
    resp = {"toVersion": "500", "nodes": [_node()], "truncated": False}
    with patch.object(client, "_post", return_value=resp), \
            patch.object(client, "_request_header", return_value={}):
        client.sync()
    assert client.target_version == "500"


# ------------------------------------------------------------------
# The self-healing backstop
# ------------------------------------------------------------------

def test_fetch_notes_periodically_promotes_to_a_full_resync():
    """Even with the cursor bugs fixed, "the pull silently stops working
    until you restart" is bad enough to deserve a backstop."""
    from keep_sync_v2 import KeepSyncV2, _FULL_RESYNC_EVERY_N_FETCHES

    s = KeepSyncV2()
    s._authenticated = True
    s._client = MagicMock()
    s._client.notes = {}
    s._client.list_notes.return_value = []
    s._client.pop_stale_snapshot_ids.return_value = set()
    s._first_fetch_done = True

    fulls = []
    s._client.sync.side_effect = lambda full=False: fulls.append(full)
    for _ in range(_FULL_RESYNC_EVERY_N_FETCHES):
        s.fetch_notes()

    assert fulls[0] is False, "ordinary fetches stay incremental"
    assert fulls[-1] is True, "the Nth fetch promotes to a full resync"
    assert fulls.count(True) == 1
