"""Read-only check: confirm Keep's text positions really are UTF-16.

SETTLED: they are. A note edited from the desktop came back with its
emoji replaced by two "?" boxes and the newly typed text stranded
between them — the signature of an insert landing between the two
halves of a surrogate pair. The encoder and decoder now convert
positions accordingly (see nested_model's UTF-16 helpers).

This script stays as a verification tool: run it after syncing a note
that contains an emoji plus a heading or some bold text, and it should
report UTF-16 with no codepoint signals. A CODEPOINT verdict would mean
the conversion has regressed or Keep changed its model.

Why it matters
--------------
Every op we send carries a character position: `is` has `ibi`, `ds` and
`as` have `si`/`ei`, and a heading anchor sits at its paragraph's
terminating newline. Python counts a character as one codepoint.
JavaScript, Java and Objective-C — the three languages Keep's own web,
Android and iOS clients are written in — count strings in UTF-16 code
units, where any character outside the Basic Multilingual Plane takes
TWO. Emoji are the everyday case; so are some CJK extension and maths
characters.

If the server counts UTF-16 and we send codepoint offsets, then for a
note containing an emoji every position after it is short by one per
emoji: text inserts land one character early and bold/italic ranges are
applied one character off. Notes with no emoji are unaffected, because
the two conventions agree exactly across the BMP.

This script does not write anything. It syncs, then looks for a note
that can tell the two apart and reports which convention the server's
own snapshot uses.

Usage
-----
    python tools/check_position_encoding.py

It reuses the token already stored by the app, so sign in through
KeepDesktop first.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_token  # noqa: E402
from keep_protocol.auth import _get_active_email  # noqa: E402
from keep_sync_v2 import KeepSyncV2  # noqa: E402


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def has_astral(text: str) -> bool:
    return any(ord(ch) > 0xFFFF for ch in text)


def analyse(note) -> list[str]:
    """Compare each paragraph anchor / style range in the note's raw
    snapshot against both conventions. Returns a list of verdict lines."""
    findings: list[str] = []
    chunks = note.serialized_chunks or []
    ops: list = []
    for chunk in chunks:
        try:
            parsed = json.loads(chunk)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, list) and parsed:
            if isinstance(parsed[0], str):
                ops.append(parsed)
            else:
                ops.extend(parsed)

    # Reconstruct the inserted text (compacted snapshots use one `is`).
    text = ""
    for op in ops:
        if (isinstance(op, list) and len(op) >= 3
                and op[0] == "docs-nestedModel"
                and isinstance(op[2], dict) and op[2].get("ty") == "is"):
            text = str(op[2].get("s", ""))
            break
    if not text or not has_astral(text):
        return findings

    astral_before = lambda idx: sum(  # noqa: E731
        1 for ch in text[:idx] if ord(ch) > 0xFFFF)

    for op in ops:
        if not (isinstance(op, list) and len(op) >= 3
                and op[0] == "docs-nestedModel"
                and isinstance(op[2], dict) and op[2].get("ty") == "as"):
            continue
        body = op[2]
        si = int(body.get("si", 0))
        if body.get("st") == "paragraph":
            # The anchor sits on a terminating newline. Find which
            # newline it is under each convention.
            cp_positions = [i + 1 for i, ch in enumerate(text) if ch == "\n"]
            u16_positions = [utf16_len(text[:i]) + 1
                             for i, ch in enumerate(text) if ch == "\n"]
            cp_hit = si in cp_positions
            u16_hit = si in u16_positions
            if cp_hit != u16_hit:
                findings.append(
                    "  heading anchor si=%d -> %s" % (
                        si, "CODEPOINT" if cp_hit else "UTF-16"))
        elif body.get("st") == "text":
            ei = int(body.get("ei", si))
            cp_slice = text[si - 1:ei]
            u = text.encode("utf-16-le")
            u16_slice = u[(si - 1) * 2:ei * 2].decode("utf-16-le", "replace")
            if cp_slice != u16_slice and astral_before(si - 1):
                findings.append(
                    "  style range si=%d ei=%d -> codepoint reading %r "
                    "vs UTF-16 reading %r" % (si, ei, cp_slice, u16_slice))
    return findings


def main() -> int:
    token = load_token()
    if not token:
        print("No stored token. Sign in through KeepDesktop first.")
        return 2
    email = _get_active_email() or ""
    sync = KeepSyncV2()
    if not sync.login(email, master_token=token):
        print("Could not authenticate with the stored token.")
        return 2

    print("Syncing (read-only)...")
    sync.fetch_notes()
    notes = list(sync._server_notes.values())
    print("fetched %d notes\n" % len(notes))

    candidates = 0
    verdicts: list[str] = []
    for note in notes:
        if note.type != "NOTE":
            continue
        body = note.indexable_text or ""
        if not has_astral(body):
            continue
        candidates += 1
        print("candidate note %s (%d chars, %d UTF-16 units)"
              % (note.id[:12], len(body), utf16_len(body)))
        found = analyse(note)
        if found:
            verdicts.extend(found)
            for line in found:
                print(line)
        else:
            print("  no distinguishing anchor or style range in this note")

    print()
    if not candidates:
        print("INCONCLUSIVE - none of your notes contain a non-BMP character.")
        print()
        print("To settle it, create a note in Keep ON THE WEB containing:")
        print("    Line one with an emoji \\U0001F600 in it")
        print("    A second line")
        print("then make the FIRST line a heading and bold a word AFTER the")
        print("emoji, let it save, and run this script again.")
        return 1
    if not verdicts:
        print("INCONCLUSIVE - found %d note(s) with non-BMP characters, but"
              % candidates)
        print("none carried a heading or style range positioned AFTER one.")
        print("Add a heading and some bold text after the emoji, then re-run.")
        return 1

    utf16_votes = sum(1 for v in verdicts if "UTF-16" in v)
    cp_votes = sum(1 for v in verdicts if "CODEPOINT" in v)
    print("VERDICT: %d signal(s) point at UTF-16, %d at codepoints."
          % (utf16_votes, cp_votes))
    if cp_votes:
        print()
        print("UNEXPECTED: a codepoint signal means the UTF-16 conversion in")
        print("nested_model has regressed, or Keep changed its model.")
        return 1
    print("Matches what the encoder/decoder now assume. Nothing to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
