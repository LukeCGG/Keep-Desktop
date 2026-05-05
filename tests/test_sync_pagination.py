"""Tests for KeepClient.sync() pagination handling.

Keep's ``/changes`` endpoint paginates large responses via a
``"truncated": true`` flag. The client must keep posting (with the
previous response's ``toVersion`` as the cursor) until the server
returns ``"truncated": false``. Without that loop, the very first
sync after launch only sees a fraction of the user's notes; the
full-sync stale-id sweep then drops everything else from the cache,
producing visible "deleted then resynced" churn in the manager.

These tests mock ``KeepClient._post`` so they run offline.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from keep_protocol.client import KeepClient


def _make_note_node(note_id: str, title: str) -> dict[str, Any]:
    return {
        "id": note_id,
        "kind": "notes#node",
        "type": "NOTE",
        "title": title,
        "color": "DEFAULT",
        "text": title + " body",
        "isArchived": False,
        "isPinned": False,
        "trashState": 0,
        "deletionState": 0,
        "sortValue": 0,
        "baseVersion": "0",
        "timestamps": {
            "kind": "notes#timestamps",
            "created": "2024-01-01T00:00:00.000Z",
            "updated": "2024-01-01T00:00:00.000Z",
            "userEdited": "2024-01-01T00:00:00.000Z",
            "deleted": "1970-01-01T00:00:00.000Z",
            "trashed": "1970-01-01T00:00:00.000Z",
        },
        "parentId": "root",
    }


def _new_client() -> KeepClient:
    """Build a client without exercising the auth machinery — we only
    care about sync() here."""
    creds = MagicMock()
    creds.mint_bearer.return_value = "fake-bearer"
    return KeepClient(creds)


def test_sync_loops_through_paginated_full_response():
    """A full sync that returns three pages must produce a cache
    containing every note from every page, not just the first."""
    client = _new_client()
    page1 = {
        "toVersion": "v1",
        "truncated": True,
        "nodes": [_make_note_node("1.aaa", "page1-a"),
                  _make_note_node("1.bbb", "page1-b")],
    }
    page2 = {
        "toVersion": "v2",
        "truncated": True,
        "nodes": [_make_note_node("2.aaa", "page2-a")],
    }
    page3 = {
        "toVersion": "v3",
        "truncated": False,
        "nodes": [_make_note_node("3.aaa", "page3-a")],
    }
    posts: list[dict] = []

    def fake_post(url: str, body: dict) -> dict:
        posts.append(body)
        return [page1, page2, page3][len(posts) - 1]

    client._post = fake_post  # type: ignore[assignment]

    client.sync(full=True)

    # All three pages were fetched.
    assert len(posts) == 3
    # First page of a full sync sends no targetVersion; subsequent
    # pages use the cursor returned by the previous page.
    assert "targetVersion" not in posts[0]
    assert posts[1].get("targetVersion") == "v1"
    assert posts[2].get("targetVersion") == "v2"
    # Cache contains every node from every page.
    assert set(client.notes.keys()) == {"1.aaa", "1.bbb", "2.aaa", "3.aaa"}
    # Cursor advanced to the final page's toVersion.
    assert client.target_version == "v3"


def test_full_sync_stale_purge_runs_only_after_last_page():
    """Notes that exist in the cache at the start of a full sync but
    appear only on a LATER page must NOT be purged after page 1.
    Doing the stale-id sweep per-page is the bug that wipes notes
    "between pages"; the fix runs the sweep once at the end against
    the union of every page's ids."""
    client = _new_client()
    # Pre-populate the cache with two notes. One will reappear on
    # page 2 (so should survive); the other never appears (so should
    # be purged at the end).
    client.notes["existing.survives"] = MagicMock(
        raw={"id": "existing.survives"}, type="NOTE",
    )
    client.notes["existing.purged"] = MagicMock(
        raw={"id": "existing.purged"}, type="NOTE",
    )

    page1 = {
        "toVersion": "v1",
        "truncated": True,
        "nodes": [_make_note_node("new.aaa", "new-a")],
    }
    page2 = {
        "toVersion": "v2",
        "truncated": False,
        "nodes": [_make_note_node("existing.survives", "still-here")],
    }
    pages = [page1, page2]

    def fake_post(_url, _body):
        return pages.pop(0)

    client._post = fake_post  # type: ignore[assignment]
    client.sync(full=True)

    # Survivor must still be in the cache (would be wiped if the
    # purge ran after page 1, since page 1 didn't echo it).
    assert "existing.survives" in client.notes
    # New page-1 node should be there.
    assert "new.aaa" in client.notes
    # Truly-missing note should have been purged at the end.
    assert "existing.purged" not in client.notes


def test_force_full_resync_restarts_loop_in_full_mode():
    """The server can respond with ``forceFullResync: true`` if our
    cursor is too stale. The client must throw away the cursor and
    restart the sync from scratch in full mode."""
    client = _new_client()
    client.target_version = "very-old-cursor"

    page_force = {
        "forceFullResync": True,
        "toVersion": "ignored",
        "truncated": False,
        "nodes": [],
    }
    page_full = {
        "toVersion": "v-fresh",
        "truncated": False,
        "nodes": [_make_note_node("rebuilt.aaa", "rebuilt")],
    }
    pages = [page_force, page_full]
    posts: list[dict] = []

    def fake_post(_url, body):
        posts.append(body)
        return pages.pop(0)

    client._post = fake_post  # type: ignore[assignment]
    client.sync(full=False)  # incremental — but server says rebuild

    # Two posts: the original cursor-bearing one, then the
    # fresh full sync with no cursor.
    assert len(posts) == 2
    assert posts[0].get("targetVersion") == "very-old-cursor"
    assert "targetVersion" not in posts[1]
    # Cache reflects the rebuilt page.
    assert "rebuilt.aaa" in client.notes
    assert client.target_version == "v-fresh"


def test_incremental_sync_loops_until_not_truncated():
    """Incremental syncs paginate too — long-idle clients can have
    multiple pages of catch-up. The loop must consume all of them."""
    client = _new_client()
    client.target_version = "v0"

    page1 = {
        "toVersion": "v1",
        "truncated": True,
        "nodes": [_make_note_node("delta.aaa", "delta-a")],
    }
    page2 = {
        "toVersion": "v2",
        "truncated": False,
        "nodes": [_make_note_node("delta.bbb", "delta-b")],
    }
    pages = [page1, page2]
    posts: list[dict] = []

    def fake_post(_url, body):
        posts.append(body)
        return pages.pop(0)

    client._post = fake_post  # type: ignore[assignment]
    client.sync(full=False)

    assert len(posts) == 2
    # Both pages used a cursor (incremental sync always sends one).
    assert posts[0]["targetVersion"] == "v0"
    assert posts[1]["targetVersion"] == "v1"
    assert "delta.aaa" in client.notes
    assert "delta.bbb" in client.notes
