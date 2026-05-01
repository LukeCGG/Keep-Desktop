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
)

log = logging.getLogger(__name__)


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
        self._base_text: dict[str, str] = {}
        # Notes whose cached snapshot decoded to empty despite having
        # indexable_text. We force a full resync next pull to repair.
        self._force_full_resync_for: set[str] = set()

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

    def fetch_notes(self, force_resync: bool = False) -> list[KeepNote]:
        if not self._authenticated or not self._client:
            return []
        with self._lock:
            # Promote to full sync if we previously detected stale
            # snapshots that need repairing.
            do_full = force_resync or bool(self._force_full_resync_for)
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

        out: list[KeepNote] = []
        for note in self._client.list_notes():
            self._server_notes[note.id] = note
            if note.is_deleted or note.is_trashed:
                continue
            color_hex = _wire_color_to_hex(note.color)
            # Use Keep's actual sortValue so the manager order matches
            # what you see on keep.google.com. Higher sortValue = higher
            # in Keep — the manager sorts descending on this.
            sort_idx = int(note.sort_value or 0)
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
                doc = decode_chunks(note.serialized_chunks) if note.sct_id else None
                decode_failed = False
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
                        log.warning(
                            "v2 fetch: decode produced empty doc for %s but "
                            "indexableText=%r; falling back to plain text "
                            "(chunks=%d, sct_id=%s, rev=%s)",
                            note.id[:8], note.indexable_text[:80],
                            len(note.serialized_chunks or []),
                            note.sct_id, note.nested_revision,
                        )
                        plain = note.indexable_text or note.text
                        html = ""
                        decode_failed = True
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
                out.append(kn)
                # If the cached snapshot was stale (decoder failed but
                # indexable_text had content), drop the chunks so the
                # next write doesn't try to ds-rewrite based on bogus
                # state. The next server sync will repopulate them.
                if decode_failed:
                    note.serialized_chunks = []
            # Remember the server-side plain text as the merge base for
            # the next push. (Set for both NOTE and LIST so list edits
            # have a base too once we add list-write support.)
            self._base_text[note.id] = out[-1].text
        log.info("v2 fetched %d notes", len(out))
        return out

    # ----- write --------------------------------------------------------

    def push_metadata(
        self, keep_note: KeepNote,
        *, is_pinned: Optional[bool] = None,
        sort_value: Optional[int] = None,
    ) -> bool:
        """Push metadata-only changes (pin/sort/title) to Keep without
        rewriting the note body.

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
            try:
                self._client.update_note_metadata(
                    server,
                    is_pinned=is_pinned,
                    sort_value=sort_value,
                    new_title=keep_note.title,
                )
            except KeepError as exc:
                log.error(
                    "metadata push failed for %s: %s",
                    keep_note.id, exc,
                )
                return False
            log.info(
                "metadata push ok for %s (pinned=%s sort=%s)",
                keep_note.id[:8], is_pinned, sort_value,
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
            # ALWAYS refetch right before push. Two reasons:
            #   1. Get the latest nested_revision so update_note_doc
            #      sends a clientRevision the server will accept.
            #   2. Detect whether web edited the note since we last
            #      pulled, so we can 3-way-merge instead of clobbering.
            try:
                self._client.sync()
            except KeepError as exc:
                log.warning("pre-push resync failed: %s (proceeding anyway)", exc)
            for n in self._client.notes.values():
                self._server_notes[n.id] = n

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
                log.info(
                    "v2 push: note %s has no sct_id; using legacy text "
                    "fallback (formatting will not be preserved)",
                    keep_note.id[:8],
                )
                try:
                    self._client.update_note_legacy_text(
                        server, keep_note.text or "",
                        new_title=keep_note.title,
                    )
                except KeepError as exc:
                    log.error(
                        "legacy text push failed for %s: %s",
                        keep_note.id, exc,
                    )
                    return False
                self._base_text[keep_note.id] = keep_note.text or ""
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

            if remote_changed and local_changed and server_text != local_text:
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
                # Merging text-only is the v1 behaviour for the merged
                # paragraphs — formatting may be lost. Plain-text round
                # trip is more important than preserving styles when
                # both sides edited concurrently.
                keep_note.text = merged_text
                keep_note.html = ""
                push_html = merged_text
            elif remote_changed and not local_changed:
                # Remote moved, local didn't — nothing to push. The
                # remote refresh path will pick up the new content on
                # the next pull.
                log.info(
                    "v2 push: skipping %s; remote changed but local "
                    "matches base (no local edits)", keep_note.id[:8],
                )
                self._base_text[keep_note.id] = server_text
                return True
            elif local_changed and server_doc and server_doc.paragraphs:
                # Local text edits only (text-wise). But web may have
                # changed *formatting* on the same base text since our
                # last pull. If we just push the local HTML we'd wipe
                # those format runs. Apply the local text diff onto
                # the remote StyledDoc instead, inheriting formatting
                # at insertion points from the surrounding remote runs.
                # Only safe when remote text == base text (otherwise
                # the offsets don't line up; fall back to local push).
                if server_text == base_text and local_text != server_text:
                    merged_doc = _apply_text_edits_preserve_format(
                        server_doc, base_text, local_text,
                    )
                    if merged_doc is not server_doc:
                        log.info(
                            "v2 push: format-preserving merge for %s "
                            "(remote re-styled, local added %d chars)",
                            keep_note.id[:8],
                            len(local_text) - len(base_text),
                        )
                        override_doc = merged_doc

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
            self._base_text[keep_note.id] = keep_note.text or ""
            return True

    def _push_list(self, keep_note: KeepNote, server: ServerNote) -> bool:
        """Push checklist edits via minimal cbx ops (diff against server).

        Collaboration-friendly: items are matched to the server's
        existing cbx blocks by id (when the local list_items carry
        them) or by best-effort text alignment, so concurrent web
        edits to row text / checked state aren't clobbered.

        Caller MUST hold `self._lock`.
        """
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
                    server, keep_note.text or "",
                    new_title=keep_note.title,
                )
            except KeepError as exc:
                log.error("legacy text fallback failed: %s", exc)
                return False
            self._base_text[keep_note.id] = keep_note.text or ""
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

        self._base_text[keep_note.id] = keep_note.text or ""
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

        out: list[CheckboxItem] = []
        # Fast-path: the UI already tagged each row with an id.
        if all(isinstance(r, dict) and r.get("cbx_id") for r in local_rows):
            for i, r in enumerate(local_rows):
                out.append(CheckboxItem(
                    cbx_id=str(r["cbx_id"]),
                    text=str(r.get("text", "")),
                    checked=bool(r.get("checked")),
                    position=(i,),
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

        for i, r in enumerate(local_rows):
            cbx_id = local_to_server.get(i, "")
            # If the local row also carries an explicit cbx_id, prefer it.
            if isinstance(r, dict) and r.get("cbx_id"):
                cbx_id = str(r["cbx_id"])
            out.append(CheckboxItem(
                cbx_id=cbx_id,
                text=str(r.get("text", "")),
                checked=bool(r.get("checked")),
                position=(i,),
            ))
        return out

    @staticmethod
    def _derive_list_items(keep_note: KeepNote) -> list[dict]:
        """Return a uniform `[{"text": str, "checked": bool, "cbx_id": str?}, ...]`
        list, preferring `keep_note.list_items` over rendered text."""
        if keep_note.list_items:
            out = []
            for it in keep_note.list_items:
                if isinstance(it, dict):
                    out.append({
                        "text": str(it.get("text", "")),
                        "checked": bool(it.get("checked")),
                        "cbx_id": str(it.get("cbx_id", "")) or None,
                    })
                else:
                    out.append({"text": str(it), "checked": False, "cbx_id": None})
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

    def create_note(self, title="", text="", color_hex="#FFF475") -> Optional[KeepNote]:
        """Create a new note via /changes and return a KeepNote mirror."""
        if not self._authenticated or not self._client:
            log.warning("v2 create_note: not authenticated")
            return None
        with self._lock:
            try:
                created = self._client.create_note(
                    title=title or "",
                    text=text or "",
                    color=_hex_to_wire_color(color_hex),
                )
            except KeepError as exc:
                log.error("v2 create_note failed: %s", exc)
                return None
            self._server_notes[created.id] = created
            self._base_text[created.id] = text or ""
            log.info("v2 create_note ok: %s", created.id[:12])
            return KeepNote(
                id=created.id,
                title=created.title,
                text=text or "",
                color_hex=color_hex,
                pinned=created.is_pinned,
                sort_key=int(created.sort_value or 0),
            )

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

def _three_way_merge(base: str, local: str, remote: str) -> tuple[str, bool]:
    """Line-based diff3-style merge. Returns (merged_text, had_conflict).

    Strategy: compute (base->local) and (base->remote) opcodes via
    difflib, then walk base in lockstep applying both. Non-overlapping
    edits combine; overlapping edits conflict and we prefer local.

    Good enough for the common cases (web added a paragraph at the
    bottom while desktop tweaked the middle, etc.). Not a true OT
    rebase — that would need character-level transform logic.
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

    def compute(other: list[str], opcodes, nb: int):
        # For each base index: 'keep' / 'del' / 'rep' (first idx of
        # replacement run) / 'cont' (continuation of a replacement).
        disp = ['keep'] * nb
        rep_lines: dict[int, list[str]] = {}
        ins_before: list[list[str]] = [[] for _ in range(nb + 1)]
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == 'equal':
                continue
            if tag == 'delete':
                for bi in range(i1, i2):
                    disp[bi] = 'del'
            elif tag == 'insert':
                ins_before[i1].extend(other[j1:j2])
            elif tag == 'replace':
                for bi in range(i1, i2):
                    disp[bi] = 'rep' if bi == i1 else 'cont'
                rep_lines[i1] = list(other[j1:j2])
        return disp, rep_lines, ins_before

    disp_l, rep_l, ins_l = compute(
        l, difflib.SequenceMatcher(a=b, b=l, autojunk=False).get_opcodes(),
        len(b),
    )
    disp_r, rep_r, ins_r = compute(
        r, difflib.SequenceMatcher(a=b, b=r, autojunk=False).get_opcodes(),
        len(b),
    )

    def merge_inserts(left: list[str], right: list[str]) -> list[str]:
        """Combine two insert lists, deduping identical lines."""
        if left == right:
            return list(left)
        if not left:
            return list(right)
        if not right:
            return list(left)
        merged = list(left)
        left_set = set(left)
        for line in right:
            if line not in left_set:
                merged.append(line)
        return merged

    out: list[str] = []
    conflict = False

    out.extend(merge_inserts(ins_l[0], ins_r[0]))

    bi = 0
    while bi < len(b):
        dl, dr = disp_l[bi], disp_r[bi]
        if dl == 'cont' or dr == 'cont':
            # Inside a replacement that started on an earlier base
            # index; nothing more to emit for this row.
            pass
        elif dl == 'keep' and dr == 'keep':
            out.append(b[bi])
        elif dl == 'keep' and dr == 'del':
            pass  # remote deleted
        elif dl == 'del' and dr == 'keep':
            pass  # local deleted
        elif dl == 'del' and dr == 'del':
            pass  # both deleted (no conflict)
        elif dl == 'rep' and dr == 'keep':
            out.extend(rep_l.get(bi, []))
        elif dl == 'keep' and dr == 'rep':
            out.extend(rep_r.get(bi, []))
        elif dl == 'rep' and dr == 'rep':
            if rep_l.get(bi) == rep_r.get(bi):
                out.extend(rep_l.get(bi, []))
            else:
                conflict = True
                out.extend(rep_l.get(bi, []))
        elif dl == 'rep' and dr == 'del':
            conflict = True
            out.extend(rep_l.get(bi, []))  # local replacement wins over remote delete
        elif dl == 'del' and dr == 'rep':
            conflict = True
            out.extend(rep_r.get(bi, []))  # remote replacement wins over local delete
        bi += 1
        out.extend(merge_inserts(ins_l[bi], ins_r[bi]))

    return "\n".join(out), conflict


def _apply_text_edits_preserve_format(
    remote_doc: StyledDoc, base_text: str, local_text: str,
) -> StyledDoc:
    """Apply the diff base→local onto remote_doc, inheriting style from
    surrounding remote text at insertion points.

    Pre-condition: remote_doc.plain_text == base_text (i.e. the remote
    side only changed *formatting*, not text).

    The result keeps every formatting run remote applied while folding
    in the local plain-text edits as un-styled (or neighbour-styled)
    insertions.
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

    sm = difflib.SequenceMatcher(a=base_text, b=local_text, autojunk=False)
    new_flat: list[tuple[str, Optional[StyleRun], Optional[int]]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            new_flat.extend(flat[i1:i2])
        elif tag == "delete":
            continue
        else:  # 'insert' or 'replace'
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
    from keep_protocol.nested_model import Paragraph, StyleRun

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
        # Headings: Qt auto-bolds H1/H2 fragments unless we've overridden
        # the default stylesheet on the source document. To distinguish
        # "user really bolded the whole heading" from "Qt added bold for
        # presentation", treat bold as redundant ONLY when every run in
        # the heading is bold (uniform). If only some runs are bold,
        # those represent intentional user formatting and must be kept.
        if heading > 0 and runs and all(r.bold for r in runs):
            runs = [
                StyleRun(text=r.text, bold=False, italic=r.italic,
                         underline=r.underline, strikethrough=r.strikethrough)
                for r in runs
            ]
        paragraphs.append(Paragraph(runs=runs, heading=heading))
        block = block.next()

    # Trim a trailing empty paragraph that Qt likes to add.
    while len(paragraphs) > 1 and not paragraphs[-1].runs:
        paragraphs.pop()

    return StyledDoc(sct_id=sct_id, paragraphs=paragraphs)
