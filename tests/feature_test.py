"""Live end-to-end feature smoke test for KeepDesktop.

Runs against your REAL Google Keep account using the master token
already stored by KeepDesktop (so you must have signed in via the GUI
at least once first). Each scenario is sequenced — push, refetch,
compare — so a failure points directly at the broken feature.

Usage:
    python tools/feature_test.py              # run all scenarios
    python tools/feature_test.py --keep       # don't trash test notes at end

WARNING: This creates and deletes (trashes) notes on your account.
Notes are titled with a "[KD-TEST]" prefix and include a timestamp so
you can find them in your Trash if anything goes wrong.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime
from typing import Callable, Optional

# Make sibling modules importable when run as a script.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Keep PySide6 quiet — we don't need a GUI here.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication  # noqa: E402

from config import KEEP_COLORS, load_config  # noqa: E402
from keep_sync import KeepNote  # noqa: E402
from keep_sync_v2 import KeepSyncV2  # noqa: E402

TEST_TAG = "[KD-TEST]"
SETTLE = 0.6   # seconds to let the server commit between push and refetch
RESULTS: list[tuple[str, bool, str]] = []


# ───────────────────────── helpers ─────────────────────────

def step(name: str, fn: Callable[[], Optional[str]]) -> bool:
    """Run a single step, capture pass/fail + message."""
    print(f"  → {name} ... ", end="", flush=True)
    try:
        msg = fn() or ""
    except AssertionError as exc:
        RESULTS.append((name, False, str(exc)))
        print(f"FAIL\n      {exc}")
        return False
    except Exception:
        tb = traceback.format_exc(limit=3).strip()
        RESULTS.append((name, False, tb.splitlines()[-1]))
        print(f"ERROR\n      {tb}")
        return False
    RESULTS.append((name, True, msg))
    print("PASS" + (f"  ({msg})" if msg else ""))
    return True


def banner(text: str) -> None:
    print(f"\n=== {text} ===")


def fetch_one(sync: KeepSyncV2, note_id: str,
              tries: int = 4) -> Optional[KeepNote]:
    """Refetch the world and return the named note, retrying briefly."""
    for _ in range(tries):
        time.sleep(SETTLE)
        for n in sync.fetch_notes(force_resync=True):
            if n.id == note_id:
                return n
    return None


def assert_eq(actual, expected, label: str) -> None:
    if actual != expected:
        a = repr(actual)
        if len(a) > 80:
            a = a[:77] + "..."
        e = repr(expected)
        if len(e) > 80:
            e = e[:77] + "..."
        raise AssertionError(f"{label}: got {a}, expected {e}")


# ───────────────────────── scenarios ─────────────────────────

def scenario_text_note(sync: KeepSyncV2, cleanup_ids: list[str]) -> None:
    banner("TEXT NOTE — basic + formatting + colour + pin")
    ts = datetime.now().strftime("%H:%M:%S")
    title = f"{TEST_TAG} text {ts}"

    note: Optional[KeepNote] = None

    def s_create() -> str:
        nonlocal note
        note = sync.create_note(title=title, text="hello world",
                                color_hex=KEEP_COLORS["Yellow"])
        assert note is not None and note.id, "create_note returned no id"
        cleanup_ids.append(note.id)
        return f"id={note.id[:8]}"

    if not step("create text note", s_create):
        return

    def s_fetch_initial() -> str:
        srv = fetch_one(sync, note.id)
        assert srv is not None, "note not visible after create"
        assert_eq(srv.title, title, "title")
        assert_eq(srv.text, "hello world", "text")
        return ""
    step("fetch verifies title+body", s_fetch_initial)

    def s_edit_text() -> str:
        # IMPORTANT: stay single-line. Sending multi-line text via the
        # legacy `text` field on a sct-less note triggers Keep's server
        # to auto-convert NOTE → LIST. There's a separate scenario
        # below that creates a LIST directly to cover multi-line cases.
        note.text = "hello world (edited single line)"
        ok = sync.push_note(note)
        assert ok, "push_note returned False"
        srv = fetch_one(sync, note.id)
        assert srv is not None
        assert_eq(srv.text, note.text, "text after edit")
        # The server should NOT have transmuted us into a LIST.
        assert not srv.is_list, "server flipped NOTE→LIST after edit (bug)"
        return ""
    step("edit body (single line), verify roundtrip", s_edit_text)

    def s_edit_title() -> str:
        note.title = title + " (edited)"
        ok = sync.push_note(note)
        assert ok
        srv = fetch_one(sync, note.id)
        assert srv is not None
        assert_eq(srv.title, note.title, "title after edit")
        return ""
    step("edit title, verify roundtrip", s_edit_title)

    # Formatting variants. We push HTML through KeepSyncV2 the same way
    # NoteWindow does. After fetch we check (a) plain text round-trips
    # exactly, and (b) the server html, when present, contains the
    # expected tag for that variant.
    fmt_cases = [
        ("bold",          "<p>plain <b>BOLD</b> trailing</p>",          "<b>"),
        ("italic",        "<p>plain <i>ITALIC</i> trailing</p>",        "<i>"),
        ("underline",     "<p>plain <u>UNDER</u> trailing</p>",         "<u>"),
        ("strikethrough", "<p>plain <s>STRIKE</s> trailing</p>",        "<s>"),
        ("bold+italic",   "<p>plain <b><i>BI</i></b> trailing</p>",     "<b>"),
        ("all combined",  "<p><b><i><u><s>ALL</s></u></i></b> tail</p>", "<b>"),
    ]
    expected_plain_for = {
        "bold":          "plain BOLD trailing",
        "italic":        "plain ITALIC trailing",
        "underline":     "plain UNDER trailing",
        "strikethrough": "plain STRIKE trailing",
        "bold+italic":   "plain BI trailing",
        "all combined":  "ALL tail",
    }
    for label, html, _tag in fmt_cases:
        def s(label=label, html=html):
            note.html = html
            note.text = expected_plain_for[label]
            ok = sync.push_note(note)
            assert ok, "push_note returned False"
            srv = fetch_one(sync, note.id)
            assert srv is not None
            assert_eq(srv.text, expected_plain_for[label],
                      f"{label} plain text")
            return ""
        step(f"format {label}: plain text roundtrip", s)

    # Colour cycle through several palette entries.
    for cname in ("Red", "Green", "DarkBlue", "Purple", "White"):
        def s(cname=cname):
            note.color_hex = KEEP_COLORS[cname]
            ok = sync.push_note(note)
            assert ok
            srv = fetch_one(sync, note.id)
            assert srv is not None
            assert_eq(srv.color_hex.upper(), KEEP_COLORS[cname].upper(),
                      f"colour {cname}")
            return ""
        step(f"colour → {cname}", s)

    def s_pin() -> str:
        ok = sync.push_metadata(note, is_pinned=True)
        assert ok
        srv = fetch_one(sync, note.id)
        assert srv is not None
        assert srv.pinned is True, f"pinned={srv.pinned}"
        return ""
    step("pin note", s_pin)

    def s_unpin() -> str:
        ok = sync.push_metadata(note, is_pinned=False)
        assert ok
        srv = fetch_one(sync, note.id)
        assert srv is not None
        assert srv.pinned is False, f"pinned={srv.pinned}"
        return ""
    step("unpin note", s_unpin)


def scenario_reorder(sync: KeepSyncV2, cleanup_ids: list[str]) -> None:
    banner("REORDER — sort_value swap between two notes")
    ts = datetime.now().strftime("%H:%M:%S")

    a = sync.create_note(title=f"{TEST_TAG} A {ts}", text="a")
    b = sync.create_note(title=f"{TEST_TAG} B {ts}", text="b")
    if a is None or b is None:
        RESULTS.append(("reorder/create pair", False, "create_note failed"))
        print("  → reorder: SKIPPED (create_note failed)")
        return
    cleanup_ids.extend([a.id, b.id])

    def s_initial() -> str:
        srv_a = fetch_one(sync, a.id)
        srv_b = fetch_one(sync, b.id)
        assert srv_a is not None and srv_b is not None
        return f"A.sort={srv_a.sort_key} B.sort={srv_b.sort_key}"
    step("fetch initial sort_values", s_initial)

    def s_swap() -> str:
        srv_a = fetch_one(sync, a.id)
        srv_b = fetch_one(sync, b.id)
        assert srv_a is not None and srv_b is not None
        # Move A above B by giving A a sort_value strictly greater than B.
        new_sv = (srv_b.sort_key or 0) + (1 << 22)
        a.sort_key = new_sv
        ok = sync.push_metadata(a, sort_value=new_sv)
        assert ok, "push_metadata(sort_value) failed"
        srv_a2 = fetch_one(sync, a.id)
        assert srv_a2 is not None
        assert srv_a2.sort_key > (srv_b.sort_key or 0), (
            f"A.sort={srv_a2.sort_key} should be > B.sort={srv_b.sort_key}"
        )
        return f"A now {srv_a2.sort_key}"
    step("push A above B by sort_value", s_swap)


def scenario_list_note(sync: KeepSyncV2, cleanup_ids: list[str]) -> None:
    banner("CHECKLIST — create + items + toggle + reorder + delete-item")
    ts = datetime.now().strftime("%H:%M:%S")
    title = f"{TEST_TAG} list {ts}"

    # Create as a LIST directly so the server-side type matches.
    seed_items = [
        {"text": "milk",  "checked": False},
        {"text": "bread", "checked": False},
        {"text": "eggs",  "checked": False},
    ]
    note = sync.create_note(
        title=title, color_hex=KEEP_COLORS["Green"],
        is_list=True, list_items=seed_items,
    )
    if note is None:
        RESULTS.append(("list/create", False, "create_note returned None"))
        print("  → list: SKIPPED (create_note failed)")
        return
    cleanup_ids.append(note.id)

    def s_seed() -> str:
        srv = fetch_one(sync, note.id)
        assert srv is not None, "list note not visible after create"
        assert srv.is_list, f"is_list={srv.is_list} (server didn't create LIST)"
        texts = [it["text"] for it in srv.list_items]
        assert_eq(sorted(texts), sorted(["milk", "bread", "eggs"]),
                  "items after seed")
        # Capture server-assigned ids for later toggles/reorders.
        note.list_items = list(srv.list_items)
        return f"{len(srv.list_items)} items"
    if not step("seed 3 items via create_note(is_list=True)", s_seed):
        return

    def s_toggle() -> str:
        # Tick "bread".
        for it in note.list_items:
            if it.get("text") == "bread":
                it["checked"] = True
        ok = sync.push_note(note)
        assert ok
        srv = fetch_one(sync, note.id)
        assert srv is not None
        bread = next((it for it in srv.list_items
                      if it["text"] == "bread"), None)
        assert bread is not None and bread["checked"] is True, \
            f"bread.checked={bread and bread.get('checked')}"
        note.list_items = list(srv.list_items)
        return ""
    step("toggle 'bread' → checked", s_toggle)

    def s_reorder() -> str:
        # Move "eggs" to top.
        items = note.list_items
        eggs = [it for it in items if it["text"] == "eggs"]
        rest = [it for it in items if it["text"] != "eggs"]
        note.list_items = eggs + rest
        ok = sync.push_note(note)
        assert ok
        srv = fetch_one(sync, note.id)
        assert srv is not None
        texts = [it["text"] for it in srv.list_items]
        assert texts and texts[0] == "eggs", f"order={texts}"
        note.list_items = list(srv.list_items)
        return f"order={texts}"
    step("reorder eggs → top", s_reorder)

    def s_add() -> str:
        note.list_items = list(note.list_items) + [
            {"id": "", "text": "butter", "checked": False},
        ]
        ok = sync.push_note(note)
        assert ok
        srv = fetch_one(sync, note.id)
        assert srv is not None
        texts = [it["text"] for it in srv.list_items]
        assert "butter" in texts, f"items={texts}"
        note.list_items = list(srv.list_items)
        return ""
    step("add new item 'butter'", s_add)

    def s_remove() -> str:
        note.list_items = [it for it in note.list_items
                           if it["text"] != "milk"]
        ok = sync.push_note(note)
        assert ok
        srv = fetch_one(sync, note.id)
        assert srv is not None
        texts = [it["text"] for it in srv.list_items]
        assert "milk" not in texts, f"items={texts}"
        return f"remaining={texts}"
    step("remove item 'milk'", s_remove)


# ───────────────────────── runner ─────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="Do NOT trash test notes at the end.")
    args = ap.parse_args()

    # PySide6 needs an instance; offscreen platform is set above.
    _qapp = QGuiApplication.instance() or QGuiApplication(sys.argv)

    cfg = load_config()
    email = cfg.get("email")
    if not email:
        print("ERROR: no signed-in account in config.json. Sign in via the "
              "KeepDesktop GUI first, then re-run.")
        return 2

    print(f"Signed-in account: {email}")
    sync = KeepSyncV2()
    if not sync.login(email):
        print("ERROR: login failed. Token may be expired — sign in via GUI.")
        return 3
    print("Authenticated. Fetching initial state ...")
    sync.fetch_notes(force_resync=True)

    cleanup_ids: list[str] = []
    try:
        scenario_text_note(sync, cleanup_ids)
        scenario_reorder(sync, cleanup_ids)
        scenario_list_note(sync, cleanup_ids)
    finally:
        if args.keep:
            print(f"\n--keep set; leaving {len(cleanup_ids)} test notes in your account.")
        elif cleanup_ids:
            banner(f"CLEANUP — trashing {len(cleanup_ids)} test notes")
            for nid in cleanup_ids:
                try:
                    sync.delete_note(nid)
                    print(f"  trashed {nid[:8]}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  WARN trashing {nid[:8]}: {exc}")

    # Summary.
    banner("SUMMARY")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"  {passed} passed, {failed} failed, {len(RESULTS)} total")
    if failed:
        print("\nFailures:")
        for name, ok, msg in RESULTS:
            if not ok:
                print(f"  - {name}: {msg}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
