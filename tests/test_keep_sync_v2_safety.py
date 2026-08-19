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
from keep_sync_v2 import (
    KeepSyncV2, _apply_text_edits_preserve_format,
    _three_way_merge, _three_way_merge_styled,
)
from keep_protocol.client import KeepError
from keep_protocol.models import Note as ServerNote
from keep_protocol.nested_model import (
    StyledDoc, Paragraph, StyleRun, encode_full_replace, to_html,
)


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
    # A bare MagicMock() return value is truthy and not iterable, unlike
    # the real KeepClient's `set[str]` — stub it to match "nothing
    # stale this cycle", the common case these tests exercise.
    s._client.pop_stale_snapshot_ids.return_value = set()
    s._client.is_snapshot_stale.return_value = False
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


# ------------------------------------------------------- formatting-only push

def test_push_note_sends_local_formatting_change_despite_remote_text_move(sync):
    """Regression: bold/italic/heading toggles never touch plain text, so
    `local_changed` (a pure text comparison) is always False for them. If
    the server's plain text has ALSO moved since our last fetch for an
    unrelated reason (remote_changed=True), push_note used to treat "text
    unchanged locally" as "nothing to push" and return True without ever
    calling update_text_diff — silently dropping the user's formatting
    change and clearing the dirty flag, so the very next pull would
    overwrite the widget with the server's unstyled copy (looks exactly
    like the edit being reverted)."""
    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Alpha line")], heading=0),
        Paragraph(runs=[StyleRun(text="Gamma line")], heading=0),
    ])
    local_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[
            StyleRun(text="A"), StyleRun(text="l", bold=True), StyleRun(text="pha line"),
        ], heading=0),
        Paragraph(runs=[StyleRun(text="Gamma line")], heading=0),
    ])
    server_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Alpha line")], heading=0),
        Paragraph(runs=[StyleRun(text="Gamma line changed remotely")], heading=0),
    ])
    chunks = [__import__("json").dumps(
        encode_full_replace(server_doc, current_text_length=0)
    )]
    server = _server_note(
        "note.restyle", sct_id="sct.x",
        indexable_text=server_doc.plain_text, chunks=chunks,
    )
    sync._server_notes["note.restyle"] = server
    sync._base_text["note.restyle"] = base_doc.plain_text  # == local plain text
    sync._base_doc["note.restyle"] = base_doc

    note = KeepNote(
        id="note.restyle", title="", text=local_doc.plain_text,
        html=to_html(local_doc), color_hex="#FFF475",
    )
    result = sync.push_note(note)

    assert result is True
    sync._client.update_text_diff.assert_called_once()
    new_doc = sync._client.update_text_diff.call_args.args[1]
    assert any(r.bold for p in new_doc.paragraphs for r in p.runs), (
        "local bold formatting was dropped instead of being pushed"
    )


def test_push_note_merges_concurrent_formatting_only_changes_on_both_sides(sync):
    """Regression: when BOTH local and remote make a PURE formatting
    change (no plain-text change anywhere, on EITHER side), plain
    text equality can't distinguish this from "nothing changed" --
    local_changed and remote_changed are both False. push_note's
    "not local_changed" branch detects the LOCAL restyle correctly
    (local_restyled, comparing local's widget against base_doc) but
    used to push local_doc_for_style_check UNCONDITIONALLY once
    detected, with no check for whether REMOTE also restyled a
    DIFFERENT paragraph since the same base. That diffs local's
    (remote-restyle-unaware) doc against the server's CURRENT
    (already remote-restyled) doc via update_text_diff, silently
    reverting the web's concurrent formatting change -- with no
    conflict warning, since remote_changed being False meant the main
    3-way-merge branch was never even reached."""
    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="hello")], heading=0),
        Paragraph(runs=[StyleRun(text="world")], heading=0),
    ])
    # Remote independently bolds paragraph 0. Plain text unchanged.
    server_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="hello", bold=True)], heading=0),
        Paragraph(runs=[StyleRun(text="world")], heading=0),
    ])
    chunks = [__import__("json").dumps(
        encode_full_replace(server_doc, current_text_length=0)
    )]
    server = _server_note(
        "note.bothrestyle", sct_id="sct.x",
        indexable_text=server_doc.plain_text, chunks=chunks,
    )
    sync._server_notes["note.bothrestyle"] = server
    sync._base_text["note.bothrestyle"] = base_doc.plain_text
    sync._base_doc["note.bothrestyle"] = base_doc

    # Local independently italicizes paragraph 1. Plain text unchanged.
    local_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="hello")], heading=0),
        Paragraph(runs=[StyleRun(text="world", italic=True)], heading=0),
    ])
    note = KeepNote(
        id="note.bothrestyle", title="", text=local_doc.plain_text,
        html=to_html(local_doc), color_hex="#FFF475",
    )
    result = sync.push_note(note)

    assert result is True
    sync._client.update_text_diff.assert_called_once()
    new_doc = sync._client.update_text_diff.call_args.args[1]
    hello_run = new_doc.paragraphs[0].runs[0]
    world_run = new_doc.paragraphs[1].runs[0]
    assert hello_run.bold is True, "remote's concurrent bold was reverted"
    assert world_run.italic is True, "local's concurrent italic was dropped"


def test_push_note_does_not_revert_remote_restyle_widget_never_saw(sync):
    """Regression (mirror image of the above): a web-only formatting
    change (e.g. bold) also never touches plain text, so both
    remote_changed and local_changed can be False even though the
    server's styling moved. If the local widget was never refreshed
    with that change (its refresh can be deferred while the note is
    busy/dirty — see AppController._refresh_window_when_idle),
    push_note used to fall straight through to building new_doc from
    the stale, unstyled widget HTML and diffing it against the fresh
    (styled) server doc — sending an explicit "un-bold" op and
    silently reverting the web's change instead of adopting it."""
    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Alpha line")], heading=0),
    ])
    # The server now has "Alpha" bolded (a web-only restyle); the
    # local widget's content below still matches base_doc exactly —
    # i.e. genuinely stale, not a real local edit.
    server_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[
            StyleRun(text="Alpha", bold=True), StyleRun(text=" line"),
        ], heading=0),
    ])
    chunks = [__import__("json").dumps(
        encode_full_replace(server_doc, current_text_length=0)
    )]
    server = _server_note(
        "note.webrestyle", sct_id="sct.x",
        indexable_text=server_doc.plain_text, chunks=chunks,
    )
    sync._server_notes["note.webrestyle"] = server
    sync._base_text["note.webrestyle"] = base_doc.plain_text
    sync._base_doc["note.webrestyle"] = base_doc

    note = KeepNote(
        id="note.webrestyle", title="T", text=base_doc.plain_text,
        html=to_html(base_doc), color_hex="#FFF475",
    )
    result = sync.push_note(note)

    assert result is True
    # Nothing local to push (widget was just stale) -- update_text_diff
    # must NOT be called at all, let alone with an un-bold op.
    sync._client.update_text_diff.assert_not_called()
    # The baseline must NOT be advanced to server's styling here: the
    # widget still shows the pre-restyle body, so advancing would put
    # the baseline "ahead of" the widget. If the user's very next edit
    # is read from that still-stale widget, it would then look like
    # it's missing the web's restyle relative to the NEW baseline --
    # and the push after that would silently revert it right back off
    # the server. Leaving the baseline alone lets fetch_notes() (which
    # only advances it once it can confirm the widget was actually
    # refreshed) or a genuine subsequent local edit's fresh 3-way
    # merge handle it correctly instead.
    assert sync._base_doc["note.webrestyle"] is base_doc
    assert not any(
        r.bold for p in sync._base_doc["note.webrestyle"].paragraphs for r in p.runs
    )


def test_push_note_base_text_reflects_what_was_actually_sent_not_late_read(sync):
    """Regression: push_note used to finish by re-reading keep_note.text
    fresh (AFTER the network calls) to set _base_text, instead of using
    new_doc.plain_text (what was actually diffed and sent, same source
    _base_doc uses). push_note holds no lock over the KeepNote object
    itself, so on a slow push the main thread can (and does, via
    _on_note_changed) mutate keep_note.text/.html while push_note is
    still running -- e.g. during the network round-trip inside
    update_text_diff. A late re-read then picks up text the user typed
    mid-push that was never actually included in what got sent, so
    _base_text ends up claiming the server has content it doesn't.
    _base_doc (built from the captured new_doc) then disagrees with
    _base_text, and the NEXT push's remote_changed/local_changed check
    reads the real server state as a conflicting edit -- landing in the
    3-way-merge's conflict branch, which discards ALL formatting and
    sends plain text only. This is exactly the failure chain seen in a
    real user log: "format-preserving merge" -> "edited again mid-push"
    -> "3-way merge ... had conflict; preferring local edits" -> bold
    lost."""
    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Alpha line")], heading=0),
    ])
    chunks = [__import__("json").dumps(
        encode_full_replace(base_doc, current_text_length=0)
    )]
    server = _server_note(
        "note.midpush", sct_id="sct.x",
        indexable_text=base_doc.plain_text, chunks=chunks,
    )
    sync._server_notes["note.midpush"] = server
    sync._base_text["note.midpush"] = base_doc.plain_text
    sync._base_doc["note.midpush"] = base_doc

    # What's actually being pushed: a new bolded paragraph.
    pushed_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Alpha line")], heading=0),
        Paragraph(runs=[StyleRun(text="Bold text", bold=True)], heading=0),
    ])
    note = KeepNote(
        id="note.midpush", title="T", text=pushed_doc.plain_text,
        html=to_html(pushed_doc), color_hex="#FFF475",
    )

    def mutate_mid_push(server, new_doc, *, new_title=None, dry_run=False):
        # Simulate the main thread editing further WHILE this network
        # call is in flight -- note.text now reflects text that was
        # never part of what we're diffing/sending right now.
        note.text = pushed_doc.plain_text + " and even more"
        note.html = to_html(StyledDoc(sct_id="sct.x", paragraphs=[
            Paragraph(runs=[StyleRun(text="Alpha line")], heading=0),
            Paragraph(runs=[StyleRun(text="Bold text and even more", bold=True)], heading=0),
        ]))
        return {}

    sync._client.update_text_diff.side_effect = mutate_mid_push

    result = sync.push_note(note)

    assert result is True
    assert sync._base_text["note.midpush"] == pushed_doc.plain_text, (
        f"_base_text ({sync._base_text['note.midpush']!r}) should match what "
        f"was actually sent ({pushed_doc.plain_text!r}), not the mid-push "
        f"mutation ({note.text!r})"
    )
    assert sync._base_text["note.midpush"] == sync._base_doc["note.midpush"].plain_text, (
        "_base_text and _base_doc must agree -- a mismatch here is exactly "
        "what makes the next push misread the real server state as a "
        "conflicting edit and discard formatting"
    )


# --------------------------------------- _apply_text_edits_preserve_format

def test_preserve_format_uses_local_styling_for_inserted_text():
    """Regression: when the note has a genuine pending remote restyle
    (web bolded existing text since our last fetch) AND the user
    separately types brand new text and styles it themselves (e.g.
    two new lines, bolding a few letters in one), the merge used to
    only know how to inherit style from the NEAREST REMOTE character —
    it had no notion of what the user just did locally, so newly
    typed formatting came out unstyled. Passing local_doc lets
    inserted/changed ranges carry the user's own styling instead."""
    base_text = "Alpha line"
    local_text = "Alpha line\nBeta line\nGamma line"
    remote_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[
            StyleRun(text="Alpha", bold=True), StyleRun(text=" line"),
        ], heading=0),
    ])
    local_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Alpha line")], heading=0),
        Paragraph(runs=[
            StyleRun(text="Beta", bold=True), StyleRun(text=" line"),
        ], heading=0),
        Paragraph(runs=[StyleRun(text="Gamma line")], heading=0),
    ])
    assert local_doc.plain_text == local_text

    without_local = _apply_text_edits_preserve_format(remote_doc, base_text, local_text)
    beta_bold_without = any(
        r.bold for p in without_local.paragraphs for r in p.runs if r.text == "Beta"
    )
    assert not beta_bold_without, (
        "sanity check: the old neighbour-inherit-only behaviour should "
        "drop the local bold -- if this fails, the repro itself is wrong"
    )

    merged = _apply_text_edits_preserve_format(
        remote_doc, base_text, local_text, local_doc=local_doc,
    )
    assert len(merged.paragraphs) == 3, (
        f"expected 3 separate paragraphs, got {len(merged.paragraphs)} -- "
        f"newly typed paragraph breaks must not collapse into one "
        f"paragraph with a literal embedded newline"
    )
    beta_bold = any(
        r.bold for p in merged.paragraphs for r in p.runs if r.text == "Beta"
    )
    alpha_bold = any(
        r.bold for p in merged.paragraphs for r in p.runs if r.text == "Alpha"
    )
    assert beta_bold, "local bold on newly typed 'Beta' was dropped"
    assert alpha_bold, "remote's existing bold on 'Alpha' should still be preserved"


def test_preserve_format_fallback_path_splits_newly_typed_enter_into_new_paragraph():
    """Regression: in the FALLBACK path (local_doc unavailable, so
    inserted/changed text style-templates from the nearest REMOTE
    character instead of the user's own local styling), a literal
    '\\n' character from a freshly-typed Enter keypress was tagged
    with that same template -- indistinguishable, in the rebuild
    loop, from an ordinary character. The rebuild loop only starts a
    new paragraph when a '\\n' carries NO template (the sentinel
    convention every other paragraph-boundary marker in this function
    uses), so a nearby styled run being found (the common case in any
    note with existing formatting, e.g. the whole note here is bold)
    silently merged what should be two separate paragraphs into one,
    leaving a stray literal newline embedded inside a single run's
    text -- corrupting the paragraph structure the wire encoder
    anchors headings/styles against."""
    base_text = "Section\nbody"
    local_text = "Section\nbody\nnew line text"
    remote_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Section", bold=True)]),
        Paragraph(runs=[StyleRun(text="body", bold=True)]),
    ])

    result = _apply_text_edits_preserve_format(
        remote_doc, base_text, local_text, local_doc=None,
    )

    assert result.plain_text == local_text
    assert len(result.paragraphs) == 3, (
        f"expected 3 paragraphs (Section / body / new line text), got "
        f"{len(result.paragraphs)}: {[p.text for p in result.paragraphs]}"
    )
    for p in result.paragraphs:
        for r in p.runs:
            assert "\n" not in r.text, (
                f"literal newline embedded in run text: {r.text!r}"
            )


def test_push_note_format_preserving_merge_keeps_local_style_on_new_text(sync):
    """Full push_note-level regression for the same scenario: server has
    a genuine pending restyle, local adds new paragraphs with its own
    new bold — the pushed doc must carry both the local bold AND
    proper paragraph boundaries (not everything flattened into one
    paragraph with a literal \\n)."""
    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Alpha line")], heading=0),
    ])
    server_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[
            StyleRun(text="Alpha", bold=True), StyleRun(text=" line"),
        ], heading=0),
    ])
    chunks = [__import__("json").dumps(
        encode_full_replace(server_doc, current_text_length=0)
    )]
    server = _server_note(
        "note.newlinesbold", sct_id="sct.x",
        indexable_text=server_doc.plain_text, chunks=chunks,
    )
    sync._server_notes["note.newlinesbold"] = server
    sync._base_text["note.newlinesbold"] = base_doc.plain_text
    sync._base_doc["note.newlinesbold"] = base_doc

    local_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Alpha line")], heading=0),
        Paragraph(runs=[
            StyleRun(text="Beta", bold=True), StyleRun(text=" line"),
        ], heading=0),
        Paragraph(runs=[StyleRun(text="Gamma line")], heading=0),
    ])
    note = KeepNote(
        id="note.newlinesbold", title="T", text=local_doc.plain_text,
        html=to_html(local_doc), color_hex="#FFF475",
    )

    result = sync.push_note(note)

    assert result is True
    sync._client.update_text_diff.assert_called_once()
    new_doc = sync._client.update_text_diff.call_args.args[1]
    assert len(new_doc.paragraphs) == 3
    beta_bold = any(
        r.bold for p in new_doc.paragraphs for r in p.runs if r.text == "Beta"
    )
    assert beta_bold, "local bold on newly typed 'Beta' was dropped from the pushed doc"


# --------------------------------------------------- _three_way_merge(_styled)

def test_three_way_merge_plain_text_combines_non_overlapping_edits():
    """Anchor _three_way_merge's existing plain-text behaviour across
    the _diff3_merge refactor: non-overlapping line edits on different
    lines combine cleanly with no conflict."""
    base = "Heading\nFirst body line\nLast body line"
    local = "Heading\nFirst body line\nLast body line edited locally"
    remote = "Heading Updated On Web\nFirst body line\nLast body line"
    merged, conflict = _three_way_merge(base, local, remote)
    assert conflict is False
    assert "Heading Updated On Web" in merged
    assert "edited locally" in merged


def test_three_way_merge_plain_text_conflict_prefers_local():
    """Anchor: overlapping edits to the SAME line are a genuine
    conflict, and local wins."""
    base = "one\ntwo\nthree"
    local = "one\nTWO-LOCAL\nthree"
    remote = "one\nTWO-REMOTE\nthree"
    merged, conflict = _three_way_merge(base, local, remote)
    assert conflict is True
    assert "TWO-LOCAL" in merged
    assert "TWO-REMOTE" not in merged


def test_three_way_merge_styled_preserves_heading_on_each_side():
    """Regression: the OLD 3-way merge branch always discarded ALL
    formatting to plain text whenever both sides had genuinely
    concurrent (different-location) edits — even a clean,
    non-conflicting merge — which looked like local had wholesale
    overwritten the web version and, separately, reverted every
    heading back to body text. _three_way_merge_styled must keep each
    paragraph's own heading/run styling instead."""
    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="My Heading")], heading=1),
        Paragraph(runs=[StyleRun(text="First body line")], heading=0),
        Paragraph(runs=[StyleRun(text="Last body line")], heading=0),
    ])
    remote_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="My Heading Updated On Web")], heading=1),
        Paragraph(runs=[StyleRun(text="First body line")], heading=0),
        Paragraph(runs=[StyleRun(text="Last body line")], heading=0),
    ])
    local_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="My Heading")], heading=1),
        Paragraph(runs=[StyleRun(text="First body line")], heading=0),
        Paragraph(runs=[StyleRun(text="Last body line edited locally")], heading=2),
    ])

    merged, conflict = _three_way_merge_styled(base_doc, local_doc, remote_doc)

    assert conflict is False
    assert "Updated On Web" in merged.plain_text
    assert "edited locally" in merged.plain_text
    assert merged.paragraphs[0].heading == 1, "remote's Heading 1 should survive"
    assert merged.paragraphs[2].heading == 2, "local's Heading 2 should survive"


def test_push_note_3way_merge_preserves_formatting_and_combines_edits(sync):
    """Full push_note-level regression for the exact reported scenario:
    web edits one paragraph, local independently edits a different
    paragraph (also changing its heading). Both concurrent, non-
    overlapping edits, so this must land as a clean 3-way merge that
    keeps BOTH edits and BOTH paragraphs' heading formatting -- not
    collapse everything to unstyled body text."""
    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="My Heading")], heading=1),
        Paragraph(runs=[StyleRun(text="First body line")], heading=0),
        Paragraph(runs=[StyleRun(text="Last body line")], heading=0),
    ])
    server_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="My Heading Updated On Web")], heading=1),
        Paragraph(runs=[StyleRun(text="First body line")], heading=0),
        Paragraph(runs=[StyleRun(text="Last body line")], heading=0),
    ])
    chunks = [__import__("json").dumps(
        encode_full_replace(server_doc, current_text_length=0)
    )]
    server = _server_note(
        "note.3way", sct_id="sct.x",
        indexable_text=server_doc.plain_text, chunks=chunks,
    )
    sync._server_notes["note.3way"] = server
    sync._base_text["note.3way"] = base_doc.plain_text
    sync._base_doc["note.3way"] = base_doc

    local_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="My Heading")], heading=1),
        Paragraph(runs=[StyleRun(text="First body line")], heading=0),
        Paragraph(runs=[StyleRun(text="Last body line edited locally")], heading=2),
    ])
    note = KeepNote(
        id="note.3way", title="T", text=local_doc.plain_text,
        html=to_html(local_doc), color_hex="#FFF475",
    )

    result = sync.push_note(note)

    assert result is True
    sync._client.update_text_diff.assert_called_once()
    new_doc = sync._client.update_text_diff.call_args.args[1]
    assert "Updated On Web" in new_doc.plain_text, (
        "web's edit was lost -- local overwrote it instead of 3-way merging"
    )
    assert "edited locally" in new_doc.plain_text
    assert new_doc.paragraphs[0].heading == 1, "remote's Heading 1 was reverted to body text"
    assert new_doc.paragraphs[2].heading == 2, "local's Heading 2 was reverted to body text"


def _make_two_section_doc(section_a_body: str, section_b_body: str) -> StyledDoc:
    return StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Title")], heading=1),
        Paragraph(runs=[], heading=0),  # blank line
        Paragraph(runs=[StyleRun(text="Section A")], heading=2),
        Paragraph(runs=[StyleRun(text=section_a_body)], heading=0),
        Paragraph(runs=[], heading=0),  # blank line
        Paragraph(runs=[StyleRun(text="Section B")], heading=2),
        Paragraph(runs=[StyleRun(text=section_b_body)], heading=0),
    ])


def test_three_way_merge_styled_handles_shifted_indices_without_crash_or_loss():
    """Regression: _diff3_merge's "both sides kept this paragraph, but
    check if either restyled it" branch used to index local_vals[bi]/
    remote_vals[bi] directly by the BASE position bi. That's only
    correct when local/remote happen to have the exact same paragraph
    count and no structural shift — otherwise it reads the wrong
    paragraph (or, if the other side is shorter, raises IndexError).
    A realistic note (two H2 sections separated by blank lines, one
    paragraph edited on each side) has no length mismatch itself, but
    exercises the exact "equal" block bookkeeping that the fix
    (keep_l/keep_r, keyed by the actual matched position) depends on.
    Must not raise, must not lose the blank lines or headings, and
    must not duplicate either edit."""
    base_doc = _make_two_section_doc("body A", "body B")
    server_doc = _make_two_section_doc("body A", "body B updated on web")
    local_doc = _make_two_section_doc("body A edited locally", "body B")

    merged, conflict = _three_way_merge_styled(base_doc, local_doc, server_doc)

    assert conflict is False
    assert len(merged.paragraphs) == len(base_doc.paragraphs) == 7
    assert [p.heading for p in merged.paragraphs] == [1, 0, 2, 0, 0, 2, 0]
    assert merged.plain_text.count("body A edited locally") == 1
    assert merged.plain_text.count("body B updated on web") == 1
    assert merged.paragraphs[1].text == "" and merged.paragraphs[4].text == "", (
        "blank separator paragraphs must survive the merge"
    )


def test_three_way_merge_styled_shifted_index_no_duplication_or_loss():
    """Precise repro for the indexing bug (unlike the test above, this
    one genuinely requires it): local INSERTS a whole new paragraph,
    shifting every base position after it by one. Remote separately
    restyles a paragraph further down (heading-only, no text change).
    Resolving that restyled paragraph's "did either side change this"
    check by directly indexing local_vals[bi]/remote_vals[bi] with the
    BASE position bi (instead of the position it actually matched to
    on each side) picks up the WRONG paragraph once local's insertion
    has shifted things — duplicating local's inserted paragraph and
    losing remote's restyled one entirely."""
    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Intro")], heading=0),
        Paragraph(runs=[StyleRun(text="A")], heading=0),
        Paragraph(runs=[StyleRun(text="B")], heading=2),
    ])
    local_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Intro")], heading=0),
        Paragraph(runs=[StyleRun(text="NEW inserted paragraph")], heading=0),
        Paragraph(runs=[StyleRun(text="A")], heading=0),
        Paragraph(runs=[StyleRun(text="B")], heading=2),
    ])
    remote_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Intro")], heading=0),
        Paragraph(runs=[StyleRun(text="A")], heading=0),
        Paragraph(runs=[StyleRun(text="B")], heading=1),  # heading-only change
    ])

    merged, conflict = _three_way_merge_styled(base_doc, local_doc, remote_doc)

    texts = [p.text for p in merged.paragraphs]
    assert texts.count("A") == 1, f"'A' duplicated or lost -- {texts}"
    assert texts.count("B") == 1, f"'B' duplicated or lost -- {texts}"
    assert texts.count("NEW inserted paragraph") == 1, f"local's insertion duplicated -- {texts}"
    b_para = merged.paragraphs[texts.index("B")]
    assert b_para.heading == 1, "remote's heading-only restyle on 'B' was lost"


def test_three_way_merge_styled_misaligned_block_lengths():
    """Regression: SequenceMatcher can group multiple consecutive
    paragraph edits into ONE 'replace' opcode on one side while the
    other side's corresponding edit only covers PART of that range
    (e.g. remote's SequenceMatcher block replaces paragraphs A+B
    together, while local's block only replaces A). The merge used to
    assume both sides' replace blocks start AND end at the same base
    position; when they don't, the base position that falls in the
    gap (here, B) got silently dropped from the output entirely --
    not just reverted, but missing. This is exactly the shape that
    happens when local pushes an edit to one paragraph while a
    SEPARATE, already-server-side edit (from an earlier push or a
    genuine web edit) touches an adjacent paragraph too."""
    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Paragraph A original")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph B original")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph C original")], heading=0),
    ])
    # Local only touched A; its widget never saw B's change.
    local_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Paragraph A edited locally")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph B original")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph C edited locally too")], heading=0),
    ])
    # Remote has BOTH A (same edit local already knows about, e.g.
    # from an earlier push) AND B (a change local hasn't seen) --
    # SequenceMatcher groups A+B into one replace block here.
    remote_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Paragraph A edited locally")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph B EDITED ON WEB")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph C original")], heading=0),
    ])

    merged, conflict = _three_way_merge_styled(base_doc, local_doc, remote_doc)

    assert conflict is False
    texts = [p.text for p in merged.paragraphs]
    assert texts == [
        "Paragraph A edited locally",
        "Paragraph B EDITED ON WEB",
        "Paragraph C edited locally too",
    ], f"got {texts}"


def test_three_way_merge_styled_local_insert_inside_remote_replace_block_not_dropped():
    """Regression: a paragraph LOCAL inserted between two OTHER
    paragraphs (no other local change) that remote's SequenceMatcher
    happened to group into a single 'replace' block (because remote
    concurrently edited both of them) used to vanish from the merge
    entirely. The block-processing loop resolved base positions
    [start, end) either position-by-position or via a whole-block
    side_output comparison, but only ever checked for insertions at
    the block's END boundary (bi=end) -- an insertion strictly INSIDE
    the block (ins_before[start+1..end-1]) was never consulted at
    all, silently dropping the user's newly-typed paragraph with no
    conflict warning."""
    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Paragraph A")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph B")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph C")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph D")], heading=0),
    ])
    # Local inserts a brand-new paragraph between B and C -- no other
    # local change at all.
    local_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Paragraph A")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph B")], heading=0),
        Paragraph(runs=[StyleRun(text="Newly typed paragraph")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph C")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph D")], heading=0),
    ])
    # Remote concurrently replaces B AND C (count-matching), which
    # SequenceMatcher groups into ONE replace block spanning both --
    # exactly straddling the position where local's insert landed.
    remote_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Paragraph A")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph B edited on web")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph C edited on web")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph D")], heading=0),
    ])

    merged, conflict = _three_way_merge_styled(base_doc, local_doc, remote_doc)

    texts = [p.text for p in merged.paragraphs]
    assert "Newly typed paragraph" in texts, (
        f"local's inserted paragraph was dropped from the merge -- got {texts}"
    )
    assert "Paragraph B edited on web" in texts
    assert "Paragraph C edited on web" in texts


class _StatefulFakeClient:
    """Minimal fake KeepClient that actually accumulates server-side
    state across calls (unlike a static MagicMock), for tests that
    need push_note's OWN pre-push full resync to see what a PRIOR
    push_note call in the same test actually wrote."""

    def __init__(self, note_id: str, doc: StyledDoc):
        self.note_id = note_id
        self.doc = doc
        self.revision = 1

    def _raw_node(self):
        return {
            "id": self.note_id, "type": "NOTE", "title": "T",
            "serverChanges": {"snapshot": {
                "revision": str(self.revision),
                "serializedChunks": [__import__("json").dumps(
                    encode_full_replace(self.doc, current_text_length=0)
                )],
            }},
            "indexableText": self.doc.plain_text,
        }

    def sync(self, full=False):
        pass

    def pop_stale_snapshot_ids(self):
        return set()

    def is_snapshot_stale(self, note_id):
        return False

    @property
    def notes(self):
        n = ServerNote.from_server(self._raw_node())
        n.sct_id = "sct.x"
        return {self.note_id: n}

    def update_note_metadata(self, server, new_title=None, new_color=None):
        pass

    def update_text_diff(self, server, new_doc, *, new_title=None, dry_run=False):
        from keep_protocol.nested_model import decode_chunks, encode_text_diff
        old_doc = decode_chunks(server.serialized_chunks or [])
        old_doc.sct_id = server.sct_id
        ops = encode_text_diff(old_doc, new_doc)
        if not ops:
            return {}
        # Good enough for these tests: we only assert on END content,
        # so full-replace-apply rather than actually walking each op.
        self.doc = new_doc
        self.revision += 1
        return {}


def test_push_note_stale_widget_after_merge_does_not_revert_remote_edit():
    """Full end-to-end regression for the reported scenario: web edits
    paragraph B concurrently; local edits paragraph A and pushes --
    push_note correctly 3-way merges, and the SERVER now has both
    edits. But the local WIDGET was never refreshed to show web's
    edit to B (that refresh is async and can be deferred while the
    user keeps typing -- see AppController._refresh_window_when_idle).
    Before that refresh lands, the user makes ANOTHER local edit, to a
    third, different paragraph C. This second push must NOT revert
    B's web edit back to its pre-edit state, even though the widget
    it's built from still doesn't know about it."""
    note_id = "note.stalewidget"
    original = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Paragraph A original")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph B original")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph C original")], heading=0),
    ])
    fake_client = _StatefulFakeClient(note_id, original)

    sync = KeepSyncV2()
    sync._authenticated = True
    sync._base_text[note_id] = original.plain_text
    sync._base_doc[note_id] = original
    sync._client = fake_client

    # Web edits paragraph B concurrently (simulated directly on the
    # fake server, as if a web push already landed).
    fake_client.doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Paragraph A original")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph B EDITED ON WEB")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph C original")], heading=0),
    ])
    fake_client.revision += 1

    # Push #1: local edits A. The widget's own view of B is still the
    # pre-web-edit original (it never saw the concurrent web change).
    local_after_edit_a = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Paragraph A edited locally")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph B original")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph C original")], heading=0),
    ])
    note1 = KeepNote(
        id=note_id, title="T", text=local_after_edit_a.plain_text,
        html=to_html(local_after_edit_a), color_hex="#FFF475",
    )
    assert sync.push_note(note1) is True
    assert "EDITED ON WEB" in fake_client.doc.plain_text
    assert "edited locally" in fake_client.doc.plain_text

    # Push #2: BEFORE any widget refresh, the user edits paragraph C.
    # The widget's view of B is STILL the stale, pre-web-edit version.
    local_after_edit_c = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Paragraph A edited locally")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph B original")], heading=0),
        Paragraph(runs=[StyleRun(text="Paragraph C edited locally too")], heading=0),
    ])
    note2 = KeepNote(
        id=note_id, title="T", text=local_after_edit_c.plain_text,
        html=to_html(local_after_edit_c), color_hex="#FFF475",
    )
    assert sync.push_note(note2) is True

    final_text = fake_client.doc.plain_text
    assert "EDITED ON WEB" in final_text, (
        "web's edit to paragraph B was reverted by the second local push"
    )
    assert "edited locally" in final_text
    assert "edited locally too" in final_text
