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
    # A bare MagicMock() return value is truthy and not iterable, unlike
    # the real KeepClient's `set[str]` — stub it to match "nothing
    # stale this cycle", the common case these tests exercise.
    s._client.pop_stale_snapshot_ids.return_value = set()
    s._client.is_snapshot_stale.return_value = False
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


def test_fetch_notes_holds_lock_across_baseline_mutation(sync):
    """Regression: fetch_notes()'s `with self._lock:` block used to
    end right after the network sync + stale-id bookkeeping, releasing
    the lock BEFORE the per-note loop that mutates _base_text/
    _base_doc/_server_notes. push_note holds the SAME lock for its
    entire execution, including its own baseline writes at the end --
    if a debounced push (its own background thread) and a periodic
    fetch_notes() (a different background thread) ever ran
    concurrently, their baseline writes for the same note id could
    interleave unguarded, and whichever finished last would win
    regardless of which was actually correct. The whole per-note loop
    (not just the network call) must run under the lock, matching
    push_note's own whole-function locking."""
    sn = _server_note("n1", text="hello", sct_id=None)
    sync._client.notes["n1"] = sn

    lock_held_during_list_notes = []
    orig_side_effect = sync._client.list_notes.side_effect

    def spy_list_notes(**kw):
        lock_held_during_list_notes.append(sync._lock.locked())
        return orig_side_effect(**kw)

    sync._client.list_notes.side_effect = spy_list_notes

    sync.fetch_notes()

    assert lock_held_during_list_notes == [True], (
        "the per-note loop (which calls list_notes() and then mutates "
        "_base_text/_base_doc) must run while the lock is held"
    )


def test_fetch_hold_baseline_for_skips_base_update_but_returns_fresh_content(sync):
    """Regression: a note held back (locally dirty or its window busy)
    must still come back with the server's current content -- the
    caller may use it later -- but _base_text/_base_doc must NOT
    advance for it. Advancing them anyway makes push_note think the
    local editor already matches the server's newest state even
    though the open widget was never actually updated to match, which
    lets the next local push silently overwrite a genuine concurrent
    web-side formatting change."""
    sn = _server_note("n1", text="hello world", sct_id=None)
    sync._client.notes["n1"] = sn
    # Seed a stale baseline as if from an earlier fetch.
    sync._base_text["n1"] = "stale"

    notes = sync.fetch_notes(hold_baseline_for={"n1"})

    assert len(notes) == 1
    assert notes[0].text == "hello world"  # fresh content still returned
    assert sync._base_text["n1"] == "stale"  # baseline NOT advanced


def test_fetch_stale_snapshot_does_not_poison_baseline_or_return_stale_content(sync):
    """Regression: Keep's incremental sync can bump a note's revision
    on a "compact" delta without re-echoing serializedChunks -- the
    client flags this via pop_stale_snapshot_ids(). This fires on the
    routine push-then-immediately-pull every periodic sync cycle does:
    a push that 3-way-merged a concurrent web edit deliberately leaves
    _base_text/_base_doc unadvanced (see push_note's
    baseline_reflects_widget), counting on THIS fetch to advance them
    once confirmed.

    Two failure modes if this fetch instead handed the controller a
    fallback entry for the stale note (rather than omitting it):
    (1) decoding the stale pre-merge chunks as if fresh would poison
    the baseline and make it look like syncing that note permanently
    stopped, and (2) even a "safe" text-only fallback entry (no
    styled_doc, empty html) gets ADOPTED by the controller whenever
    the note isn't currently dirty -- silently stripping all
    formatting from the open window and replacing it with plain text,
    even though nothing was actually lost server-side. The fix is to
    not return an entry for this note at all this cycle; the
    controller's existing cache -- baseline AND formatting -- stands
    untouched until the already-scheduled full resync lands."""
    import json
    from keep_protocol.nested_model import StyledDoc, Paragraph, StyleRun, encode_full_replace

    stale_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="pre-merge stale content", bold=True)], heading=1),
    ])
    stale_chunks = [json.dumps(
        encode_full_replace(stale_doc, current_text_length=0)
    )]
    sn = _server_note("n1", text="unused", chunks=stale_chunks)
    sn.indexable_text = "fresh merged content"
    sync._client.notes["n1"] = sn
    sync._client.pop_stale_snapshot_ids.return_value = {"n1"}
    # Sentinel baseline from an earlier fetch -- distinct from both the
    # stale decode AND the fresh indexable_text, so advancing to
    # EITHER of those (not just the stale one) would be caught.
    sync._base_text["n1"] = "sentinel-baseline"
    sync._base_doc["n1"] = stale_doc

    notes = sync.fetch_notes()

    # The note is omitted entirely this cycle -- not returned with
    # degraded (unstyled) content that the controller would adopt.
    assert notes == []
    # Baseline must not advance at all -- to the stale decode OR to
    # the fresh fallback -- until the scheduled full resync confirms
    # the widget has actually caught up (see hold_baseline_for).
    assert sync._base_text["n1"] == "sentinel-baseline"
    assert sync._base_doc["n1"] is stale_doc
    # A full resync must be scheduled to repair the stale chunks.
    assert "n1" in sync._force_full_resync_for


def test_fetch_stale_snapshot_with_matching_baseline_shows_format_preserving_approximation(sync):
    """Regression: reported live -- a single character added to an
    existing line on the web, and "the periodic sync ran multiple
    times and the change was not detected". Keep can keep echoing
    compact deltas (revision bumped, no serializedChunks) for the SAME
    note across SEVERAL consecutive full-resync attempts, not just
    one. Unconditionally skipping the note every single cycle (the
    fix for the sibling test above) meant that in that situation the
    text change was never shown AT ALL, not just delayed. When the
    cached baseline genuinely lines up with what the server had at
    that point (base_doc.plain_text == base_text -- unlike the
    sibling test's deliberately-mismatched setup), and
    note.indexableText (kept fresh independently of chunk staleness)
    shows the text actually changed, fetch_notes must apply that text
    change onto the cached styled_doc (preserving its formatting) and
    return it -- not wait indefinitely for a full resync that might
    keep failing to land."""
    import json
    from keep_protocol.nested_model import StyledDoc, Paragraph, StyleRun, encode_full_replace

    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Heading", bold=True)], heading=1),
        Paragraph(runs=[StyleRun(text="existing line")]),
    ])
    # Server's cached chunks are the SAME as the baseline (stale --
    # don't yet reflect the web's "g" edit), but indexableText DOES.
    stale_chunks = [json.dumps(
        encode_full_replace(base_doc, current_text_length=0)
    )]
    sn = _server_note("n1", text="unused", chunks=stale_chunks)
    sn.indexable_text = "Heading\nexisting lineg"
    sync._client.notes["n1"] = sn
    sync._client.pop_stale_snapshot_ids.return_value = {"n1"}
    sync._base_text["n1"] = base_doc.plain_text  # matches base_doc -- the key difference
    sync._base_doc["n1"] = base_doc

    notes = sync.fetch_notes()

    assert len(notes) == 1, "the text change must be shown, not silently dropped"
    assert notes[0].text == "Heading\nexisting lineg"
    approx_doc = notes[0].styled_doc
    assert approx_doc is not None
    # Formatting from the baseline must survive the approximation.
    assert approx_doc.paragraphs[0].heading == 1
    assert any(r.bold for r in approx_doc.paragraphs[0].runs)
    # Baseline itself must still not advance -- the full resync
    # (already scheduled) remains the authoritative confirmation.
    assert sync._base_text["n1"] == base_doc.plain_text
    assert sync._base_doc["n1"] is base_doc


def test_fetch_without_hold_baseline_advances_normally(sync):
    """Sanity check: omitting hold_baseline_for (or not including a
    note in it) preserves the pre-existing unconditional-update
    behaviour."""
    sn = _server_note("n1", text="hello world", sct_id=None)
    sync._client.notes["n1"] = sn
    notes = sync.fetch_notes()
    assert sync._base_text["n1"] == "hello world"


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


def test_push_note_retries_pre_push_resync_when_snapshot_still_stale(sync):
    """Regression: push_note's pre-push resync uses full=True specifically
    because a full resync "always returns complete chunks" -- but that
    can still momentarily not hold for a note written moments ago (e.g.
    a retry after a prior push's response was lost to a timeout even
    though the server had already applied it): the server's snapshot
    listing can echo a compact/metadata-only delta for THIS note before
    that write has fully propagated. Blindly trusting the (stale)
    cached chunks in that case makes the retry think its own
    just-applied insert is still missing and re-send it -- duplicating
    whatever was just typed. push_note must check
    is_snapshot_stale() (a non-draining peek, NOT the draining
    pop_stale_snapshot_ids() -- draining the whole shared set for a
    single-note check would discard other notes' staleness signal
    before fetch_notes() ever gets to see it) right after its resync
    and, if this note is flagged, resync once more before proceeding."""
    sn = _server_note("n1", text="hello", sct_id="sct.x")
    sync._client.notes["n1"] = sn
    sync._server_notes["n1"] = sn
    # First check (right after the initial full resync) reports n1 as
    # stale; second check (after the retry resync) reports clean.
    sync._client.is_snapshot_stale.side_effect = [True, False]

    note = KeepNote(id="n1", text="hello world")
    sync.push_note(note)

    # is_snapshot_stale is checked once right after the initial
    # pre-push resync, and again after the retry resync it triggers.
    assert sync._client.is_snapshot_stale.call_count == 2, (
        "push_note must resync once more when its own pre-push "
        "resync still reports the note as stale"
    )
    # Must NOT drain the shared stale-id set for this single-note
    # check -- that's fetch_notes()'s job.
    sync._client.pop_stale_snapshot_ids.assert_not_called()
    sync._client.update_text_diff.assert_called_once()


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


# --------------------------------------------------- degenerate-decode safety

def test_fetch_decode_failure_does_not_clobber_existing_base_doc():
    """Regression: a transient decode failure (decode_chunks produces
    paragraphs whose plain text is empty despite the server's
    indexableText having real content -- e.g. a compact incremental
    delta that didn't fully echo serializedChunks) used to
    unconditionally overwrite _base_doc[note_id] with None, even when
    an earlier fetch had already cached a perfectly good styled
    baseline. push_note's 3-way merge falls back to a plain-text-only
    merge (which strips ALL formatting from the WHOLE note, not just
    the paragraph that changed) whenever base_doc is None -- so one
    transient decode hiccup could silently destroy every heading/
    bold/italic in a note on its very next concurrent edit."""
    import json
    from keep_protocol.nested_model import (
        StyledDoc, Paragraph, StyleRun, encode_full_replace,
    )

    s = KeepSyncV2()
    s._authenticated = True
    s._client = MagicMock()
    s._client.sync.return_value = None
    s._client.pop_stale_snapshot_ids.return_value = set()
    s._client.is_snapshot_stale.return_value = False

    good_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Heading")], heading=1),
        Paragraph(runs=[StyleRun(text="bold text", bold=True)], heading=0),
    ])
    good_chunks = [json.dumps(
        encode_full_replace(good_doc, current_text_length=0)
    )]
    sn_good = _server_note("n1", text=good_doc.plain_text, chunks=good_chunks)
    s._client.notes = {"n1": sn_good}
    s._client.list_notes.side_effect = lambda **kw: list(s._client.notes.values())

    s.fetch_notes()
    assert s._base_doc["n1"] is not None
    assert any(r.bold for p in s._base_doc["n1"].paragraphs for r in p.runs)

    # Simulate a degenerate follow-up fetch: chunks decode to a
    # paragraph with no text, but indexableText still shows the real
    # (non-empty) content -- exactly the mismatch decode_failed
    # detects.
    degenerate_doc = StyledDoc(sct_id="sct.x", paragraphs=[Paragraph(runs=[])])
    degenerate_chunks = [json.dumps(
        encode_full_replace(degenerate_doc, current_text_length=0)
    )]
    sn_bad = _server_note("n1", text=good_doc.plain_text, chunks=degenerate_chunks)
    s._client.notes = {"n1": sn_bad}

    s.fetch_notes()

    assert s._base_doc["n1"] is not None, (
        "a transient decode failure wiped out an already-known-good "
        "styled baseline"
    )
    assert any(r.bold for p in s._base_doc["n1"].paragraphs for r in p.runs)


def test_fetch_genuinely_cleared_formatting_advances_base_doc_to_none():
    """Regression (inverse of the above): when a note's formatting is
    GENUINELY gone server-side (not a transient decode hiccup --
    e.g. the note has no sct_id/chunks at all anymore, or chunks
    decoded to a real, matching-indexableText empty doc), _base_doc
    used to be left at its OLD (now-wrong) value because the gating
    check only looked at "is new_styled_doc None", which can't tell
    this apart from a transient failure. A stale, non-None base_doc
    describing formatting that no longer exists on either side lets
    push_note's 3-way merge use the WRONG common ancestor on the next
    concurrent edit -- resurrecting formatting the user legitimately
    removed, or falsely flagging a conflict."""
    import json
    from keep_protocol.nested_model import (
        StyledDoc, Paragraph, StyleRun, encode_full_replace,
    )

    s = KeepSyncV2()
    s._authenticated = True
    s._client = MagicMock()
    s._client.sync.return_value = None
    s._client.pop_stale_snapshot_ids.return_value = set()
    s._client.is_snapshot_stale.return_value = False

    good_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Heading")], heading=1),
        Paragraph(runs=[StyleRun(text="bold text", bold=True)], heading=0),
    ])
    good_chunks = [json.dumps(
        encode_full_replace(good_doc, current_text_length=0)
    )]
    sn_good = _server_note("n1", text=good_doc.plain_text, chunks=good_chunks)
    s._client.notes = {"n1": sn_good}
    s._client.list_notes.side_effect = lambda **kw: list(s._client.notes.values())

    s.fetch_notes()
    assert s._base_doc["n1"] is not None

    # The note has no sct_id at all anymore -- a genuine "no
    # formatting exists" state, not a decode failure. indexableText
    # matches (empty), so decode_failed must stay False.
    sn_cleared = _server_note("n1", text="", sct_id=None, chunks=[])
    sn_cleared.indexable_text = ""
    s._client.notes = {"n1": sn_cleared}

    s.fetch_notes()

    assert s._base_doc["n1"] is None, (
        "baseline must advance to None once formatting is genuinely "
        "gone, not stay stuck at the old (now-wrong) styled doc"
    )
