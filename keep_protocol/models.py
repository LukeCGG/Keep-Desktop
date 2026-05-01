"""Typed models for Keep entities.

Stage 1: just enough to round-trip metadata and plain text. Formatting models
will land in nested_model.py once we've decoded the read-side shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


def _parse_ts(s: str | None) -> Optional[datetime]:
    if not s or s.startswith("1970"):
        return None
    # Keep uses ISO8601 with Z. fromisoformat handles +00:00 in 3.11+, not Z.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class ListItem:
    id: str
    text: str
    checked: bool
    sort_value: int
    parent_id: Optional[str]   # for indented sub-items


@dataclass
class Note:
    """Server-side note. `raw` keeps the full payload so we can round-trip
    fields we don't model yet."""
    id: str
    server_id: str
    type: str                  # "NOTE" or "LIST"
    title: str
    text: str                  # plain text only at this stage
    color: str                 # e.g. "DEFAULT", "RED", "BLUE", ...
    is_archived: bool
    is_pinned: bool
    is_trashed: bool
    is_deleted: bool
    created: Optional[datetime]
    updated: Optional[datetime]
    user_edited: Optional[datetime]
    sort_value: int
    base_version: str
    label_ids: list[str] = field(default_factory=list)
    list_items: list[ListItem] = field(default_factory=list)
    # Per-note nested-model anchor id (sct.xxx). None if the note has never
    # been touched by Keep web — those notes have only the legacy plaintext
    # `text` field and no docs-nestedModel state.
    sct_id: Optional[str] = None
    # docs-nestedModel state. `serialized_chunks` is the snapshot's authoritative
    # op stream (list of JSON strings); `revision` is its monotonic version.
    # `serialized_commands` is the previewData mirror — same content, slightly
    # different framing (wrapped in ["docs-mlti", [...]]). We keep both so the
    # decoder can pick whichever is convenient.
    serialized_chunks: list[str] = field(default_factory=list)
    serialized_commands: Optional[str] = None
    nested_revision: Optional[str] = None
    indexable_text: str = ""
    # Stash of raw server payload for fields we don't model yet, plus any
    # formatting blobs we haven't decoded. Set by client.py.
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_server(cls, n: dict[str, Any]) -> "Note":
        ts = n.get("timestamps") or {}
        indexable = n.get("indexableText") or ""
        # `text` exists for legacy/never-touched-by-web notes. Once a note
        # has a docs-nestedModel snapshot, the server stops echoing `text`
        # at the top level and `indexableText` becomes authoritative.
        text = n.get("text") or indexable

        # plain list items, ignoring nested structure for now
        items = []
        for child in n.get("listItem") or n.get("listItems") or []:
            items.append(ListItem(
                id=child.get("id", ""),
                text=child.get("text", ""),
                checked=bool(child.get("checked", False)),
                sort_value=int(child.get("sortValue", 0)),
                parent_id=child.get("superListItemId"),
            ))

        # Pull docs-nestedModel state out of serverChanges.snapshot.
        sc = (n.get("serverChanges") or {})
        snap = sc.get("snapshot") or {}
        chunks = list(snap.get("serializedChunks") or [])
        revision = snap.get("revision")
        preview_cmds = ((n.get("previewData") or {}).get("serializedCommands")) or None
        sct_id = _find_sct_id_in_chunks(chunks) or _find_sct_id_in_str(preview_cmds or "")

        return cls(
            id=n.get("id", ""),
            server_id=n.get("serverId", ""),
            type=n.get("type", "NOTE"),
            title=n.get("title", "") or "",
            text=text,
            color=(n.get("color") or "DEFAULT"),
            is_archived=bool(n.get("isArchived", False)),
            is_pinned=bool(n.get("isPinned", False)),
            is_trashed=bool(n.get("trashState", 0)),
            is_deleted=bool(n.get("deletionState", 0)),
            created=_parse_ts(ts.get("created")),
            updated=_parse_ts(ts.get("updated")),
            user_edited=_parse_ts(ts.get("userEdited")),
            sort_value=int(n.get("sortValue", 0)),
            base_version=str(n.get("baseVersion", "0")),
            label_ids=[lr.get("labelId", "") for lr in (n.get("labelIds") or [])],
            list_items=items,
            sct_id=sct_id,
            serialized_chunks=chunks,
            serialized_commands=preview_cmds,
            nested_revision=revision,
            indexable_text=indexable,
            raw=n,
        )


import re as _re
_SCT_RE = _re.compile(r'"(sct\.[a-z0-9]+)"')


def _find_sct_id_in_str(s: str) -> Optional[str]:
    if not s:
        return None
    m = _SCT_RE.search(s)
    return m.group(1) if m else None


def _find_sct_id_in_chunks(chunks: list[str]) -> Optional[str]:
    for c in chunks:
        sct = _find_sct_id_in_str(c)
        if sct:
            return sct
    return None
