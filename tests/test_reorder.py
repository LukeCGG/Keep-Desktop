"""Tests for the reorder sort_value computation.

These cover the case where ``local_order`` (drives desktop display)
and ``sort_key`` (drives Keep server display) have drifted apart —
which can happen any time a metadata push failed mid-flight or a
sync race overwrote a sort_key. In that state the previously-buggy
behaviour was that "move to top" / "move to bottom" syncs to Keep
as a single-step move (because new_sv was anchored to the visual
neighbour, which wasn't actually the global extreme on the wire).
"""

from __future__ import annotations

from dataclasses import dataclass

from app_controller import _compute_new_sort_value, _REORDER_STEP


@dataclass
class _Note:
    """Minimal stub matching the duck-type _compute_new_sort_value uses."""
    id: str
    sort_key: int


def _move_to_top(notes: list[_Note], idx: int) -> list[_Note]:
    out = list(notes)
    moved = out.pop(idx)
    out.insert(0, moved)
    return out


def _move_to_bottom(notes: list[_Note], idx: int) -> list[_Note]:
    out = list(notes)
    moved = out.pop(idx)
    out.append(moved)
    return out


# ----- happy path: sort_keys monotonic with desktop order -----

def test_move_to_top_when_sort_keys_match_local_order():
    notes = [_Note("a", 5_000_000), _Note("b", 4_000_000),
             _Note("c", 3_000_000), _Note("d", 2_000_000)]
    moved = notes[3]                       # "d" — currently at the bottom
    new_order = _move_to_top(notes, 3)     # [d, a, b, c]
    new_sv = _compute_new_sort_value(new_order, moved)
    # Server descending sort: d's sort_value must beat all others.
    assert new_sv > max(n.sort_key for n in notes if n is not moved)


def test_move_to_bottom_when_sort_keys_match_local_order():
    notes = [_Note("a", 5_000_000), _Note("b", 4_000_000),
             _Note("c", 3_000_000), _Note("d", 2_000_000)]
    moved = notes[0]                       # "a"
    new_order = _move_to_bottom(notes, 0)  # [b, c, d, a]
    new_sv = _compute_new_sort_value(new_order, moved)
    assert new_sv < min(n.sort_key for n in notes if n is not moved)


# ----- the actual bug: sort_keys don't match local_order -----

def test_move_to_top_dominates_global_max_even_when_neighbour_is_not_top():
    """The bug. Suppose desktop view is [b, c, d, a] (driven by the
    user's prior reorders into local_order) but the server's sort_keys
    are still [a=5M, b=4M, c=3M, d=2M] — a previous push failed or a
    sync overwrote them. Now the user clicks "move to top" on `a`.

    The buggy behaviour anchored new_sv on ``ordered[1]`` (= b, sk=4M),
    yielding new_sv ≈ 4M + STEP. But `a` already had sk=5M, so the
    server sees a NO-OP relative move, not the requested move-to-top.
    """
    a = _Note("a", 5_000_000)
    b = _Note("b", 4_000_000)
    c = _Note("c", 3_000_000)
    d = _Note("d", 2_000_000)
    # Desktop view (after past reorders) is [b, c, d, a] but on the
    # wire `a` still has the highest sort_key.
    ordered_after_move_to_top = [a, b, c, d]
    new_sv = _compute_new_sort_value(ordered_after_move_to_top, a)
    # Must dominate b/c/d AND a's prior sort_key on the wire.
    assert new_sv > max(b.sort_key, c.sort_key, d.sort_key, 5_000_000)


def test_move_to_bottom_dominates_global_min_even_when_neighbour_is_not_bottom():
    """Symmetric to the move-to-top case."""
    a = _Note("a", 5_000_000)
    b = _Note("b", 4_000_000)
    c = _Note("c", 3_000_000)
    d = _Note("d", 2_000_000)
    # Desktop wants [a, b, c, d] -> [b, c, d, a]; but on the wire d
    # still has the lowest sort_key, so the immediate visual neighbour
    # (d, sk=2M) is NOT the right anchor.
    ordered_after_move_to_bottom = [b, c, d, a]
    new_sv = _compute_new_sort_value(ordered_after_move_to_bottom, a)
    assert new_sv < min(b.sort_key, c.sort_key, d.sort_key, 5_000_000)


# ----- adjacent moves (up / down) keep using midpoints -----

def test_move_up_picks_midpoint_between_neighbours():
    a = _Note("a", 10_000_000)
    b = _Note("b", 8_000_000)
    c = _Note("c", 6_000_000)
    d = _Note("d", 4_000_000)
    # User moves c up one slot: [a, c, b, d]
    new_order = [a, c, b, d]
    new_sv = _compute_new_sort_value(new_order, c)
    # Should land strictly between a's and b's sort_keys so the
    # server orders them a > c > b.
    assert b.sort_key < new_sv < a.sort_key


def test_move_down_picks_midpoint_between_neighbours():
    a = _Note("a", 10_000_000)
    b = _Note("b", 8_000_000)
    c = _Note("c", 6_000_000)
    d = _Note("d", 4_000_000)
    # User moves b down one slot: [a, c, b, d]
    new_order = [a, c, b, d]
    new_sv = _compute_new_sort_value(new_order, b)
    assert d.sort_key < new_sv < c.sort_key


# ----- edge cases -----

def test_single_note_list_keeps_existing_sort_key():
    only = _Note("only", 7_777_777)
    assert _compute_new_sort_value([only], only) == 7_777_777


def test_single_note_list_with_zero_sort_key_returns_step():
    only = _Note("only", 0)
    # Falls back to STEP so the note has SOME sort_value on first push.
    assert _compute_new_sort_value([only], only) == _REORDER_STEP


def test_duplicate_neighbour_sort_keys_get_tiebreak():
    a = _Note("a", 5_000_000)
    b = _Note("b", 5_000_000)  # tied with a
    c = _Note("c", 4_000_000)
    # User moves c up between two equal-sort_key notes. Midpoint
    # would equal both — ensure we tiebreak below ``above``.
    new_order = [a, c, b]
    new_sv = _compute_new_sort_value(new_order, c)
    assert new_sv < a.sort_key
    assert new_sv != b.sort_key
