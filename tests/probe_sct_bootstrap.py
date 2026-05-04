"""Probe: NOTE that has no sct_id (legacy text-only) → push multi-line
text + colour change. Reproduces the production failure where the
sct bootstrap returned 400 and the colour change silently reverted.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtGui import QGuiApplication  # noqa: E402

from config import load_config  # noqa: E402
from keep_sync_v2 import KeepSyncV2  # noqa: E402
from keep_sync import KeepNote  # noqa: E402


def main() -> int:
    _ = QGuiApplication.instance() or QGuiApplication([])
    cfg = load_config()
    email = cfg.get("email")
    if not email:
        print("ERR: no email in config.")
        return 1
    sync = KeepSyncV2()
    if not sync.login(email):
        print("ERR: login failed.")
        return 1

    title = f"[KD-SCT-PROBE] {datetime.now():%H:%M:%S}"
    print(f"[probe] create legacy single-line NOTE title={title!r}")
    # Create via legacy path: minimal text, no sct.
    created = sync._client.create_note(title=title, text="seed", color="DEFAULT")
    nid = created.id
    print(f"[probe] id={nid[:8]}")
    sync.fetch_notes()

    # Now simulate the production scenario: user added a 2nd line AND
    # changed colour to White. Push.
    note = KeepNote(
        id=nid,
        title=title,
        text="seed\nsecond line",
        color_hex="#FFFFFF",
    )
    print("[probe] push multi-line + colour change ...")
    ok = sync.push_note(note)
    print(f"[probe] push ok={ok}")

    sync.fetch_notes()
    refetched = next((n for n in sync.fetch_notes() if n.id == nid), None)
    if refetched is None:
        print("[probe] FAIL: note vanished")
        return 1
    print(f"[probe] server text={refetched.text!r}")
    print(f"[probe] server color={refetched.color_hex!r}")
    text_ok = "second line" in (refetched.text or "")
    colour_ok = refetched.color_hex and refetched.color_hex.upper() == "#FFFFFF"

    # Trash
    try:
        sync._client.trash_note(nid)
        print(f"[probe] trashed {nid[:8]}")
    except Exception as exc:
        print(f"[probe] trash failed: {exc}")

    if text_ok and colour_ok:
        print("PASS")
        return 0
    print(f"FAIL  text_ok={text_ok} colour_ok={colour_ok}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
