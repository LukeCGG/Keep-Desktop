"""Probe: LIST reorder + delete after creation.

Creates a 3-item LIST, reorders eggs to top, then removes milk.
Trashes the note at the end.
"""
from __future__ import annotations
import os, sys, time
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtGui import QGuiApplication  # noqa
from config import load_config  # noqa
from keep_sync_v2 import KeepSyncV2  # noqa


def fetch(sync, note_id, tries=4):
    for _ in range(tries):
        time.sleep(0.5)
        for n in sync.fetch_notes(force_resync=True):
            if n.id == note_id:
                return n
    return None


def main():
    _ = QGuiApplication.instance() or QGuiApplication([])
    cfg = load_config()
    sync = KeepSyncV2()
    if not sync.login(cfg["email"]):
        print("login failed"); return 2

    ts = datetime.now().strftime("%H:%M:%S")
    title = f"[KD-PROBE-MUT] {ts}"
    seed = [
        {"text": "milk",  "checked": False},
        {"text": "bread", "checked": False},
        {"text": "eggs",  "checked": False},
    ]
    print(f"[probe] create LIST title={title!r}")
    kn = sync.create_note(title=title, is_list=True, list_items=seed,
                          color_hex="#CCFF90")
    if not kn or not kn.id:
        print("create_note failed"); return 1
    print(f"[probe] id={kn.id[:8]}")

    srv = fetch(sync, kn.id)
    print(f"[probe] after seed: {[it['text'] for it in srv.list_items]}")
    kn.list_items = list(srv.list_items)

    # --- reorder eggs → top ---
    eggs = [it for it in kn.list_items if it["text"] == "eggs"]
    rest = [it for it in kn.list_items if it["text"] != "eggs"]
    kn.list_items = eggs + rest
    print(f"[probe] requesting reorder: {[it['text'] for it in kn.list_items]}")
    ok = sync.push_note(kn)
    print(f"[probe] reorder push ok={ok}")
    srv = fetch(sync, kn.id)
    after = [it["text"] for it in srv.list_items]
    print(f"[probe] after reorder: {after}")
    kn.list_items = list(srv.list_items)

    # --- remove milk ---
    kn.list_items = [it for it in kn.list_items if it["text"] != "milk"]
    print(f"[probe] requesting remove: {[it['text'] for it in kn.list_items]}")
    ok = sync.push_note(kn)
    print(f"[probe] remove push ok={ok}")
    srv = fetch(sync, kn.id)
    after_rm = [it["text"] for it in srv.list_items]
    print(f"[probe] after remove: {after_rm}")

    # cleanup
    server = sync._client.notes.get(kn.id)
    if server:
        sync._client.trash_note(server)
        print(f"[probe] trashed {kn.id[:8]}")

    fail = []
    if after and after[0] != "eggs":
        fail.append(f"reorder failed: {after}")
    if "milk" in after_rm:
        fail.append(f"remove failed: {after_rm}")
    if fail:
        print("\nFAILED:")
        for f in fail: print("  -", f)
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
