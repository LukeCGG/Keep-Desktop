"""Tests for AppController's sync-adjacent methods.

app_controller.py has no dedicated Qt-widget test harness, so these
tests call the unbound method directly against a minimal stand-in
object carrying just the attributes/methods `_on_note_changed` touches
-- avoiding the cost of spinning up a real QWidget-based AppController
for what's fundamentally plain-Python state-mutation logic.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app_controller import AppController
from keep_sync import KeepNote
from keep_protocol.nested_model import StyledDoc, Paragraph, StyleRun


class _FakeControllerSelf:
    def __init__(self):
        self.windows = {}
        self._notes = {}
        self._dirty = set()
        self._last_edit_time = {}
        self._pending_widget_refresh = set()
        self._save_debounce = MagicMock()
        self._sync_debounce = MagicMock()
        self._note_merged_during_push = MagicMock()
        self.sync = MagicMock(is_authenticated=False)

    def _refresh_manager_if_open(self):
        pass

    def _push_dirty_worker(self):
        pass

    def _sync_worker(self, busy_ids):
        pass


def _make_window(text: str, html: str, title: str = "T"):
    win = MagicMock()
    win.get_text.return_value = text
    win.get_title.return_value = title
    win.color_hex = "#FFF475"
    win.dark_mode = False
    win.get_html.return_value = html
    win._is_list = False
    return win


def test_on_note_changed_preserves_styled_doc_when_body_unchanged():
    """Regression: _on_note_changed used to unconditionally delattr
    note.styled_doc on EVERY invocation, even for a title-only or
    colour-only edit that never touched the body. styled_doc absence
    is also how sync_merge.py's decide_merge tells "local just echoed
    its own push back" apart from "a genuine concurrent web restyle
    arrived" (see decide_merge's local_doc-is-None branch) -- clearing
    it for edits that never touched the body made that heuristic wrong
    far more often than intended: a title rename landing in the same
    sync cycle as a concurrent web restyle would silently swallow the
    restyle instead of showing it."""
    fake_self = _FakeControllerSelf()
    note = KeepNote(id="n1", title="Old Title", text="hello", html="<p>hello</p>")
    note.styled_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="hello", bold=True)]),
    ])
    fake_self._notes["n1"] = note
    fake_self.windows["n1"] = _make_window(
        text="hello", html="<p>hello</p>", title="New Title",
    )

    AppController._on_note_changed(fake_self, "n1")

    assert hasattr(note, "styled_doc"), (
        "a title-only edit must not clear the cached styled_doc baseline"
    )
    assert note.title == "New Title"


def test_on_note_changed_clears_styled_doc_when_body_changed():
    """Sanity check alongside the above: a GENUINE body edit must
    still invalidate the cached styled_doc, since it no longer
    reflects what's on screen."""
    fake_self = _FakeControllerSelf()
    note = KeepNote(id="n1", title="T", text="hello", html="<p>hello</p>")
    note.styled_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="hello", bold=True)]),
    ])
    fake_self._notes["n1"] = note
    fake_self.windows["n1"] = _make_window(
        text="hello world", html="<p>hello world</p>",
    )

    AppController._on_note_changed(fake_self, "n1")

    assert not hasattr(note, "styled_doc"), (
        "a genuine body edit must clear the now-stale styled_doc baseline"
    )


def test_push_dirty_notes_skips_when_a_push_is_already_running():
    """Regression: _push_dirty_notes (the debounced post-edit push)
    had no re-entrancy guard against _sync_worker's own "push dirty
    notes first" step. Both paths call _push_one_dirty_note for
    whatever's in self._dirty from two INDEPENDENT background
    threads with no mutual exclusion between them, so a debounced
    push firing while a periodic sync's push phase is still in
    flight (or vice versa) could push the SAME note concurrently --
    redundant network calls racing on dirty-clearing and
    _note_merged_during_push bookkeeping. _push_dirty_notes must
    guard on the DEDICATED _push_running flag (not _sync_running --
    see the sibling test below for why sharing _sync_running is
    itself a bug, not the fix)."""
    fake_self = _FakeControllerSelf()
    fake_self.sync = MagicMock(is_authenticated=True)
    fake_self._dirty = {"n1"}
    fake_self._push_running = True  # a push is already in flight

    with patch("app_controller.threading.Thread") as mock_thread:
        AppController._push_dirty_notes(fake_self)

    mock_thread.assert_not_called()


def test_push_dirty_notes_runs_when_no_push_in_flight():
    """Sanity check alongside the above: the debounced push must
    still actually fire when nothing else is running."""
    fake_self = _FakeControllerSelf()
    fake_self.sync = MagicMock(is_authenticated=True)
    fake_self._dirty = {"n1"}

    with patch("app_controller.threading.Thread") as mock_thread:
        AppController._push_dirty_notes(fake_self)

    mock_thread.assert_called_once()
    assert fake_self._push_running is True


def test_push_dirty_notes_does_not_share_sync_running_with_full_sync():
    """Regression: an EARLIER version of this guard reused
    _full_sync's own _sync_running flag. _full_sync guards its ENTIRE
    cycle (push AND pull) with that flag, so sharing it meant a
    debounced push in flight -- which fires on every edit during
    active typing, far more often than the 30s periodic tick --
    blocked that periodic cycle's PULL phase too. That starves
    fetch_notes() (the only path that brings in web edits) during
    exactly the "actively editing while a web change also landed"
    scenario the push/pull split exists to handle correctly. A
    debounced push in flight must NOT prevent _full_sync from
    starting its own cycle."""
    fake_self = _FakeControllerSelf()
    fake_self.sync = MagicMock(is_authenticated=True)
    fake_self._push_running = True  # a debounced push is mid-flight
    fake_self._sync_running = False
    fake_self.windows = {}

    def fake_is_note_busy(_nid):
        return False

    fake_self._is_note_busy = fake_is_note_busy

    with patch("app_controller.threading.Thread") as mock_thread:
        AppController._full_sync(fake_self)

    mock_thread.assert_called_once(), (
        "_full_sync must start its own cycle (including the pull) "
        "even while an unrelated debounced push is in flight"
    )


def test_push_one_dirty_note_marks_pending_refresh_when_merge_changes_note():
    """Regression: when push_note's 3-way/format-preserving merge
    rewrites note.text/.html in place (a concurrent web edit folded
    in), _push_one_dirty_note clears the note from self._dirty (the
    push succeeded) and emits _note_merged_during_push to queue a
    widget refresh on the main thread -- but that signal is only
    PROCESSED whenever the main thread's event loop gets to it, which
    can be arbitrarily delayed. In that gap, the note is in neither
    self._dirty nor busy_ids, so _sync_worker's next fetch_notes()
    call (running on a DIFFERENT background thread, possibly only
    moments later) had nothing telling it this note's widget hasn't
    caught up yet -- it would advance the note's baseline to the
    server's latest state, and the next local edit (read from the
    still-stale widget) would silently revert the just-merged content
    right back off the server. self._pending_widget_refresh must be
    populated before the widget has actually caught up."""
    fake_self = _FakeControllerSelf()
    note = KeepNote(id="n1", title="T", text="local text", html="")
    fake_self._notes["n1"] = note
    fake_self._dirty = {"n1"}

    def fake_push_note(pushed_note):
        # Simulate push_note's merge rewriting the note in place.
        pushed_note.text = "merged text (local + concurrent web edit)"
        return True

    fake_self.sync = MagicMock(push_note=fake_push_note)

    AppController._push_one_dirty_note(fake_self, "n1")

    assert "n1" in fake_self._pending_widget_refresh, (
        "a note whose cache was updated by a merge-during-push must be "
        "tracked as pending a widget refresh"
    )
    assert "n1" not in fake_self._dirty  # push succeeded, no further edit


def test_refresh_window_clears_pending_refresh_after_rendering():
    """Sanity check alongside the above: once _refresh_window actually
    applies the cache to the widget, the note must no longer be held
    back from baseline advancement."""
    fake_self = _FakeControllerSelf()
    note = KeepNote(id="n1", title="T", text="merged text", html="")
    fake_self._notes["n1"] = note
    fake_self._pending_widget_refresh = {"n1"}
    fake_self.windows["n1"] = _make_window(text="merged text", html="")

    AppController._refresh_window(fake_self, "n1")

    assert "n1" not in fake_self._pending_widget_refresh, (
        "the widget has now caught up -- the note must no longer be "
        "held back from baseline advancement"
    )
