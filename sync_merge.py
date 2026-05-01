"""Pure-function helpers for sync conflict resolution.

These were originally inline in :class:`AppController._apply_remote_notes`
but were extracted so they can be exercised in isolation without
spinning up a Qt application + tray icon. Keeping them pure makes the
"who wins?" decisions explicit and unit-testable.

A `MergeDecision` describes what the controller should do for a single
note when a remote pull arrives:

  - ``skip_dirty``   — local has unpushed edits; ignore remote completely
  - ``adopt_remote`` — apply remote text/title/colour/list to local cache
  - ``preserve_local`` — remote looks suspicious (empty when local non-empty);
                        keep local cache, only adopt safe metadata
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class MergeAction(Enum):
    SKIP_DIRTY = "skip_dirty"
    ADOPT_REMOTE = "adopt_remote"
    PRESERVE_LOCAL_BODY = "preserve_local_body"


@dataclass
class MergeDecision:
    action: MergeAction
    # Truthy when caller should also refresh the open NoteWindow.
    refresh_window: bool = False
    # Detected diffs (for logging / UI).
    text_changed: bool = False
    title_changed: bool = False
    color_changed: bool = False
    list_changed: bool = False
    html_changed: bool = False


def decide_merge(
    *,
    local: Any,
    remote: Any,
    is_dirty: bool,
    user_busy: bool,
) -> MergeDecision:
    """Compute the merge decision for one note.

    `local` and `remote` are :class:`keep_sync.KeepNote` (or anything
    with the same shape: ``text``, ``title``, ``color_hex``, ``html``,
    ``is_list``, ``list_items``).

    `is_dirty` — True iff the local note has un-pushed edits.
    `user_busy` — True iff the user is currently typing in this note's
    window.
    """
    if is_dirty:
        return MergeDecision(action=MergeAction.SKIP_DIRTY)

    text_changed = (local.text != remote.text)
    title_changed = (local.title != remote.title)
    color_changed = (local.color_hex != remote.color_hex)
    list_changed = (
        local.is_list != remote.is_list
        or local.list_items != remote.list_items
    )
    html_changed = (
        not text_changed
        and bool(getattr(remote, "html", ""))
        and (remote.html != (local.html or ""))
    )

    visible_changed = (
        text_changed or title_changed or color_changed
        or list_changed or html_changed
    )

    # SAFETY: if remote text is empty but local has content, treat as
    # a suspicious fetch (decode failure, partial response, etc.) and
    # preserve the local body. Metadata changes are still adopted.
    remote_text_empty = not (remote.text or "").strip()
    local_text_nonempty = bool((local.text or "").strip())
    if remote_text_empty and local_text_nonempty:
        return MergeDecision(
            action=MergeAction.PRESERVE_LOCAL_BODY,
            refresh_window=False,
            text_changed=text_changed,
            title_changed=title_changed,
            color_changed=color_changed,
            list_changed=list_changed,
            html_changed=html_changed,
        )

    return MergeDecision(
        action=MergeAction.ADOPT_REMOTE,
        # Skip the visible refresh while the user is typing — the cache
        # is still updated so subsequent reads see fresh metadata.
        refresh_window=visible_changed and not user_busy,
        text_changed=text_changed,
        title_changed=title_changed,
        color_changed=color_changed,
        list_changed=list_changed,
        html_changed=html_changed,
    )
