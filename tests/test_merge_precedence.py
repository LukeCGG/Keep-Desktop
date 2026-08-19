"""Concurrent-edit merge: what wins when web and desktop both moved.

_diff3_merge drives the diff off KEYS (paragraph text) but emits VALUES
(Paragraph objects carrying heading/bold/etc). Two bugs came out of
conflating the two, both of which showed up as "sync did something
weird to my note" rather than as an error:

  * insert dedup compared VALUES, so the same line added on both sides
    with any styling difference between them appeared TWICE;
  * conflict resolution preferred local unconditionally, so a
    formatting-only local change outranked a remote rewrite or
    deletion — silently reverting real content typed on the web.

The rule these pin down: a change to the TEXT outranks a change that
only restyles it, and the prefer-local tie-break applies only when both
sides (or neither) actually changed the text.
"""

from __future__ import annotations

from keep_protocol.nested_model import Paragraph, StyledDoc, StyleRun
from keep_sync_v2 import _three_way_merge, _three_way_merge_styled


def P(text="", heading=0, **style) -> Paragraph:
    return Paragraph(runs=[StyleRun(text=text, **style)] if text else [],
                     heading=heading)


def D(*paras) -> StyledDoc:
    return StyledDoc(paragraphs=list(paras))


def texts(doc: StyledDoc) -> list[str]:
    return [p.text for p in doc.paragraphs]


# ------------------------------------------------------------------
# Insert dedup
# ------------------------------------------------------------------

def test_same_line_added_on_both_sides_appears_once():
    """Desktop types a line; the web copy of that same line arrives
    styled differently (or not at all). Deduping by object equality saw
    two different Paragraphs and kept both."""
    base = D(P("one"), P("three"))
    local = D(P("one"), P("two", bold=True), P("three"))
    remote = D(P("one"), P("two"), P("three"))
    merged, _ = _three_way_merge_styled(base, local, remote)
    assert texts(merged) == ["one", "two", "three"]


def test_deduped_line_keeps_local_styling():
    base = D(P("one"), P("three"))
    local = D(P("one"), P("two", bold=True), P("three"))
    remote = D(P("one"), P("two"), P("three"))
    merged, _ = _three_way_merge_styled(base, local, remote)
    assert merged.paragraphs[1].runs[0].bold is True


def test_same_line_added_with_a_heading_on_one_side_appears_once():
    base = D(P("one"), P("three"))
    local = D(P("one"), P("two", heading=1), P("three"))
    remote = D(P("one"), P("two"), P("three"))
    merged, _ = _three_way_merge_styled(base, local, remote)
    assert texts(merged) == ["one", "two", "three"]


def test_a_genuinely_repeated_line_is_not_collapsed():
    """Dedup counts copies rather than testing membership, so a line the
    user really did enter twice stays twice."""
    base = D(P("one"), P("three"))
    local = D(P("one"), P("two"), P("two"), P("three"))
    remote = D(P("one"), P("two"), P("three"))
    merged, _ = _three_way_merge_styled(base, local, remote)
    assert texts(merged) == ["one", "two", "two", "three"]


def test_different_lines_added_on_each_side_both_survive():
    base = D(P("one"))
    local = D(P("local line"), P("one"))
    remote = D(P("one"), P("remote line"))
    merged, _ = _three_way_merge_styled(base, local, remote)
    assert texts(merged) == ["local line", "one", "remote line"]


def test_same_text_inserted_at_different_places_is_not_deduped():
    """Dedup is per base position: two insertions of the same text at
    genuinely different places are two separate edits."""
    base = D(P("a"), P("b"))
    local = D(P("x"), P("a"), P("b"))
    remote = D(P("a"), P("b"), P("x"))
    merged, _ = _three_way_merge_styled(base, local, remote)
    assert texts(merged) == ["x", "a", "b", "x"]


# ------------------------------------------------------------------
# Content outranks presentation
# ------------------------------------------------------------------

def test_remote_rewrite_beats_a_local_formatting_only_change():
    base = D(P("alpha"), P("beta"))
    local = D(P("alpha"), P("beta", bold=True))
    remote = D(P("alpha"), P("beta rewritten"))
    merged, _ = _three_way_merge_styled(base, local, remote)
    assert texts(merged) == ["alpha", "beta rewritten"]


def test_remote_deletion_beats_a_local_formatting_only_change():
    """Bolding a word on the desktop must not resurrect a paragraph
    deleted on the web."""
    base = D(P("alpha"), P("beta"))
    local = D(P("alpha"), P("beta", bold=True))
    remote = D(P("alpha"))
    merged, _ = _three_way_merge_styled(base, local, remote)
    assert texts(merged) == ["alpha"]


def test_remote_heading_change_does_not_beat_a_local_rewrite():
    """The mirror image: local changed the text, remote only restyled,
    so local wins."""
    base = D(P("alpha"), P("beta"))
    local = D(P("alpha"), P("beta rewritten"))
    remote = D(P("alpha"), P("beta", heading=1))
    merged, _ = _three_way_merge_styled(base, local, remote)
    assert texts(merged) == ["alpha", "beta rewritten"]


def test_both_sides_rewrote_the_same_line_prefers_local():
    base = D(P("alpha"), P("beta"))
    local = D(P("alpha"), P("beta local"))
    remote = D(P("alpha"), P("beta remote"))
    merged, conflict = _three_way_merge_styled(base, local, remote)
    assert texts(merged) == ["alpha", "beta local"]
    assert conflict is True


def test_formatting_only_change_on_both_sides_prefers_local():
    base = D(P("alpha"))
    local = D(P("alpha", bold=True))
    remote = D(P("alpha", italic=True))
    merged, _ = _three_way_merge_styled(base, local, remote)
    assert merged.paragraphs[0].runs[0].bold is True
    assert merged.paragraphs[0].runs[0].italic is False


# ------------------------------------------------------------------
# Baseline diff3 behaviour that must not regress
# ------------------------------------------------------------------

def test_one_side_unchanged_adopts_the_other_wholesale():
    base = D(P("a"), P("b"))
    unchanged = D(P("a"), P("b"))
    changed = D(P("a"), P("b"), P("c"))
    merged, conflict = _three_way_merge_styled(base, unchanged, changed)
    assert texts(merged) == ["a", "b", "c"]
    assert conflict is False
    merged, conflict = _three_way_merge_styled(base, changed, unchanged)
    assert texts(merged) == ["a", "b", "c"]
    assert conflict is False


def test_non_overlapping_edits_combine():
    base = D(P("a"), P("b"), P("c"))
    local = D(P("a EDITED"), P("b"), P("c"))
    remote = D(P("a"), P("b"), P("c EDITED"))
    merged, conflict = _three_way_merge_styled(base, local, remote)
    assert texts(merged) == ["a EDITED", "b", "c EDITED"]
    assert conflict is False


def test_plain_text_merge_still_combines_non_overlapping_edits():
    merged, conflict = _three_way_merge(
        "a\nb\nc", "a EDITED\nb\nc", "a\nb\nc EDITED")
    assert merged == "a EDITED\nb\nc EDITED"
    assert conflict is False


def test_merge_never_smuggles_a_newline_into_a_run():
    """Run text is per-paragraph by construction; a newline inside one
    would desync every downstream character position."""
    base = D(P("a"), P("b"))
    local = D(P("a"), P("b", bold=True), P("c"))
    remote = D(P("a2"), P("b"))
    merged, _ = _three_way_merge_styled(base, local, remote)
    for para in merged.paragraphs:
        for run in para.runs:
            assert "\n" not in run.text
