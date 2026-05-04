"""Probe: prove our `create_note` bootstrap works against the real server.

Creates a NOTE with multi-line seed text, refetches, asserts the server
preserved type=NOTE (not auto-flipped to LIST), the sct anchor exists,
and the text round-trips intact. Then trashes the note unless --keep.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtGui import QGuiApplication  # noqa: E402

from config import load_config  # noqa: E402
from keep_sync_v2 import KeepSyncV2  # noqa: E402

TEST_TAG = "[KD-PROBE]"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true",
                        help="don't trash the test note at the end")
    args = parser.parse_args()

    _ = QGuiApplication.instance() or QGuiApplication([])
    cfg = load_config()
    email = cfg.get("email")
    if not email:
        print("ERR: no email in config. Sign in via the GUI first.")
        return 2
    sync = KeepSyncV2()
    if not sync.login(email):
        print("ERR: login failed (token may be expired)")
        return 2

    ts = datetime.now().strftime("%H:%M:%S")
    title = f"{TEST_TAG} bootstrap {ts}"
    body = "line1\nline2\nline3"

    print(f"[probe] creating fresh NOTE with multi-line body")
    keep_note = sync.create_note(title=title, text=body)
    if keep_note is None or not keep_note.id:
        print("ERR: create_note returned no id")
        return 1
    nid = keep_note.id
    print(f"[probe] id={nid}")

    def refetch():
        for _ in range(5):
            time.sleep(0.6)
            for n in sync.fetch_notes(force_resync=True):
                if n.id == nid:
                    return n
        return None

    server = refetch()
    if server is None:
        print("ERR: created note not visible after refetch")
        return 1
    proto = sync._client.notes.get(nid)
    print(f"[probe] post-create: ktype={proto.type if proto else '?'}, "
          f"is_list={server.is_list}, "
          f"sct_id={(proto.sct_id if proto else None)!r}, "
          f"chunks={len(proto.serialized_chunks or []) if proto else '?'}, "
          f"rev={proto.nested_revision if proto else '?'}")
    print(f"[probe] post-create text={server.text!r}")
    print(f"[probe] post-create title={server.title!r}")

    failures = []
    if proto is None or proto.type != "NOTE":
        failures.append(f"type={proto.type if proto else '?'} (expected NOTE)")
    if server.is_list:
        failures.append(f"is_list={server.is_list} (server promoted to LIST)")
    if not (proto and proto.sct_id):
        failures.append("sct_id missing -- bootstrap didn't happen")
    if server.text != body:
        failures.append(f"text mismatch: got {server.text!r}, "
                        f"expected {body!r}")
    if not (proto and (proto.serialized_chunks or [])):
        failures.append("no chunks returned")
    if server.title != title:
        failures.append(f"title mismatch: got {server.title!r}, "
                        f"expected {title!r}")

    if not args.keep:
        try:
            print(f"[probe] trashing {nid[:8]}")
            sync.delete_note(nid)
        except Exception as exc:
            print(f"WARN: trash failed: {exc!r}")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS: create_note bootstrap works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
