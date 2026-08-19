"""Tests for sync_merge.decide_merge.

These cover the conflict-resolution policy that protects user edits
from being clobbered by a remote pull. Every interleaving the user
might encounter ("typed locally then web changes arrived", "web edit
arrived while typing", "fetch returned empty body", etc.) gets a case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sync_merge import MergeAction, decide_merge
from keep_protocol.nested_model import StyledDoc, Paragraph, StyleRun


@dataclass
class FakeNote:
    """Minimal duck-typed KeepNote for the merge function."""
    text: str = ""
    title: str = ""
    color_hex: str = "#FFF475"
    html: str = ""
    is_list: bool = False
    list_items: list = None  # type: ignore[assignment]
    styled_doc: Optional[StyledDoc] = field(default=None, repr=False)

    def __post_init__(self):
        if self.list_items is None:
            self.list_items = []
        # KeepNote models "no styled_doc" as an ABSENT attribute
        # (hasattr/delattr), not a None-valued one -- mirror that so
        # decide_merge's getattr(local, "styled_doc", None) checks
        # behave the same way here as against the real object.
        if self.styled_doc is None:
            del self.styled_doc


# ------------------------------------------------------------- skip-dirty

def test_dirty_note_is_skipped_unconditionally():
    """If the local note has unpushed edits, remote must NEVER touch it.

    This is the primary user-protection: typing on desktop then a sync
    arrives → the remote pull MUST NOT overwrite the unpushed edit.
    """
    local = FakeNote(text="user just typed this")
    remote = FakeNote(text="server has totally different content")
    decision = decide_merge(local=local, remote=remote, is_dirty=True, user_busy=True)
    assert decision.action is MergeAction.SKIP_DIRTY


def test_dirty_skip_takes_priority_over_user_busy():
    local = FakeNote(text="A")
    remote = FakeNote(text="B")
    # Even when not focused, dirty wins.
    decision = decide_merge(local=local, remote=remote, is_dirty=True, user_busy=False)
    assert decision.action is MergeAction.SKIP_DIRTY


# ------------------------------------------------------------- preserve-local

def test_empty_remote_text_preserves_local_body():
    """The most insidious silent-data-loss path: a partial fetch returns
    empty `text` for a note that DOES have content on the server. The
    merge MUST detect this and keep our local cache."""
    local = FakeNote(text="100 chars of important content", title="My note")
    remote = FakeNote(text="", title="My note")
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.action is MergeAction.PRESERVE_LOCAL_BODY


def test_empty_remote_text_with_only_whitespace_local_is_adopted():
    """If local is just whitespace, remote-empty is fine to adopt — we
    shouldn't refuse a legitimate clear when the user really did empty
    the note."""
    local = FakeNote(text="   ")
    remote = FakeNote(text="")
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.action is MergeAction.ADOPT_REMOTE


def test_both_sides_empty_is_adopt_remote():
    local = FakeNote(text="")
    remote = FakeNote(text="")
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.action is MergeAction.ADOPT_REMOTE


# ------------------------------------------------------------- adopt-remote

def test_remote_text_change_adopted():
    local = FakeNote(text="old")
    remote = FakeNote(text="new")
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.action is MergeAction.ADOPT_REMOTE
    assert decision.text_changed
    assert decision.refresh_window  # visible change + user not busy


def test_user_busy_suppresses_window_refresh_but_still_adopts():
    """Cache must update so subsequent reads see the new server state,
    but the open window is left alone so we don't yank text out from
    under the user's cursor."""
    local = FakeNote(text="old")
    remote = FakeNote(text="new")
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=True)
    assert decision.action is MergeAction.ADOPT_REMOTE
    assert decision.refresh_window is False


def test_no_change_no_refresh():
    local = FakeNote(text="same", title="same", color_hex="#FFF475")
    remote = FakeNote(text="same", title="same", color_hex="#FFF475")
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.action is MergeAction.ADOPT_REMOTE
    assert decision.refresh_window is False


def test_color_change_alone_triggers_refresh():
    local = FakeNote(text="same", color_hex="#FFF475")
    remote = FakeNote(text="same", color_hex="#AECBFA")
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.color_changed
    assert decision.refresh_window


def test_title_change_alone_triggers_refresh():
    local = FakeNote(text="same", title="old")
    remote = FakeNote(text="same", title="new")
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.title_changed
    assert decision.refresh_window


def test_html_change_only_triggers_refresh():
    """Web added formatting (bold/italic) but plain text identical —
    the open window should refresh so the new formatting appears
    without requiring close/reopen."""
    local = FakeNote(text="hello", html="hello")
    remote = FakeNote(text="hello", html="<b>hello</b>")
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.html_changed
    assert decision.refresh_window


def test_list_items_change_triggers_refresh():
    local = FakeNote(is_list=True, list_items=[{"text": "a", "checked": False}])
    remote = FakeNote(
        is_list=True,
        list_items=[
            {"text": "a", "checked": True},
            {"text": "b", "checked": False},
        ],
    )
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.list_changed
    assert decision.refresh_window


# --------------------------------------------- html comparison (styled_doc)

def test_html_string_mismatch_alone_is_not_a_false_positive_restyle():
    """Regression: remote.html (our own to_html(), e.g. "<h1>x</h1>")
    and local.html (Qt's native toHtml() output captured from the live
    widget after any local edit, verbose with full CSS) are DIFFERENT
    byte strings for IDENTICAL formatted content. Comparing them
    directly false-positived html_changed on nearly every sync cycle
    once a note had ever been locally edited, forcing a pointless
    window refresh (and the scroll-position reset that comes with it)
    even though nothing had actually changed. With a structured
    styled_doc on BOTH sides showing the SAME content, the mismatched
    raw HTML strings must not trigger a refresh."""
    doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="Heading")], heading=1),
        Paragraph(runs=[StyleRun(text="bold text", bold=True)], heading=0),
    ])
    local = FakeNote(
        text=doc.plain_text,
        html='<html><body><h1 style="font-size:16pt;">Heading</h1>'
             '<p><span style="font-weight:700;">bold text</span></p></body></html>',
        styled_doc=doc,
    )
    remote = FakeNote(text=doc.plain_text, html="<h1>Heading</h1><p><b>bold text</b></p>", styled_doc=doc)
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.html_changed is False
    assert decision.refresh_window is False


def test_html_missing_local_styled_doc_after_own_edit_is_not_a_false_positive():
    """The common real case: local was just edited then successfully
    pushed (styled_doc is cleared on every local keystroke -- see
    AppController._on_note_changed -- and a normal push doesn't
    restore it), so on the very next fetch cycle local has NO
    styled_doc while remote (freshly fetched) always does. With no
    reliable structured comparison available and plain text matching,
    this must not be treated as a remote restyle -- the far more
    likely explanation is our own edit being echoed back."""
    doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="hello", bold=True)], heading=0),
    ])
    local = FakeNote(text=doc.plain_text, html="<html>...Qt native...</html>")  # no styled_doc
    remote = FakeNote(text=doc.plain_text, html="<p><b>hello</b></p>", styled_doc=doc)
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.html_changed is False
    assert decision.refresh_window is False


def test_genuine_remote_restyle_still_detected_via_styled_doc():
    """Sanity check for the fix above: when BOTH sides have a
    styled_doc and they genuinely differ, that's still a real remote
    restyle and must still trigger a refresh."""
    base_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="hello")], heading=0),
    ])
    restyled_doc = StyledDoc(sct_id="sct.x", paragraphs=[
        Paragraph(runs=[StyleRun(text="hello", bold=True)], heading=0),
    ])
    local = FakeNote(text=base_doc.plain_text, html="<p>hello</p>", styled_doc=base_doc)
    remote = FakeNote(text=restyled_doc.plain_text, html="<p><b>hello</b></p>", styled_doc=restyled_doc)
    decision = decide_merge(local=local, remote=remote, is_dirty=False, user_busy=False)
    assert decision.html_changed is True
    assert decision.refresh_window is True
