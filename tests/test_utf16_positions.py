"""Wire positions are UTF-16 code units, not Python codepoints.

Keep's web client is JavaScript, Android is Java, iOS is
Objective-C/Swift — all three count strings in UTF-16 natively, so every
`ibi`/`si`/`ei` and every heading anchor on the wire is a code-unit
offset. Python counts codepoints. The two agree across the entire Basic
Multilingual Plane, which is why this went unnoticed: it only bites on
astral characters, and every emoji is one.

Sending a codepoint offset for a note containing an emoji puts each
later position one short per emoji, and an insert computed that way
lands BETWEEN the two halves of the emoji's surrogate pair. The note
comes back with the emoji replaced by two lone surrogates — two "?"
boxes — and the inserted text stranded between them. Reading is equally
affected: style ranges decoded from a snapshot land on the wrong
characters.

`apply_ops_u16` below models the server in code units, so a regression
reproduces the corruption instead of hiding it behind matching
Python-side arithmetic.
"""

from __future__ import annotations

import pytest

from keep_protocol.nested_model import (
    Paragraph,
    StyleRun,
    StyledDoc,
    cp_span_to_u16,
    cp_to_u16_pos,
    decode_chunks,
    encode_doc,
    encode_text_diff,
    str_to_u16_units,
    u16_len,
    u16_units_to_chars,
)

import json

SCT = "sct.u16"
GRIN = chr(0x1F600)
PARTY = chr(0x1F389)

_TEXT_KEYS = {"ts_bd": "bold", "ts_it": "italic",
              "ts_un": "underline", "ts_st": "strikethrough"}


def apply_ops_u16(doc: StyledDoc, ops: list) -> StyledDoc:
    """Apply `ops` the way a UTF-16-indexed server would."""
    units: list[str] = []
    styles: list[dict] = []
    anchors: dict[int, dict] = {}
    for i, para in enumerate(doc.paragraphs):
        if i:
            units.append("\n")
            styles.append({})
        for run in para.runs:
            for unit in str_to_u16_units(run.text):
                units.append(unit)
                styles.append({
                    "bold": run.bold, "italic": run.italic,
                    "underline": run.underline,
                    "strikethrough": run.strikethrough,
                })
    pos = 0
    for para in doc.paragraphs:
        pos += u16_len(para.text)
        if para.heading:
            anchors[pos + 1] = {"ps_hd": para.heading, "ps_hdid": ""}
        pos += 1

    for op in ops:
        body = op[2]
        ty = body.get("ty")
        if ty == "is":
            ibi = int(body["ibi"])
            new = str_to_u16_units(str(body["s"]))
            for k, unit in enumerate(new):
                units.insert(ibi - 1 + k, unit)
                styles.insert(ibi - 1 + k, {})
            anchors = {(k + len(new) if k >= ibi else k): v
                       for k, v in anchors.items()}
        elif ty == "ds":
            si, ei = int(body["si"]), int(body["ei"])
            n = ei - si + 1
            del units[si - 1:ei]
            del styles[si - 1:ei]
            anchors = {(k - n if k > ei else k): v
                       for k, v in anchors.items() if not si <= k <= ei}
        elif ty == "as":
            si, ei = int(body["si"]), int(body["ei"])
            sm = body.get("sm") or {}
            if body.get("st") == "text":
                st = {_TEXT_KEYS[k]: bool(v) for k, v in sm.items()
                      if k in _TEXT_KEYS}
                for i in range(max(0, si - 1), min(len(units), ei)):
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
        para = Paragraph(runs=runs)
        sm = anchors.get(anchor)
        if sm and isinstance(sm.get("ps_hd"), int):
            para.heading = sm["ps_hd"]
        out.paragraphs.append(para)
        runs = []

    for ch, unit_idx in u16_units_to_chars(units):
        if ch == "\n":
            close(unit_idx + 1)
            continue
        st = styles[unit_idx] or {}
        run = StyleRun(text=ch, bold=bool(st.get("bold")),
                       italic=bool(st.get("italic")),
                       underline=bool(st.get("underline")),
                       strikethrough=bool(st.get("strikethrough")))
        if cur is None or cur.style_tuple() != run.style_tuple():
            if cur and cur.text:
                runs.append(cur)
            cur = run
        else:
            cur.text += ch
    close(len(units) + 1)
    return out


def shape(doc: StyledDoc) -> list:
    return [(p.heading, [(r.text, r.style_tuple()) for r in p.runs if r.text])
            for p in doc.paragraphs]


def push(old: StyledDoc, new: StyledDoc) -> StyledDoc:
    old.sct_id = new.sct_id = SCT
    return apply_ops_u16(old, encode_text_diff(old, new))


def lone_surrogates(doc: StyledDoc) -> list[str]:
    return [ch for ch in doc.plain_text if 0xD800 <= ord(ch) <= 0xDFFF]


def P(text="", heading=0, **style) -> Paragraph:
    return Paragraph(runs=[StyleRun(text=text, **style)] if text else [],
                     heading=heading)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def test_u16_len_counts_astral_as_two():
    assert u16_len("abc") == 3
    assert u16_len(GRIN) == 2
    assert u16_len("hi " + GRIN + " there") == 11


def test_surrogate_split_and_recombine_round_trips():
    text = "a" + GRIN + "b" + PARTY
    units = str_to_u16_units(text)
    assert len(units) == 6
    assert "".join(ch for ch, _ in u16_units_to_chars(units)) == text


def test_unpaired_surrogate_survives_decoding():
    """A note already corrupted by the old bug contains lone surrogates;
    decoding must carry them through rather than crash, so the repair
    can be seen and pushed."""
    chars = u16_units_to_chars(["\ud83d", "a"])
    assert [ch for ch, _ in chars] == ["\ud83d", "a"]


def test_position_conversion_matches_utf16():
    text = "hi " + GRIN + " there"
    assert cp_to_u16_pos(text, len(text) + 1) == u16_len(text) + 1
    # "there" is codepoints 6..10 -> UTF-16 units 7..11
    assert cp_span_to_u16(text, 6, 10) == (7, 11)


# ------------------------------------------------------------------
# The reported corruption
# ------------------------------------------------------------------

def test_appending_after_an_emoji_does_not_split_it():
    """The exact reported failure: type after an emoji, sync, and the
    emoji comes back as two "?" with the new text stranded between
    them."""
    old = StyledDoc(paragraphs=[P("hi " + GRIN)])
    new = StyledDoc(paragraphs=[P("hi " + GRIN + " there")])
    server = push(old, new)
    assert lone_surrogates(server) == [], "emoji surrogate pair was split"
    assert server.plain_text == "hi " + GRIN + " there"


def test_appending_after_an_emoji_keeps_the_trailing_space():
    old = StyledDoc(paragraphs=[P(GRIN + " a")])
    new = StyledDoc(paragraphs=[P(GRIN + " a b ")])
    assert push(old, new).plain_text == GRIN + " a b "


def test_styling_after_an_emoji_lands_on_the_right_characters():
    old = StyledDoc(paragraphs=[P("hi " + GRIN + " there")])
    new = StyledDoc(paragraphs=[Paragraph(runs=[
        StyleRun(text="hi " + GRIN + " "),
        StyleRun(text="there", bold=True)])])
    assert shape(push(old, new)) == shape(new)


def test_italic_on_the_emoji_itself_covers_both_units():
    old = StyledDoc(paragraphs=[P("a" + GRIN + "b")])
    new = StyledDoc(paragraphs=[Paragraph(runs=[
        StyleRun(text="a"),
        StyleRun(text=GRIN, italic=True),
        StyleRun(text="b")])])
    assert shape(push(old, new)) == shape(new)


def test_heading_anchor_after_an_emoji():
    old = StyledDoc(paragraphs=[P("a" + GRIN + "b"), P("second")])
    new = StyledDoc(paragraphs=[P("a" + GRIN + "b", heading=1), P("second")])
    server = push(old, new)
    assert [p.heading for p in server.paragraphs] == [1, 0]


def test_deleting_across_an_emoji_removes_it_whole():
    old = StyledDoc(paragraphs=[P("keep " + GRIN + " drop")])
    new = StyledDoc(paragraphs=[P("keep ")])
    server = push(old, new)
    assert lone_surrogates(server) == []
    assert server.plain_text == "keep "


def test_multiple_emoji_compound_the_offset():
    old = StyledDoc(paragraphs=[P(GRIN + PARTY + GRIN + "tail")])
    new = StyledDoc(paragraphs=[P(GRIN + PARTY + GRIN + "tail!")])
    server = push(old, new)
    assert lone_surrogates(server) == []
    assert server.plain_text == GRIN + PARTY + GRIN + "tail!"


def test_emoji_in_a_later_paragraph_keeps_earlier_anchors_correct():
    old = StyledDoc(paragraphs=[
        P("Title", heading=1), P("body " + GRIN), P("last")])
    new = StyledDoc(paragraphs=[
        P("Title", heading=1), P("body " + GRIN), P("last!")])
    server = push(old, new)
    assert lone_surrogates(server) == []
    assert [p.heading for p in server.paragraphs] == [1, 0, 0]


def test_decoding_a_utf16_snapshot_places_styles_correctly():
    """Read side: a snapshot whose style range is expressed in UTF-16
    must not shift onto the wrong characters."""
    text = "hi " + GRIN + " there"
    ops = [
        ["sct-add", 0, SCT, "txt"],
        ["docs-nestedModel", ["text", 1, SCT],
         {"ibi": 1, "s": text, "ty": "is"}],
        # "there" = UTF-16 units 7..11
        ["docs-nestedModel", ["text", 1, SCT],
         {"st": "text", "si": 7, "ei": 11, "ty": "as",
          "sm": {"ts_bd": True, "ts_bd_i": False}}],
    ]
    doc = decode_chunks([json.dumps(ops)])
    assert doc.plain_text == text
    bold = [r.text for p in doc.paragraphs for r in p.runs if r.bold]
    assert bold == ["there"]


def test_fresh_note_encoding_uses_utf16_positions():
    doc = StyledDoc(sct_id=SCT, paragraphs=[Paragraph(runs=[
        StyleRun(text="hi " + GRIN + " "),
        StyleRun(text="there", bold=True)])])
    ops = encode_doc(doc)
    style = [op[2] for op in ops
             if op[0] == "docs-nestedModel" and op[2].get("st") == "text"][0]
    assert (style["si"], style["ei"]) == (7, 11)


@pytest.mark.parametrize("text", [
    "plain ascii",
    "café naïve",
    "日本語",
    GRIN,
    "a" + GRIN,
    GRIN + "a",
    GRIN + PARTY,
])
def test_round_trip_is_lossless_for_assorted_text(text):
    old = StyledDoc(paragraphs=[P(text)])
    new = StyledDoc(paragraphs=[P(text + "X")])
    server = push(old, new)
    assert lone_surrogates(server) == []
    assert server.plain_text == text + "X"


# ------------------------------------------------------------------
# Deletion seams (not UTF-16 specific, found by the same fuzz)
# ------------------------------------------------------------------

def test_merging_paragraphs_does_not_inherit_the_deleted_ones_styling():
    """Styling is per character and survives a delete, so backspacing
    "ne " (italic) together with "XY " (bold) leaves a paragraph whose
    trailing space is the BOLD one — the italic paragraph's own space is
    what got deleted. Both sides read "ne ", so matching paragraphs by
    text saw no style change, and the deletion produced no inserted
    range for the changed-range gate to catch. The note came back with a
    stray bold space."""
    old = StyledDoc(paragraphs=[
        Paragraph(runs=[StyleRun(text="ne ", italic=True)]),
        Paragraph(runs=[StyleRun(text="XY ", bold=True)]),
        Paragraph(runs=[StyleRun(text="tail")]),
    ])
    new = StyledDoc(paragraphs=[
        Paragraph(runs=[StyleRun(text="ne ", italic=True)]),
        # edited too, so the whole-document fallback does not paper over it
        Paragraph(runs=[StyleRun(text="tail!")]),
    ])
    assert shape(push(old, new)) == shape(new)


def test_untouched_note_still_emits_no_ops():
    doc = StyledDoc(sct_id=SCT, paragraphs=[
        P("Title", heading=1),
        Paragraph(runs=[StyleRun(text="bold", bold=True),
                        StyleRun(text=" and " + GRIN)]),
    ])
    same = StyledDoc(sct_id=SCT, paragraphs=[
        P("Title", heading=1),
        Paragraph(runs=[StyleRun(text="bold", bold=True),
                        StyleRun(text=" and " + GRIN)]),
    ])
    assert encode_text_diff(doc, same) == []
