"""Google Keep synchronisation layer using gkeepapi."""

import logging
import threading
from dataclasses import dataclass, field
from uuid import getnode as get_mac

import gkeepapi
import gpsoauth

from config import (
    KEEP_COLORS, load_token, save_token,
)

log = logging.getLogger(__name__)

# Reverse lookup: hex -> Keep color enum name
HEX_TO_KEEP_COLOR = {v: k for k, v in KEEP_COLORS.items()}


@dataclass
class KeepNote:
    """Lightweight mirror of a Google Keep note."""
    id: str
    title: str = ""
    text: str = ""
    html: str = ""
    color_hex: str = "#FFF475"
    pinned: bool = False
    trashed: bool = False
    labels: list = field(default_factory=list)
    sort_key: int = 0
    is_list: bool = False
    list_items: list = field(default_factory=list)  # [{text, checked}]


def _color_to_hex(keep_color) -> str:
    """Convert a gkeepapi ColorValue to a hex string."""
    name = keep_color.name if keep_color else "White"
    return KEEP_COLORS.get(name, KEEP_COLORS["White"])


def _hex_to_keep_color(hex_val: str):
    """Convert a hex string to a gkeepapi ColorValue."""
    name = HEX_TO_KEEP_COLOR.get(hex_val, "White")
    try:
        return gkeepapi.node.ColorValue[name]
    except KeyError:
        return gkeepapi.node.ColorValue.White


class KeepSync:
    """Handles authentication and two-way sync with Google Keep."""

    def __init__(self):
        self.keep = gkeepapi.Keep()
        self._authenticated = False
        self._lock = threading.Lock()

    @property
    def is_authenticated(self):
        return self._authenticated

    def login(self, email: str, master_token: str | None = None,
              password: str | None = None) -> bool:
        """Authenticate with Google Keep.

        Prefers a stored master token for subsequent logins.
        """
        stored_token = load_token()
        try:
            if master_token:
                self.keep.authenticate(email, master_token)
                self._authenticated = True
                # Persist so we don't need browser login again
                save_token(master_token)
            elif stored_token:
                self.keep.authenticate(email, stored_token)
                self._authenticated = True
            elif password:
                self.keep.login(email, password)
                self._authenticated = True
                token = self.keep.getMasterToken()
                if token:
                    save_token(token)
            else:
                return False
        except Exception as exc:
            log.error("Keep login failed: %s", exc)
            self._authenticated = False
            return False

        # Save master token if we don't have one yet
        if self._authenticated and not stored_token:
            token = self.keep.getMasterToken()
            if token:
                save_token(token)

        log.info("Authenticated with Google Keep as %s", email)
        return True

    @staticmethod
    def exchange_oauth_for_master(email: str, oauth_token: str) -> dict | None:
        """Exchange a web oauth_token (from EmbeddedSetup cookie) for a master token.

        Returns the full gpsoauth response dict (which contains 'Token' and
        usually 'Email'), or None on failure.
        """
        android_id = f"{get_mac():x}"
        try:
            resp = gpsoauth.exchange_token(email, oauth_token, android_id)
            if not resp.get("Token"):
                log.error("Token exchange returned no Token: %s", resp)
                return None
            return resp
        except Exception as exc:
            log.error("Token exchange failed: %s", exc)
            return None

    def fetch_notes(self, force_resync: bool = False) -> list[KeepNote]:
        """Pull all non-trashed notes from Keep.

        gkeepapi's incremental sync (`keep.sync()`) is known to miss remote
        changes such as colour updates and list-item checked-state toggles
        because they can be applied without bumping the timestamps the
        delta-sync relies on. We therefore force a full resync
        (`keep.sync(True)`) to guarantee remote changes are picked up.
        """
        if not self._authenticated:
            return []
        with self._lock:
            try:
                # Always do a full resync to reliably catch remote edits
                # (colour changes, checked items, etc.) that incremental
                # sync sometimes drops.
                self.keep.sync(True)
            except TypeError:
                # Older gkeepapi versions don't accept the `resync` arg
                try:
                    self.keep.sync()
                except Exception as exc:
                    log.error("Keep sync error: %s", exc)
                    return []
            except Exception as exc:
                log.error("Keep sync error: %s", exc)
                return []

        results = []
        sort_idx = 0
        all_notes = list(self.keep.all())
        log.info("Fetched %d notes from Keep", len(all_notes))
        for note in all_notes:
            if note.trashed:
                continue
            if isinstance(note, gkeepapi.node.Note):
                results.append(KeepNote(
                    id=note.id,
                    title=note.title,
                    text=note.text,
                    color_hex=_color_to_hex(note.color),
                    pinned=note.pinned,
                    trashed=note.trashed,
                    sort_key=sort_idx,
                ))
            elif isinstance(note, gkeepapi.node.List):
                items = []
                for item in note.items:
                    items.append({
                        "text": item.text,
                        "checked": item.checked,
                    })
                # Build plain text representation
                lines = []
                for it in items:
                    mark = "☑" if it["checked"] else "☐"
                    lines.append(f"{mark} {it['text']}")
                text = "\n".join(lines)
                results.append(KeepNote(
                    id=note.id,
                    title=note.title,
                    text=text,
                    color_hex=_color_to_hex(note.color),
                    pinned=note.pinned,
                    trashed=note.trashed,
                    sort_key=sort_idx,
                    is_list=True,
                    list_items=items,
                ))
            else:
                continue
            sort_idx += 1
        return results

    def push_note(self, keep_note: KeepNote):
        """Push local changes for a single note back to Keep."""
        if not self._authenticated:
            return
        with self._lock:
            gn = self.keep.get(keep_note.id)
            if gn is None:
                # Create new note
                gn = self.keep.createNote(keep_note.title, keep_note.text)
                keep_note.id = gn.id
            elif isinstance(gn, gkeepapi.node.List):
                gn.title = keep_note.title
                # Update list items from parsed text
                if keep_note.list_items:
                    existing = list(gn.items)
                    for i, item_data in enumerate(keep_note.list_items):
                        if i < len(existing):
                            existing[i].text = item_data["text"]
                            existing[i].checked = item_data["checked"]
                        else:
                            gn.add(item_data["text"], item_data["checked"])
                    # Remove extra items
                    for j in range(len(keep_note.list_items), len(existing)):
                        existing[j].delete()
            else:
                gn.title = keep_note.title
                gn.text = keep_note.text
            gn.color = _hex_to_keep_color(keep_note.color_hex)
            gn.pinned = keep_note.pinned
            try:
                self.keep.sync()
            except Exception as exc:
                log.error("Error pushing note %s: %s", keep_note.id, exc)

    def create_note(self, title="", text="", color_hex="#FFF475") -> KeepNote | None:
        """Create a new note on Keep and return its mirror."""
        if not self._authenticated:
            return None
        with self._lock:
            gn = self.keep.createNote(title, text)
            gn.color = _hex_to_keep_color(color_hex)
            try:
                self.keep.sync()
            except Exception as exc:
                log.error("Error creating note: %s", exc)
                return None
            return KeepNote(
                id=gn.id,
                title=gn.title,
                text=gn.text,
                color_hex=color_hex,
            )

    def delete_note(self, note_id: str):
        """Trash a note on Keep."""
        if not self._authenticated:
            return
        with self._lock:
            gn = self.keep.get(note_id)
            if gn:
                gn.trash()
                try:
                    self.keep.sync()
                except Exception as exc:
                    log.error("Error deleting note %s: %s", note_id, exc)
