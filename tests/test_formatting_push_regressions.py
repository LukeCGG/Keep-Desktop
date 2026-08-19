"""Regressions for formatting changes silently not reaching Keep web.

Each test here corresponds to a way `encode_text_diff` used to emit a
push that the LOCAL editor and the SERVER disagreed about — the app
looked right until the next pull overwrote it with the server's copy,
which is what "my formatting keeps reverting" actually was.

The `apply_ops` helper models the server the way the captured snapshots
in archive/keep-protocol-dev/sessions/_server_dumps/*.json show it
behaving — in particular that paragraph/heading state is anchored to a
paragraph's terminating newline and RIDES ALONG with that character
through inserts and deletes. The app's own `_decode_ops` cannot stand in
for this: it only ever reads compacted snapshots, where every op is
already expressed in final coordinates, so it never has to move an
anchor and would happily "pass" the merge case below.
"""

from __future__ import annotations

import difflib

import pytest

from keep_protocol.nested_model import (
    Paragraph,
    StyleRun,
    StyledDoc,
    _align_paragraphs,
    _predict_server_headings,
    _styles_equal,
    coalesce_runs,
    encode_text_diff,
    styled_doc_from_dict,
)

SCT = "sct.test"

_TEXT_KEYS = {"ts_bd": "bold", "ts_it": "italic",
              "ts_un": "underline", "ts_st": "strikethrough"}


def apply_ops(doc: StyledDoc, ops: list) -> StyledDoc:
    """Apply `ops` to `doc` the way Keep's server model does."""
    chars: list[str] = []
    styles: list[dict] = []
    anchors: dict[int, dict] = {}
    for i, p in enumerate(doc.paragraphs):
        if i:
            chars.append("\n")
            styles.append({})
        for run in p.runs:
            for ch in run.text:
                chars.append(ch)
                styles.append({
                    "bold": run.bold, "italic": run.italic,
                    "underline": run.underline,
                    "strikethrough": run.strikethrough,
                })
    pos = 0
    for p in doc.paragraphs:
        pos += len(p.text)
        if p.heading:
            anchors[pos + 1] = {"ps_hd": p.heading,
                                "ps_hdid": p.heading_id or ""}
        pos += 1

    for op in ops:
        body = op[2]
        ty = body.get("ty")
        if ty == "is":
            ibi, s = int(body["ibi"]), str(body["s"])
            for k, ch in enumerate(s):
                chars.insert(ibi - 1 + k, ch)
                styles.insert(ibi - 1 + k, {})
            anchors = {(k + len(s) if k >= ibi else k): v
                       for k, v in anchors.items()}
        elif ty == "ds":
            si, ei = int(body["si"]), int(body["ei"])
            n = ei - si + 1
            del chars[si - 1:ei]
            del styles[si - 1:ei]
            anchors = {(k - n if k > ei else k): v
                       for k, v in anchors.items() if not si <= k <= ei}
        elif ty == "as":
            si, ei = int(body["si"]), int(body["ei"])
            sm = body.get("sm") or {}
            if body.get("st") == "text":
                st = {_TEXT_KEYS[k]: bool(v) for k, v in sm.items()
                      if k in _TEXT_KEYS}
                for i in range(max(0, si - 1), min(len(chars), ei)):
                    styles[i] = dict(st)
            elif sm.get("ps_hd"):
                anchors[si] = sm
            else:
                anchors.pop(si, None)

    out = StyledDoc(sct_id=doc.sct_id)
    runs: list[StyleRun] = []
    cur: StyleRun | None = None

    def close(anchor: int) -> None:
        nonlocal runs, cur
        if cur and cur.text:
            runs.append(cur)
        cur = None
        p = Paragraph(runs=runs)
        sm = anchors.get(anchor)
        if sm and isinstance(sm.get("ps_hd"), int):
            p.heading = sm["ps_hd"]
        out.paragraphs.append(p)
        runs = []

    for i, ch in enumerate(chars):
        if ch == "\n":
            close(i + 1)
            continue
        s = styles[i] or {}
        r = StyleRun(text=ch, bold=bool(s.get("bold")),
                     italic=bool(s.get("italic")),
                     underline=bool(s.get("underline")),
                     strikethrough=bool(s.get("strikethrough")))
        if cur is None or cur.style_tuple() != r.style_tuple():
            if cur and cur.text:
                runs.append(cur)
            cur = r
        else:
            cur.text += ch
    close(len(chars) + 1)
    return out


def shape(doc: StyledDoc) -> list:
    """Segmentation-independent (heading, [(text, style)]) per paragraph."""
    coalesce_runs(doc)
    return [(p.heading, [(r.text, r.style_tuple()) for r in p.runs if r.text])
            for p in doc.paragraphs]


def push(old: StyledDoc, new: StyledDoc) -> StyledDoc:
    """Encode old->new, apply it server-side, return the server result."""
    old.sct_id = new.sct_id = SCT
    return apply_ops(old, encode_text_diff(old, new))


def para(text="", heading=0, **style) -> Paragraph:
    return Paragraph(runs=[StyleRun(text=text, **style)] if text else [],
                     heading=heading)


def test_apply_ops_matches_a_captured_server_snapshot():
    """Guard the harness itself against the real wire format: the ops
    below are lifted verbatim from fmt_headings.json's serializedChunks
    (one `is` plus two paragraph anchors at the newline positions)."""
    target = ["text", 1, SCT]
    ops = [
        ["docs-nestedModel", target,
         {"ibi": 1, "s": "one\ntwo\nthree", "ty": "is"}],
        ["docs-nestedModel", target,
         {"st": "paragraph", "ei": 4, "ty": "as", "si": 4,
          "sm": {"ps_hdid": "h.bffgdhh67a1o", "ps_hd": 1}}],
        ["docs-nestedModel", target,
         {"st": "paragraph", "ei": 8, "ty": "as", "si": 8,
          "sm": {"ps_hdid": "h.s9ju7equ6euq", "ps_hd": 2}}],
    ]
    got = apply_ops(StyledDoc(sct_id=SCT, paragraphs=[]), ops)
    assert got.plain_text == "one\ntwo\nthree"
    assert [p.heading for p in got.paragraphs] == [1, 2, 0]


# --------------------------------------------------------------------
# 1. Style change + text change to the SAME paragraph in one push
# --------------------------------------------------------------------

def test_bold_survives_when_same_paragraph_text_also_changed():
    """Bold a word, keep typing; autosave batches both into one push.

    Old paragraphs were matched to new ones by EXACT TEXT, so a
    paragraph whose text had just been edited matched nothing; run
    styling then fell back to "only emit for runs overlapping the text
    diff", and the bold — nowhere near the typed character — was
    dropped from the push entirely.
    """
    old = StyledDoc(paragraphs=[para("hello world")])
    new = StyledDoc(paragraphs=[Paragraph(runs=[
        StyleRun(text="hello", bold=True), StyleRun(text=" world!")])])
    assert shape(push(old, new)) == shape(new)


def test_heading_clear_survives_when_its_text_also_changed():
    """Clearing a heading on a line you also typed into must be sent."""
    old = StyledDoc(paragraphs=[para("Title", heading=1), para("body")])
    new = StyledDoc(paragraphs=[para("TitleX", heading=0), para("body")])
    assert shape(push(old, new)) == shape(new)


def test_heading_level_change_survives_alongside_a_text_edit():
    old = StyledDoc(paragraphs=[para("Title", heading=1), para("body")])
    new = StyledDoc(paragraphs=[para("TitleX", heading=2), para("body")])
    assert shape(push(old, new)) == shape(new)


def test_italic_survives_alongside_edit_in_another_paragraph():
    old = StyledDoc(paragraphs=[para("one"), para("two")])
    new = StyledDoc(paragraphs=[
        para("one!"),
        Paragraph(runs=[StyleRun(text="two", italic=True)]),
    ])
    assert shape(push(old, new)) == shape(new)


def test_unbolding_survives_when_paragraph_text_also_changed():
    old = StyledDoc(paragraphs=[Paragraph(runs=[
        StyleRun(text="hello", bold=True), StyleRun(text=" world")])])
    new = StyledDoc(paragraphs=[para("hello world!")])
    assert shape(push(old, new)) == shape(new)


# --------------------------------------------------------------------
# 2. Heading inherited through a paragraph merge
# --------------------------------------------------------------------

def test_merging_body_into_heading_does_not_inherit_stale_heading():
    """Backspacing two paragraphs together deletes the FIRST one's
    newline, so the merged paragraph ends at the SECOND one's
    terminator — and inherits ITS heading. Qt keeps the first block's
    format, so the editor shows body text while the server shows H1
    unless we explicitly clear it."""
    old = StyledDoc(paragraphs=[para("one", heading=0), para("two", heading=1)])
    new = StyledDoc(paragraphs=[para("onetwo", heading=0)])
    assert shape(push(old, new)) == shape(new)


def test_merging_heading_into_body_keeps_first_headings_level():
    old = StyledDoc(paragraphs=[para("one", heading=2), para("two", heading=0)])
    new = StyledDoc(paragraphs=[para("onetwo", heading=2)])
    assert shape(push(old, new)) == shape(new)


def test_deleting_a_heading_paragraph_entirely_leaves_no_residue():
    old = StyledDoc(paragraphs=[
        para("keep"), para("Head", heading=1), para("tail")])
    new = StyledDoc(paragraphs=[para("keep"), para("tail")])
    assert shape(push(old, new)) == shape(new)


def test_predict_server_headings_reports_inherited_heading():
    old = StyledDoc(paragraphs=[para("one", heading=0), para("two", heading=1)])
    opcodes = difflib.SequenceMatcher(
        a=old.plain_text, b="onetwo", autojunk=False).get_opcodes()
    assert _predict_server_headings(old, "onetwo", opcodes) == [1]


def test_predict_server_headings_trailing_paragraph_uses_end_slot():
    old = StyledDoc(paragraphs=[para("body"), para("Last", heading=2)])
    opcodes = difflib.SequenceMatcher(
        a=old.plain_text, b=old.plain_text, autojunk=False).get_opcodes()
    assert _predict_server_headings(old, old.plain_text, opcodes) == [0, 2]


def test_predict_server_headings_unchanged_text_is_identity():
    old = StyledDoc(paragraphs=[
        para("a", heading=1), para("b"), para("c", heading=2)])
    opcodes = difflib.SequenceMatcher(
        a=old.plain_text, b=old.plain_text, autojunk=False).get_opcodes()
    assert _predict_server_headings(old, old.plain_text, opcodes) == [1, 0, 2]


# --------------------------------------------------------------------
# 3. Run segmentation must not count as a formatting change
# --------------------------------------------------------------------

def test_split_runs_with_identical_styling_are_not_a_change():
    """Qt splits fragments on attributes we don't model (font size), so
    a merged heading/body line arrives as two identically-styled runs.
    Treating that as a restyle made every sync tick push `as` ops — and
    a freshly minted ps_hdid — for a note nobody had edited."""
    server = StyledDoc(sct_id=SCT, paragraphs=[para("onea ", heading=2)])
    split = StyledDoc(sct_id=SCT, paragraphs=[Paragraph(
        runs=[StyleRun(text="one"), StyleRun(text="a ")], heading=2)])
    assert _styles_equal(server, split)
    assert encode_text_diff(server, split) == []


def test_genuinely_different_styling_is_still_a_change():
    server = StyledDoc(sct_id=SCT, paragraphs=[para("onea ")])
    restyled = StyledDoc(sct_id=SCT, paragraphs=[Paragraph(
        runs=[StyleRun(text="one", bold=True), StyleRun(text="a ")])])
    assert not _styles_equal(server, restyled)
    assert encode_text_diff(server, restyled)


def test_coalesce_runs_merges_only_identical_styling():
    doc = StyledDoc(paragraphs=[Paragraph(runs=[
        StyleRun(text="a"), StyleRun(text="b"),
        StyleRun(text="c", bold=True), StyleRun(text="d", bold=True),
        StyleRun(text="e"),
    ])])
    coalesce_runs(doc)
    assert [(r.text, r.bold) for r in doc.paragraphs[0].runs] == [
        ("ab", False), ("cd", True), ("e", False)]


def test_coalesce_runs_does_not_mutate_shared_runs():
    """_three_way_merge_styled hands back Paragraphs (and their runs) by
    reference from the docs it merged, so a run can be live in two
    StyledDocs at once; coalescing one must not corrupt the other."""
    shared = StyleRun(text="a")
    other = StyledDoc(paragraphs=[Paragraph(runs=[shared])])
    doc = StyledDoc(paragraphs=[Paragraph(runs=[shared, StyleRun(text="b")])])
    coalesce_runs(doc)
    assert shared.text == "a"
    assert other.paragraphs[0].runs[0].text == "a"
    assert doc.paragraphs[0].runs[0].text == "ab"


def test_coalesce_runs_drops_empty_runs():
    doc = StyledDoc(paragraphs=[Paragraph(runs=[
        StyleRun(text=""), StyleRun(text="x"), StyleRun(text="")])])
    coalesce_runs(doc)
    assert [r.text for r in doc.paragraphs[0].runs] == ["x"]


def test_cached_baseline_is_coalesced_on_restore():
    """Caches written before html_to_styled_doc coalesced its output
    hold split runs; restoring them as-is would push a no-op restyle
    for every cached note on the first sync after upgrading."""
    restored = styled_doc_from_dict({
        "sct_id": SCT, "revision": None,
        "paragraphs": [{"heading": 0, "heading_id": None, "runs": [
            {"text": "one", "bold": False, "italic": False,
             "underline": False, "strikethrough": False},
            {"text": "a ", "bold": False, "italic": False,
             "underline": False, "strikethrough": False},
        ]}],
    })
    assert [r.text for r in restored.paragraphs[0].runs] == ["onea "]


# --------------------------------------------------------------------
# 4. Paragraph alignment
# --------------------------------------------------------------------

def test_align_paragraphs_tracks_a_paragraph_through_a_text_edit():
    old = [para("Title", heading=1), para("body")]
    new = [para("TitleX", heading=1), para("body")]
    mapping = _align_paragraphs(old, new)
    assert mapping[0] is old[0]
    assert mapping[1] is old[1]


def test_align_paragraphs_survives_an_insertion_above():
    old = [para("a"), para("b")]
    new = [para("NEW"), para("a"), para("b")]
    mapping = _align_paragraphs(old, new)
    assert mapping[0] is None
    assert mapping[1] is old[0]
    assert mapping[2] is old[1]


def test_align_paragraphs_marks_extra_new_paragraphs_as_new():
    old = [para("a")]
    new = [para("x"), para("y")]
    mapping = _align_paragraphs(old, new)
    assert mapping[0] is old[0]
    assert mapping[1] is None


@pytest.mark.parametrize("old_paras,new_paras", [([], []), ([], [para("a")])])
def test_align_paragraphs_handles_empty_inputs(old_paras, new_paras):
    mapping = _align_paragraphs(old_paras, new_paras)
    assert all(v is None for v in mapping.values())
    assert len(mapping) == len(new_paras)

def test_heading_id_is_reused_when_its_paragraph_text_is_edited():
    """A heading's server-side identity (ps_hdid) must survive ordinary
    typing in that heading. Matching old paragraphs by exact text lost
    the counterpart the moment its text changed, so every autosave
    re-sent the heading under a brand-new id — churning its identity on
    essentially every keystroke batch."""
    old = StyledDoc(sct_id=SCT, paragraphs=[para("Title", heading=1)])
    old.paragraphs[0].heading_id = "h.original"
    new = StyledDoc(sct_id=SCT, paragraphs=[para("TitleX", heading=1)])
    heading_ops = [
        op[2] for op in encode_text_diff(old, new)
        if op[2].get("st") == "paragraph"
    ]
    assert heading_ops, "heading op must still be emitted"
    assert all(op["sm"]["ps_hdid"] == "h.original" for op in heading_ops)


def test_align_paragraphs_pairs_a_replaced_block_positionally():
    old = [para("aaa"), para("bbb")]
    new = [para("aaaX"), para("bbbY")]
    mapping = _align_paragraphs(old, new)
    assert mapping[0] is old[0]
    assert mapping[1] is old[1]

