"""Surfacing a FORMATTING-ONLY web edit.

Bolding existing text moves no characters. It lives entirely in the
note's docs-nestedModel snapshot, so if an incremental delta bumps the
revision without re-echoing that snapshot, nothing in the response says
the note changed — and every ordinary check (text, title, colour, list
items) reports "identical". The note then sits at its old formatting
until something forces a full resync.

Two mechanisms cover that here:

  * revision tracking — `nested_revision` is a single counter the server
    bumps for ANY content change. If it moved while the content we
    decoded did not, our chunks must be stale relative to it, so a
    repair is scheduled; and
  * an adaptive full-resync cadence — frequent while note windows are
    on screen (a stale note is visibly wrong), infrequent when
    everything is closed (nothing to be wrong).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from keep_protocol.models import Note as ServerNote
from keep_protocol.nested_model import (
    Paragraph,
    StyledDoc,
    StyleRun,
    encode_doc,
)
from keep_sync_v2 import KeepSyncV2, _content_moved

SCT = "sct.a"


def R(text, **style):
    return StyleRun(text=text, **style)


PLAIN = StyledDoc(sct_id=SCT, paragraphs=[Paragraph(runs=[R("hello world")])])
BOLDED = StyledDoc(sct_id=SCT, paragraphs=[Paragraph(runs=[
    R("hello", bold=True), R(" world")])])


def _server_note(doc: StyledDoc, revision: str) -> ServerNote:
    return ServerNote(
        id="n1", server_id="n1", type="NOTE", title="T", text=doc.plain_text,
        color="DEFAULT", is_archived=False, is_pinned=False, is_trashed=False,
        is_deleted=False, created=None, updated=None, user_edited=None,
        sort_value=0, base_version="0", sct_id=SCT,
        serialized_chunks=[json.dumps(encode_doc(doc))],
        nested_revision=revision, indexable_text=doc.plain_text,
        raw={"id": "n1", "type": "NOTE", "title": "T"})


@pytest.fixture
def sync():
    s = KeepSyncV2()
    s._authenticated = True
    s._client = MagicMock()
    s._client.notes = {}
    s._client.pop_stale_snapshot_ids.return_value = set()
    s._first_fetch_done = True
    return s


# ------------------------------------------------------------------
# Revision tracking
# ------------------------------------------------------------------

def test_revision_bump_without_content_change_schedules_a_repair(sync):
    """The reported failure: bold something on the web, the revision
    moves, but the delta hands back the same old snapshot."""
    sync._client.list_notes.return_value = [_server_note(PLAIN, "10")]
    sync.fetch_notes()
    assert sync._seen_revision["n1"] == "10"

    # Revision moved; chunks did not (still the un-bolded snapshot).
    sync._client.list_notes.return_value = [_server_note(PLAIN, "11")]
    sync.fetch_notes()
    assert "n1" in sync._force_full_resync_for, (
        "a revision bump with unchanged content means our chunks are "
        "stale and must be repaired"
    )


def test_revision_bump_with_real_content_change_is_not_a_repair(sync):
    """When the delta DOES carry the new formatting there is nothing to
    repair — scheduling a resync every time would be pure waste."""
    sync._client.list_notes.return_value = [_server_note(PLAIN, "10")]
    sync.fetch_notes()
    sync._client.list_notes.return_value = [_server_note(BOLDED, "11")]
    out = sync.fetch_notes()
    assert "n1" not in sync._force_full_resync_for
    doc = getattr(out[0], "styled_doc", None)
    assert doc is not None
    assert [r.bold for p in doc.paragraphs for r in p.runs] == [True, False]


def test_unchanged_revision_schedules_nothing(sync):
    sync._client.list_notes.return_value = [_server_note(PLAIN, "10")]
    sync.fetch_notes()
    sync.fetch_notes()
    assert not sync._force_full_resync_for


def test_first_sighting_of_a_note_schedules_nothing(sync):
    """No previous revision to compare against."""
    sync._client.list_notes.return_value = [_server_note(PLAIN, "10")]
    sync.fetch_notes()
    assert not sync._force_full_resync_for


# ------------------------------------------------------------------
# _content_moved
# ------------------------------------------------------------------

class _KN:
    def __init__(self, **kw):
        self.id = "n1"
        self.text = "hello world"
        self.title = "T"
        self.is_list = False
        self.list_items = []
        self.__dict__.update(kw)


def test_content_moved_sees_a_formatting_only_difference():
    a = _KN(styled_doc=PLAIN)
    b = _KN(styled_doc=BOLDED)
    assert _content_moved(a, b) is True


def test_content_moved_false_for_identical_notes():
    assert _content_moved(_KN(styled_doc=PLAIN), _KN(styled_doc=PLAIN)) is False


def test_content_moved_sees_text_and_title_changes():
    assert _content_moved(_KN(styled_doc=PLAIN),
                          _KN(styled_doc=PLAIN, text="other")) is True
    assert _content_moved(_KN(styled_doc=PLAIN),
                          _KN(styled_doc=PLAIN, title="other")) is True


def test_content_moved_sees_list_changes():
    assert _content_moved(
        _KN(styled_doc=PLAIN),
        _KN(styled_doc=PLAIN, is_list=True, list_items=[{"text": "x"}]),
    ) is True


def test_content_moved_is_conservative_without_docs():
    """Missing structured docs on one side can't prove equality."""
    assert _content_moved(_KN(), _KN(styled_doc=PLAIN)) is True
    assert _content_moved(None, _KN(styled_doc=PLAIN)) is True


# ------------------------------------------------------------------
# Adaptive full-resync cadence
# ------------------------------------------------------------------

def _fake_controller(visible: bool):
    from app_controller import (
        AppController, _FULL_RESYNC_TICKS_ACTIVE, _FULL_RESYNC_TICKS_IDLE,
    )

    class _Win:
        def isVisible(self):
            return visible

    class _Fake:
        def __init__(self):
            self.sync = MagicMock(is_authenticated=True)
            self.windows = {"n1": _Win()}
            self._tick_count = 0
            self.forced = []

        def _full_sync(self, force_resync=False):
            self.forced.append(force_resync)

    return (_Fake(), AppController._periodic_sync,
            _FULL_RESYNC_TICKS_ACTIVE, _FULL_RESYNC_TICKS_IDLE)


def test_full_resync_runs_often_while_a_note_is_on_screen():
    fake, tick, active, _idle = _fake_controller(visible=True)
    for _ in range(active):
        tick(fake)
    assert fake.forced.count(True) == 1
    assert fake.forced[-1] is True
    assert fake.forced[0] is False


def test_full_resync_backs_off_when_every_note_is_closed():
    fake, tick, active, idle = _fake_controller(visible=False)
    for _ in range(active):
        tick(fake)
    assert True not in fake.forced, (
        "with nothing on screen there is nothing visibly stale to fix"
    )
    for _ in range(idle - active):
        tick(fake)
    assert fake.forced.count(True) == 1


def test_periodic_sync_does_nothing_when_signed_out():
    fake, tick, _a, _i = _fake_controller(visible=True)
    fake.sync.is_authenticated = False
    tick(fake)
    assert fake.forced == []
