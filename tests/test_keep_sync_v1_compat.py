"""Compatibility tests for the v1 (gkeepapi) KeepSync backend.

AppController._sync_worker always calls fetch_notes(hold_baseline_for=...)
regardless of which backend (KeepSyncV2 or the v1 KeepSync fallback) is
active -- v1 remains reachable via config.keep_protocol_v2=False, or
automatically if KeepSyncV2's constructor raises.
"""

from __future__ import annotations

from keep_sync import KeepSync


def test_fetch_notes_accepts_hold_baseline_for_kwarg():
    """Regression: KeepSyncV2.fetch_notes() gained a hold_baseline_for
    parameter, and AppController._sync_worker started passing it on
    every call unconditionally. The v1 KeepSync backend's
    fetch_notes() had no matching parameter, so any installation still
    running v1 got a TypeError on every single periodic/manual sync --
    silently swallowed by _sync_worker's broad exception handler,
    which degrades that install to push-only forever with no
    user-visible error. Must accept (and may ignore) the kwarg to stay
    a drop-in replacement."""
    sync = KeepSync()
    # Not authenticated -- fetch_notes short-circuits to [], but only
    # AFTER accepting the call signature. A TypeError from an
    # unexpected kwarg happens before that check ever runs.
    assert sync.fetch_notes(hold_baseline_for={"some-id"}) == []
    assert sync.fetch_notes(force_resync=True, hold_baseline_for=set()) == []
