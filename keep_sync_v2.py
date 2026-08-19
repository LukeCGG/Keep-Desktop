"""Google Keep sync v2 — uses the keep_protocol package directly to talk
to Keep's HTTP API, including full support for docs-nestedModel formatting
state.

Drop-in replacement for keep_sync.KeepSync: same method surface so
app_controller / note_window can use either implementation behind a
feature flag.

Key differences from v1:
- Auth via keep_protocol.auth (gpsoauth + keyring). The same master token
  v1 stores in DPAPI is reused — we just hand it to keep_protocol's
  Credentials object. New logins write to keyring AND save_token() so
  v1 still works if the user flips the flag back.
- fetch_notes() decodes serializedChunks → StyledDoc → HTML. Notes get
  KeepNote.nested_doc and KeepNote.html populated; KeepNote.text is the
  plain-text projection.
- push_note() reads back HTML from the editor (via KeepNote.html), parses
  it into a StyledDoc with QTextDocument, and emits ds+is+as ops via
  keep_protocol.client.update_note_doc.
- Lists fall back to legacy plain-text path for now (list-item modelling
  in keep_protocol is still pending).
"""

from __future__ import annotations

import collections
import difflib
import logging
import threading
from typing import Optional
from uuid import getnode as get_mac

import gpsoauth

from config import KEEP_COLORS, load_token, save_token
from keep_sync import KeepNote   # reuse the dataclass
from keep_protocol.auth import (
    Credentials, AuthError, _store_credentials, load_credentials, _get_active_email,
)
from keep_protocol.client import KeepClient, KeepError
from keep_protocol.models import Note as ServerNote
from keep_protocol.nested_model import (
    CheckboxItem, StyledDoc, Paragraph, StyleRun, decode_chunks, to_html,
    _styles_equal,
)

log = logging.getLogger(__name__)

# How often fetch_notes() promotes an incremental pull to a full
# resync purely as a safety net. At the 30s sync interval this is
# roughly every 10 minutes.
_FULL_RESYNC_EVERY_N_FETCHES = 20


# Keep's wire color names (UPPERCASE) <-> our CamelCase config names.
_KEEP_COLOR_TO_NAME = {
    "DEFAULT": "White",
    "WHITE": "White",
    "RED": "Red",
    "ORANGE": "Orange",
    "YELLOW": "Yellow",
    "GREEN": "Green",
    "TEAL": "Teal",
    "BLUE": "Blue",
    "CERULEAN": "DarkBlue",
    "PURPLE": "Purple",
    "PINK": "Pink",
    "BROWN": "Brown",
    "GRAY": "Gray",
}
_NAME_TO_KEEP_COLOR = {
    "White": "DEFAULT",
    "Red": "RED",
    "Orange": "ORANGE",
    "Yellow": "YELLOW",
    "Green": "GREEN",
    "Teal": "TEAL",
    "Blue": "BLUE",
    "DarkBlue": "CERULEAN",
    "Purple": "PURPLE",
    "Pink": "PINK",
    "Brown": "BROWN",
    "Gray": "GRAY",
}
_HEX_TO_NAME = {v: k for k, v in KEEP_COLORS.items()}


def _wire_color_to_hex(wire: str) -> str:
    name = _KEEP_COLOR_TO_NAME.get((wire or "").upper(), "White")
    return KEEP_COLORS.get(name, KEEP_COLORS["White"])


def _hex_to_wire_color(hex_val: str) -> str:
    name = _HEX_TO_NAME.get(hex_val, "White")
    return _NAME_TO_KEEP_COLOR.get(name, "DEFAULT")


def _content_moved(prev, cur) -> bool:
    """Did the note's user-visible content actually change between two
    fetches? Compares plain text AND structured formatting, because a
    bold/italic/heading toggle moves no text at all."""
    if prev is None or cur is None:
        return True
    if (prev.text or "") != (cur.text or ""):
        return True
    if (prev.title or "") != (cur.title or ""):
        return True
    if bool(prev.is_list) != bool(cur.is_list) or prev.list_items != cur.list_items:
        return True
    prev_doc = getattr(prev, "styled_doc", None)
    cur_doc = getattr(cur, "styled_doc", None)
    if prev_doc is None or cur_doc is None:
        return prev_doc is not cur_doc
    return not _styles_equal(prev_doc, cur_doc)


class KeepSyncV2:
    """Same surface as KeepSync, backed by keep_protocol."""

    def __init__(self):
        self._client: Optional[KeepClient] = None
        self._email: Optional[str] = None
        self._authenticated = False
        self._lock = threading.Lock()
        # Mirror cache so push_note() can look up the corresponding
        # ServerNote without re-syncing.
        self._server_notes: dict[str, ServerNote] = {}
        # Snapshot of the *plain text* of each note as we last saw it on
        # the server. Used as the common ancestor for 3-way merges when
        # both desktop AND web have edited a note since the last sync.
        self._fetch_count = 0
        # note id -> the nested_revision whose CONTENT we last
        # decoded and handed to the controller.
        self._seen_revision: dict[str, str] = {}
        # Last batch handed to the controller, for the
        # revision-vs-content comparison above.
        self._last_returned: list = []
        self._base_text: dict[str, str] = {}
        # Companion snapshot of the *styled* doc (headings/bold/etc) at
        # the same sync point. server_text == base_text alone can't
        # tell "the web re-styled this note" apart from "nothing
        # changed remotely at all" — both leave plain text untouched.
        # Comparing server_doc against THIS baseline is what lets
        # push_note tell those two cases apart, instead of assuming a
        # concurrent web re-style on every local-only edit and silently
        # discarding the user's own formatting changes to "preserve"
        # remote's (unchanged) styling.
        self._base_doc: dict[str, Optional[StyledDoc]] = {}
        # Notes whose cached snapshot decoded to empty despite having
        # indexable_text. We force a full resync next pull to repair.
        self._force_full_resync_for: set[str] = set()
        # First fetch_notes() call after construction always does a
        # full resync. Incremental deltas don't reliably echo trash /
        # archive state changes made on the web side, so we'd carry
        # stale notes forward across restarts.
        self._first_fetch_done: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    # ----- auth ---------------------------------------------------------

    def login(self, email: str, master_token: Optional[str] = None,
              password: Optional[str] = None) -> bool:
        """Authenticate. Same signature as v1.

        - master_token: if given, store it (in keyring AND v1's DPAPI
          token file) and use it.
        - password: NOT supported in v2 (Google killed password auth).
        - neither: try loading from keyring, then v1's stored token.
        """
        if password and not master_token:
            log.error("v2 sync does not support password login")
            return False

        if not master_token:
            master_token = load_token()
            if not master_token:
                # Try keyring (in case the user previously logged in via
                # keep_protocol's own CLI).
                try:
                    creds = load_credentials(email)
                    master_token = creds.master_token
                except AuthError:
                    return False

        if not master_token:
            return False

        # Store in both stores so either v1 or v2 can pick it up later.
        android_id = f"{get_mac():x}"
        try:
            _store_credentials(email, master_token, android_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not write keyring creds: %s", exc)
        save_token(master_token)

        # Build a Credentials and verify by minting a bearer.
        creds = Credentials(email=email, master_token=master_token,
                            android_id=android_id)
        try:
            creds.mint_bearer()
        except AuthError as exc:
            log.error("v2 bearer mint failed: %s", exc)
            self._authenticated = False
            return False

        self._client = KeepClient(creds)
        self._email = email
        self._authenticated = True
        log.info("v2 authenticated as %s", email)
        return True

    @staticmethod
    def exchange_oauth_for_master(email: str, oauth_token: str) -> Optional[dict]:
        """Same shape as v1 — exchange an EmbeddedSetup oauth_token for a
        master token via gpsoauth. Returns the gpsoauth response dict,
        or None on failure."""
        android_id = f"{get_mac():x}"
        try:
            resp = gpsoauth.exchange_token(email, oauth_token, android_id)
            if not resp.get("Token"):
                log.error("Token exchange returned no Token: %s", resp)
                return None
            return resp
        except Exception as exc:  # noqa: BLE001
            log.error("Token exchange failed: %s", exc)
            return None

    # ----- read ---------------------------------------------------------

    def fetch_notes(
        self,
        force_resync: bool = False,
        hold_baseline_for: Optional[set] = None,
    ) -> list[KeepNote]:
        """Fetch the current server state for every note.

        `hold_baseline_for` — note ids the caller knows it will NOT
        apply this fetch's content to right away (e.g. locally dirty,
        or the user is actively typing in that note's window). For
        those notes, the returned KeepNote still carries the fresh
        server content, but `_base_text`/`_base_doc` are left alone.
        Those two dicts are push_note's record of "what the local
        editor is known to match" — advancing them to the server's
        latest here, before the editor has actually caught up, makes
        a genuine concurrent web restyle indistinguishable from "no
        remote change", so the next local push would silently
        overwrite it right back off the server.
        """
        if not self._authenticated or not self._client:
            return []
        hold_baseline_for = hold_baseline_for or set()
        with self._lock:
            # Promote to full sync if we previously detected stale
            # snapshots that need repairing. Also always full-sync on
            # the first call after launch so trash/archive changes made
            # on the web while we were closed get reflected.
            # Self-heal: fold in a full resync every so often even when
            # nothing asked for one. Incremental deltas are only ever as
            # good as the cursor they run from, and a cursor that gets
            # ahead of content we never absorbed leaves a note frozen
            # forever — every poll and every manual "Sync now" is
            # incremental, so nothing short of a restart recovers it.
            # The specific way that used to happen is fixed (write
            # responses no longer advance the cursor, and now merge
            # deltas as carefully as sync() does), but "the pull silently
            # stops working until you restart" is a bad enough failure
            # that it deserves a backstop rather than only a fix.
            self._fetch_count += 1
            periodic_full = (self._fetch_count % _FULL_RESYNC_EVERY_N_FETCHES) == 0
            do_full = (
                force_resync
                or not self._first_fetch_done
                or periodic_full
                or bool(self._force_full_resync_for)
            )
            if periodic_full:
                log.debug("v2 fetch: periodic full resync (every %d fetches)",
                          _FULL_RESYNC_EVERY_N_FETCHES)
            if do_full and self._force_full_resync_for:
                log.info(
                    "v2 forcing full resync to repair stale notes: %s",
                    [nid[:8] for nid in self._force_full_resync_for],
                )
                self._force_full_resync_for.clear()
            try:
                self._client.sync(full=do_full)
            except KeepError as exc:
                log.error("v2 sync failed: %s", exc)
                return []
            self._first_fetch_done = True
            # A note can come back from an incremental delta with its
            # revision bumped but serializedChunks NOT re-echoed (Keep
            # does this on compact deltas — including, apparently, a
            # heading/paragraph-style-only edit with no text change).
            # Our cached chunks are stale relative to that revision, so
            # a heading-only web edit would otherwise silently never be
            # detected on periodic polls. Schedule a full resync so the
            # next cycle picks up the real content.
            stale = self._client.pop_stale_snapshot_ids()
            if stale:
                log.info(
                    "v2 fetch: %d note(s) had a metadata-only delta with "
                    "stale chunks; scheduling full resync: %s",
                    len(stale), [nid[:8] for nid in stale],
                )
                self._force_full_resync_for.update(stale)
                # These notes are skipped entirely below (see the
                # per-note loop) rather than returned with stale
                # content -- so no baseline advancement or controller
                # adoption happens for them until the full resync
                # above actually lands. Concretely, this fires on the
                # routine "push then immediately pull" sequence every
                # periodic sync cycle does: a push that 3-way-merged a
                # concurrent web edit leaves the merged content on the
                # widget but deliberately does NOT advance the
                # baseline itself (see push_note's
                # baseline_reflects_widget) -- it counts on THIS fetch
                # to do that once confirmed. If this fetch instead
                # handed the controller stale pre-merge content as
                # current, it would silently revert the widget right
                # back to that pre-merge state (or strip its
                # formatting entirely, if the decode is skipped) --
                # which then looks, from every subsequent cycle
                # onward, like syncing that note "just stopped".
            out: list[KeepNote] = []
            prev_by_id = {n.id: n for n in self._last_returned}
            for note in self._client.list_notes():
                self._server_notes[note.id] = note
                if note.is_deleted or note.is_trashed:
                    continue
                if note.id in stale:
                    # The server just told us this note's cached snapshot
                    # predates its own revision bump (see the stale-
                    # handling comment above) -- that applies to
                    # get_checkboxes() below too, not just decode_chunks,
                    # since both read note.serialized_chunks. Returning a
                    # degraded fallback entry here (no styled_doc, empty
                    # html, possibly-stale checkbox state) used to get
                    # ADOPTED by the controller whenever the note wasn't
                    # currently dirty -- silently stripping all formatting
                    # from the open window (bold/italic/headings all reset
                    # to plain body text) even though nothing was actually
                    # lost server-side, just temporarily not re-echoed.
                    #
                    # But `note.indexable_text` IS kept fresh independently
                    # of serializedChunks staleness (see the caller of
                    # pop_stale_snapshot_ids for why), and if the server
                    # keeps echoing compact deltas for this note across
                    # SEVERAL consecutive full-resync attempts -- observed
                    # live, not just theoretical -- unconditionally
                    # skipping every single cycle means a genuine plain-
                    # text web edit is never shown AT ALL, not just
                    # delayed: "periodic sync ran multiple times and the
                    # change was not detected". When we have a known-good
                    # cached styled_doc baseline to diff against, apply
                    # the base->fresh-text edit onto it via the same
                    # format-preserving merge push_note itself uses for
                    # "remote restyled, local text-only edit" -- showing
                    # the new text immediately, with formatting mostly
                    # intact (new/changed characters inherit neighbouring
                    # style), while still leaving the baseline itself
                    # unadvanced so the eventual full resync can correct
                    # anything this approximation got wrong.
                    approx_kn: Optional[KeepNote] = None
                    if note.type != "LIST":
                        stale_base_doc = self._base_doc.get(note.id)
                        stale_base_text = self._base_text.get(note.id)
                        if (
                            stale_base_doc is not None
                            and stale_base_text is not None
                            and stale_base_doc.plain_text == stale_base_text
                        ):
                            fresh_text = note.indexable_text or note.text
                            if fresh_text and fresh_text != stale_base_text:
                                approx_doc = _apply_text_edits_preserve_format(
                                    stale_base_doc, stale_base_text, fresh_text,
                                )
                                approx_kn = KeepNote(
                                    id=note.id,
                                    title=note.title,
                                    text=approx_doc.plain_text,
                                    html=to_html(approx_doc),
                                    color_hex=_wire_color_to_hex(note.color),
                                    pinned=note.is_pinned,
                                    trashed=note.is_trashed,
                                    sort_key=int(note.sort_value or 0),
                                )
                                approx_kn.styled_doc = approx_doc  # type: ignore[attr-defined]
                    if approx_kn is not None:
                        log.info(
                            "v2 fetch: %s snapshot stale but indexableText "
                            "changed; showing a format-preserving "
                            "approximation while the full resync catches up",
                            note.id[:8],
                        )
                        out.append(approx_kn)
                    # Baseline stays unadvanced either way (approximation
                    # or full skip) -- the full resync above is what
                    # confirms the real, authoritative content.
                    continue
                color_hex = _wire_color_to_hex(note.color)
                # Use Keep's actual sortValue so the manager order matches
                # what you see on keep.google.com. Higher sortValue = higher
                # in Keep — the manager sorts descending on this.
                sort_idx = int(note.sort_value or 0)
                # Declared here (not just inside the NOTE branch below) so
                # the baseline-advance check further down can tell "this
                # note's decode transiently failed, don't trust it" apart
                # from "this note genuinely has no formatting" for BOTH
                # note types -- LIST notes have no styled_doc concept at
                # all, so they always take the "genuinely no formatting"
                # path, which is correct (harmless: push_note's list path
                # never reads _base_doc).
                decode_failed = False
                if note.type == "LIST":
                    # Two sources of items:
                    #   1. legacy LIST_ITEM child nodes (gkeepapi-era, still
                    #      populated for older lists)
                    #   2. cbx blocks inside the LIST's docs-nestedModel
                    #      snapshot (new Keep web behaviour)
                    # Prefer cbx blocks when present — they're authoritative
                    # for anything Keep web has touched recently.
                    items: list[dict] = []
                    if note.sct_id and note.serialized_chunks:
                        try:
                            cbx_items = self._client.get_checkboxes(note)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("cbx decode failed for %s: %s", note.id, exc)
                            cbx_items = []
                        for cb in cbx_items:
                            items.append({
                                "text": cb.text,
                                "checked": cb.checked,
                                "cbx_id": cb.cbx_id,
                                "indent": cb.indent,
                            })
                    if not items:
                        for li in note.list_items:
                            items.append({
                                "text": li.text,
                                "checked": li.checked,
                                # Legacy LIST_ITEM nodes don't have cbx ids.
                                "cbx_id": "",
                            })
                    lines = []
                    for it in items:
                        mark = "☑" if it["checked"] else "☐"
                        lines.append(f"{mark} {it['text']}")
                    out.append(KeepNote(
                        id=note.id,
                        title=note.title,
                        text="\n".join(lines),
                        color_hex=color_hex,
                        pinned=note.is_pinned,
                        trashed=note.is_trashed,
                        sort_key=sort_idx,
                        is_list=True,
                        list_items=items,
                    ))
                else:
                    # note.id in stale already handled above (the whole
                    # note is skipped this cycle), so serialized_chunks
                    # here are always current relative to note.sct_id.
                    doc = decode_chunks(note.serialized_chunks) if note.sct_id else None
                    if doc and doc.paragraphs:
                        plain = doc.plain_text
                        html = to_html(doc)
                        # Sanity check: if the decoded plain text is empty
                        # but the server's indexable_text isn't, the decode
                        # produced a degenerate doc (e.g. all-zero-length
                        # paragraphs after a merge artefact). Fall back to
                        # indexable_text and drop html so we don't wipe the
                        # user's content.
                        if not plain.strip() and (note.indexable_text or "").strip():
                            # Distinguish two failure modes:
                            #  (a) chunks were present but didn't decode —
                            #      genuinely broken snapshot, force resync.
                            #  (b) chunks empty (server only sent
                            #      previewData / indexableText) — normal,
                            #      no resync needed, no scary warning.
                            had_chunks = bool(note.serialized_chunks)
                            if had_chunks:
                                log.warning(
                                    "v2 fetch: decode produced empty doc for %s but "
                                    "indexableText=%r; falling back to plain text "
                                    "(chunks=%d, sct_id=%s, rev=%s)",
                                    note.id[:8], note.indexable_text[:80],
                                    len(note.serialized_chunks or []),
                                    note.sct_id, note.nested_revision,
                                )
                            else:
                                log.debug(
                                    "v2 fetch: %s has sct_id but no snapshot "
                                    "chunks; using indexableText",
                                    note.id[:8],
                                )
                            plain = note.indexable_text or note.text
                            html = ""
                            decode_failed = True
                            if had_chunks:
                                # Schedule a full resync next pull so we get a
                                # fresh snapshot from the server instead of
                                # repeatedly trying (and failing) to push based
                                # on broken cached chunks.
                                self._force_full_resync_for.add(note.id)
                    else:
                        plain = note.indexable_text or note.text
                        html = ""
                    kn = KeepNote(
                        id=note.id,
                        title=note.title,
                        text=plain,
                        html=html,
                        color_hex=color_hex,
                        pinned=note.is_pinned,
                        trashed=note.is_trashed,
                        sort_key=sort_idx,
                    )
                    # Stash the decoded StyledDoc as a non-dataclass
                    # attribute so note_window can render via the cursor
                    # API (faithful empty-line preservation, no HTML round-
                    # trip artefacts). Falls back to .html if absent.
                    if doc and doc.paragraphs and not decode_failed:
                        kn.styled_doc = doc  # type: ignore[attr-defined]
                    out.append(kn)
                    # If the cached snapshot was stale (decoder failed but
                    # indexable_text had content), drop the chunks so the
                    # next write doesn't try to ds-rewrite based on bogus
                    # state. The next server sync will repopulate them.
                    if decode_failed:
                        note.serialized_chunks = []
                # Revision-based change detection. nested_revision is a
                # single counter the server bumps for ANY content change
                # — including a formatting-only one, which by definition
                # moves no text and so is invisible to every other check
                # we have. If it moved but the content we just decoded is
                # byte-identical to what we last handed out, our chunks
                # are stale relative to that revision: the delta bumped
                # the counter without re-echoing the snapshot. Schedule
                # the repair rather than reporting "nothing changed"
                # forever.
                prev_out = prev_by_id.get(note.id)
                cur_rev = note.nested_revision
                prev_rev = self._seen_revision.get(note.id)
                if cur_rev:
                    if (prev_rev is not None and cur_rev != prev_rev
                            and not _content_moved(prev_out, out[-1])):
                        log.info(
                            "v2 fetch: %s revision %s->%s but decoded "
                            "content is unchanged; chunks are stale, "
                            "scheduling resync",
                            note.id[:8], prev_rev, cur_rev,
                        )
                        self._force_full_resync_for.add(note.id)
                    else:
                        self._seen_revision[note.id] = cur_rev

                if note.id in hold_baseline_for:
                    continue
                # Remember the server-side plain text as the merge base for
                # the next push. (Set for both NOTE and LIST so list edits
                # have a base too once we add list-write support.)
                self._base_text[note.id] = out[-1].text
                # Companion styled-doc baseline (NOTE-type only; getattr
                # safely yields None for LIST notes, which have no
                # styled_doc concept). Gated on decode_failed specifically
                # -- NOT on "new_styled_doc is None" -- because None is
                # ambiguous between two very different situations: (a) a
                # TRANSIENT decode failure (the degenerate-snapshot case
                # above; e.g. a compact incremental delta that didn't
                # fully echo chunks), where the OLD baseline is still the
                # best guess and must survive, vs (b) the note GENUINELY
                # has no formatting anymore (server-side content was
                # cleared, or a LIST note), where None IS the correct new
                # baseline and failing to advance to it leaves a stale,
                # wrong (non-None) baseline describing formatting that no
                # longer exists on either side. push_note's 3-way merge
                # trusts base_doc as the common ancestor for paragraph-
                # level merging -- a wrong ancestor there can resurrect
                # formatting the user legitimately removed, or falsely
                # flag a conflict. Only decode_failed=True (or no baseline
                # existing yet, in which case there's nothing to protect)
                # skips advancing.
                new_styled_doc = getattr(out[-1], "styled_doc", None)
                if not decode_failed or note.id not in self._base_doc:
                    self._base_doc[note.id] = new_styled_doc
            log.info("v2 fetched %d notes", len(out))
            self._last_returned = list(out)
            return out

    # ----- write --------------------------------------------------------

    def push_metadata(
        self, keep_note: KeepNote,
        *, is_pinned: Optional[bool] = None,
        sort_value: Optional[int] = None,
        new_color: Optional[str] = None,
    ) -> bool:
        """Push metadata-only changes (pin/sort/title/colour) to Keep
        without rewriting the note body.

        Works for notes that have no sct_id (i.e. ones that have never
        been touched by Keep web's docs-nestedModel). When ``new_color``
        is left as None we infer it from ``keep_note.color_hex`` so
        callers don't have to translate hex→wire-name themselves.

        Works for notes that have no sct_id (i.e. ones that have never
        been touched by Keep web's docs-nestedModel).
        """
        if not self._authenticated or not self._client:
            return False
        with self._lock:
            server = self._server_notes.get(keep_note.id)
            if server is None:
                # We may not have synced yet — try once.
                try:
                    self._client.sync()
                    for n in self._client.notes.values():
                        self._server_notes[n.id] = n
                    server = self._server_notes.get(keep_note.id)
                except KeepError as exc:
                    log.warning("metadata push: pre-sync failed: %s", exc)
                if server is None:
                    log.error(
                        "metadata push: note %s not found on server",
                        keep_note.id,
                    )
                    return False
            # Translate hex→wire-name if caller didn't pass one
            # explicitly. Don't push a colour change unless local
            # actually differs from server, to avoid spurious writes.
            if new_color is None and keep_note.color_hex:
                wire = _hex_to_wire_color(keep_note.color_hex)
                if wire and wire != server.color:
                    new_color = wire
            try:
                self._client.update_note_metadata(
                    server,
                    is_pinned=is_pinned,
                    sort_value=sort_value,
                    new_title=keep_note.title,
                    new_color=new_color,
                )
            except KeepError as exc:
                log.error(
                    "metadata push failed for %s: %s",
                    keep_note.id, exc,
                )
                return False
            log.info(
                "metadata push ok for %s (pinned=%s sort=%s color=%s)",
                keep_note.id[:8], is_pinned, sort_value, new_color,
            )
            return True

    def push_note(self, keep_note: KeepNote) -> bool:
        if not self._authenticated or not self._client:
            return False
        # Defensive: an empty/whitespace id can never round-trip. This
        # usually means the caller forgot to swap the temp id for the
        # server-assigned one after _push_new_note finished.
        if not keep_note.id or not keep_note.id.strip():
            log.error("push_note: refusing to push note with empty id")
            return False
        with self._lock:
            # ALWAYS refetch right before push. Three reasons:
            #   1. Get the latest nested_revision so update_note_doc
            #      sends a clientRevision the server will accept.
            #   2. Detect whether web edited the note since we last
            #      pulled, so we can 3-way-merge instead of clobbering.
            #   3. full=True, not incremental: Keep's incremental
            #      /changes delta can be "compact" — it bumps the
            #      revision without re-echoing serializedChunks (seen
            #      live, see keep_protocol/client.py's
            #      _stale_snapshot_ids handling). That's fine for the
            #      periodic pull (it just tries again next cycle), but
            #      here it's fatal: a rapid push-then-push (e.g. change
            #      a heading, then keep typing a few seconds later) would
            #      resync onto a snapshot that predates the FIRST push's
            #      own recent write, and the format-preserving /3-way
            #      merge below would faithfully carry that stale
            #      (pre-change) formatting forward — silently reverting
            #      the change that was just pushed. A full resync always
            #      returns complete chunks, so this can't happen.
            try:
                self._client.sync(full=True)
            except KeepError as exc:
                log.warning("pre-push resync failed: %s (proceeding anyway)", exc)
            for n in self._client.notes.values():
                self._server_notes[n.id] = n
            # A full resync usually returns complete chunks for every
            # note, but "usually" isn't "always" -- if THIS note was
            # itself written moments ago (e.g. a retry after a prior
            # push's response was lost to a timeout, even though the
            # server had already applied it), the server's snapshot
            # listing can still echo a compact/metadata-only delta for
            # it before that write has fully propagated. Proceeding
            # with what's actually a stale cached decode below would
            # make this push think its own just-applied insert is
            # still missing and re-send it -- duplicating whatever was
            # just typed. One extra resync gives that propagation a
            # moment to catch up; if it's STILL stale after that,
            # proceed anyway (logged) rather than spin indefinitely.
            # Non-draining check (is_snapshot_stale), NOT
            # pop_stale_snapshot_ids: this push only cares about ONE
            # note. Draining the whole shared set here for a single-
            # note check would discard any OTHER note's staleness
            # flag this same sync(full=True) call just discovered —
            # fetch_notes()'s own pop_stale_snapshot_ids() sweep,
            # which runs later in the same periodic-sync cycle, would
            # then find that flag already gone and never schedule the
            # protective repair/omission for that unrelated note,
            # letting IT get silently handed to the controller with
            # stale (or formatting-stripped) content.
            if self._client.is_snapshot_stale(keep_note.id):
                log.info(
                    "v2 push: %s snapshot still stale right after a full "
                    "resync; resyncing once more before pushing",
                    keep_note.id[:8],
                )
                try:
                    self._client.sync(full=True)
                    for n in self._client.notes.values():
                        self._server_notes[n.id] = n
                except KeepError as exc:
                    log.warning("second pre-push resync failed: %s", exc)
                if self._client.is_snapshot_stale(keep_note.id):
                    log.warning(
                        "v2 push: %s snapshot still stale after retry; "
                        "proceeding anyway", keep_note.id[:8],
                    )

            server = self._server_notes.get(keep_note.id)
            if server is None:
                # The note exists locally but Keep doesn't know about it.
                # Two common causes:
                #   1. It was created locally and never successfully
                #      pushed (id is still our local UUID).
                #   2. It was deleted on web while we were offline.
                # Both need user-visible recovery, not a silent retry,
                # so flag it and bail. The controller's _full_sync
                # already removes such notes from the local list when
                # the user isn't actively editing them.
                log.warning(
                    "push_note: %s not on server (deleted remotely or "
                    "never synced); marking for full resync",
                    keep_note.id[:12],
                )
                self._force_full_resync_for.add(keep_note.id)
                return False

            # If the server says the note is trashed/deleted but we
            # have a local edit pending, don't push — the edit would
            # un-trash it on Keep, which is almost certainly NOT what
            # the user wants. Drop the dirty flag and let the next
            # full sync remove the local copy.
            if server.is_trashed or server.is_deleted:
                log.warning(
                    "push_note: %s is trashed/deleted server-side; "
                    "dropping local edit (not pushing)",
                    keep_note.id[:12],
                )
                return True

            # Mirror local colour onto the server snapshot so any of
            # the downstream push paths (diff, list-replace, legacy
            # text) include the new colour in their node payload. Keep
            # has no separate colour-only endpoint; colour rides along
            # with the next body write.
            wire_color = _hex_to_wire_color(keep_note.color_hex)
            # Capture the pre-mutation colour so the metadata follow-up
            # below (after update_text_diff) can detect a colour-only
            # edit. Without this, mirroring overwrites server.color and
            # the follow-up always thinks the colour is unchanged.
            original_server_color = (
                (server.raw or {}).get("color") or server.color
            )
            original_server_title = (
                (server.raw or {}).get("title") or server.title or ""
            )
            if wire_color and wire_color != server.color:
                server.color = wire_color
                if isinstance(server.raw, dict):
                    server.raw["color"] = wire_color
            # Mirror pinned the same way (mostly cosmetic — pinned is
            # usually pushed via push_metadata — but harmless if local
            # state diverges).
            if isinstance(server.raw, dict):
                server.raw["isPinned"] = bool(keep_note.pinned)
            server.is_pinned = bool(keep_note.pinned)

            # Lists: handled by `replace_list_items` (full-replace
            # strategy). Trigger when EITHER the server already says
            # LIST or the local note is in list mode. The local-only
            # path lets us push checklist edits to a note that's still
            # tagged NOTE on the server (e.g. brand-new note that the
            # user toggled to checklist mode locally).
            if server.type == "LIST" or keep_note.is_list:
                return self._push_list(keep_note, server)

            # Notes without a docs-nestedModel anchor have never been
            # touched by Keep web. We can't ds-rewrite them, but we
            # CAN push a plain-text update via the legacy `text` field
            # (this is what gkeepapi does). Formatting is lost on these
            # notes until web touches them and creates the sct anchor.
            if not server.sct_id:
                # Safety: refuse to wipe a populated note with empty
                # local text + empty title. Same UI-glitch guard as
                # _push_list.
                server_text_len = len(server.indexable_text or "")
                server_has_content = (
                    server_text_len > 0 or bool((server.title or "").strip())
                )
                local_is_empty = (
                    not (keep_note.text or "").strip()
                    and not (keep_note.title or "").strip()
                )
                if local_is_empty and server_has_content:
                    log.warning(
                        "v2 push: %s would clear %d-char server note "
                        "with empty local text+title; refusing",
                        keep_note.id[:8], server_text_len,
                    )
                    return False
                # Multi-line text via the legacy `text` field makes
                # the Keep server auto-promote NOTE → LIST (one item
                # per \n) — silently shredding content. Bootstrap a
                # docs-nestedModel sct anchor first; the same shape
                # Keep web sends on the very first edit.
                local_text = keep_note.text or ""
                if "\n" in local_text:
                    log.info(
                        "v2 push: note %s has no sct_id and multi-line "
                        "text; bootstrapping sct anchor",
                        keep_note.id[:8],
                    )
                    try:
                        self._client.bootstrap_sct(
                            server, local_text,
                            new_title=keep_note.title,
                        )
                    except KeepError as exc:
                        log.error(
                            "sct bootstrap failed for %s: %s",
                            keep_note.id, exc,
                        )
                        return False
                    self._base_text[keep_note.id] = local_text
                    return True
                log.info(
                    "v2 push: note %s has no sct_id; using legacy text "
                    "fallback (formatting will not be preserved)",
                    keep_note.id[:8],
                )
                try:
                    self._client.update_note_legacy_text(
                        server, local_text,
                        new_title=keep_note.title,
                    )
                except KeepError as exc:
                    log.error(
                        "legacy text push failed for %s: %s",
                        keep_note.id, exc,
                    )
                    return False
                # local_text (captured before the network call above),
                # not a fresh keep_note.text read — see the comment on
                # the diff-path's baseline update below for why a late
                # re-read can silently poison the baseline with text
                # that was never actually sent.
                self._base_text[keep_note.id] = local_text
                return True

            # If the cached snapshot is broken (no chunks AND no
            # revision) but the note clearly exists with text on the
            # server, our incremental sync above didn't repair it.
            # Force a full sync NOW so we have authoritative chunks +
            # revision before we attempt to encode an update.
            if (not server.serialized_chunks
                    and not server.nested_revision
                    and (server.indexable_text or "").strip()):
                log.info(
                    "v2 push: cached snapshot is empty for %s; forcing "
                    "full sync before push", keep_note.id[:8],
                )
                try:
                    self._client.sync(full=True)
                    for n in self._client.notes.values():
                        self._server_notes[n.id] = n
                    server = self._server_notes.get(keep_note.id, server)
                except KeepError as exc:
                    log.warning("pre-push full resync failed: %s", exc)

            # 3-way merge if both sides moved since our last fetched base.
            base_text = self._base_text.get(keep_note.id, "")
            base_doc = self._base_doc.get(keep_note.id)
            local_text = keep_note.text or ""
            server_doc = (decode_chunks(server.serialized_chunks)
                          if server.serialized_chunks else None)
            if server_doc and server_doc.paragraphs:
                server_text = server_doc.plain_text
            else:
                server_text = server.indexable_text or server.text or ""

            remote_changed = (server_text != base_text)
            local_changed = (local_text != base_text)
            push_html = keep_note.html or local_text
            # Optional override: when set, push this StyledDoc directly
            # instead of converting push_html. Used by the format-
            # preserving merge path so we don't lose remote bold/italic
            # runs that local HTML doesn't know about.
            override_doc: Optional[StyledDoc] = None
            # Whether _base_text/_base_doc (below) may be advanced to
            # match what we just pushed/adopted. True whenever new_doc
            # was built PURELY from the current widget snapshot
            # (push_html) — the widget already matches, so the
            # baseline may safely track it. False whenever new_doc
            # incorporates server/remote content the widget hasn't
            # been shown yet (a 3-way merge, or adopting server's
            # styling wholesale): advancing the baseline there would
            # make it "ahead of" the widget, and the very next local
            # edit — read from that still-stale widget — would then
            # look like it's missing content relative to the NEW
            # baseline, silently reverting the just-merged remote
            # content on the following push. Leaving the baseline at
            # its old value lets the next fetch_notes() (which only
            # advances it once the widget is confirmed refreshed, see
            # hold_baseline_for) catch up properly instead, or lets a
            # genuine subsequent local edit re-trigger a fresh 3-way
            # merge that correctly recombines everything.
            baseline_reflects_widget = True

            if remote_changed and local_changed and server_text != local_text:
                # Prefer a paragraph-level styled merge (keeps each
                # paragraph's own heading/bold/etc from whichever side
                # contributed it) whenever we have a styled baseline
                # and server doc to align against. Without base_doc we
                # have no common ancestor to diff either side against
                # at the paragraph level; without server_doc there's
                # no remote structure to merge into — both fall back
                # to the plain-text-only merge, which is formatting-
                # blind but always available.
                merged_doc: Optional[StyledDoc] = None
                if base_doc is not None and server_doc is not None:
                    local_doc_for_merge = html_to_styled_doc(
                        push_html, sct_id=server.sct_id,
                    )
                    merged_doc, conflict = _three_way_merge_styled(
                        base_doc, local_doc_for_merge, server_doc,
                    )
                if merged_doc is not None:
                    if conflict:
                        log.warning(
                            "v2 push: 3-way merge for %s had conflict; "
                            "preferring local edits", keep_note.id[:8],
                        )
                    else:
                        log.info(
                            "v2 push: clean 3-way merge for %s "
                            "(base=%d local=%d remote=%d -> merged=%d chars)",
                            keep_note.id[:8], len(base_text), len(local_text),
                            len(server_text), len(merged_doc.plain_text),
                        )
                    keep_note.text = merged_doc.plain_text
                    # NOT to_html(merged_doc): to_html() renders an
                    # empty paragraph as bare <p></p>, which a later
                    # html_to_styled_doc() round-trip silently
                    # collapses away (Qt's HTML parser drops truly
                    # empty <p> elements) — see to_html()'s own
                    # docstring warning. keep_note.html can persist
                    # and get reused as push_html by a LATER push
                    # before the user's next edit resets it from the
                    # live widget, which would propagate that blank-
                    # line loss into what actually gets sent. Leave it
                    # empty and rely on styled_doc (below), which
                    # _refresh_window renders via the cursor-based
                    # path that preserves empty paragraphs faithfully.
                    keep_note.html = ""
                    # Reflect the actual merge result (not a stale pre-
                    # merge snapshot) so a refresh shows what was
                    # really sent, headings and all.
                    keep_note.styled_doc = merged_doc  # type: ignore[attr-defined]
                    push_html = merged_doc.plain_text
                    override_doc = merged_doc
                    baseline_reflects_widget = False
                else:
                    merged_text, conflict = _three_way_merge(
                        base_text, local_text, server_text
                    )
                    if conflict:
                        log.warning(
                            "v2 push: 3-way merge for %s had conflict; "
                            "preferring local edits", keep_note.id[:8],
                        )
                    else:
                        log.info(
                            "v2 push: clean 3-way merge for %s "
                            "(base=%d local=%d remote=%d -> merged=%d chars)",
                            keep_note.id[:8], len(base_text), len(local_text),
                            len(server_text), len(merged_text),
                        )
                    # Merging text-only is the fallback when we lack a
                    # styled baseline/server doc to merge against —
                    # formatting may be lost. Plain-text round trip is
                    # more important than losing the edit entirely.
                    keep_note.text = merged_text
                    keep_note.html = ""
                    # A stale styled_doc from an earlier pull outranks
                    # .html in _refresh_window's render branch — without
                    # clearing it, the window would keep showing the
                    # pre-merge formatted content instead of the fresh
                    # merged plain text.
                    if hasattr(keep_note, "styled_doc"):
                        try:
                            delattr(keep_note, "styled_doc")
                        except AttributeError:
                            pass
                    push_html = merged_text
                    baseline_reflects_widget = False
            elif not local_changed:
                # Local didn't move -- TEXT-wise (this covers BOTH
                # "remote moved" and "neither side moved": plain-text
                # equality can't see FORMATTING-only changes on
                # EITHER side, since bold/italic/underline/
                # strikethrough/heading toggles never touch plain text
                # at all). Two distinct risks share this branch:
                #
                #  1. LOCAL has a formatting-only change pending (e.g.
                #     the user just bolded something). Treating
                #     "text unchanged" as "nothing to push" would
                #     silently drop it and return True, clearing the
                #     dirty flag -- the very next pull is then free to
                #     overwrite the widget with the server's unstyled
                #     copy, which looks exactly like the edit being
                #     reverted by "the return sync".
                #
                #  2. REMOTE (web) has a formatting-only change the
                #     local WIDGET hasn't been shown yet (its refresh
                #     can be deferred while busy/dirty elsewhere, see
                #     _refresh_window_when_idle). Blindly pushing
                #     html_to_styled_doc(push_html) -- built from that
                #     stale, unstyled widget -- as new_doc would encode
                #     an explicit diff against the server's freshly
                #     restyled copy, silently reverting the web's
                #     change instead of adopting it.
                #
                # Both are resolved the same way: compare the widget's
                # CURRENT styling against our cached baseline. If it
                # matches the baseline, the widget simply hasn't caught
                # up with the remote restyle -- adopt server's styling
                # instead of pushing the stale widget over it. If it
                # differs, that's a genuine local restyle -- push it.
                # When we can't tell (no baseline yet), err toward
                # pushing rather than skipping.
                local_doc_for_style_check = html_to_styled_doc(
                    push_html, sct_id=server.sct_id,
                )
                local_restyled = (
                    base_doc is None
                    or not _styles_equal(base_doc, local_doc_for_style_check)
                )
                if not local_restyled:
                    # Body push would be a genuine no-op; but title/
                    # colour edits live on the node payload not in the
                    # body diff, so we still need to push those if they
                    # changed locally. Compare against the pre-mutation
                    # server snapshot we captured above (original_server_*).
                    title_changed = (keep_note.title or "") != original_server_title
                    color_changed = bool(wire_color) and wire_color != original_server_color
                    if title_changed or color_changed:
                        try:
                            self._client.update_note_metadata(
                                server,
                                new_title=(keep_note.title or "")
                                           if title_changed else None,
                                new_color=wire_color if color_changed else None,
                            )
                            log.info(
                                "v2 push: %s metadata-only push (title=%s color=%s)",
                                keep_note.id[:8],
                                title_changed, color_changed,
                            )
                        except KeepError as exc:
                            log.warning(
                                "v2 push: metadata-only push failed for %s: %s",
                                keep_note.id[:8], exc,
                            )
                            return False
                        # Do NOT advance _base_text/_base_doc to
                        # server's here: the widget still shows the
                        # pre-restyle body (that's WHY local_restyled
                        # is False), so the baseline would end up
                        # ahead of what's actually on screen. Leave it
                        # for fetch_notes() to advance once it can
                        # confirm (via hold_baseline_for) the widget
                        # has actually been refreshed to match.
                        return True
                    log.info(
                        "v2 push: skipping %s; local matches base "
                        "(no local edits)", keep_note.id[:8],
                    )
                    return True
                # local_restyled only proves LOCAL changed formatting
                # relative to base_doc -- it says nothing about
                # whether REMOTE also restyled something (a different
                # paragraph, say) since the same base. Pushing
                # local_doc_for_style_check unconditionally here diffs
                # it against server's CURRENT (possibly already
                # remote-restyled) doc via update_text_diff below,
                # which would silently revert whatever remote changed:
                # local_doc_for_style_check was built purely from the
                # widget, with no notion that remote moved at all.
                # Concurrent PURE formatting changes on both sides
                # (plain text untouched everywhere, so this whole
                # branch is even reached) need the same paragraph-
                # level merge the main 3-way-merge branch above uses
                # for concurrent TEXT changes -- just triggered by a
                # styling diff instead of a text diff.
                remote_also_restyled = (
                    base_doc is not None
                    and server_doc is not None
                    and not _styles_equal(base_doc, server_doc)
                )
                if remote_also_restyled:
                    merged_doc, style_conflict = _three_way_merge_styled(
                        base_doc, local_doc_for_style_check, server_doc,
                    )
                    if style_conflict:
                        log.warning(
                            "v2 push: %s concurrent formatting-only "
                            "changes on both sides had a conflict; "
                            "preferring local", keep_note.id[:8],
                        )
                    else:
                        log.info(
                            "v2 push: %s merged concurrent formatting-"
                            "only changes from both local and remote",
                            keep_note.id[:8],
                        )
                    override_doc = merged_doc
                    baseline_reflects_widget = False
                else:
                    log.info(
                        "v2 push: %s has local formatting-only changes "
                        "despite matching plain text; pushing instead of "
                        "skipping", keep_note.id[:8],
                    )
                    override_doc = local_doc_for_style_check
            elif local_changed and server_doc and server_doc.paragraphs:
                # Local text edits only (text-wise). But web may have
                # changed *formatting* on the same base text since our
                # last pull. If we just push the local HTML we'd wipe
                # those format runs. Apply the local text diff onto
                # the remote StyledDoc instead, styling the inserted
                # text from the local widget's own formatting (falling
                # back to inheriting from the surrounding remote runs
                # only if that's unavailable — see
                # _apply_text_edits_preserve_format's docstring for why
                # remote-neighbour-inherit alone silently drops new
                # local formatting on freshly typed text).
                # Only safe when remote text == base text (otherwise
                # the offsets don't line up; fall back to local push).
                #
                # server_text == base_text is trivially true both when
                # the web genuinely re-styled the note AND when NOTHING
                # changed remotely at all — plain text can't tell those
                # apart. Without also checking base_doc, this branch
                # fired on almost every local-only edit (any note with
                # no concurrent web edit satisfies server_text==base_text
                # by definition), so _apply_text_edits_preserve_format
                # below — which only inherits REMOTE's existing styling
                # and has no notion of what the user just changed
                # locally — silently discarded local formatting changes
                # (e.g. a heading) on every push, even though the local
                # widget still showed them correctly right up until the
                # next refresh overwrote it with what was actually sent.
                remote_restyled = (
                    base_doc is not None
                    and not _styles_equal(base_doc, server_doc)
                )
                if (remote_restyled and server_text == base_text
                        and local_text != server_text):
                    local_doc_for_merge = html_to_styled_doc(
                        push_html, sct_id=server.sct_id,
                    )
                    merged_doc = _apply_text_edits_preserve_format(
                        server_doc, base_text, local_text,
                        local_doc=local_doc_for_merge,
                    )
                    if merged_doc is not server_doc:
                        log.info(
                            "v2 push: format-preserving merge for %s "
                            "(remote re-styled, local added %d chars)",
                            keep_note.id[:8],
                            len(local_text) - len(base_text),
                        )
                        override_doc = merged_doc
                        baseline_reflects_widget = False

            # Convert HTML editor content -> StyledDoc (unless an
            # override doc was prepared above).
            if override_doc is not None:
                new_doc = override_doc
                new_doc.sct_id = server.sct_id
            else:
                new_doc = html_to_styled_doc(push_html, sct_id=server.sct_id)

            # Safety: refuse to wipe a populated server note with empty
            # local text + title. Catches the same UI-glitch case as
            # the no-sct branch above.
            new_text_len = sum(len(p.text) for p in new_doc.paragraphs)
            server_text_len = len(server.indexable_text or "")
            local_is_empty = (
                new_text_len == 0
                and not (keep_note.title or "").strip()
            )
            server_has_content = (
                server_text_len > 0 or bool((server.title or "").strip())
            )
            if local_is_empty and server_has_content:
                log.warning(
                    "v2 push: %s would clear %d-char server note with "
                    "empty local text+title; refusing",
                    keep_note.id[:8], server_text_len,
                )
                return False

            # Reflect any local pin/unpin into the cached note so the
            # client serialises the new isPinned value.
            server.is_pinned = bool(keep_note.pinned)
            if isinstance(server.raw, dict):
                server.raw["isPinned"] = bool(keep_note.pinned)
            try:
                self._client.update_text_diff(
                    server, new_doc,
                    new_title=keep_note.title,
                )
            except KeepError as exc:
                log.error(
                    "v2 push failed for note %s (rev=%s, sct_id=%s, "
                    "chunks=%d, idx_len=%d): %s",
                    keep_note.id, server.nested_revision, server.sct_id,
                    len(server.serialized_chunks or []),
                    len(server.indexable_text or ""), exc,
                )
                # Schedule full resync so next attempt has fresh state.
                self._force_full_resync_for.add(keep_note.id)
                return False

            # If the body was unchanged, update_text_diff sent no ops
            # and never pushed our title/colour edits. Issue a metadata
            # push to cover the title-only / colour-only edit cases.
            # (Keep web has no body-less title-update via clientChanges
            # — it sends a plain node payload instead, which is what
            # update_note_metadata does.)
            try:
                wire_color = _hex_to_wire_color(keep_note.color_hex)
                title_changed = (keep_note.title or "") != original_server_title
                color_changed = (wire_color and wire_color != original_server_color)
                if title_changed or color_changed:
                    self._client.update_note_metadata(
                        server,
                        new_title=(keep_note.title or "")
                                   if title_changed else None,
                        new_color=wire_color if color_changed else None,
                    )
            except KeepError as exc:
                log.warning(
                    "v2 push: metadata follow-up failed for %s: %s",
                    keep_note.id[:8], exc,
                )

            # Refresh cache entry + base text from what we just wrote.
            # Force a full sync so the server's authoritative post-write
            # state (including new serializedChunks) replaces any stale
            # cached fragments — incremental deltas often skip echoing
            # the chunks back, leaving our cache out of sync with what
            # we just wrote.
            try:
                self._client.sync(full=True)
                for n in self._client.notes.values():
                    self._server_notes[n.id] = n
            except KeepError as exc:
                log.warning("post-push full sync failed: %s", exc)
            # new_doc is exactly what was just written (whether it came
            # from the override merge or straight from the editor) — the
            # correct new "last known good" baseline for BOTH dicts,
            # PROVIDED the widget actually shows this content already
            # (baseline_reflects_widget). keep_note.text must NOT be
            # re-read here: push_note holds no lock over the KeepNote
            # object itself, so the main thread can (and, on a slow
            # push, routinely does) mutate keep_note.text/.html via
            # _on_note_changed while this function is still running --
            # e.g. right after the network call just above. A fresh
            # read at this point can pick up text the user typed mid-
            # push that was never actually included in what we just
            # sent, poisoning _base_text with content the server
            # doesn't have.
            #
            # When new_doc incorporated server/remote content the
            # widget hasn't been shown yet (a 3-way or format-
            # preserving merge), advancing the baseline here would
            # make it "ahead of" the widget: the next local edit, read
            # from that still-stale widget, would then look like it's
            # missing the just-merged remote content relative to the
            # NEW baseline -- and the push after THAT would silently
            # revert it, exactly like the mid-push-mutation race above
            # but via a different door. Leave the baseline at its old
            # value in that case; the next fetch_notes() only advances
            # it once it can confirm (via hold_baseline_for) the
            # widget was actually refreshed to match, and a genuine
            # subsequent local edit will re-trigger a fresh 3-way
            # merge that correctly recombines everything anyway.
            if baseline_reflects_widget:
                self._base_text[keep_note.id] = new_doc.plain_text
                self._base_doc[keep_note.id] = new_doc
            return True

    def _push_list(self, keep_note: KeepNote, server: ServerNote) -> bool:
        """Push checklist edits via minimal cbx ops (diff against server).

        Collaboration-friendly: items are matched to the server's
        existing cbx blocks by id (when the local list_items carry
        them) or by best-effort text alignment, so concurrent web
        edits to row text / checked state aren't clobbered.

        Caller MUST hold `self._lock`.
        """
        # Captured once, up front — see the diff-path's baseline-update
        # comment near the end of push_note for why a fresh keep_note.text
        # read AFTER a network call can pick up a mid-push edit that was
        # never actually sent, poisoning _base_text.
        local_text = keep_note.text or ""
        # Server must already have a cbx-type sct anchor. If not, we
        # can't bootstrap one yet — fall through to the legacy text
        # path so the edit at least round-trips as plain text.
        if not server.sct_id or server.type != "LIST":
            log.info(
                "v2 push_list: %s lacks cbx anchor (sct=%s type=%s); "
                "deferring to legacy text path",
                keep_note.id[:8], server.sct_id, server.type,
            )
            try:
                self._client.update_note_legacy_text(
                    server, local_text,
                    new_title=keep_note.title,
                )
            except KeepError as exc:
                log.error("legacy text fallback failed: %s", exc)
                return False
            self._base_text[keep_note.id] = local_text
            return True

        # Snapshot server cbx state, then align local rows to it so
        # unchanged rows keep their server-assigned ids → minimal ops.
        server_items = self._client.get_checkboxes(server)
        local_rows = self._derive_list_items(keep_note)

        # Safety: refuse to wipe a populated server list with an empty
        # local list. The most common cause is the editor handing us
        # an empty `text` after a transient parse failure — better to
        # skip the push and let the next edit retry than to delete the
        # user's data.
        if not local_rows and server_items:
            log.warning(
                "_push_list: %s would clear %d server items with empty "
                "local list; refusing (likely UI parse glitch)",
                keep_note.id[:8], len(server_items),
            )
            return False

        new_items = self._align_local_to_server(local_rows, server_items)

        try:
            self._client.update_list_diff(
                server, new_items,
                new_title=keep_note.title,
            )
        except KeepError as exc:
            log.error("v2 push_list failed for %s: %s", keep_note.id, exc)
            return False

        # Realign local cache after a list write — server may have
        # restructured (e.g. graveyard collapse).
        try:
            self._client.sync(full=True)
            for n in self._client.notes.values():
                self._server_notes[n.id] = n
        except KeepError as exc:
            log.warning("post-list-push sync failed: %s", exc)

        self._base_text[keep_note.id] = local_text
        return True

    @staticmethod
    def _align_local_to_server(
        local_rows: list[dict],
        server_items: list["CheckboxItem"],
    ) -> list["CheckboxItem"]:
        """Produce a CheckboxItem list whose rows reuse server cbx_ids
        wherever a local row clearly corresponds to a server row.

        Strategy:
          1. If a local row carries `cbx_id` already (UI tracks it),
             trust it.
          2. Otherwise, align by text via difflib.SequenceMatcher on
             the per-row text sequences. Matched rows inherit the
             server's cbx_id; unmatched local rows are fresh additions.

        Position is set sequentially as top-level `[i]`. Indented
        layouts are out of scope until the UI exposes indent.
        """
        import difflib

        def _pos_for(local_indent: int, idx: int, prev_pos: tuple[int, ...]) -> tuple[int, ...]:
            """Build a tree position tuple for an item given its indent
            level and the position of the previous (outer) row.

            Top-level rows get `(idx,)`. Indented rows get
            `(parent_idx, child_idx)` — keeping the encoder happy when
            it emits multi-element cbx-mv positions.
            """
            if local_indent <= 0 or not prev_pos:
                return (idx,)
            # Trim/extend prev_pos so its length == indent, then append a
            # fresh trailing index. We can't know the true child index
            # locally without walking server state — use the running
            # local position counter under the parent.
            base = prev_pos[: max(1, local_indent)]
            return tuple(base) + (idx,)

        out: list[CheckboxItem] = []
        # Track the index of the most recent top-level (indent=0) row
        # so indent=1 children are encoded as (parent_idx, child_idx)
        # rather than (i-1, i) — the latter produces 2-level positions
        # like (1, 2) when two consecutive children share a parent,
        # which the Keep API rejects with HTTP 400 "Invalid Value".
        last_parent_idx: int = 0
        child_counter: int = 0

        # Fast-path: the UI already tagged each row with an id.
        if all(isinstance(r, dict) and r.get("cbx_id") for r in local_rows):
            for i, r in enumerate(local_rows):
                indent = min(1, int(r.get("indent", 0) or 0))
                if indent <= 0:
                    last_parent_idx = i
                    child_counter = 0
                    pos: tuple[int, ...] = (i,)
                else:
                    pos = (last_parent_idx, child_counter)
                    child_counter += 1
                out.append(CheckboxItem(
                    cbx_id=str(r["cbx_id"]),
                    text=str(r.get("text", "")),
                    checked=bool(r.get("checked")),
                    position=pos,
                ))
            return out

        # Text-based alignment.
        local_texts = [str(r.get("text", "")) for r in local_rows]
        server_texts = [it.text for it in server_items]
        sm = difflib.SequenceMatcher(
            a=server_texts, b=local_texts, autojunk=False,
        )
        # Map local index -> server cbx_id (when the SequenceMatcher
        # aligns them as equal).
        local_to_server: dict[int, str] = {}
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    local_to_server[j1 + k] = server_items[i1 + k].cbx_id

        last_parent_idx = 0
        child_counter = 0
        for i, r in enumerate(local_rows):
            cbx_id = local_to_server.get(i, "")
            # If the local row also carries an explicit cbx_id, prefer it.
            if isinstance(r, dict) and r.get("cbx_id"):
                cbx_id = str(r["cbx_id"])
            indent = (min(1, int(r.get("indent", 0) or 0))
                      if isinstance(r, dict) else 0)
            if indent <= 0:
                last_parent_idx = i
                child_counter = 0
                position: tuple[int, ...] = (i,)
            else:
                position = (last_parent_idx, child_counter)
                child_counter += 1
            out.append(CheckboxItem(
                cbx_id=cbx_id,
                text=str(r.get("text", "")),
                checked=bool(r.get("checked")),
                position=position,
            ))
        return out

    @staticmethod
    def _derive_list_items(keep_note: KeepNote) -> list[dict]:
        """Return a uniform `[{"text": str, "checked": bool, "cbx_id": str?}, ...]`
        list, preferring `keep_note.list_items` over rendered text.

        Indent values are clamped to {0, 1} (Keep API only supports one
        nesting level — anything deeper triggers HTTP 400 Invalid Value)
        and the first row is forced to indent=0 so subsequent indent=1
        rows always have a real parent to attach to.
        """
        if keep_note.list_items:
            out = []
            seen_parent = False
            for it in keep_note.list_items:
                if isinstance(it, dict):
                    indent = min(1, max(0, int(it.get("indent", 0) or 0)))
                    if indent > 0 and not seen_parent:
                        # Promote orphan child to top-level parent.
                        indent = 0
                    if indent == 0:
                        seen_parent = True
                    out.append({
                        "text": str(it.get("text", "")),
                        "checked": bool(it.get("checked")),
                        "cbx_id": str(it.get("cbx_id", "")) or None,
                        "indent": indent,
                    })
                else:
                    seen_parent = True
                    out.append({"text": str(it), "checked": False,
                                "cbx_id": None, "indent": 0})
            return out

        out: list[dict] = []
        for raw_line in (keep_note.text or "").splitlines():
            line = raw_line.rstrip("\r")
            if not line.strip():
                continue
            checked = False
            if line.startswith("☑"):
                checked = True
                line = line[1:].lstrip()
            elif line.startswith("☐"):
                line = line[1:].lstrip()
            out.append({"text": line, "checked": checked, "cbx_id": None})
        return out

    def create_note(self, title="", text="", color_hex="#FFF475",
                    is_list: bool = False,
                    list_items: Optional[list[dict]] = None) -> Optional[KeepNote]:
        """Create a new note via /changes and return a KeepNote mirror.

        Pass ``is_list=True`` (and optionally pre-populated ``list_items``,
        each ``{"text": str, "checked": bool}``) to create a checklist
        directly. Without this the new note is created as a plain NOTE
        and converting NOTE→LIST later requires a separate flow Keep
        web doesn't expose to clients — so we don't try.
        """
        if not self._authenticated or not self._client:
            log.warning("v2 create_note: not authenticated")
            return None
        with self._lock:
            try:
                created = self._client.create_note(
                    title=title or "",
                    text=text or "",
                    color=_hex_to_wire_color(color_hex),
                    node_type=("LIST" if is_list else "NOTE"),
                    list_items=(list_items if is_list else None),
                )
            except KeepError as exc:
                log.error("v2 create_note failed: %s", exc)
                return None
            self._server_notes[created.id] = created
            self._base_text[created.id] = text or ""
            log.info("v2 create_note ok: %s (type=%s)",
                     created.id[:12], created.type)
            kn = KeepNote(
                id=created.id,
                title=created.title,
                text=text or "",
                color_hex=color_hex,
                pinned=created.is_pinned,
                sort_key=int(created.sort_value or 0),
                is_list=is_list,
                list_items=(list(list_items or []) if is_list else []),
            )
            return kn

    def delete_note(self, note_id: str):
        """Trash a note via /changes (matches Keep web's Delete action)."""
        if not self._authenticated or not self._client:
            log.warning("v2 delete_note: not authenticated")
            return
        with self._lock:
            server = self._server_notes.get(note_id)
            if server is None:
                # Resync once in case the note exists on the server but
                # we haven't seen it yet (happens for notes created
                # mid-session before the next pull).
                try:
                    self._client.sync()
                    for n in self._client.notes.values():
                        self._server_notes[n.id] = n
                    server = self._server_notes.get(note_id)
                except KeepError as exc:
                    log.warning("v2 delete_note pre-sync failed: %s", exc)
                if server is None:
                    log.warning(
                        "v2 delete_note: note %s not found on server "
                        "(already deleted, or never synced)",
                        note_id[:12],
                    )
                    # Still purge our local cache so we don't try to
                    # push edits to it later.
                    self._base_text.pop(note_id, None)
                    self._force_full_resync_for.discard(note_id)
                    return
            # Idempotent: if the note is already trashed/deleted on the
            # server, no point sending another trash op.
            if server.is_trashed or server.is_deleted:
                log.info(
                    "v2 delete_note: %s already trashed server-side; "
                    "skipping no-op", note_id[:12],
                )
                self._server_notes.pop(note_id, None)
                self._base_text.pop(note_id, None)
                return
            try:
                self._client.trash_note(server)
            except KeepError as exc:
                log.error("v2 delete_note failed for %s: %s", note_id[:12], exc)
                return
            self._server_notes.pop(note_id, None)
            self._base_text.pop(note_id, None)
            self._force_full_resync_for.discard(note_id)
            log.info("v2 delete_note ok: %s", note_id[:12])


# ----------------------------------------------------------------------
# 3-way text merge
# ----------------------------------------------------------------------

def _diff3_merge(
    base_keys: list, local_keys: list, remote_keys: list,
    base_vals: list, local_vals: list, remote_vals: list,
) -> tuple[list, bool]:
    """Generic diff3-style merge. Returns (merged_vals, had_conflict).

    `*_keys` (must be hashable, e.g. str) drive the SequenceMatcher
    diff; `*_vals` are the parallel per-item payloads actually emitted
    (each list is the same length as its matching `*_keys` list, and
    for plain-text merging keys and vals are identical). Separating
    the two lets the same algorithm merge plain text lines OR styled
    Paragraph objects (keyed by paragraph text, but emitting the
    Paragraph itself so heading/bold/etc survive) without duplicating
    the merge logic.

    Strategy: compute (base->local) and (base->remote) opcodes via
    difflib, then walk base in lockstep applying both. Non-overlapping
    edits combine; overlapping edits conflict and we prefer local.

    Good enough for the common cases (web added a paragraph at the
    bottom while desktop tweaked the middle, etc.). Not a true OT
    rebase — that would need character-level transform logic.
    """
    if local_vals == remote_vals:
        return list(local_vals), False
    if base_vals == remote_vals:
        return list(local_vals), False
    if base_vals == local_vals:
        return list(remote_vals), False

    nb = len(base_keys)

    def compute(other_vals: list, other_keys: list, opcodes):
        # For each base index: 'keep' / 'del' / 'rep' (first idx of a
        # replace/delete run) / 'cont' (continuation of one).
        disp = ['keep'] * nb
        rep_items: dict[int, list] = {}
        rep_keys: dict[int, list] = {}
        ins_before: list[list] = [[] for _ in range(nb + 1)]
        # Diff KEYS parallel to ins_before/pos_vals. Insert dedup and
        # conflict resolution below both need to compare items by the
        # same key the diff was computed on (paragraph TEXT), not by
        # full value equality -- two Paragraphs holding the same text
        # with different bold/heading are unequal as values but are
        # the same paragraph as far as the merge is concerned.
        ins_keys_before: list[list] = [[] for _ in range(nb + 1)]
        pos_keys: dict[int, object] = {}
        # block_range[bi] = (start, end) of the replace/delete block
        # bi belongs to (exclusive end), or (bi, bi+1) for a trivial
        # single/kept position. SequenceMatcher's block boundaries on
        # the LOCAL side don't generally line up with the REMOTE
        # side's — e.g. remote can replace TWO consecutive base
        # paragraphs in one block while local's corresponding block
        # only covers the first of them. Tracking each side's actual
        # block extent (instead of assuming both sides' blocks start
        # and end at the same base position) is what lets the merge
        # loop below correctly reconcile misaligned blocks instead of
        # silently dropping whatever base content falls in the gap.
        block_range: list[tuple[int, int]] = [(bi, bi + 1) for bi in range(nb)]
        # pos_vals[bi] = "what this side shows at base position bi",
        # whenever that's knowable position-for-position: always for
        # an 'equal' opcode (bi does NOT generally equal its matching
        # index in other_vals — earlier inserts/deletes on THIS side
        # shift everything after them — so this is computed from the
        # opcode's own i/j offsets, not assumed), and for a 'replace'
        # opcode ONLY when it substitutes the same COUNT of items it
        # covers (the common "edited these N paragraphs' text" case).
        # Deletions, and replace blocks that also insert/remove whole
        # paragraphs, have no clean per-position mapping and are left
        # out — the merge loop falls back to whole-block comparison
        # for those.
        pos_vals: dict[int, object] = {}
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == 'equal':
                for bi in range(i1, i2):
                    pos_vals[bi] = other_vals[j1 + (bi - i1)]
                    pos_keys[bi] = other_keys[j1 + (bi - i1)]
                continue
            if tag == 'delete':
                for bi in range(i1, i2):
                    disp[bi] = 'del'
                    block_range[bi] = (i1, i2)
            elif tag == 'insert':
                ins_before[i1].extend(other_vals[j1:j2])
                ins_keys_before[i1].extend(other_keys[j1:j2])
            elif tag == 'replace':
                for bi in range(i1, i2):
                    disp[bi] = 'rep' if bi == i1 else 'cont'
                    block_range[bi] = (i1, i2)
                rep_items[i1] = list(other_vals[j1:j2])
                rep_keys[i1] = list(other_keys[j1:j2])
                if (i2 - i1) == (j2 - j1):
                    for bi in range(i1, i2):
                        pos_vals[bi] = other_vals[j1 + (bi - i1)]
                        pos_keys[bi] = other_keys[j1 + (bi - i1)]
        return (disp, rep_items, ins_before, pos_vals, block_range,
                ins_keys_before, pos_keys, rep_keys)

    (disp_l, rep_l, ins_l, pos_l, range_l,
     inskeys_l, poskeys_l, rep_keys_l) = compute(
        local_vals, local_keys,
        difflib.SequenceMatcher(a=base_keys, b=local_keys, autojunk=False).get_opcodes(),
    )
    (disp_r, rep_r, ins_r, pos_r, range_r,
     inskeys_r, poskeys_r, rep_keys_r) = compute(
        remote_vals, remote_keys,
        difflib.SequenceMatcher(a=base_keys, b=remote_keys, autojunk=False).get_opcodes(),
    )

    def merge_inserts(
        left: list, right: list,
        left_keys: Optional[list] = None,
        right_keys: Optional[list] = None,
    ) -> list:
        """Combine two insert lists, deduping items both sides added.

        Dedup is by DIFF KEY (paragraph text), not by value equality.
        Value equality was wrong here: when both sides insert the same
        paragraph but with any styling difference between them — the
        web echoing back a line you just typed, two devices adding the
        same line, a heading applied on one side only — the two
        Paragraph objects compare unequal, so BOTH were appended and
        the line appeared TWICE in the merged note. Text is what the
        diff itself was computed on, so it is the identity the rest of
        this function already reasons in; local's copy wins the styling
        (matching the prefer-local policy used for conflicts).

        Counts are respected rather than membership: a line the user
        genuinely inserted twice on the left stays twice, and only the
        excess copies from the right are appended. Falls back to value
        identity when keys aren't supplied.
        """
        if left == right:
            return list(left)
        if not left:
            return list(right)
        if not right:
            return list(left)
        merged = list(left)
        if left_keys is None or right_keys is None:
            for item in right:
                if item not in left:
                    merged.append(item)
            return merged
        remaining = collections.Counter(left_keys)
        for key, item in zip(right_keys, right):
            if remaining.get(key, 0) > 0:
                remaining[key] -= 1
                continue
            merged.append(item)
        return merged

    def side_output(
        disp_side, rep_side, pos_side, start: int, end: int,
        ins_at: Optional[dict] = None, fallback: Optional[list] = None,
    ) -> list:
        """Reconstruct what ONE side's own output would be for base
        positions [start, end) as a single block — the fallback when
        position-level resolution isn't available (a deletion or a
        count-changing replace fell inside the range). Each side's
        replacement content is emitted exactly once (only at its
        block's start; 'cont' positions are covered by that single
        emission).

        `ins_at`, when given, is a {position: items} map of insertions
        to interleave at each intermediate position within the range —
        the SAME map for both the local and remote reconstruction, so
        an item inserted by either side at a position strictly INSIDE
        a multi-position replace/delete block still shows up in both
        outputs (letting the local_out == remote_out comparison below
        stay meaningful) instead of only being checked at the block's
        boundary, which silently dropped it entirely."""
        result: list = []
        for bi in range(start, end):
            if ins_at and bi > start:
                result.extend(ins_at.get(bi, []))
            d = disp_side[bi]
            if d == 'keep':
                # 'keep' positions always have a pos_side entry (from
                # the matching 'equal' opcode).
                result.append(pos_side.get(
                    bi, (fallback or base_vals)[bi]))
            elif d == 'rep':
                result.extend(rep_side.get(bi, []))
            # 'del' and 'cont': nothing to emit for this position.
        return result

    out: list = []
    conflict = False

    out.extend(merge_inserts(ins_l[0], ins_r[0],
                             inskeys_l[0], inskeys_r[0]))

    bi = 0
    while bi < nb:
        dl, dr = disp_l[bi], disp_r[bi]
        if dl == 'keep' and dr == 'keep':
            # Neither side changed the diff key (text) here, but one
            # side may have changed the VALUE without touching text
            # (e.g. a heading toggle on an otherwise-untouched
            # paragraph) — prefer whichever side actually changed it.
            local_val = pos_l.get(bi, base_vals[bi])
            remote_val = pos_r.get(bi, base_vals[bi])
            if local_val != base_vals[bi]:
                out.append(local_val)
            elif remote_val != base_vals[bi]:
                out.append(remote_val)
            else:
                out.append(base_vals[bi])
            bi += 1
        else:
            # At least one side has a non-trivial (replace/delete)
            # disposition here. Expand to the FULL combined extent of
            # whatever block(s) overlap this position on EITHER side
            # — a fixed-point expansion, since resolving one side's
            # block can pull in more of the other side's (and vice
            # versa). Bounded by nb; fine at note-sized documents.
            start, end = bi, bi + 1
            changed = True
            while changed:
                changed = False
                for rng in (range_l, range_r):
                    for check_bi in range(start, end):
                        s, e = rng[check_bi]
                        if s < start:
                            start, changed = s, True
                        if e > end:
                            end, changed = e, True
            if all(i in pos_l for i in range(start, end)) and all(i in pos_r for i in range(start, end)):
                # Both sides are knowable position-by-position across
                # the whole combined range (the common case: no
                # deletions or count-changing replacements involved).
                # Resolve each base position independently, exactly
                # like the keep+keep case above — this is what lets
                # "remote replaced two consecutive paragraphs in one
                # SequenceMatcher block" correctly separate into "one
                # of them local also touched" vs "the other local
                # left untouched" instead of treating the whole block
                # as one atomic (and thus falsely conflicting) unit.
                for i in range(start, end):
                    if i > start:
                        # An insertion strictly INSIDE this block (not
                        # at its very start, which the previous loop
                        # iteration's trailing merge already covers)
                        # used to be skipped entirely: the only
                        # merge_inserts call for this whole block runs
                        # once at the END (bi=end, after this loop),
                        # which only ever checks ins_before[end] — so
                        # e.g. a brand-new paragraph the user typed
                        # between two OTHER paragraphs that remote
                        # happened to also touch (concurrently) in the
                        # same replace block vanished from the merge
                        # with no warning.
                        out.extend(merge_inserts(
                            ins_l[i], ins_r[i],
                            inskeys_l[i], inskeys_r[i]))
                    lval, rval, bval = pos_l[i], pos_r[i], base_vals[i]
                    l_moved, r_moved = lval != bval, rval != bval
                    if l_moved and r_moved and lval != rval:
                        conflict = True
                    if l_moved and r_moved:
                        # Both sides touched this paragraph. Prefer
                        # whichever changed its TEXT over one that only
                        # restyled it: losing a bold is a cosmetic
                        # regression, losing a rewrite silently reverts
                        # someone's words. Only when both (or neither)
                        # changed the text does the prefer-local
                        # tie-break apply.
                        lkey = poskeys_l.get(i, base_keys[i])
                        rkey = poskeys_r.get(i, base_keys[i])
                        l_retext = lkey != base_keys[i]
                        r_retext = rkey != base_keys[i]
                        out.append(rval if r_retext and not l_retext
                                   else lval)
                    elif l_moved:
                        out.append(lval)
                    elif r_moved:
                        out.append(rval)
                    else:
                        out.append(bval)
            else:
                # Same intermediate-insertion fix as above, applied via
                # side_output's ins_at param so both reconstructions
                # interleave identical insertions and stay comparable.
                ins_at = {
                    i: merge_inserts(ins_l[i], ins_r[i],
                                     inskeys_l[i], inskeys_r[i])
                    for i in range(start + 1, end)
                }
                # The same interleaving expressed in key space, so the
                # key-level reconstructions below stay aligned with the
                # value-level ones they mirror.
                ins_at_keys = {
                    i: merge_inserts(inskeys_l[i], inskeys_r[i],
                                     inskeys_l[i], inskeys_r[i])
                    for i in range(start + 1, end)
                }
                local_out = side_output(disp_l, rep_l, pos_l, start, end, ins_at)
                remote_out = side_output(disp_r, rep_r, pos_r, start, end, ins_at)
                if local_out == remote_out:
                    out.extend(local_out)
                else:
                    conflict = True
                    # Same content-outranks-presentation rule as the
                    # position-level branch above, applied to a whole
                    # block: compare each side's reconstruction to
                    # base by DIFF KEY. A side whose keys still match
                    # base changed nothing but styling here, so it must
                    # not win over a side that added, removed or
                    # rewrote paragraphs — otherwise bolding a word on
                    # the desktop silently resurrects a paragraph
                    # deleted on the web (or reverts its rewrite).
                    local_keys_out = side_output(
                        disp_l, rep_keys_l, poskeys_l, start, end,
                        ins_at_keys, base_keys)
                    remote_keys_out = side_output(
                        disp_r, rep_keys_r, poskeys_r, start, end,
                        ins_at_keys, base_keys)
                    base_slice = base_keys[start:end]
                    l_retext = local_keys_out != base_slice
                    r_retext = remote_keys_out != base_slice
                    out.extend(remote_out if r_retext and not l_retext
                               else local_out)
            bi = end
        out.extend(merge_inserts(ins_l[bi], ins_r[bi],
                                 inskeys_l[bi], inskeys_r[bi]))

    return out, conflict


def _three_way_merge(base: str, local: str, remote: str) -> tuple[str, bool]:
    """Line-based diff3-style merge. Returns (merged_text, had_conflict).

    See _diff3_merge for the underlying algorithm. Formatting-blind —
    used as the fallback when a styled 3-way merge isn't possible (see
    _three_way_merge_styled), or by callers that only have plain text.
    """
    if local == remote:
        return local, False
    if base == remote:
        return local, False
    if base == local:
        return remote, False

    b = base.splitlines()
    l = local.splitlines()
    r = remote.splitlines()
    merged, conflict = _diff3_merge(b, l, r, b, l, r)
    return "\n".join(merged), conflict


def _three_way_merge_styled(
    base_doc: StyledDoc, local_doc: StyledDoc, remote_doc: StyledDoc,
) -> tuple[StyledDoc, bool]:
    """Paragraph-level diff3 merge that keeps each paragraph's OWN
    heading/run styling from whichever side contributed it, instead
    of discarding all formatting to a plain-text merge (the old
    _three_way_merge behaviour). Same non-overlapping-edits-combine,
    overlapping-edits-prefer-local strategy as _three_way_merge, just
    keyed on each paragraph's text (so the diff still lines up the
    same way) while emitting the actual Paragraph objects.
    """
    base_paras = base_doc.paragraphs
    local_paras = local_doc.paragraphs
    remote_paras = remote_doc.paragraphs
    merged_paras, conflict = _diff3_merge(
        [p.text for p in base_paras],
        [p.text for p in local_paras],
        [p.text for p in remote_paras],
        base_paras, local_paras, remote_paras,
    )
    sct_id = local_doc.sct_id or remote_doc.sct_id or base_doc.sct_id
    return StyledDoc(sct_id=sct_id, paragraphs=merged_paras), conflict


def _apply_text_edits_preserve_format(
    remote_doc: StyledDoc, base_text: str, local_text: str,
    local_doc: Optional[StyledDoc] = None,
) -> StyledDoc:
    """Apply the diff base→local onto remote_doc, styling inserted text
    from the LOCAL widget's own formatting when available.

    Pre-condition: remote_doc.plain_text == base_text (i.e. the remote
    side only changed *formatting*, not text).

    The result keeps every formatting run remote applied while folding
    in the local plain-text edits. `local_doc` — the current widget's
    content, decoded the same way remote_doc is — lets newly
    inserted/changed characters carry the styling the user actually
    applied to them locally (e.g. bolding a few letters they just
    typed). Without it, inserted text could only neighbour-inherit
    from REMOTE's existing runs, which has no notion of what the user
    just did locally: typing two new lines and bolding a few letters
    in one of them would silently come out unstyled, because the
    nearest remote character (in unrelated, unrelated-styled existing
    text) has nothing to do with the user's new formatting choice.
    """
    # Flatten remote into per-character list. Paragraph boundaries are
    # represented by ('\n', None, None) sentinel entries.
    flat: list[tuple[str, Optional[StyleRun], Optional[int]]] = []
    for p_idx, para in enumerate(remote_doc.paragraphs):
        if p_idx > 0:
            flat.append(("\n", None, None))
        for run in para.runs:
            for ch in run.text:
                flat.append((ch, run, para.heading))
        # An empty paragraph still needs a position to inherit heading.
        if not para.runs and p_idx == len(remote_doc.paragraphs) - 1:
            pass  # trailing empty paragraph handled by final flush

    # Sanity: lengths should match. If not, give up and return remote
    # unchanged — caller will fall back to a different strategy.
    if "".join(c for c, _, _ in flat) != base_text:
        return remote_doc

    # Flatten LOCAL the same way, keyed by position in local_text, so
    # inserted/changed ranges can look up what the user actually
    # styled those characters as. Skip it (fall back to neighbour-
    # inherit below) if it doesn't round-trip to local_text exactly —
    # that means local_doc came from a different snapshot than
    # local_text and positions wouldn't line up.
    local_flat: list[tuple[str, Optional[StyleRun], Optional[int]]] = []
    if local_doc is not None:
        for p_idx, para in enumerate(local_doc.paragraphs):
            if p_idx > 0:
                local_flat.append(("\n", None, None))
            for run in para.runs:
                for ch in run.text:
                    local_flat.append((ch, run, para.heading))
        if "".join(c for c, _, _ in local_flat) != local_text:
            local_flat = []

    sm = difflib.SequenceMatcher(a=base_text, b=local_text, autojunk=False)
    new_flat: list[tuple[str, Optional[StyleRun], Optional[int]]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            new_flat.extend(flat[i1:i2])
        elif tag == "delete":
            continue
        else:  # 'insert' or 'replace'
            if local_flat:
                # The user's own styling for this exact inserted text.
                new_flat.extend(local_flat[j1:j2])
                continue
            # Pick a style template from the nearest real char.
            template: Optional[StyleRun] = None
            heading: Optional[int] = None
            for prev in reversed(new_flat):
                if prev[1] is not None:
                    _, template, heading = prev
                    break
            if template is None:
                for nxt in flat[i2:]:
                    if nxt[1] is not None:
                        _, template, heading = nxt
                        break
            for ch in local_text[j1:j2]:
                if ch == "\n":
                    # A genuine paragraph break the user just typed
                    # (Enter), NOT a styled character -- must use the
                    # same (char, None, None) sentinel convention the
                    # rebuild loop below relies on to recognize
                    # paragraph boundaries (see the flat-building code
                    # above, and _decode_ops' identical convention).
                    # Tagging it with `template` like every other
                    # character here made it indistinguishable from a
                    # literal newline character: the rebuild loop's
                    # "ch == '\n' and template is None" check requires
                    # template to be None to start a new paragraph, so
                    # a nearby styled run being found (the common case
                    # in any note with existing formatting) silently
                    # merged what should be two paragraphs into one,
                    # with a stray embedded '\n' left sitting inside a
                    # single run's text.
                    new_flat.append(("\n", None, None))
                else:
                    new_flat.append((ch, template, heading))

    # Rebuild StyledDoc.
    out = StyledDoc(sct_id=remote_doc.sct_id, revision=remote_doc.revision)
    cur_para = Paragraph()
    cur_run: Optional[StyleRun] = None

    def flush_run():
        nonlocal cur_run
        if cur_run is not None and cur_run.text:
            cur_para.runs.append(cur_run)
        cur_run = None

    for ch, template, heading in new_flat:
        if ch == "\n" and template is None:
            flush_run()
            out.paragraphs.append(cur_para)
            cur_para = Paragraph()
            continue
        if heading is not None:
            cur_para.heading = heading
        if template is not None:
            sb, si, su, st = (
                template.bold, template.italic,
                template.underline, template.strikethrough,
            )
        else:
            sb = si = su = st = False
        if cur_run is None or cur_run.style_tuple() != (sb, si, su, st):
            flush_run()
            cur_run = StyleRun(
                text="", bold=sb, italic=si,
                underline=su, strikethrough=st,
            )
        cur_run.text += ch
    flush_run()
    out.paragraphs.append(cur_para)
    return out


# ----------------------------------------------------------------------
# HTML <-> StyledDoc bridge
# ----------------------------------------------------------------------

def html_to_styled_doc(html: str, *, sct_id: Optional[str] = None) -> StyledDoc:
    """Parse an HTML fragment (typically QTextEdit.toHtml() output) into
    a StyledDoc using QTextDocument.

    We walk QTextBlock + QTextFragment, extract bold/italic/underline/
    strikethrough from each fragment's QTextCharFormat, and detect
    heading level from the block's QTextBlockFormat.headingLevel().

    QTextDocument is the safest parser here: it handles every variant
    Qt's editor produces, normalises whitespace, and resolves CSS the
    same way the editor displays it."""
    from PySide6.QtGui import QTextDocument
    from keep_protocol.nested_model import Paragraph, StyleRun, coalesce_runs

    if not html:
        return StyledDoc(sct_id=sct_id, paragraphs=[])

    qdoc = QTextDocument()
    # Match NoteTextEdit's stylesheet: Qt's default makes <h1>/<h2>
    # bold, which would cause us to emit ts_bd ops for every char of a
    # heading and round-trip the whole heading as bold. Override before
    # setHtml so fragment.charFormat().fontWeight() reflects only user
    # intent (i.e. only bold when there's an actual <b> or font-weight
    # CSS in the source HTML).
    qdoc.setDefaultStyleSheet(
        "h1, h2, h3, h4, h5, h6 { font-weight: normal; }"
    )
    # If the input doesn't look like HTML, treat as plain text.
    looks_html = "<" in html and ">" in html
    if looks_html:
        qdoc.setHtml(html)
    else:
        qdoc.setPlainText(html)

    paragraphs: list[Paragraph] = []
    block = qdoc.firstBlock()
    while block.isValid():
        bf = block.blockFormat()
        try:
            heading = int(bf.headingLevel())
        except Exception:  # noqa: BLE001
            heading = 0
        # We only model H1 and H2 (Keep's UI offers exactly those).
        if heading > 2:
            heading = 2
        if heading < 0:
            heading = 0

        runs: list[StyleRun] = []
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                text = frag.text()
                if text:
                    cf = frag.charFormat()
                    runs.append(StyleRun(
                        text=text,
                        bold=cf.fontWeight() >= 600,
                        italic=cf.fontItalic(),
                        underline=cf.fontUnderline(),
                        strikethrough=cf.fontStrikeOut(),
                    ))
            it += 1
        # Qt would otherwise auto-bold H1/H2 fragments, which is why
        # qdoc's defaultStyleSheet above normalizes heading font-weight
        # to normal BEFORE parsing -- so fragment.charFormat().fontWeight()
        # here already reflects only genuine user-applied bold (an
        # explicit <b> or font-weight CSS in the source HTML), nothing
        # more needs to be inferred or stripped. An EARLIER heuristic
        # here ("if every run in the heading is bold, assume it's Qt's
        # default and strip it") predated the stylesheet override and
        # became actively wrong once the override made raw fontWeight
        # readings trustworthy: selecting a WHOLE heading and toggling
        # bold on intentionally makes every run bold, and the heuristic
        # silently discarded that genuine edit on every push.
        paragraphs.append(Paragraph(runs=runs, heading=heading))
        block = block.next()

    # Coalesce adjacent identically-styled runs. Qt splits text into
    # fragments on attributes we don't model (font point size, which
    # set_styled_doc stamps on every run to size headings), so a
    # merged heading/body line arrives here as two runs whose modelled
    # styling is identical. Without this the doc compares unequal to
    # the server's own merged form and every sync cycle pushes `as`
    # ops for a note nobody edited — see coalesce_runs' docstring.
    return coalesce_runs(StyledDoc(sct_id=sct_id, paragraphs=paragraphs))
