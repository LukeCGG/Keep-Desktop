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
        # Numeric session id used inside commandBundles (Keep web uses a
        # 19-digit int, distinct from clientSessionId).
        self._bundle_session_id = str(secrets.randbelow(10**19))

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

    def sync(self, full: bool = False) -> dict[str, Any]:
        """Pull changes from the server. Returns the raw response.
        Updates `self.notes` and `self.target_version`.

        full=True forces a full resync (ignores any prior cursor).
        """
        body = {
            "clientTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "nodes": [],
            "requestHeader": self._request_header(),
        }
        if not full and self.target_version:
            body["targetVersion"] = self.target_version

        data = self._post(CHANGES_URL, body)
        self.target_version = data.get("toVersion") or self.target_version

        for n in data.get("nodes", []):
            nid = n.get("id", "")
            ntype = n.get("type", "NOTE")
            if ntype in ("NOTE", "LIST"):
                # Server only echoes CHANGED fields in incremental syncs.
                # If we have an existing cached version, merge the delta
                # into its raw payload so unchanged fields like
                # serializedChunks / indexableText / sct_id survive.
                existing = self.notes.get(nid)
                if existing is not None and existing.raw:
                    merged = dict(existing.raw)
                    merged.update(n)
                    # serverChanges is itself a dict that needs deep merge
                    # so we don't blow away the snapshot just because the
                    # delta omits it.
                    if ("serverChanges" not in n
                            and existing.raw.get("serverChanges")):
                        merged["serverChanges"] = existing.raw["serverChanges"]
                    # If the delta brings a new serverChanges with a
                    # different revision than what we cached, the cached
                    # serializedChunks are stale by definition. Drop
                    # them so the decoder doesn't run on outdated ops.
                    new_sc = n.get("serverChanges") or {}
                    new_snap = (new_sc.get("snapshot") or {})
                    new_rev = new_snap.get("revision")
                    old_sc = existing.raw.get("serverChanges") or {}
                    old_rev = (old_sc.get("snapshot") or {}).get("revision")
                    if new_rev and new_rev != old_rev:
                        # Server gave us an updated snapshot: trust it
                        # entirely (don't fall back to old chunks).
                        merged["serverChanges"] = n["serverChanges"]
                    self.notes[nid] = Note.from_server(merged)
                    # Reset our local revision tracker too — next write
                    # should use the server's current revision, not our
                    # post-last-write counter (which is now stale).
                    if new_rev:
                        try:
                            self._client_revision[nid] = int(new_rev)
                        except (TypeError, ValueError):
                            pass
                else:
                    self.notes[nid] = Note.from_server(n)
            elif ntype == "LIST_ITEM":
                parent = self.notes.get(n.get("parentId", ""))
                if parent and parent.type == "LIST":
                    # Best-effort; full list-item modelling can come later.
                    parent.list_items.append(__import__('keep_protocol.models', fromlist=['ListItem']).ListItem(
                        id=n.get("id", ""),
                        text=n.get("text", "") or "",
                        checked=bool(n.get("checked", False)),
                        sort_value=int(n.get("sortValue", 0)),
                        parent_id=n.get("superListItemId"),
                    ))
        return data

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

        current_len = len(note.indexable_text or note.text or "")
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
        self.target_version = data.get("toVersion") or self.target_version
        # Refresh the cache from the response. The write-side response is
        # a *delta* — it only echoes back fields that changed. We merge
        # rather than replace so we don't lose serializedChunks etc.
        for n in data.get("nodes", []):
            nid = n.get("id")
            if not nid:
                continue
            existing = self.notes.get(nid)
            if existing:
                merged = dict(existing.raw)
                merged.update(n)
                self.notes[nid] = Note.from_server(merged)
            else:
                fresh = Note.from_server(n)
                if fresh.type in ("NOTE", "LIST"):
                    self.notes[nid] = fresh
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
    ) -> dict[str, Any]:
        """Wipe the note's checkboxes and recreate from `new_items`.

        Mirrors `update_note_doc` for LIST nodes. Requires the LIST to
        already have a `cbx`-type sct (i.e. has been touched by Keep
        web). Bootstrapping a brand-new list isn't supported here.
        """
        if note.type != "LIST":
            raise KeepError(f"note {note.id!r} is not a LIST")
        if not note.sct_id:
            raise KeepError(
                f"note {note.id!r} has no sct_id; cannot rewrite list "
                f"checkboxes yet (would need sct-add bootstrap)"
            )

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
        if server_rev > local_rev:
            self._client_revision[note.id] = server_rev
        elif note.id not in self._client_revision:
            self._client_revision[note.id] = server_rev
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
        self.target_version = data.get("toVersion") or self.target_version
        for n in data.get("nodes", []):
            nid = n.get("id")
            if not nid:
                continue
            existing_n = self.notes.get(nid)
            if existing_n:
                merged = dict(existing_n.raw)
                merged.update(n)
                self.notes[nid] = Note.from_server(merged)
            else:
                fresh = Note.from_server(n)
                if fresh.type in ("NOTE", "LIST"):
                    self.notes[nid] = fresh
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
        payload_ops: list = ops if op_count == 1 else [["docs-mlti", ops]]

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
        self._client_revision[note.id] = client_revision + op_count
        self._next_request_id[note.id] = request_id + 1
        self.target_version = data.get("toVersion") or self.target_version
        for n in data.get("nodes", []):
            nid = n.get("id")
            if not nid:
                continue
            existing_n = self.notes.get(nid)
            if existing_n:
                merged = dict(existing_n.raw)
                merged.update(n)
                self.notes[nid] = Note.from_server(merged)
            else:
                fresh = Note.from_server(n)
                if fresh.type in ("NOTE", "LIST"):
                    self.notes[nid] = fresh
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
        self.target_version = data.get("toVersion") or self.target_version
        for n in data.get("nodes", []):
            nid = n.get("id")
            if not nid:
                continue
            existing = self.notes.get(nid)
            if existing:
                merged = dict(existing.raw)
                merged.update(n)
                if ("serverChanges" not in n
                        and existing.raw.get("serverChanges")):
                    merged["serverChanges"] = existing.raw["serverChanges"]
                self.notes[nid] = Note.from_server(merged)
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
        self.target_version = data.get("toVersion") or self.target_version
        for n in data.get("nodes", []):
            nid = n.get("id")
            if not nid:
                continue
            existing = self.notes.get(nid)
            if existing:
                merged = dict(existing.raw)
                merged.update(n)
                if ("serverChanges" not in n
                        and existing.raw.get("serverChanges")):
                    merged["serverChanges"] = existing.raw["serverChanges"]
                self.notes[nid] = Note.from_server(merged)
        return data

    # ------------------------------------------------------------ create / trash

    def create_note(
        self,
        title: str = "",
        text: str = "",
        color: str = "DEFAULT",
    ) -> Note:
        """Create a new NOTE on Keep and return the parsed Note.

        Sends a single node with kind=notes#node, type=NOTE, parentId=root,
        plus a fresh client-generated id. Body lives in the legacy ``text``
        field; Keep will create the docs-nestedModel anchor (sct_id) the
        first time the note is opened in the web UI.
        """
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
            "type": "NOTE",
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
            "text": text or "",
            "isArchived": False,
            "isPinned": False,
            "color": color or "DEFAULT",
            "nodeSettings": {"graveyardState": "EXPANDED"},
        }
        body = {
            "clientTimestamp": now_iso,
            "nodes": [node],
            "requestHeader": self._request_header(),
        }
        if self.target_version:
            body["targetVersion"] = self.target_version
        data = self._post(CHANGES_URL_WRITE, body)
        self.target_version = data.get("toVersion") or self.target_version
        # Pull the canonical version of the new note from the response
        # if the server echoed it back; otherwise build one from what
        # we sent so callers get something sensible immediately.
        for n in data.get("nodes", []):
            if n.get("id") == node_id:
                created = Note.from_server(n)
                self.notes[node_id] = created
                return created
        created = Note.from_server(node)
        self.notes[node_id] = created
        return created

    def trash_note(self, note: Note) -> dict[str, Any]:
        """Move a note to Trash (Keep's soft-delete) by setting
        ``trashState=1`` on the node.

        This matches Keep web's "Delete" action — the note is
        recoverable from the Trash for ~7 days before Keep purges it.
        """
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
        self.target_version = data.get("toVersion") or self.target_version
        # Drop the trashed note from our cache so subsequent list_notes
        # calls don't show it (matches the read-side filter).
        self.notes.pop(note.id, None)
        return data


def _serialize_ops(ops: list) -> str:
    """Encode an op list as the JSON string Keep expects in serializedCommands."""
    import json as _json
    return _json.dumps(ops, separators=(",", ":"), ensure_ascii=False)
