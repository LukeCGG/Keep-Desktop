"""HTTP client for Keep's `/notes/v1/*` endpoints.

Stage 1: read path only.

Usage:
    from keep_protocol.auth import load_credentials
    from keep_protocol.client import KeepClient

    client = KeepClient(load_credentials())
    notes = client.list_notes()
    for n in notes:
        print(n.title, n.text[:40])
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Optional

import requests

from .auth import Credentials
from .models import Note
from .nested_model import (
    CheckboxItem,
    StyledDoc,
    decode_checkboxes,
    decode_chunks,
    encode_full_replace,
    u16_len,
    encode_list_diff,
    encode_replace_list,
    encode_text_diff,
)


CHANGES_URL = "https://notes-pa.clients6.google.com/notes/v1/changes"
# Public API key Keep web includes on every call. Without it the server
# returns 400 "Invalid Value" on writes (reads still work).
_API_KEY = "AIzaSyDE7NHMUZfMoJVu-YNkK-7AXFSuL1Q9gKE"
CHANGES_URL_WRITE = f"{CHANGES_URL}?alt=json&key={_API_KEY}"

# Capabilities Keep web sends. CL = formatting (docs-nestedModel) opt-in.
# Sending the same set Keep web sends maximises the chance the server gives
# us full responses with formatting state attached.
DEFAULT_CAPABILITIES = [
    {"type": t} for t in ("EC", "TR", "SH", "LB", "RB", "DR", "AN", "PI",
                          "EX", "IN", "SNB", "CO", "MI", "NC", "CL")
]

CLIENT_VERSION = {"major": "3", "minor": "3", "build": "0", "revision": "387"}


class KeepError(RuntimeError):
    pass


log = logging.getLogger(__name__)


class KeepClient:
    """Talks to Keep's HTTP API. Single-account, single-thread. Holds the
    short-lived bearer and re-mints it on demand (~1h TTL)."""

    def __init__(self, creds: Credentials):
        self.creds = creds
        self._bearer: Optional[str] = None
        self._bearer_exp: float = 0.0
        # Per-process session id (used for clientSessionId field)
        self._session_id = f"s--{int(time.time() * 1000)}--{secrets.randbelow(1_000_000_000)}"
        # Cursor for incremental sync. None = full sync.
        self.target_version: Optional[str] = None
        # Cached notes by id. Updated on every sync.
        self.notes: dict[str, Note] = {}
        # Per-note write counters. Keep web increments clientRevision by
        # the number of ops sent in each bundle, and requestId by 1 per
        # bundle. We track both per note for the lifetime of this client.
        # Loaded lazily from server state on first write.
        self._client_revision: dict[str, int] = {}
        self._next_request_id: dict[str, int] = {}
        # When create_note bootstraps a LIST, it pre-mints the first
        # cbx-add inline. The follow-up seeder reuses that id for
        # item[0] so we don't end up with a duplicate row.
        self._pending_first_cbx: dict[str, str] = {}
        # Numeric session id used inside commandBundles (Keep web uses a
        # 19-digit int, distinct from clientSessionId).
        self._bundle_session_id = str(secrets.randbelow(2**63 - 1))
        # Notes where an incremental delta bumped the snapshot revision
        # but didn't echo serializedChunks (Keep does this on compact/
        # metadata-only deltas — including, apparently, paragraph-style
        # ops like a heading change with no text edit). We keep the old
        # chunks rather than guess, which means our cached content is
        # stale relative to the new revision. Surfaced via
        # pop_stale_snapshot_ids() so callers can schedule a full resync.
        self._stale_snapshot_ids: set[str] = set()

    # -------------------------------------------------------------- internal

    def _get_bearer(self) -> str:
        # Re-mint a few minutes before expiry to be safe.
        if not self._bearer or time.monotonic() >= self._bearer_exp - 60:
            self._bearer = self.creds.mint_bearer()
            # gpsoauth doesn't tell us the TTL; Keep bearers are typically 1h.
            self._bearer_exp = time.monotonic() + 50 * 60
        return self._bearer

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"OAuth {self._get_bearer()}",
            "Content-Type": "application/json",
            # Keep web's API key is restricted to its own origin. Without
            # these headers writes get 403 API_KEY_HTTP_REFERRER_BLOCKED.
            "Origin": "https://keep.google.com",
            "Referer": "https://keep.google.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
        }
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        if resp.status_code == 401:
            # Bearer expired mid-call; force a re-mint and retry once.
            self._bearer = None
            headers["Authorization"] = f"OAuth {self._get_bearer()}"
            resp = requests.post(url, headers=headers, json=body, timeout=30)
        if resp.status_code != 200:
            raise KeepError(f"{url} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def _request_header(self) -> dict[str, Any]:
        return {
            "requestId": f"request.{secrets.token_hex(6)}.{int(time.time() * 1000)}",
            "clientVersion": CLIENT_VERSION,
            "clientPlatform": "WEB",
            "capabilities": DEFAULT_CAPABILITIES,
            "clientSessionId": self._session_id,
            "clientLocale": "en-GB",
        }

    # ------------------------------------------------------------------ sync

    def _merge_node_delta(self, nid: str, n: dict,
                          *, update_revision: bool = True) -> None:
        """Merge one server-echoed node delta into ``self.notes``.

        BOTH endpoints return node DELTAS — only the fields that
        changed. The read path (/changes) has always merged carefully;
        the write path used a bare ``merged.update(n)``, which is wrong
        in two ways that between them make a concurrent web edit
        invisible until the app is restarted:

          * ``update()`` overwrites "serverChanges" wholesale whenever
            the key is present at all — even when its inner snapshot is
            empty or degenerate — silently wiping serializedChunks; and
          * a compact delta (revision bumped, chunks not re-echoed) was
            not flagged in ``_stale_snapshot_ids``, so
            KeepSyncV2.fetch_notes never scheduled the full resync that
            repairs it.

        That matters far more on the write path than it looks, because
        a write also advances ``target_version``. Once the cursor moves
        past a change whose content we failed to absorb, no later
        INCREMENTAL sync will ever mention that note again — the server
        is right to consider it already delivered. The note then sits
        frozen at its stale content through every periodic poll and
        every manual "Sync now" (both incremental), and only a restart
        — whose first fetch is full — brings it back.
        """
        existing = self.notes.get(nid)
        if existing is None or not existing.raw:
            fresh = Note.from_server(n)
            if fresh.type in ("NOTE", "LIST"):
                self.notes[nid] = fresh
                self._stale_snapshot_ids.discard(nid)
            return

        merged = dict(existing.raw)
        merged.update(n)
        new_sc = n.get("serverChanges") or {}
        new_snap = new_sc.get("snapshot") or {}
        new_rev = new_snap.get("revision")
        # `is not None`, not truthiness: a snapshot that legitimately
        # echoes serializedChunks: [] (the note's text was fully
        # emptied) is a REAL, authoritative empty snapshot.
        new_chunks = new_snap.get("serializedChunks")
        old_sc = existing.raw.get("serverChanges") or {}
        old_snap = old_sc.get("snapshot") or {}
        old_rev = old_snap.get("revision")

        if new_chunks is not None:
            merged["serverChanges"] = new_sc
            self._stale_snapshot_ids.discard(nid)
        elif new_rev and new_rev != old_rev:
            merged_snap = dict(old_snap)
            merged_snap["revision"] = new_rev
            merged_sc = dict(old_sc)
            merged_sc["snapshot"] = merged_snap
            merged["serverChanges"] = merged_sc
            self._stale_snapshot_ids.add(nid)
        elif old_sc:
            merged["serverChanges"] = old_sc
        elif "serverChanges" in merged:
            del merged["serverChanges"]

        self.notes[nid] = Note.from_server(merged)
        if update_revision and new_rev:
            # Next write should use the server's current revision, not
            # our post-last-write counter. Write paths pass
            # update_revision=False: they have already committed their
            # own (client_revision + op_count) bookkeeping, which the
            # server's echoed value must not clobber mid-sequence.
            try:
                self._client_revision[nid] = int(new_rev)
            except (TypeError, ValueError):
                pass

    def sync(self, full: bool = False) -> dict[str, Any]:
        """Pull changes from the server. Returns the raw response of
        the LAST page (the one that closed out the loop). Updates
        ``self.notes`` and ``self.target_version``.

        Keep's ``/changes`` endpoint is paginated via the
        ``"truncated"`` flag: when it's ``True`` the response only
        carries part of the available change set, and the next call
        — using the response's ``toVersion`` as the cursor — picks up
        where the previous one left off. Looping is required for both
        the initial full sync (a busy account easily exceeds one
        page's worth of nodes) and incremental deltas (long-idle
        clients can have several pages of catch-up). Without the loop
        the cache is silently incomplete; the controller's "stale
        ID" purge then deletes every note that didn't make it into
        the first page, only for them to "reappear" once the next
        sync cycle walks the remaining pages.

        ``full=True`` ignores any prior cursor and asks for the
        complete state. The cache-purge sweep that drops cached notes
        not echoed by the server runs once at the END of the loop —
        not after each page — so partial pages don't trigger
        spurious deletions.
        """
        ids_seen_this_sync: set[str] = set()
        forced_resync = False
        last_data: dict[str, Any] = {}

        first_page = True
        while True:
            body = {
                "clientTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "nodes": [],
                "requestHeader": self._request_header(),
            }
            # First page of a full sync: send no targetVersion so the
            # server resends everything. Subsequent pages — and every
            # page of an incremental sync — use the cursor returned by
            # the previous page (or our existing one for incremental).
            send_cursor = (not first_page) or (not full)
            if send_cursor and self.target_version:
                body["targetVersion"] = self.target_version

            data = self._post(CHANGES_URL, body)
            last_data = data

            if data.get("forceFullResync"):
                # Server says our cursor is too stale or otherwise
                # invalid. Restart the loop in full mode from a clean
                # slate; the next iteration will send no cursor.
                self.target_version = None
                ids_seen_this_sync.clear()
                forced_resync = True
                full = True
                first_page = True
                continue

            self.target_version = data.get("toVersion") or self.target_version

            for n in data.get("nodes", []):
                nid = n.get("id", "")
                ntype = n.get("type", "NOTE")
                if ntype in ("NOTE", "LIST"):
                    if nid:
                        ids_seen_this_sync.add(nid)
                    # Server only echoes CHANGED fields in incremental
                    # syncs. If we have an existing cached version,
                    # merge the delta into its raw payload so unchanged
                    # fields like serializedChunks / indexableText /
                    # sct_id survive.
                    self._merge_node_delta(nid, n)
                elif ntype == "LIST_ITEM":
                    parent = self.notes.get(n.get("parentId", ""))
                    if parent and parent.type == "LIST":
                        # Best-effort; full list-item modelling can
                        # come later.
                        parent.list_items.append(__import__('keep_protocol.models', fromlist=['ListItem']).ListItem(
                            id=n.get("id", ""),
                            text=n.get("text", "") or "",
                            checked=bool(n.get("checked", False)),
                            sort_value=int(n.get("sortValue", 0)),
                            parent_id=n.get("superListItemId"),
                        ))

            first_page = False
            if not data.get("truncated"):
                break

        # On a full sync, after we've seen EVERY page, drop any
        # cached notes that didn't appear in the union of pages —
        # those have been hard-deleted server-side (purged from
        # Trash). Doing this per-page would clobber legitimate notes
        # that sit on a later page.
        if full:
            for stale_id in [nid for nid in self.notes if nid not in ids_seen_this_sync]:
                self.notes.pop(stale_id, None)
                self._stale_snapshot_ids.discard(stale_id)

        if forced_resync:
            log.info("v2 sync: server requested forceFullResync; rebuilt cache")

        return last_data

    def pop_stale_snapshot_ids(self) -> set[str]:
        """Return and clear the set of note ids whose cached
        serializedChunks are known stale (revision bumped but the
        server's incremental delta didn't re-echo the snapshot blob).
        Callers should schedule a full resync for these — see
        KeepSyncEngine.fetch_notes.

        Draining: this is meant for the ONE caller (fetch_notes) that
        sweeps and repairs every stale note each cycle. A caller that
        only cares about ONE specific note id (push_note's pre-push
        staleness check) must use is_snapshot_stale() instead —
        draining the whole set here for a single-note check would
        silently discard OTHER notes' staleness signal before
        fetch_notes ever gets to see it, defeating its protection for
        notes this caller was never even asking about.
        """
        ids, self._stale_snapshot_ids = self._stale_snapshot_ids, set()
        return ids

    def is_snapshot_stale(self, note_id: str) -> bool:
        """Non-draining membership check — does NOT clear the shared
        set, unlike pop_stale_snapshot_ids(). Safe to call for a
        single note of interest without discarding other notes'
        pending staleness flags."""
        return note_id in self._stale_snapshot_ids

    def list_notes(self, include_archived: bool = False, include_trashed: bool = False) -> list[Note]:
        """Return cached notes. Calls sync() if cache is empty."""
        if not self.notes:
            self.sync(full=True)
        out = []
        for n in self.notes.values():
            if n.is_deleted:
                continue
            if n.is_trashed and not include_trashed:
                continue
            if n.is_archived and not include_archived:
                continue
            if n.type not in ("NOTE", "LIST"):
                continue
            out.append(n)
        out.sort(key=lambda n: n.sort_value, reverse=True)
        return out

    def get_note(self, note_id: str) -> Optional[Note]:
        return self.notes.get(note_id)

    # ------------------------------------------------------------ write side

    def update_note_doc(
        self,
        note: Note,
        new_doc: StyledDoc,
        *,
        new_title: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Replace a note's body with `new_doc`, preserving formatting.

        Strategy is full-rewrite: we send a `ds` covering the existing text
        followed by `is`+`as` ops for the new doc. This is corruption-proof
        even if the server's nested-model state has drifted from our cache,
        because we always normalise back to a known StyledDoc.

        `note.sct_id` must be set — i.e. the note must already have been
        touched by Keep web. Notes without an sct_id need a separate path
        (sct-add bootstrap), which we'll add later.

        `dry_run=True` returns the request body without POSTing.
        """
        if not note.sct_id:
            raise KeepError(
                f"note {note.id!r} has no sct_id; cannot rewrite via "
                f"docs-nestedModel yet (would need sct-add bootstrap)"
            )
        # Force the doc to use the existing note's sct id so the encoder
        # targets the right text container.
        new_doc.sct_id = note.sct_id

        # UTF-16 code units, not Python characters: the `ds` this length
        # drives has to cover the WHOLE existing text, and an astral
        # character (any emoji) counts as two units server-side. A
        # codepoint length would leave the final surrogate undeleted and
        # the rebuilt text would be appended after that orphan.
        current_len = u16_len(note.indexable_text or note.text or "")
        ops = encode_full_replace(new_doc, current_len)
        if not ops:
            raise KeepError("encoder produced no ops")

        # Bundle bookkeeping. clientRevision = the snapshot revision the
        # server is currently at *before* applying these ops. After the
        # write the server's revision will be (clientRevision + N ops).
        # ALWAYS prefer the freshest server revision when it's higher
        # than our locally tracked one — web edits, server-side compaction,
        # or a previous failed write all leave our local counter stale,
        # and stale clientRevision is the #1 cause of 400 "Invalid Value".
        try:
            server_rev = int(note.nested_revision or 0)
        except (TypeError, ValueError):
            server_rev = 0
        local_rev = self._client_revision.get(note.id, 0)
        if server_rev > local_rev:
            self._client_revision[note.id] = server_rev
        elif note.id not in self._client_revision:
            self._client_revision[note.id] = server_rev
        if note.id not in self._next_request_id:
            self._next_request_id[note.id] = 1

        client_revision = self._client_revision[note.id]
        request_id = self._next_request_id[note.id]
        serialized = _serialize_ops(ops)

        # Build the node by echoing the server's last view of it (from
        # `note.raw`) with our `clientChanges` block bolted on. Keep's
        # server requires the metadata round-trip; sending a minimal node
        # gets a 400 "Invalid Value".
        raw = note.raw or {}
        node: dict[str, Any] = {
            "id": note.id,
            "kind": "notes#node",
            "parentId": raw.get("parentId", "root"),
            "timestamps": dict(raw.get("timestamps") or {"kind": "notes#timestamps"}),
            "type": note.type,
            "trashState": int(raw.get("trashState", 0)),
            "serverId": note.server_id,
            "deletionState": int(raw.get("deletionState", 0)),
            "sortValue": int(raw.get("sortValue", note.sort_value)),
            "baseVersion": str(raw.get("baseVersion", note.base_version or "0")),
            "title": new_title if new_title is not None else (raw.get("title", note.title) or ""),
            "isArchived": bool(raw.get("isArchived", note.is_archived)),
            "isPinned": bool(raw.get("isPinned", note.is_pinned)),
            "color": raw.get("color") or note.color or "DEFAULT",
            "nodeSettings": dict(raw.get("nodeSettings") or {"graveyardState": "EXPANDED"}),
            "tasks": list(raw.get("tasks") or []),
            "clientChanges": {
                "clientRevision": str(client_revision),
                "commandBundles": [{
                    "sessionId": self._bundle_session_id,
                    "requestId": str(request_id),
                    "serializedCommands": serialized,
                }],
            },
        }
        # Bump userEdited/updated to current time so the server treats it
        # as a real edit (matches Keep web behaviour).
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        node["timestamps"]["updated"] = now_iso
        node["timestamps"]["userEdited"] = now_iso
        # Keep web always sends `deleted` even when zero. The server
        # rejects writes that omit it (400 "Invalid Value").
        node["timestamps"].setdefault("deleted", "1970-01-01T00:00:00.000Z")
        node["timestamps"].setdefault("trashed", "1970-01-01T00:00:00.000Z")
        node["timestamps"].setdefault("kind", "notes#timestamps")

        body = {
            "clientTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "nodes": [node],
            "requestHeader": self._request_header(),
        }
        if self.target_version:
            body["targetVersion"] = self.target_version

        if dry_run:
            return body

        data = self._post(CHANGES_URL_WRITE, body)
        # On success commit the bookkeeping. Server is now at
        # (client_revision + N ops); next write should use that value.
        self._client_revision[note.id] = client_revision + len(ops)
        self._next_request_id[note.id] = request_id + 1
        # Deliberately NOT advancing self.target_version here.
        # target_version is the cursor recording "we have processed
        # every change up to this version". Only sync() can honestly
        # claim that: it LOOPS over pages until the response stops
        # setting "truncated", merging each one. A write response is
        # a single unpaginated payload that echoes whatever the server
        # felt like including, so adopting its toVersion asserts we
        # absorbed changes we may never have been shown — and once the
        # cursor is past them, no later INCREMENTAL sync mentions them
        # again (the server is right to consider them delivered). A
        # concurrent web edit lost that way stays invisible through
        # every periodic poll AND every manual "Sync now", until the
        # app is restarted and its first fetch runs full.
        # Leaving the cursor where it is costs only a little repeated
        # echo on the next sync, which _merge_node_delta absorbs
        # idempotently.
        # Refresh the cache from the response. The write-side response is
        # a *delta* — it only echoes back fields that changed. We merge
        # rather than replace so we don't lose serializedChunks etc.
        for n in data.get("nodes", []):
            nid = n.get("id")
            if not nid:
                continue
            self._merge_node_delta(nid, n, update_revision=False)
        return data

    # ------------------------------------------------------------ list write

    def get_checkboxes(self, note: Note) -> list[CheckboxItem]:
        """Decode the LIST node's snapshot into ordered checkbox items."""
        if note.type != "LIST":
            return []
        items, _ = decode_checkboxes(
            note.serialized_chunks or [], note.sct_id
        )
        return items

    def replace_list_items(
        self,
        note: Note,
        new_items: list[CheckboxItem],
        *,
        new_title: Optional[str] = None,
        dry_run: bool = False,
        existing_ids_override: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Wipe the note's checkboxes and recreate from `new_items`.

        Mirrors `update_note_doc` for LIST nodes. Requires the LIST to
        already have a `cbx`-type sct (i.e. has been touched by Keep
        web). Bootstrapping a brand-new list isn't supported here.

        ``existing_ids_override`` lets the caller declare the cbx ids
        currently on the server when our local cache hasn't yet caught
        up (e.g. right after a LIST bootstrap). When supplied, the
        decoded snapshot is ignored.
        """
        if note.type != "LIST":
            raise KeepError(f"note {note.id!r} is not a LIST")
        if not note.sct_id:
            raise KeepError(
                f"note {note.id!r} has no sct_id; cannot rewrite list "
                f"checkboxes yet (would need sct-add bootstrap)"
            )

        if existing_ids_override is not None:
            existing_ids = list(existing_ids_override)
        else:
            existing, _ = decode_checkboxes(
                note.serialized_chunks or [], note.sct_id
            )
            existing_ids = [item.cbx_id for item in existing]
        ops = encode_replace_list(note.sct_id, existing_ids, new_items)
        if not ops:
            # Nothing on either side — no-op.
            return {}

        # Wrap multi-op writes in docs-mlti the way Keep web does.
        if len(ops) == 1:
            payload_ops: list = ops
        else:
            payload_ops = [["docs-mlti", ops]]

        # Bundle bookkeeping (mirrors update_note_doc).
        try:
            server_rev = int(note.nested_revision or 0)
        except (TypeError, ValueError):
            server_rev = 0
        local_rev = self._client_revision.get(note.id, 0)
        # Trust the server's revision when it knows about this node — our
        # local count can drift if the server silently dropped ops in a
        # bundle (e.g. nested sct-add). Use the higher of the two only
        # when we've never heard back from the server.
        if server_rev > 0:
            self._client_revision[note.id] = server_rev
        elif note.id not in self._client_revision:
            self._client_revision[note.id] = local_rev
        if note.id not in self._next_request_id:
            self._next_request_id[note.id] = 1

        client_revision = self._client_revision[note.id]
        request_id = self._next_request_id[note.id]
        serialized = _serialize_ops(payload_ops)

        raw = note.raw or {}
        node: dict[str, Any] = {
            "id": note.id,
            "kind": "notes#node",
            "parentId": raw.get("parentId", "root"),
            "timestamps": dict(raw.get("timestamps") or {"kind": "notes#timestamps"}),
            "type": note.type,
            "trashState": int(raw.get("trashState", 0)),
            "serverId": note.server_id,
            "deletionState": int(raw.get("deletionState", 0)),
            "sortValue": int(raw.get("sortValue", note.sort_value)),
            "baseVersion": str(raw.get("baseVersion", note.base_version or "0")),
            "title": new_title if new_title is not None else (raw.get("title", note.title) or ""),
            "isArchived": bool(raw.get("isArchived", note.is_archived)),
            "isPinned": bool(raw.get("isPinned", note.is_pinned)),
            "color": raw.get("color") or note.color or "DEFAULT",
            "nodeSettings": dict(raw.get("nodeSettings") or {"graveyardState": "EXPANDED"}),
            "tasks": list(raw.get("tasks") or []),
            "clientChanges": {
                "clientRevision": str(client_revision),
                "commandBundles": [{
                    "sessionId": self._bundle_session_id,
                    "requestId": str(request_id),
                    "serializedCommands": serialized,
                }],
            },
        }
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        node["timestamps"]["updated"] = now_iso
        node["timestamps"]["userEdited"] = now_iso
        node["timestamps"].setdefault("deleted", "1970-01-01T00:00:00.000Z")
        node["timestamps"].setdefault("trashed", "1970-01-01T00:00:00.000Z")
        node["timestamps"].setdefault("kind", "notes#timestamps")

        body = {
            "clientTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "nodes": [node],
            "requestHeader": self._request_header(),
        }
        if self.target_version:
            body["targetVersion"] = self.target_version

        if dry_run:
            return body

        data = self._post(CHANGES_URL_WRITE, body)
        # The number of ops we charged on the wire == number of inner ops,
        # not the docs-mlti wrapper count.
        self._client_revision[note.id] = client_revision + len(ops)
        self._next_request_id[note.id] = request_id + 1
        # Deliberately NOT advancing self.target_version here.
        # target_version is the cursor recording "we have processed
        # every change up to this version". Only sync() can honestly
        # claim that: it LOOPS over pages until the response stops
        # setting "truncated", merging each one. A write response is
        # a single unpaginated payload that echoes whatever the server
        # felt like including, so adopting its toVersion asserts we
        # absorbed changes we may never have been shown — and once the
        # cursor is past them, no later INCREMENTAL sync mentions them
        # again (the server is right to consider them delivered). A
        # concurrent web edit lost that way stays invisible through
        # every periodic poll AND every manual "Sync now", until the
        # app is restarted and its first fetch runs full.
        # Leaving the cursor where it is costs only a little repeated
        # echo on the next sync, which _merge_node_delta absorbs
        # idempotently.
        for n in data.get("nodes", []):
            nid = n.get("id")
            if not nid:
                continue
            self._merge_node_delta(nid, n, update_revision=False)
        return data

    # ------------------------------------------------------------ collab-friendly diffs

    def update_text_diff(
        self,
        note: Note,
        new_doc: StyledDoc,
        *,
        new_title: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Push the smallest set of ops that turns the server's current
        text into `new_doc`.

        Collaboration-safe: the server's OT engine transforms each op
        against any concurrent web edits arriving between our last
        sync and our write. Falls through to a no-op if the doc is
        already up to date.
        """
        if note.type != "NOTE":
            raise KeepError(f"note {note.id!r} is not a NOTE")
        if not note.sct_id:
            raise KeepError(
                f"note {note.id!r} has no sct_id; cannot diff (would need "
                f"sct-add bootstrap)"
            )
        new_doc.sct_id = note.sct_id

        old_doc = decode_chunks(note.serialized_chunks or [])
        # Make sure the diff thinks we're starting from the server's
        # actual sct, not whatever the local doc happened to have.
        old_doc.sct_id = note.sct_id

        ops = encode_text_diff(old_doc, new_doc)
        if not ops:
            return {}
        return self._post_node_with_ops(
            note, ops, new_title=new_title, dry_run=dry_run,
        )

    def update_list_diff(
        self,
        note: Note,
        new_items: list[CheckboxItem],
        *,
        new_title: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Push the smallest set of cbx ops that turns the server's
        current list into `new_items`.

        Items in `new_items` SHOULD carry their server-assigned
        `cbx_id` (preserves identity through reorders/text edits and
        plays nicely with concurrent web edits). Items without a
        `cbx_id` are treated as fresh additions.
        """
        if note.type != "LIST":
            raise KeepError(f"note {note.id!r} is not a LIST")
        if not note.sct_id:
            raise KeepError(
                f"note {note.id!r} has no sct_id; cannot diff list "
                f"(would need sct-add bootstrap)"
            )

        old_items, _ = decode_checkboxes(
            note.serialized_chunks or [], note.sct_id
        )
        ops = encode_list_diff(note.sct_id, old_items, new_items)
        if not ops:
            return {}
        return self._post_node_with_ops(
            note, ops, new_title=new_title, dry_run=dry_run,
        )

    # ------------------------------------------------------------ shared post helper

    def _post_node_with_ops(
        self,
        note: Note,
        ops: list,
        *,
        new_title: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Wrap a flat op list as a commandBundle and POST it.

        Multi-op writes are wrapped in `docs-mlti` to match Keep web's
        behaviour. The clientRevision is bumped by the inner op count
        (NOT counting the docs-mlti wrapper).
        """
        op_count = len(ops)
        if op_count == 0:
            return {}
        # `sct-add` MUST stay at the top level of serializedCommands —
        # nesting it inside `docs-mlti` makes the server reject the
        # whole bundle with 400 Invalid Value (and, for LIST creates,
        # silently drop the op so the revision counter desyncs).
        # Mirror Keep web: peel any leading sct-add(s) out of the
        # docs-mlti wrapper. The outer sct-add(s) also do NOT count
        # toward clientRevision — only the inner-wrapped ops do.
        leading_sct_adds: list = []
        rest = list(ops)
        while rest and isinstance(rest[0], list) and rest[0] and rest[0][0] == "sct-add":
            leading_sct_adds.append(rest.pop(0))
        if not rest:
            payload_ops = leading_sct_adds
            inner_count = len(leading_sct_adds)
        elif len(rest) == 1 and not leading_sct_adds:
            payload_ops = rest
            inner_count = 1
        elif len(rest) == 1 and leading_sct_adds:
            payload_ops = leading_sct_adds + rest
            inner_count = 1
        else:
            payload_ops = leading_sct_adds + [["docs-mlti", rest]]
            inner_count = len(rest)

        try:
            server_rev = int(note.nested_revision or 0)
        except (TypeError, ValueError):
            server_rev = 0
        local_rev = self._client_revision.get(note.id, 0)
        # Trust the server's revision when it knows about this node — our
        # local count can drift if the server silently dropped ops in a
        # bundle (e.g. nested sct-add). Use the local count only when the
        # server hasn't echoed a nested_revision yet.
        if server_rev > 0:
            self._client_revision[note.id] = server_rev
        elif note.id not in self._client_revision:
            self._client_revision[note.id] = local_rev
        if note.id not in self._next_request_id:
            # Keep web uses requestId=0 for the very first commandBundle
            # ever sent on a node (e.g. when bootstrapping an sct anchor
            # on a previously-untouched legacy note). Anything else
            # produces a 400 Invalid Value. Subsequent bundles are 1, 2…
            first_bundle = (
                not note.sct_id
                or not note.nested_revision
                or server_rev == 0
            )
            self._next_request_id[note.id] = 0 if first_bundle else 1

        client_revision = self._client_revision[note.id]
        request_id = self._next_request_id[note.id]
        serialized = _serialize_ops(payload_ops)

        raw = note.raw or {}
        node: dict[str, Any] = {
            "id": note.id,
            "kind": "notes#node",
            "parentId": raw.get("parentId", "root"),
            "timestamps": dict(raw.get("timestamps") or {"kind": "notes#timestamps"}),
            "type": note.type,
            "trashState": int(raw.get("trashState", 0)),
            "serverId": note.server_id,
            "deletionState": int(raw.get("deletionState", 0)),
            "sortValue": int(raw.get("sortValue", note.sort_value)),
            "baseVersion": str(raw.get("baseVersion", note.base_version or "0")),
            "title": new_title if new_title is not None else (raw.get("title", note.title) or ""),
            "isArchived": bool(raw.get("isArchived", note.is_archived)),
            "isPinned": bool(raw.get("isPinned", note.is_pinned)),
            "color": raw.get("color") or note.color or "DEFAULT",
            "nodeSettings": dict(raw.get("nodeSettings") or {"graveyardState": "EXPANDED"}),
            "tasks": list(raw.get("tasks") or []),
            "clientChanges": {
                "clientRevision": str(client_revision),
                "commandBundles": [{
                    "sessionId": self._bundle_session_id,
                    "requestId": str(request_id),
                    "serializedCommands": serialized,
                }],
            },
        }
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        node["timestamps"]["updated"] = now_iso
        node["timestamps"]["userEdited"] = now_iso
        node["timestamps"].setdefault("deleted", "1970-01-01T00:00:00.000Z")
        node["timestamps"].setdefault("trashed", "1970-01-01T00:00:00.000Z")
        node["timestamps"].setdefault("kind", "notes#timestamps")

        body = {
            "clientTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "nodes": [node],
            "requestHeader": self._request_header(),
        }
        if self.target_version:
            body["targetVersion"] = self.target_version

        if dry_run:
            return body

        data = self._post(CHANGES_URL_WRITE, body)
        self._client_revision[note.id] = client_revision + inner_count
        self._next_request_id[note.id] = request_id + 1
        # Deliberately NOT advancing self.target_version here.
        # target_version is the cursor recording "we have processed
        # every change up to this version". Only sync() can honestly
        # claim that: it LOOPS over pages until the response stops
        # setting "truncated", merging each one. A write response is
        # a single unpaginated payload that echoes whatever the server
        # felt like including, so adopting its toVersion asserts we
        # absorbed changes we may never have been shown — and once the
        # cursor is past them, no later INCREMENTAL sync mentions them
        # again (the server is right to consider them delivered). A
        # concurrent web edit lost that way stays invisible through
        # every periodic poll AND every manual "Sync now", until the
        # app is restarted and its first fetch runs full.
        # Leaving the cursor where it is costs only a little repeated
        # echo on the next sync, which _merge_node_delta absorbs
        # idempotently.
        for n in data.get("nodes", []):
            nid = n.get("id")
            if not nid:
                continue
            self._merge_node_delta(nid, n, update_revision=False)
        return data

    # ------------------------------------------------------------ metadata write

    def update_note_metadata(
        self,
        note: Note,
        *,
        is_pinned: Optional[bool] = None,
        sort_value: Optional[int] = None,
        new_title: Optional[str] = None,
        new_color: Optional[str] = None,
    ) -> dict[str, Any]:
        """Update node metadata (pin/sort/title/color) without touching
        the doc body.

        Sends a node payload WITHOUT clientChanges, which is what Keep
        web does for pin- or reorder-only edits. Works for any note,
        including ones with no sct_id (those that have never been
        touched by Keep web's docs-nestedModel).
        """
        raw = note.raw or {}
        node: dict[str, Any] = {
            "id": note.id,
            "kind": "notes#node",
            "parentId": raw.get("parentId", "root"),
            "timestamps": dict(raw.get("timestamps") or {"kind": "notes#timestamps"}),
            "type": note.type,
            "trashState": int(raw.get("trashState", 0)),
            "serverId": note.server_id,
            "deletionState": int(raw.get("deletionState", 0)),
            "sortValue": int(sort_value if sort_value is not None
                             else raw.get("sortValue", note.sort_value)),
            "baseVersion": str(raw.get("baseVersion", note.base_version or "0")),
            "title": new_title if new_title is not None else (raw.get("title", note.title) or ""),
            "isArchived": bool(raw.get("isArchived", note.is_archived)),
            "isPinned": bool(is_pinned if is_pinned is not None
                             else raw.get("isPinned", note.is_pinned)),
            "color": new_color if new_color is not None else (raw.get("color") or note.color),
            "nodeSettings": dict(raw.get("nodeSettings") or {"graveyardState": "EXPANDED"}),
            "tasks": list(raw.get("tasks") or []),
        }
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        node["timestamps"]["updated"] = now_iso
        node["timestamps"]["userEdited"] = now_iso
        node["timestamps"].setdefault("deleted", "1970-01-01T00:00:00.000Z")
        node["timestamps"].setdefault("trashed", "1970-01-01T00:00:00.000Z")
        node["timestamps"].setdefault("kind", "notes#timestamps")

        body = {
            "clientTimestamp": now_iso,
            "nodes": [node],
            "requestHeader": self._request_header(),
        }
        if self.target_version:
            body["targetVersion"] = self.target_version

        data = self._post(CHANGES_URL_WRITE, body)
        # Deliberately NOT advancing self.target_version here.
        # target_version is the cursor recording "we have processed
        # every change up to this version". Only sync() can honestly
        # claim that: it LOOPS over pages until the response stops
        # setting "truncated", merging each one. A write response is
        # a single unpaginated payload that echoes whatever the server
        # felt like including, so adopting its toVersion asserts we
        # absorbed changes we may never have been shown — and once the
        # cursor is past them, no later INCREMENTAL sync mentions them
        # again (the server is right to consider them delivered). A
        # concurrent web edit lost that way stays invisible through
        # every periodic poll AND every manual "Sync now", until the
        # app is restarted and its first fetch runs full.
        # Leaving the cursor where it is costs only a little repeated
        # echo on the next sync, which _merge_node_delta absorbs
        # idempotently.
        for n in data.get("nodes", []):
            nid = n.get("id")
            if not nid:
                continue
            self._merge_node_delta(nid, n, update_revision=False)
        if is_pinned is not None:
            note.is_pinned = bool(is_pinned)
            if isinstance(note.raw, dict):
                note.raw["isPinned"] = bool(is_pinned)
        if sort_value is not None:
            note.sort_value = int(sort_value)
            if isinstance(note.raw, dict):
                note.raw["sortValue"] = int(sort_value)
        return data


    # ------------------------------------------------------------ legacy text write

    def update_note_legacy_text(
        self,
        note: Note,
        new_text: str,
        *,
        new_title: Optional[str] = None,
    ) -> dict[str, Any]:
        """Push a plain-text update for notes that have no sct_id (i.e.
        no docs-nestedModel state). This mirrors how gkeepapi writes
        notes — sets the node's ``text`` field directly without any
        ``clientChanges`` ops.

        Use this only as a fallback when ``update_note_doc`` can't be
        used because the note has never been touched by Keep web.
        """
        raw = note.raw or {}
        node: dict[str, Any] = {
            "id": note.id,
            "kind": "notes#node",
            "parentId": raw.get("parentId", "root"),
            "timestamps": dict(raw.get("timestamps") or {"kind": "notes#timestamps"}),
            "type": note.type,
            "trashState": int(raw.get("trashState", 0)),
            "serverId": note.server_id,
            "deletionState": int(raw.get("deletionState", 0)),
            "sortValue": int(raw.get("sortValue", note.sort_value)),
            "baseVersion": str(raw.get("baseVersion", note.base_version or "0")),
            "title": new_title if new_title is not None else (raw.get("title", note.title) or ""),
            "text": new_text or "",
            "isArchived": bool(raw.get("isArchived", note.is_archived)),
            "isPinned": bool(raw.get("isPinned", note.is_pinned)),
            "color": raw.get("color") or note.color,
            "nodeSettings": dict(raw.get("nodeSettings") or {"graveyardState": "EXPANDED"}),
            "tasks": list(raw.get("tasks") or []),
        }
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        node["timestamps"]["updated"] = now_iso
        node["timestamps"]["userEdited"] = now_iso
        node["timestamps"].setdefault("deleted", "1970-01-01T00:00:00.000Z")
        node["timestamps"].setdefault("trashed", "1970-01-01T00:00:00.000Z")
        node["timestamps"].setdefault("kind", "notes#timestamps")

        body = {
            "clientTimestamp": now_iso,
            "nodes": [node],
            "requestHeader": self._request_header(),
        }
        if self.target_version:
            body["targetVersion"] = self.target_version

        data = self._post(CHANGES_URL_WRITE, body)
        # Deliberately NOT advancing self.target_version here.
        # target_version is the cursor recording "we have processed
        # every change up to this version". Only sync() can honestly
        # claim that: it LOOPS over pages until the response stops
        # setting "truncated", merging each one. A write response is
        # a single unpaginated payload that echoes whatever the server
        # felt like including, so adopting its toVersion asserts we
        # absorbed changes we may never have been shown — and once the
        # cursor is past them, no later INCREMENTAL sync mentions them
        # again (the server is right to consider them delivered). A
        # concurrent web edit lost that way stays invisible through
        # every periodic poll AND every manual "Sync now", until the
        # app is restarted and its first fetch runs full.
        # Leaving the cursor where it is costs only a little repeated
        # echo on the next sync, which _merge_node_delta absorbs
        # idempotently.
        for n in data.get("nodes", []):
            nid = n.get("id")
            if not nid:
                continue
            # Server frequently strips `text`/`indexableText` from the
            # response. Echo the value we just sent so the in-memory
            # cache doesn't lie about content until the next sync.
            if nid == note.id and new_text:
                n = dict(n)
                if not (n.get("text") or "").strip():
                    n["text"] = new_text
                if not (n.get("indexableText") or "").strip():
                    n["indexableText"] = new_text
            self._merge_node_delta(nid, n, update_revision=False)
        return data

    # ------------------------------------------------------------ sct bootstrap

    def bootstrap_sct(
        self,
        note: Note,
        initial_text: str,
        *,
        new_title: Optional[str] = None,
    ) -> dict[str, Any]:
        """Mint a docs-nestedModel sct anchor for a note that has none,
        and seed it with ``initial_text``.

        This is the same shape Keep web sends on the very first edit of
        a fresh note. Without it, multi-line ``text`` pushed via the
        legacy field gets server-side auto-promoted into a LIST (one
        line per item) — silently destroying user formatting.

        After this call returns, ``note.sct_id`` is populated and
        subsequent edits should route through ``update_text_diff`` /
        ``update_note_doc``.

        Op shape (verified against captured Keep-web traffic)::

            ["sct-add", 0, "sct.<rand>", "txt"]
            ["docs-nestedModel", ["text", 1, sct], {"ty":"is","ibi":1,"s":text}]
        """
        if note.type != "NOTE":
            raise KeepError(
                f"bootstrap_sct: note {note.id!r} is type {note.type}, "
                f"not NOTE"
            )
        if note.sct_id:
            # Already bootstrapped — nothing to do.
            return {}
        sct_id = f"sct.{secrets.token_hex(8)}"
        ops: list = [["sct-add", 0, sct_id, "txt"]]
        if initial_text:
            ops.append([
                "docs-nestedModel",
                ["text", 1, sct_id],
                {"ty": "is", "ibi": 1, "s": initial_text},
            ])
        # Pre-seed the note's sct_id locally so the post-helper's
        # bookkeeping (and any cache merge afterwards) sees the right
        # anchor. The server will echo it back in serializedChunks.
        note.sct_id = sct_id
        return self._post_node_with_ops(
            note, ops, new_title=new_title, dry_run=False,
        )

    # ------------------------------------------------------------ create / trash

    def create_note(
        self,
        title: str = "",
        text: str = "",
        color: str = "DEFAULT",
        node_type: str = "NOTE",
        list_items: Optional[list[dict]] = None,
    ) -> Note:
        """Create a new NOTE or LIST on Keep and return the parsed Note.

        Sends a single node with kind=notes#node, the requested ``type``,
        parentId=root, plus a fresh client-generated id. NOTE bodies
        live in the legacy ``text`` field; LIST bodies are seeded via
        ``list_items`` (each ``{text, checked}``).
        """
        if node_type not in ("NOTE", "LIST"):
            raise ValueError(f"create_note: unsupported type {node_type!r}")
        # Keep ids look like "1789aabbccdd.eeffgghhiijjkkll" — 12 hex
        # chars from the client clock + 16 hex chars of randomness, dot
        # separated. Web uses the same shape.
        node_id = f"{int(time.time() * 1000):x}.{secrets.token_hex(8)}"
        # Default sortValue: well above any existing note (so the new
        # one shows at the top). Microsecond timestamp matches Keep's
        # own scale.
        sort_value = int(time.time() * 1_000_000)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        node = {
            "id": node_id,
            "kind": "notes#node",
            "parentId": "root",
            "type": node_type,
            "timestamps": {
                "kind": "notes#timestamps",
                "created": now_iso,
                "updated": now_iso,
                "userEdited": now_iso,
                "deleted": "1970-01-01T00:00:00.000Z",
                "trashed": "1970-01-01T00:00:00.000Z",
            },
            "trashState": 0,
            "deletionState": 0,
            "sortValue": sort_value,
            "baseVersion": "0",
            "title": title or "",
            "isArchived": False,
            "isPinned": False,
            "color": color or "DEFAULT",
            "nodeSettings": {"graveyardState": "EXPANDED"},
        }
        # NOTE bodies use docs-nestedModel state from the very first
        # write — Keep web posts an `sct-add` op at clientRevision=0
        # bundled into the create node. Without it, the server defaults
        # the new node to LIST. We mirror that exactly.
        # LIST creation uses the same trick: mint a txt sct, replace
        # it with a cbx sct via `sct-rp`, then `cbx-add` per item plus
        # per-character `is` ops to seed the text. Keep web sends this
        # as type=NOTE; the server promotes the node to LIST when it
        # sees the cbx state.
        extra_nodes: list[dict[str, Any]] = []
        sct_id_minted: Optional[str] = None
        if node_type == "NOTE":
            sct_id_minted = f"sct.{secrets.token_hex(8)}"
            seed_text = text or ""
            ops: list = [["sct-add", 0, sct_id_minted, "txt"]]
            if seed_text:
                ops.append([
                    "docs-nestedModel",
                    ["text", 1, sct_id_minted],
                    {"ty": "is", "ibi": 1, "s": seed_text},
                ])
            payload_ops = ops if len(ops) == 1 else [["docs-mlti", ops]]
            node["clientChanges"] = {
                "clientRevision": "0",
                "commandBundles": [{
                    "sessionId": self._bundle_session_id,
                    "requestId": "0",
                    "serializedCommands": _serialize_ops(payload_ops),
                }],
            }
            # Initialise our local revision/request bookkeeping for
            # this note. After the create, the server is at revision
            # = len(ops); next request_id is 1.
            self._client_revision[node_id] = len(ops)
            self._next_request_id[node_id] = 1
        else:  # LIST
            # Send as type=NOTE; cbx ops promote it to LIST server-side.
            # Mirror Keep web's exact bootstrap shape: sct-add OUTSIDE
            # docs-mlti, then docs-mlti wrapping sct-rp + the first
            # cbx-add. Without the cbx-add the server only commits 1 op
            # (it silently drops sct-add when nested in docs-mlti) and
            # later writes go out-of-sync. Items beyond the first are
            # written via a follow-up replace_list_items call.
            node["type"] = "NOTE"
            txt_sct = f"sct.{secrets.token_hex(8)}"
            cbx_sct = f"sct.{secrets.token_hex(8)}"
            sct_id_minted = cbx_sct
            first_cbx_id = f"cbx.{secrets.token_hex(6)}"
            payload_ops: list = [
                ["sct-add", 0, txt_sct, "txt"],
                ["docs-mlti", [
                    ["sct-rp", 0, txt_sct, "cbx", cbx_sct],
                    ["cbx-add", cbx_sct, first_cbx_id, [0]],
                ]],
            ]
            node["clientChanges"] = {
                "clientRevision": "0",
                "commandBundles": [{
                    "sessionId": self._bundle_session_id,
                    "requestId": "0",
                    "serializedCommands": _serialize_ops(payload_ops),
                }],
            }
            # Server commits the docs-mlti's 2 inner ops (sct-rp +
            # cbx-add). The outer sct-add appears not to count toward
            # the nested revision counter — empirically the next POST
            # must use clientRevision=2 to be accepted.
            self._client_revision[node_id] = 2
            self._next_request_id[node_id] = 1
            # Stash the first cbx_id so the follow-up seeder can reuse it
            # for item[0] instead of minting a duplicate row.
            self._pending_first_cbx[node_id] = first_cbx_id
            # Items beyond the first are seeded after the create lands —
            # see code path below that dispatches `replace_list_items`.
        body = {
            "clientTimestamp": now_iso,
            "nodes": [node] + extra_nodes,
            "requestHeader": self._request_header(),
        }
        if self.target_version:
            body["targetVersion"] = self.target_version
        data = self._post(CHANGES_URL_WRITE, body)
        # Deliberately NOT advancing self.target_version here.
        # target_version is the cursor recording "we have processed
        # every change up to this version". Only sync() can honestly
        # claim that: it LOOPS over pages until the response stops
        # setting "truncated", merging each one. A write response is
        # a single unpaginated payload that echoes whatever the server
        # felt like including, so adopting its toVersion asserts we
        # absorbed changes we may never have been shown — and once the
        # cursor is past them, no later INCREMENTAL sync mentions them
        # again (the server is right to consider them delivered). A
        # concurrent web edit lost that way stays invisible through
        # every periodic poll AND every manual "Sync now", until the
        # app is restarted and its first fetch runs full.
        # Leaving the cursor where it is costs only a little repeated
        # echo on the next sync, which _merge_node_delta absorbs
        # idempotently.
        # Pull the canonical version of the new note from the response
        # if the server echoed it back; otherwise build one from what
        # we sent so callers get something sensible immediately.
        echoed: Optional[dict[str, Any]] = None
        for n in data.get("nodes", []):
            if n.get("id") == node_id:
                echoed = n
                break
        canonical = echoed or node
        # Server often strips `text` and `indexableText` from the create
        # response since the body lives in docs-nestedModel state for
        # any note that gets opened in web. Echo our sent text into
        # both fields so the in-memory cache doesn't lie about content
        # until the next full sync.
        if node_type == "NOTE" and text:
            canonical = dict(canonical)
            if not (canonical.get("text") or "").strip():
                canonical["text"] = text
            if not (canonical.get("indexableText") or "").strip():
                canonical["indexableText"] = text
        created = Note.from_server(canonical)
        self.notes[node_id] = created
        # Cache the LIST_ITEM children too so list_notes can find them
        # (gkeepapi-style legacy items). Keep web's response usually
        # includes them already, but be defensive.
        if extra_nodes:
            for child in extra_nodes:
                cid = child["id"]
                if cid not in self.notes:
                    self.notes[cid] = Note.from_server(child)
        # For LIST creates, the bootstrap above only set up the cbx
        # sct anchor — items still need to be added. Sync once to pick
        # up the server's freshly promoted LIST type, then write the
        # items via a single full-replace.
        if node_type == "LIST" and list_items:
            try:
                self.sync()
            except KeepError as exc:
                log.warning("create_note: post-bootstrap sync failed: %s", exc)
            # Re-grab the canonical note from the cache.
            promoted = self.notes.get(node_id)
            if promoted is None or promoted.type != "LIST":
                log.warning(
                    "create_note: bootstrap didn't promote %s to LIST "
                    "(cached type=%s); items not seeded",
                    node_id, getattr(promoted, "type", "?"),
                )
                return created
            # The sync may not have refreshed serialized_chunks yet
            # (server's read endpoint can lag the write). We minted
            # the cbx sct ourselves, so plug it in if Note.from_server
            # couldn't recover it from chunks.
            if not promoted.sct_id and sct_id_minted:
                promoted.sct_id = sct_id_minted
            # position carries INDENT into encode_replace_list, which
            # rebuilds the real [parent, child] paths from it. A flat
            # (i,) for every row (what this used to pass) threw the
            # user's sub-items away on the very first sync of a
            # newly-created checklist.
            cbx_items = [
                CheckboxItem(
                    cbx_id="",  # always mint fresh; the bootstrap row
                                # is wiped via existing_ids_override
                    text=str(it.get("text", "") or ""),
                    checked=bool(it.get("checked", False)),
                    position=((i,) if not min(1, int(it.get("indent", 0) or 0))
                              else (0, 0)),
                )
                for i, it in enumerate(list_items)
            ]
            # The bootstrap pre-minted one cbx-add. Tell replace_list_items
            # to wipe it so we don't try to re-add a row that already
            # exists server-side (which 400s with "Invalid Value").
            bootstrap_cbx = self._pending_first_cbx.pop(node_id, "")
            existing_override = [bootstrap_cbx] if bootstrap_cbx else []
            try:
                self.replace_list_items(
                    promoted, cbx_items,
                    existing_ids_override=existing_override,
                )
            except KeepError as exc:
                log.warning("create_note: list-item seed failed: %s", exc)
            else:
                created = self.notes.get(node_id, created)
        return created

    def trash_note(self, note: Note) -> dict[str, Any]:
        """Move a note to Trash (Keep's soft-delete) by setting
        ``trashState=1`` on the node.

        This matches Keep web's "Delete" action — the note is
        recoverable from the Trash for ~7 days before Keep purges it.
        """
        # Always pull the freshest cached version of the note before
        # building the trash payload — the caller may be holding an
        # old `Note` from before a bootstrap/seed write bumped the
        # server's baseVersion. A stale baseVersion makes the server
        # silently no-op the trash request (200 OK, no state change),
        # which is what was leaving feature_test garbage on the account.
        cached = self.notes.get(note.id)
        if cached is not None:
            note = cached
        raw = note.raw or {}
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        node: dict[str, Any] = {
            "id": note.id,
            "kind": "notes#node",
            "parentId": raw.get("parentId", "root"),
            "timestamps": dict(raw.get("timestamps") or {"kind": "notes#timestamps"}),
            "type": note.type,
            "trashState": 1,
            "serverId": note.server_id,
            "deletionState": int(raw.get("deletionState", 0)),
            "sortValue": int(raw.get("sortValue", note.sort_value)),
            "baseVersion": str(raw.get("baseVersion", note.base_version or "0")),
            "title": raw.get("title", note.title) or "",
            "isArchived": bool(raw.get("isArchived", note.is_archived)),
            "isPinned": bool(raw.get("isPinned", note.is_pinned)),
            "color": raw.get("color") or note.color,
            "nodeSettings": dict(raw.get("nodeSettings") or {"graveyardState": "EXPANDED"}),
            "tasks": list(raw.get("tasks") or []),
        }
        node["timestamps"]["updated"] = now_iso
        node["timestamps"]["userEdited"] = now_iso
        node["timestamps"]["trashed"] = now_iso
        node["timestamps"].setdefault("deleted", "1970-01-01T00:00:00.000Z")
        node["timestamps"].setdefault("kind", "notes#timestamps")
        body = {
            "clientTimestamp": now_iso,
            "nodes": [node],
            "requestHeader": self._request_header(),
        }
        if self.target_version:
            body["targetVersion"] = self.target_version
        data = self._post(CHANGES_URL_WRITE, body)
        # Deliberately NOT advancing self.target_version here.
        # target_version is the cursor recording "we have processed
        # every change up to this version". Only sync() can honestly
        # claim that: it LOOPS over pages until the response stops
        # setting "truncated", merging each one. A write response is
        # a single unpaginated payload that echoes whatever the server
        # felt like including, so adopting its toVersion asserts we
        # absorbed changes we may never have been shown — and once the
        # cursor is past them, no later INCREMENTAL sync mentions them
        # again (the server is right to consider them delivered). A
        # concurrent web edit lost that way stays invisible through
        # every periodic poll AND every manual "Sync now", until the
        # app is restarted and its first fetch runs full.
        # Leaving the cursor where it is costs only a little repeated
        # echo on the next sync, which _merge_node_delta absorbs
        # idempotently.
        # Drop the trashed note from our cache so subsequent list_notes
        # calls don't show it (matches the read-side filter).
        self.notes.pop(note.id, None)
        return data


def _serialize_ops(ops: list) -> str:
    """Encode an op list as the JSON string Keep expects in serializedCommands."""
    import json as _json
    return _json.dumps(ops, separators=(",", ":"), ensure_ascii=False)
