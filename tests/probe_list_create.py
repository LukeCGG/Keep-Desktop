"""Probe: LIST creation via cbx ops."""
from __future__ import annotations
import argparse, os, sys, time
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from PySide6.QtGui import QGuiApplication  # noqa
from config import load_config  # noqa
from keep_sync_v2 import KeepSyncV2  # noqa


def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument("--keep", action="store_true")
    args = p.parse_args()
    _ = QGuiApplication.instance() or QGuiApplication([])
    cfg = load_config()
    sync = KeepSyncV2()
    if not sync.login(cfg["email"]):
        print("login failed"); return 2

    ts = datetime.now().strftime("%H:%M:%S")
    title = f"[KD-PROBE] list {ts}"
    items = [
        {"text": "milk", "checked": False},
        {"text": "bread", "checked": False},
        {"text": "eggs", "checked": False},
    ]
    print(f"[probe] create LIST title={title!r} items={[i['text'] for i in items]}")
    kn = sync.create_note(title=title, is_list=True, list_items=items,
                          color_hex="#CCFF90")
    if kn is None or not kn.id:
        print("create_note failed"); return 1
    nid = kn.id
    print(f"[probe] id={nid}")

    def refetch():
        for _ in range(5):
            time.sleep(0.6)
            for n in sync.fetch_notes(force_resync=True):
                if n.id == nid: return n
        return None

    srv = refetch()
    if srv is None:
        print("not visible"); return 1
    proto = sync._client.notes.get(nid)
    print(f"[probe] post: ktype={proto.type if proto else '?'}, "
          f"is_list={srv.is_list}, sct_id={(proto.sct_id if proto else None)!r}")
    print(f"[probe] items: {srv.list_items}")

    failures = []
    if not srv.is_list:
        failures.append(f"is_list={srv.is_list} (expected True)")
    got_texts = [it.get("text") for it in (srv.list_items or [])]
    want_texts = [i["text"] for i in items]
    if sorted(got_texts) != sorted(want_texts):
        failures.append(f"items: got {got_texts}, want {want_texts}")
    got_checked = {it.get("text"): it.get("checked") for it in (srv.list_items or [])}
    for i in items:
        if got_checked.get(i["text"]) != i["checked"]:
            failures.append(f"{i['text']!r} checked={got_checked.get(i['text'])} "
                            f"want={i['checked']}")

    if not args.keep:
        try: sync.delete_note(nid); print(f"[probe] trashed {nid[:8]}")
        except Exception as e: print(f"trash failed: {e}")

    if failures:
        print("\nFAILED:"); [print(f"  - {f}") for f in failures]; return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
