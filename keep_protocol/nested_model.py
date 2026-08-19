"""Decoder for Google Keep's docs-nestedModel format.

The wire format is documented in /memories/repo/keep-protocol-wire.md. Brief
recap: the server stores a list of ops as JSON strings under
`serverChanges.snapshot.serializedChunks`. Each op is one of:

    ["sct-add", 0, "<sct-id>", "txt"]
        Creates the text container. Always op[0] for text-bearing notes.

    ["docs-nestedModel", ["text", 1, "<sct-id>"], {"ty": "is", "ibi": N, "s": "..."}]
        Insert string `s` before 1-based position `ibi`.

    ["docs-nestedModel", ["text", 1, "<sct-id>"], {"ty": "as", "st": "text", "si": N, "ei": M, "sm": {...}}]
        Apply style to the character range [si, ei] (1-based, inclusive).
        `sm` is a flat dict of style-marker keys:
            ts_bd  = bold        (true/false)
            ts_it  = italic      (true/false)
            ts_un  = underline   (true/false)
            ts_st  = strikethrough (true/false)
        Each marker may have a paired `<key>_i: bool` indicating whether the
        property was inherited from the previous range. We currently treat
        the explicit value as authoritative and ignore `_i` for read-side
        decoding (it matters for write-side delta tracking).

    ["docs-nestedModel", ["text", 1, "<sct-id>"], {"ty": "as", "st": "paragraph", "si": N, "ei": N, "sm": {"ps_hd": 1|2, "ps_hdid": "h.xxx"}}]
        Apply paragraph style. For headings, si == ei == position of the
        paragraph's terminating \\n. ps_hd: 1 = heading-1, 2 = heading-2.

Position 0 is a virtual "start of document" marker; characters live at
1-based positions 1..N. Position N+1 is the implicit end-of-document caret
slot (used by zero-width `as` ops that record the trailing caret style).

Stage 2 scope: read-side only. Write-side encoder will live alongside this
once we've round-tripped a few notes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Style-marker keys we currently model.
_TEXT_KEYS = {
    "ts_bd": "bold",
    "ts_it": "italic",
    "ts_un": "underline",
    "ts_st": "strikethrough",
}


@dataclass
class StyleRun:
    """A maximal run of identically-styled characters within a paragraph."""
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False

    def style_tuple(self) -> tuple:
        return (self.bold, self.italic, self.underline, self.strikethrough)


@dataclass
class Paragraph:
    runs: list[StyleRun] = field(default_factory=list)
    heading: int = 0          # 0 = body, 1 = H1, 2 = H2
    heading_id: Optional[str] = None

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class StyledDoc:
    paragraphs: list[Paragraph] = field(default_factory=list)
    sct_id: Optional[str] = None
    revision: Optional[str] = None

    @property
    def plain_text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs)


def styled_doc_to_dict(doc: StyledDoc) -> dict:
    """JSON-safe serialization of a StyledDoc, for disk-caching the
    per-note styled baseline across app restarts. `styled_doc` is a
    dynamically-attached (non-dataclass) attribute on KeepNote, so it
    was never persisted to notes.json -- meaning the "local has no
    styled_doc" state (used by decide_merge/sync_merge.py to guess
    "this is our own edit echoing back") was ALSO true on literally
    every cold boot, for every note, regardless of whether it was
    ever locally edited. That made a genuine concurrent web restyle
    indistinguishable from a local echo on the very first sync after
    launch, silently swallowing it."""
    return {
        "sct_id": doc.sct_id,
        "revision": doc.revision,
        "paragraphs": [
            {
                "heading": p.heading,
                "heading_id": p.heading_id,
                "runs": [
                    {
                        "text": r.text,
                        "bold": r.bold,
                        "italic": r.italic,
                        "underline": r.underline,
                        "strikethrough": r.strikethrough,
                    }
                    for r in p.runs
                ],
            }
            for p in doc.paragraphs
        ],
    }


def styled_doc_from_dict(data: Optional[dict]) -> Optional[StyledDoc]:
    """Inverse of styled_doc_to_dict(). Returns None for falsy/
    malformed input rather than raising -- a cache-load path should
    degrade to "no baseline yet" (the pre-existing behavior) instead
    of crashing on a corrupt or old-format cache entry."""
    if not data or not isinstance(data, dict):
        return None
    try:
        doc = StyledDoc(
            sct_id=data.get("sct_id"),
            revision=data.get("revision"),
            paragraphs=[
                Paragraph(
                    heading=p.get("heading", 0),
                    heading_id=p.get("heading_id"),
                    runs=[
                        StyleRun(
                            text=r.get("text", ""),
                            bold=r.get("bold", False),
                            italic=r.get("italic", False),
                            underline=r.get("underline", False),
                            strikethrough=r.get("strikethrough", False),
                        )
                        for r in p.get("runs", [])
                    ],
                )
                for p in data.get("paragraphs", [])
            ],
        )
        # Cache entries written before html_to_styled_doc coalesced its
        # output hold split-but-identically-styled runs; left as-is they
        # would make this restored baseline compare unequal to the
        # server's merged form on the first sync after upgrading, and
        # push a no-op restyle for every cached note.
        return coalesce_runs(doc)
    except (TypeError, AttributeError):
        return None


def decode_chunks(chunks: Iterable[str], revision: Optional[str] = None) -> StyledDoc:
    """Decode the snapshot's serializedChunks into a StyledDoc.

    Empty input or no `sct-add` op yields an empty doc."""
    ops: list[Any] = []
    for chunk in chunks:
        if not chunk:
            continue
        parsed = json.loads(chunk)
        # Each chunk is itself a list of ops.
        if isinstance(parsed, list):
            ops.extend(parsed)
    return _decode_ops(ops, revision=revision)


def decode_serialized_commands(serialized_commands: str) -> StyledDoc:
    """Decode the previewData.serializedCommands form.

    Wrapped as `["docs-mlti", [<ops...>]]`.
    """
    if not serialized_commands:
        return StyledDoc()
    parsed = json.loads(serialized_commands)
    if (
        isinstance(parsed, list)
        and len(parsed) == 2
        and parsed[0] == "docs-mlti"
        and isinstance(parsed[1], list)
    ):
        return _decode_ops(parsed[1])
    return StyledDoc()


# ---------------------------------------------------------------------------
# UTF-16 position arithmetic
# ---------------------------------------------------------------------------
#
# Every position on the wire (`ibi`, `si`, `ei`, and a heading's paragraph
# anchor) is an offset into the text measured in UTF-16 CODE UNITS, not
# codepoints. That is not a guess: Keep's web client is JavaScript, its
# Android client is Java and its iOS client is Objective-C/Swift — all
# three count strings in UTF-16 natively, and a note edited from the
# desktop proved it by corrupting exactly the way this predicts.
#
# Python counts codepoints. The two agree across the whole Basic
# Multilingual Plane, so for the overwhelming majority of notes this is a
# no-op — but any character outside it (every emoji, some CJK extension
# and maths characters) is ONE Python character and TWO UTF-16 units.
#
# Sending a codepoint offset for a note containing an emoji puts every
# later position one short per emoji, and an insert computed that way
# lands BETWEEN the two halves of the emoji's surrogate pair: the note
# comes back with the emoji split into two lone surrogates (rendered as
# two "?" boxes) and the inserted text stranded in the middle of them.
# Reading is equally affected — style ranges decoded from a snapshot land
# on the wrong characters.


def u16_len(text: str) -> int:
    """Length of `text` in UTF-16 code units."""
    extra = 0
    for ch in text:
        if ord(ch) > 0xFFFF:
            extra += 1
    return len(text) + extra


def cp_to_u16_pos(text: str, cp_pos: int) -> int:
    """1-based codepoint position -> 1-based UTF-16 position."""
    return u16_len(text[:max(0, cp_pos - 1)]) + 1


def cp_span_to_u16(text: str, si: int, ei: int) -> tuple[int, int]:
    """Inclusive 1-based codepoint span -> inclusive 1-based UTF-16 span.

    `ei` maps to the LAST unit of the character at `ei`, so an astral
    character at the end of the range keeps both of its units covered.
    """
    return u16_len(text[:max(0, si - 1)]) + 1, u16_len(text[:max(0, ei)])


def str_to_u16_units(text: str) -> list[str]:
    """Split into UTF-16 code units; astral chars become two surrogates."""
    units: list[str] = []
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            units.append(chr(0xD800 + (code >> 10)))
            units.append(chr(0xDC00 + (code & 0x3FF)))
        else:
            units.append(ch)
    return units


def u16_units_to_chars(units: list[str]) -> list[tuple[str, int]]:
    """Recombine surrogate pairs into characters.

    Returns (character, index_of_its_first_unit) pairs. An UNPAIRED
    surrogate is passed through untouched rather than dropped or raised
    on — a note already corrupted by the codepoint-offset bug contains
    exactly that, and it has to survive decoding so the repair can be
    seen and re-sent rather than crashing the sync.
    """
    out: list[tuple[str, int]] = []
    i = 0
    n = len(units)
    while i < n:
        code = ord(units[i])
        if 0xD800 <= code <= 0xDBFF and i + 1 < n:
            low = ord(units[i + 1])
            if 0xDC00 <= low <= 0xDFFF:
                cp = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                out.append((chr(cp), i))
                i += 2
                continue
        out.append((units[i], i))
        i += 1
    return out


def _decode_ops(ops: list[Any], revision: Optional[str] = None) -> StyledDoc:
    sct_id: Optional[str] = None
    # Positions in the op stream are UTF-16 code-unit offsets (see the
    # helpers above), so the working buffer is a list of UNITS, not of
    # characters. Indexing it with the server's own numbers is then
    # exact for astral characters too; the units are recombined into
    # real characters at the end.
    text_chars: list[str] = []                  # 0-based UTF-16 units
    text_styles: list[dict[str, bool]] = []     # parallel to text_chars
    # paragraph_styles: keyed by 1-based position of the terminating \n (or
    # end-of-doc position N+1 for the trailing paragraph). value = sm dict.
    paragraph_styles: dict[int, dict[str, Any]] = {}

    for op in ops:
        if not isinstance(op, list) or not op:
            continue
        head = op[0]
        if head == "sct-add" and len(op) >= 3:
            sct_id = op[2]
            continue
        if head != "docs-nestedModel" or len(op) < 3:
            continue
        target = op[1]
        body = op[2]
        if not isinstance(body, dict):
            continue
        ty = body.get("ty")
        # Only `text` containers are interesting at this stage.
        if not (isinstance(target, list) and target and target[0] == "text"):
            continue

        if ty == "is":
            # Insert string `s` before 1-based position `ibi`.
            ibi = int(body.get("ibi", 1))
            s = str(body.get("s", ""))
            insert_idx = max(0, ibi - 1)
            for i, unit in enumerate(str_to_u16_units(s)):
                text_chars.insert(insert_idx + i, unit)
                text_styles.insert(insert_idx + i, {})
        elif ty == "ds":
            # Delete string from 1-based [si, ei] inclusive (best guess).
            si = int(body.get("si", 1))
            ei = int(body.get("ei", si))
            start = max(0, si - 1)
            end = min(len(text_chars), ei)
            for _ in range(end - start):
                if start < len(text_chars):
                    del text_chars[start]
                    del text_styles[start]
        elif ty == "as":
            st = body.get("st")
            sm = body.get("sm") or {}
            si = int(body.get("si", 1))
            ei = int(body.get("ei", si))
            if st == "text":
                # Apply char styles to 1-based [si, ei] inclusive.
                style = {}
                for k, v in sm.items():
                    if k.endswith("_i"):
                        continue   # inherit-flag, ignore for now
                    if k in _TEXT_KEYS:
                        style[_TEXT_KEYS[k]] = bool(v)
                start = max(0, si - 1)
                end = min(len(text_chars), ei)   # ei is inclusive 1-based -> 0-based exclusive = ei
                for i in range(start, end):
                    # Replace in-place: missing keys = false (per protocol notes).
                    text_styles[i] = dict(style)
            elif st == "paragraph":
                # Heading or paragraph-level style. Key by si (== ei for
                # zero-width paragraph anchors).
                paragraph_styles[si] = sm

    # Build paragraphs by splitting on \n.
    doc = StyledDoc(sct_id=sct_id, revision=revision)
    para_runs: list[StyleRun] = []
    para_start_idx = 0   # 1-based index of the first char of the current paragraph
    cur_run: Optional[StyleRun] = None

    def _flush_run():
        nonlocal cur_run
        if cur_run and cur_run.text:
            para_runs.append(cur_run)
        cur_run = None

    def _close_paragraph(terminator_pos: Optional[int]):
        """terminator_pos: 1-based position of the \\n that ends this para,
        or None for the trailing paragraph (no terminator)."""
        nonlocal para_runs
        _flush_run()
        para = Paragraph(runs=para_runs)
        # Look up paragraph styles. Heading anchors live at the \n position.
        anchor = terminator_pos if terminator_pos is not None else len(text_chars) + 1
        sm = paragraph_styles.get(anchor)
        if sm:
            ps_hd = sm.get("ps_hd")
            if isinstance(ps_hd, int):
                para.heading = ps_hd
            para.heading_id = sm.get("ps_hdid")
        doc.paragraphs.append(para)
        para_runs = []

    # Walk recombined CHARACTERS (a surrogate pair is one character)
    # while keeping every anchor lookup in UNIT positions -- emitting
    # one run per unit would put lone surrogates into run text and
    # corrupt the very emoji this conversion exists to protect. Styling
    # comes from the character's FIRST unit: a range covering an astral
    # character always covers both of its units, so the two agree.
    for ch, unit_idx in u16_units_to_chars(text_chars):
        pos1 = unit_idx + 1   # 1-based UTF-16 position
        if ch == "\n":
            _close_paragraph(pos1)
            continue
        style = text_styles[unit_idx] or {}
        s = StyleRun(
            text=ch,
            bold=bool(style.get("bold")),
            italic=bool(style.get("italic")),
            underline=bool(style.get("underline")),
            strikethrough=bool(style.get("strikethrough")),
        )
        if cur_run is None or cur_run.style_tuple() != s.style_tuple():
            _flush_run()
            cur_run = s
        else:
            cur_run.text += ch

    # Final paragraph (no \n terminator)
    _close_paragraph(None)
    return doc


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------

# Reverse of _TEXT_KEYS for encoding: friendly name -> wire key
_TEXT_NAMES = {v: k for k, v in _TEXT_KEYS.items()}


def encode_doc(doc: StyledDoc) -> list:
    """Encode a StyledDoc as a fresh op stream (sct-add + is + as...).

    Use for newly-created notes. `doc.sct_id` must be set."""
    if not doc.sct_id:
        raise ValueError("StyledDoc.sct_id is required for encode_doc")
    ops: list = [["sct-add", 0, doc.sct_id, "txt"]]
    ops.extend(_encode_text_ops(doc, doc.sct_id))
    return ops


def encode_full_replace(doc: StyledDoc, current_text_length: int) -> list:
    """Encode ops that overwrite an existing note.

    Strategy: delete current text (one `ds` covering [1, len]), insert the
    new text, then apply each style range. This is the safest write mode —
    we never partially mutate the model, we always rebuild it from a known
    StyledDoc. Slower wire footprint than diffing, but corruption-proof.

    `doc.sct_id` must match the existing note's sct id.
    `current_text_length` is measured in UTF-16 code units."""
    if not doc.sct_id:
        raise ValueError("StyledDoc.sct_id is required for encode_full_replace")
    target = ["text", 1, doc.sct_id]
    ops: list = []
    if current_text_length > 0:
        # `current_text_length` MUST be in UTF-16 code units (use
        # u16_len), not Python characters -- the doc passed in is the
        # NEW text, so this function cannot derive the old length
        # itself. A codepoint length leaves an astral character's final
        # surrogate undeleted.
        ops.append([
            "docs-nestedModel", target,
            {"ty": "ds", "si": 1, "ei": current_text_length},
        ])
    ops.extend(_encode_text_ops(doc, doc.sct_id))
    return ops


def encode_text_diff(old_doc: StyledDoc, new_doc: StyledDoc) -> list:
    """Encode minimal `is`/`ds`/`as` ops to transform old_doc into new_doc.

    Collaboration-friendly: rather than wiping the whole text and
    re-inserting it (which races with concurrent edits because the
    server's OT transforms a sweeping `ds` against incoming inserts by
    deleting them too), we emit per-region edits via SequenceMatcher on
    the plain text. Style ops are emitted for any styled run that falls
    inside an inserted/changed region, plus for the whole doc when only
    style differs.

    `new_doc.sct_id` (or, failing that, `old_doc.sct_id`) determines the
    target sct. The two docs are expected to share the same sct.

    Returns a flat op list. Empty when the docs are identical.
    """
    import difflib

    sct_id = new_doc.sct_id or old_doc.sct_id
    if not sct_id:
        raise ValueError("encode_text_diff: at least one doc must have sct_id")

    target = ["text", 1, sct_id]
    old_text = old_doc.plain_text
    new_text = new_doc.plain_text
    if old_text == new_text and _styles_equal(old_doc, new_doc):
        return []

    ops: list = []
    sm = difflib.SequenceMatcher(a=old_text, b=new_text, autojunk=False)
    opcodes = sm.get_opcodes()
    # Walk back-to-front so each op's positions remain valid in the
    # current (pre-op) document. The server applies them in array
    # order, transforming each through any concurrent ops.
    for tag, i1, i2, j1, j2 in reversed(opcodes):
        if tag == "equal":
            continue
        if tag in ("delete", "replace") and i2 > i1:
            # SequenceMatcher works in Python codepoints; the wire wants
            # UTF-16 code units. They differ only for astral characters
            # -- but there the difference is the whole bug: a span
            # computed in codepoints can cut a surrogate pair in half.
            ds_si, ds_ei = cp_span_to_u16(old_text, i1 + 1, i2)
            ops.append([
                "docs-nestedModel", target,
                {"ty": "ds", "si": ds_si, "ei": ds_ei},
            ])
        if tag in ("insert", "replace") and j2 > j1:
            ops.append([
                "docs-nestedModel", target,
                {"ty": "is", "ibi": cp_to_u16_pos(old_text, i1 + 1),
                 "s": new_text[j1:j2]},
            ])

    # Style ops on inserted/changed regions in the NEW doc.
    changed_ranges: list[tuple[int, int]] = []
    for tag, _i1, _i2, j1, j2 in opcodes:
        if tag in ("insert", "replace") and j2 > j1:
            changed_ranges.append((j1 + 1, j2))   # 1-based inclusive

    # What the server's heading state will be once the is/ds ops above
    # land — the baseline both call sites below diff the desired
    # headings against, so a heading the server INHERITS through a
    # paragraph merge gets explicitly cleared instead of sticking.
    predicted_headings = _predict_server_headings(old_doc, new_text, opcodes)
    predicted_styles = _predict_server_styles(old_doc, new_text, opcodes)

    if changed_ranges:
        # old_doc is passed here too — heading is a paragraph-level
        # property independent of where any text edit landed. Without
        # it, a heading changed on paragraph 5 while the user also
        # deletes an unrelated blank paragraph near paragraph 2 would
        # only get its heading op emitted if paragraph 5 happened to
        # fall inside changed_ranges — otherwise the change was
        # silently dropped from the push and reverted on the next pull.
        ops.extend(_emit_styles_for_ranges(
            new_doc, target, changed_ranges, old_doc=old_doc,
            predicted_headings=predicted_headings,
            predicted_styles=predicted_styles,
        ))
    elif not _styles_equal(old_doc, new_doc):
        n = len(new_text)
        # +1: a trailing paragraph's heading anchor sits at the implicit
        # end-of-document caret slot (position N+1), one past the last
        # real character — see keep-protocol-wire.md. Using [(1, n)] here
        # excluded that slot, so a heading applied to (or cleared from) an
        # empty trailing paragraph was silently dropped from the push.
        ops.extend(_emit_styles_for_ranges(
            new_doc, target, [(1, n + 1)], old_doc=old_doc,
            predicted_headings=predicted_headings,
            predicted_styles=predicted_styles,
        ))

    return ops


def coalesce_runs(doc: StyledDoc) -> StyledDoc:
    """Merge adjacent runs that carry identical styling, in place.

    A StyledDoc is only well-defined up to run segmentation: two runs
    sitting next to each other with the same bold/italic/underline/
    strikethrough are indistinguishable from one merged run, and the
    server always stores the merged form (it keeps styling per
    CHARACTER and re-derives runs, as the captured snapshots show).

    Locally, though, html_to_styled_doc reads one run per Qt text
    FRAGMENT, and Qt splits fragments on attributes we deliberately do
    not model — font point size above all, which set_styled_doc stamps
    on every run to size headings. Merge a heading line into the body
    line below it and Qt yields two fragments ("one" at H2 size, "a "
    at body size) whose *modelled* styling is byte-identical.

    Left uncoalesced that difference is invisible to the user but very
    loud to the code: _styles_equal compares run sequences, so it
    reported "formatting changed" for two identical docs, which made
    encode_text_diff skip its no-op early-return and push `as` ops —
    plus a freshly minted ps_hdid — on every single sync cycle for a
    note nobody had touched. Canonicalising here makes equality mean
    what it says.
    """
    for para in doc.paragraphs:
        merged: list[StyleRun] = []
        for run in para.runs:
            if not run.text:
                continue
            if merged and merged[-1].style_tuple() == run.style_tuple():
                # Replace with a fresh StyleRun rather than growing the
                # existing one in place: _three_way_merge_styled hands
                # back Paragraph objects (and their runs) by reference
                # from the base/local/remote docs it merged, so runs
                # can be shared between two live StyledDocs. Mutating
                # one here would silently corrupt the other.
                prev = merged[-1]
                merged[-1] = StyleRun(
                    text=prev.text + run.text,
                    bold=prev.bold, italic=prev.italic,
                    underline=prev.underline,
                    strikethrough=prev.strikethrough,
                )
            else:
                merged.append(run)
        para.runs = merged
    return doc


def _canonical_runs(runs: list["StyleRun"]) -> list[tuple]:
    """(style, length) pairs with adjacent same-style runs coalesced —
    the segmentation-independent form used for style comparisons."""
    out: list[list] = []
    for run in runs:
        if not run.text:
            continue
        style = run.style_tuple()
        if out and out[-1][0] == style:
            out[-1][1] += len(run.text)
        else:
            out.append([style, len(run.text)])
    return [tuple(x) for x in out]


def _styles_equal(a: StyledDoc, b: StyledDoc) -> bool:
    """True iff a and b have identical per-character styles AND headings."""
    if len(a.paragraphs) != len(b.paragraphs):
        return False
    for pa, pb in zip(a.paragraphs, b.paragraphs):
        if pa.heading != pb.heading or pa.text != pb.text:
            return False
        if _canonical_runs(pa.runs) != _canonical_runs(pb.runs):
            return False
    return True


def _predict_server_styles(
    old_doc: StyledDoc, new_text: str, opcodes: list,
) -> list[Optional[tuple]]:
    """Per NEW character, the style the server will hold once the is/ds
    ops land but before any `as` op — or None where it inherits.

    Styling is stored per character and rides along with that character
    through inserts and deletes, exactly like a heading anchor rides its
    newline (see _predict_server_headings). That has a consequence the
    old range heuristics could not express: DELETING text can change the
    styling of characters that were not touched at all.

        "ne " (italic) / "XY " (bold), backspaced together

    leaves one paragraph reading "ne " — whose trailing space is the one
    that came from the BOLD paragraph, because the italic paragraph's
    own space is what got deleted. The text is unchanged either side, so
    matching paragraphs by text found "no style change"; the deletion
    produced no inserted range, so the changed-range gate did not cover
    it either. The note came back with a stray bold space.

    Comparing the predicted styling against what the editor actually
    shows is exact, and subsumes both heuristics: inserted characters
    predict None (so they always get an explicit op, which is also what
    stops them inheriting a neighbour's styling), restyled characters
    differ, and genuinely untouched ones match and can be skipped.
    """
    old_styles: list[Optional[tuple]] = []
    for p_idx, para in enumerate(old_doc.paragraphs):
        if p_idx:
            old_styles.append(None)          # the paragraph separator
        for run in para.runs:
            old_styles.extend([run.style_tuple()] * len(run.text))

    predicted: list[Optional[tuple]] = [None] * len(new_text)
    for tag, i1, i2, j1, j2 in opcodes:
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            old_i, new_i = i1 + k, j1 + k
            if old_i < len(old_styles) and new_i < len(predicted):
                predicted[new_i] = old_styles[old_i]
    return predicted


def _server_would_keep_heading(
    p_idx: int,
    old_para: Optional["Paragraph"],
    predicted_headings: Optional[list[int]],
) -> bool:
    """Whether this now-body paragraph still needs an explicit ps_hd:0.

    Prefer the predicted post-text-ops server state when we have it —
    it is the only thing that catches a heading the server INHERITS
    rather than kept, which is what a paragraph merge does (see
    _predict_server_headings). Falling back to "did the aligned old
    paragraph have a heading" keeps the previous behaviour for callers
    that pass no prediction.
    """
    if predicted_headings is not None and p_idx < len(predicted_headings):
        return bool(predicted_headings[p_idx])
    return old_para is not None and bool(old_para.heading)


def _predict_server_headings(
    old_doc: StyledDoc, new_text: str, opcodes: list,
) -> list[int]:
    """Predict each NEW paragraph's heading as the server will have it
    after our `is`/`ds` text ops land, but BEFORE any paragraph-style op.

    Heading state is not stored per paragraph; it is anchored to the
    paragraph's terminating newline (the trailing paragraph uses the
    virtual end-of-document slot at N+1 -- see the module docstring and
    the captured snapshot in archive/.../fmt_headings.json). Those
    anchors ride along with their character through inserts and
    deletes, which produces a consequence that is easy to miss:

        deleting a newline MERGES two paragraphs, and the merged result
        inherits the heading of the LATER one, because the later
        paragraph's terminator is the one that survives.

    So backspacing a body line into the line above it can silently turn
    that line into an H1 server-side, while the local editor (Qt keeps
    the FIRST block's format when merging) shows it as body text. The
    encoder previously only emitted a heading-clear when the aligned
    OLD paragraph had a heading -- which is the wrong question: here
    the old counterpart is the body paragraph, so nothing was emitted
    and the note came back with a heading nobody asked for.

    Returning the predicted state lets the caller emit a clear exactly
    when the server would otherwise be left with a stale heading.
    """
    old_text = old_doc.plain_text
    # 0-based index of each old paragraph's terminating newline; the
    # trailing paragraph has no terminator and uses len(old_text),
    # standing in for the virtual end-of-document anchor slot.
    terminators: list[int] = []
    cursor = 0
    for para in old_doc.paragraphs:
        cursor += len(para.text)
        terminators.append(cursor)
        cursor += 1

    # Old char index -> new char index, for characters the diff keeps.
    surviving: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for k in range(i2 - i1):
                surviving[i1 + k] = j1 + k

    # New terminator index -> heading inherited from the old anchor
    # that lands there. The end-of-document anchor can never fall
    # inside a `ds` range (those only ever cover real characters), so
    # it always survives and re-lands on the new end slot.
    inherited: dict[int, int] = {}
    for old_idx, para in zip(terminators, old_doc.paragraphs):
        if old_idx >= len(old_text):
            inherited[len(new_text)] = para.heading
        elif old_idx in surviving:
            inherited[surviving[old_idx]] = para.heading

    predicted: list[int] = []
    cursor = 0
    for chunk in new_text.split("\n"):
        cursor += len(chunk)
        predicted.append(inherited.get(cursor, 0))
        cursor += 1
    return predicted


def _align_paragraphs(
    old_paras: list["Paragraph"], new_paras: list["Paragraph"],
) -> dict[int, Optional["Paragraph"]]:
    """Map each new-paragraph index to its old counterpart (or None).

    Correspondence is what every style/heading decision in
    _emit_styles_for_ranges hinges on: "did THIS paragraph's styling
    change since the server's copy?" needs to know which old paragraph
    *is* this one.

    Two approaches that don't work:
      * raw index (old[i] <-> new[i]) breaks the moment a paragraph is
        inserted or deleted anywhere above -- every index after that
        point shifts and compares the wrong pair, producing spurious
        heading clears and wrongly-reused heading ids;
      * exact text equality breaks whenever a paragraph's own text is
        edited -- which is exactly when the user is most likely to
        also be changing its formatting.

    A sequence diff over the paragraph texts handles both: paragraphs
    that survived unchanged align on `equal` runs (so an unrelated
    insertion above doesn't disturb them), and a paragraph whose text
    was merely edited still aligns with its old self through the
    `replace` block that covers it. Surplus new paragraphs inside a
    `replace` block, and everything in an `insert` block, are genuinely
    new and map to None.
    """
    import difflib

    mapping: dict[int, Optional[Paragraph]] = {}
    if not new_paras:
        return mapping
    if not old_paras:
        return {j: None for j in range(len(new_paras))}

    matcher = difflib.SequenceMatcher(
        a=[p.text for p in old_paras], b=[p.text for p in new_paras],
        autojunk=False,
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                mapping[j1 + k] = old_paras[i1 + k]
        elif tag == "replace":
            # Pair positionally within the replaced block. A 1:1
            # replace (the common "user edited this line" case) pairs
            # the paragraph with its own previous version; surplus new
            # paragraphs beyond the old block's length are new.
            for k in range(j2 - j1):
                mapping[j1 + k] = old_paras[i1 + k] if i1 + k < i2 else None
        elif tag == "insert":
            for k in range(j2 - j1):
                mapping[j1 + k] = None
        # "delete" contributes no new paragraphs.
    return mapping


def _emit_styles_for_ranges(
    doc: StyledDoc,
    target: list,
    ranges_1based: list[tuple[int, int]],
    old_doc: Optional[StyledDoc] = None,
    predicted_headings: Optional[list[int]] = None,
    predicted_styles: Optional[list[Optional[tuple]]] = None,
) -> list:
    ops: list = []
    pos = 1
    # Cached once: every emitted position is converted against this.
    doc_text = doc.plain_text
    # Positional alignment of old -> new paragraphs, computed with a
    # sequence diff over paragraph TEXTS (see _align_paragraphs).
    #
    # This used to be an exact-text dict ({p.text: p}), which silently
    # broke the two checks below for the single most common editing
    # pattern there is: changing a paragraph's formatting AND its text
    # in the same push cycle (bold a word, then keep typing -- the
    # 30-second autosave batches both into one push). Looking the
    # paragraph up by its NEW text found nothing, because the text is
    # exactly what changed, so `old_para` came back None and:
    #   * para_style_changed stayed False, leaving run styling gated
    #     purely on spatial overlap with the text-diff ranges -- so
    #     bolding "hello" while typing "!" at the end of the line
    #     emitted an `as` op for the "!" region only and dropped the
    #     bold entirely; and
    #   * the heading-clear branch below (`elif old_para is not None
    #     and old_para.heading`) never fired, so clearing a heading on
    #     a line you also typed into never reached the server.
    # Both looked correct in the widget until the next pull overwrote
    # it with the server's copy -- the "it reverts my formatting"
    # symptom. A diff-based alignment still tracks a paragraph across
    # an edit to its own text, which is precisely the case exact-text
    # keying cannot represent.
    para_alignment = _align_paragraphs(
        old_doc.paragraphs if old_doc is not None else [], doc.paragraphs,
    )
    for p_idx, para in enumerate(doc.paragraphs):
        old_para = para_alignment.get(p_idx)
        # A paragraph whose run-level styling differs from its old
        # counterpart must have its style ops emitted even when none
        # of its characters fall inside ranges_1based (the TEXT
        # diff's changed regions) -- a pure formatting change (e.g.
        # bolding an untouched paragraph while ALSO editing text
        # elsewhere in the same push) doesn't move any text, so it
        # never appears in ranges_1based at all.
        #
        # We can only PROVE the server already has this paragraph's
        # styling when the aligned old paragraph has identical text;
        # then a run-by-run comparison is meaningful. Otherwise (text
        # edited, or no counterpart at all) the run boundaries have
        # shifted and no cheap comparison is valid, so we emit the
        # paragraph's styling rather than guess. Emitting is safe:
        # each `as` op carries explicit values for every style
        # property (explicit_all below) and describes exactly the
        # state the user is looking at, so a redundant op is a no-op
        # server-side. Guessing wrong, by contrast, silently drops the
        # edit.
        para_style_changed = True
        if old_para is not None and old_para.text == para.text:
            para_style_changed = (
                _canonical_runs(old_para.runs) != _canonical_runs(para.runs)
            )
        for run in para.runs:
            run_len = len(run.text)
            if run_len == 0:
                continue
            run_si = pos
            run_ei = pos + run_len - 1
            if predicted_styles is not None:
                # Exact: does the server already hold this run's styling
                # for every character the run covers?
                want = run.style_tuple()
                needs_emit = any(
                    predicted_styles[k - 1] != want
                    for k in range(run_si, run_ei + 1)
                    if 0 <= k - 1 < len(predicted_styles)
                )
            else:
                needs_emit = para_style_changed or any(
                    _range_intersects((run_si, run_ei), r)
                    for r in ranges_1based
                )
            if needs_emit:
                # Range bookkeeping above stays in codepoints (so it
                # lines up with ranges_1based, which comes from the
                # codepoint text diff); only the emitted numbers are
                # converted to the UTF-16 units the wire counts in.
                emit_si, emit_ei = cp_span_to_u16(doc_text, run_si, run_ei)
                # explicit_all=True: this run is inside an inserted or
                # explicitly-restyled region, possibly right next to
                # existing differently-styled server text. A run with
                # every style off must still say so explicitly, or the
                # server auto-inherits the neighbour's styling (e.g.
                # newly-typed text right after a bold run silently
                # comes back bold even though bold was just turned off).
                sm = _style_marker_dict(run, explicit_all=True)
                ops.append([
                    "docs-nestedModel", target,
                    {"ty": "as", "st": "text",
                     "si": emit_si, "ei": emit_ei, "sm": sm},
                ])
            pos += run_len
        anchor = pos
        # Heading is a paragraph-level property, not a text-run style —
        # unlike bold/italic it isn't scoped to ranges_1based (which
        # only covers the literal character span a text edit touched).
        # A heading changed on one paragraph while the user also edits
        # text somewhere else in the same push must still be emitted,
        # even though that paragraph's anchor falls outside every
        # changed range. Gating this on ranges_1based (as run styling
        # correctly is, above) silently dropped any heading change that
        # didn't spatially overlap a text edit in the same push cycle —
        # it would look pushed locally but revert on the next pull.
        if para.heading:
            sm_p = {
                "ps_hd": para.heading,
                # Reuse the existing heading anchor id whenever this
                # paragraph already had one server-side, rather than
                # minting a fresh one on every push. html_to_styled_doc
                # never sets heading_id (HTML has no such concept), so
                # without this every push that touched ANY text in a
                # note containing a heading re-sent that heading under
                # a brand-new ps_hdid even though the heading itself
                # never changed -- churning its server-side identity
                # on essentially every keystroke-triggered autosave.
                "ps_hdid": (
                    para.heading_id
                    or (old_para.heading_id if old_para and old_para.heading else None)
                    or _mint_heading_id()
                ),
            }
            u16_anchor = cp_to_u16_pos(doc_text, anchor)
            ops.append([
                "docs-nestedModel", target,
                {"ty": "as", "st": "paragraph",
                 "si": u16_anchor, "ei": u16_anchor, "sm": sm_p},
            ])
        elif _server_would_keep_heading(
            p_idx, old_para, predicted_headings,
        ):
            # Heading must be explicitly cleared. Keep web sends an
            # explicit ps_hd:0 rather than omitting the op — without
            # this the server keeps the old heading forever and every
            # periodic pull reverts the local edit.
            u16_anchor = cp_to_u16_pos(doc_text, anchor)
            ops.append([
                "docs-nestedModel", target,
                {"ty": "as", "st": "paragraph",
                 "si": u16_anchor, "ei": u16_anchor,
                 "sm": {"ps_hd": 0, "ps_hdid": ""}},
            ])
        if p_idx < len(doc.paragraphs) - 1:
            pos += 1
    return ops


def _range_intersects(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return not (a[1] < b[0] or b[1] < a[0])



def _encode_text_ops(doc: StyledDoc, sct_id: str) -> list:
    """Insert all text + apply style/heading ops for a fresh model."""
    target = ["text", 1, sct_id]
    ops: list = []
    if not doc.paragraphs:
        return ops

    # Build the joined plain text (\n-separated paragraphs).
    plain = "\n".join(p.text for p in doc.paragraphs)
    if plain:
        ops.append([
            "docs-nestedModel", target,
            {"ty": "is", "ibi": 1, "s": plain},
        ])

    # Walk runs and emit `as` ops for any non-default styling.
    # Position bookkeeping: 1-based char index across the whole text.
    pos = 1
    for p_idx, para in enumerate(doc.paragraphs):
        for run in para.runs:
            run_len = len(run.text)
            if run_len == 0:
                continue
            sm = _style_marker_dict(run)
            if sm:
                # Inclusive range [pos, pos + run_len - 1], converted
                # from codepoints to the UTF-16 units the wire counts.
                emit_si, emit_ei = cp_span_to_u16(
                    plain, pos, pos + run_len - 1)
                ops.append([
                    "docs-nestedModel", target,
                    {
                        "ty": "as",
                        "st": "text",
                        "si": emit_si,
                        "ei": emit_ei,
                        "sm": sm,
                    },
                ])
            pos += run_len
        # Paragraph-level heading style. For non-trailing paragraphs the
        # anchor is the \n we're about to step over (= pos). For the
        # trailing paragraph the anchor is one past the last char.
        if para.heading:
            anchor = pos if p_idx < len(doc.paragraphs) - 1 else pos
            sm = {
                "ps_hd": para.heading,
                "ps_hdid": para.heading_id or _mint_heading_id(),
            }
            u16_anchor = cp_to_u16_pos(plain, anchor)
            ops.append([
                "docs-nestedModel", target,
                {"ty": "as", "st": "paragraph",
                 "si": u16_anchor, "ei": u16_anchor, "sm": sm},
            ])
        # Step over the paragraph terminator \n (except after the last para)
        if p_idx < len(doc.paragraphs) - 1:
            pos += 1
    return ops


def _style_marker_dict(run: "StyleRun", *, explicit_all: bool = False) -> dict:
    """Build the `sm` dict for a styled text run.

    Per the wire protocol, each style key has a paired `<key>_i` flag
    indicating whether the value was inherited from a neighbouring run.
    For full-rewrite encoding we set `_i: false` (explicit) for every key
    we touch and omit keys whose value is the default (false).

    explicit_all: also emit an explicit `false` for properties that are
    OFF. Needed when this run sits at an insert/edit boundary next to
    differently-styled existing server text — omitting a key there
    doesn't mean "off", it means "inherit the neighbour's value" (see
    keep-protocol-wire.md, "Insertion at styled boundary"). Without
    this, turning bold off and typing more text right after a bold run
    would omit ts_bd entirely for the new (non-bold) text, and the
    server would auto-inherit bold from the preceding run — the new
    text comes back bold despite the user explicitly un-bolding first.
    Not needed for fresh/full-replace encoding (_encode_text_ops),
    where there's no adjacent existing text to inherit from."""
    sm: dict = {}
    for name in ("bold", "italic", "underline", "strikethrough"):
        wire = _TEXT_NAMES[name]
        if getattr(run, name):
            sm[wire] = True
            sm[wire + "_i"] = False
        elif explicit_all:
            sm[wire] = False
            sm[wire + "_i"] = False
    return sm


def _mint_heading_id() -> str:
    """Random heading id matching Keep's `h.xxxxxxxxxxxx` shape."""
    import secrets
    return "h." + secrets.token_hex(6)


def to_html(doc: StyledDoc) -> str:
    """Render a StyledDoc to minimal HTML suitable for QTextEdit.setHtml."""
    parts: list[str] = []
    for p in doc.paragraphs:
        if p.heading == 1:
            tag = "h1"
        elif p.heading == 2:
            tag = "h2"
        else:
            tag = "p"
        inner: list[str] = []
        for r in p.runs:
            txt = (
                r.text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            if r.bold:
                txt = f"<b>{txt}</b>"
            if r.italic:
                txt = f"<i>{txt}</i>"
            if r.underline:
                txt = f"<u>{txt}</u>"
            if r.strikethrough:
                txt = f"<s>{txt}</s>"
            inner.append(txt)
        joined = "".join(inner)
        if not joined:
            # A bare <p></p> (or <h1></h1>/<h2></h2>) silently vanishes
            # on the next html_to_styled_doc() round-trip — Qt's own
            # HTML parser drops truly empty block elements. Qt's own
            # toHtml() marks blank lines with this CSS property
            # specifically so they survive re-parsing; without it,
            # every empty paragraph (including deliberate blank lines
            # between sections) gets silently collapsed away.
            parts.append(f'<{tag} style="-qt-paragraph-type:empty;"></{tag}>')
        else:
            parts.append(f"<{tag}>{joined}</{tag}>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Checklist (cbx) decoding + encoding
# ---------------------------------------------------------------------------

@dataclass
class CheckboxItem:
    """A single checkbox row in a Keep LIST node.

    Decoded from `cbx-add` / `docs-nestedModel(cbx)` / `cbx-p` ops in the
    snapshot. Position is a tree-path: top-level rows are `[i]`, nested
    rows are `[parent_i, child_i]` (and deeper).
    """
    cbx_id: str                       # "cbx.xxxxx"
    text: str = ""
    checked: bool = False
    position: tuple[int, ...] = (0,)

    @property
    def indent(self) -> int:
        """How many levels deep this row is (0 = top-level)."""
        return max(0, len(self.position) - 1)


def decode_checkboxes(chunks: Iterable[str], list_sct_id: Optional[str] = None) -> tuple[list[CheckboxItem], Optional[str]]:
    """Decode the snapshot's serializedChunks for a LIST node.

    Returns (items_in_position_order, sct_id). Items are sorted by their
    final tree-position so the order matches what Keep web shows.

    `list_sct_id` is optional — if given, ops targeting other sct ids are
    ignored. If omitted, the first `sct-add … cbx` op encountered wins.
    """
    ops: list[Any] = []
    for chunk in chunks:
        if not chunk:
            continue
        try:
            parsed = json.loads(chunk)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, list) or not parsed:
            continue
        # A chunk is either a flat list of ops OR a single op (e.g. a
        # `docs-mlti` wrapper whose first element is the verb string).
        if isinstance(parsed[0], str):
            ops.append(parsed)
        else:
            ops.extend(parsed)

    sct_id = list_sct_id
    # cbx_id -> {"text": [chars], "checked": bool, "position": tuple}
    boxes: dict[str, dict[str, Any]] = {}
    # position lookup: list of cbx_ids in the order they were first added,
    # so we can resolve `cbx-mv` source paths.
    order: list[str] = []

    def _box(cbx_id: str) -> dict[str, Any]:
        b = boxes.get(cbx_id)
        if b is None:
            b = {"text": [], "checked": False, "position": (len(order),)}
            boxes[cbx_id] = b
            order.append(cbx_id)
        return b

    def _flatten_iter() -> list[str]:
        """Return cbx_ids ordered by current tree position."""
        return sorted(order, key=lambda c: boxes[c]["position"])

    def _flatten_unwrap(op: Any) -> Iterable[Any]:
        """Yield the inner ops, flattening any docs-mlti wrappers."""
        if (
            isinstance(op, list)
            and len(op) >= 2
            and op[0] == "docs-mlti"
            and isinstance(op[1], list)
        ):
            for inner in op[1]:
                yield from _flatten_unwrap(inner)
        else:
            yield op

    flat_ops: list[Any] = []
    for op in ops:
        flat_ops.extend(_flatten_unwrap(op))

    for op in flat_ops:
        if not isinstance(op, list) or not op:
            continue
        head = op[0]

        if head == "sct-add" and len(op) >= 4 and op[3] == "cbx":
            if sct_id is None:
                sct_id = op[2]
            continue

        if head == "sct-rp" and len(op) >= 5 and op[3] == "cbx":
            sct_id = op[4]
            continue

        if sct_id is not None and len(op) >= 3 and op[1] != sct_id:
            # cbx-add / cbx-p / cbx-mv / cbx-rm all carry sct as op[1].
            # Skip ops that target a different sct (shouldn't happen per
            # node, but be safe).
            if head in ("cbx-add", "cbx-p", "cbx-mv", "cbx-rm"):
                continue

        if head == "cbx-add" and len(op) >= 4:
            cbx_id = op[2]
            pos = op[3]
            b = _box(cbx_id)
            if isinstance(pos, list) and pos:
                b["position"] = tuple(int(x) for x in pos)
            continue

        if head == "cbx-rm" and len(op) >= 3:
            cbx_id = op[2]
            boxes.pop(cbx_id, None)
            if cbx_id in order:
                order.remove(cbx_id)
            continue

        if head == "cbx-p" and len(op) >= 5:
            cbx_id = op[2]
            prop = op[4]
            if isinstance(prop, list) and len(prop) >= 2 and prop[0] == "cb:ck":
                _box(cbx_id)["checked"] = bool(prop[1])
            continue

        if head == "cbx-mv" and len(op) >= 4:
            from_path = tuple(int(x) for x in op[2])
            to_path = tuple(int(x) for x in op[3])
            # Find cbx whose current position matches from_path.
            target_id: Optional[str] = None
            for cid, b in boxes.items():
                if b["position"] == from_path:
                    target_id = cid
                    break
            if target_id is not None:
                boxes[target_id]["position"] = to_path
            continue

        if head == "docs-nestedModel" and len(op) >= 3:
            target = op[1]
            body = op[2]
            # cbx-targeted text op: ["text", 0, sct, cbx_id]
            if not (isinstance(target, list) and len(target) >= 4 and target[0] == "text"):
                continue
            cbx_id = target[3]
            if not isinstance(cbx_id, str) or not cbx_id.startswith("cbx."):
                continue
            if not isinstance(body, dict):
                continue
            ty = body.get("ty")
            b = _box(cbx_id)
            # UTF-16 code units, matching the positions the wire uses --
            # recombined into characters when the row is emitted below.
            chars: list[str] = b["text"]
            if ty == "is":
                ibi = int(body.get("ibi", 1))
                s = str(body.get("s", ""))
                insert_idx = max(0, ibi - 1)
                for i, unit in enumerate(str_to_u16_units(s)):
                    chars.insert(insert_idx + i, unit)
            elif ty == "ds":
                si = int(body.get("si", 1))
                ei = int(body.get("ei", si))
                start = max(0, si - 1)
                end = min(len(chars), ei)
                del chars[start:end]
            # `as` (style) ops on cbx text are ignored — we only model
            # plain text per row.
            continue

    items: list[CheckboxItem] = []
    for cbx_id in _flatten_iter():
        b = boxes[cbx_id]
        items.append(CheckboxItem(
            cbx_id=cbx_id,
            text="".join(ch for ch, _ in u16_units_to_chars(b["text"])),
            checked=bool(b["checked"]),
            position=tuple(b["position"]),
        ))
    return items, sct_id


def encode_replace_list(
    list_sct_id: str,
    existing_cbx_ids: Iterable[str],
    new_items: list["CheckboxItem"],
) -> list:
    """Encode ops that delete every existing checkbox and recreate the
    list from `new_items`.

    Strategy mirrors `encode_full_replace` for text notes — we don't try
    to compute a minimal diff, we wipe and rebuild. Each item gets a
    `cbx-add`, an `is` text insert, and (if checked) a `cbx-p` op.

    Output is a flat list of ops; the caller wraps in `docs-mlti` and
    attaches it to a `commandBundles` payload.

    Note: this assumes the LIST node already has an sct of type `cbx`
    (i.e. has been touched by Keep web). Bootstrapping a brand-new list
    needs an `sct-add` op which we don't emit here.
    """
    ops: list = []
    for cbx_id in existing_cbx_ids:
        # `cbx-rm` accepts either a position path or just the cbx id.
        # Keep web sends `[..., position, 0]` but we don't need the
        # position for a delete — the cbx_id is unique. Use a placeholder
        # `[0]` path; the server resolves by id.
        ops.append(["cbx-rm", list_sct_id, cbx_id, [0], 0])

    # Position paths must express NESTING, not just order: a top-level
    # row is [i], a child is [parent_i, child_i] — that path is the
    # only record of indentation on the wire, and decode_checkboxes
    # reads an item's tree position straight back out of it.
    #
    # This used to emit a flat [idx] for every row, so seeding a
    # brand-new checklist (the one path that reaches here — see
    # KeepClient.replace_list_items, called during LIST bootstrap)
    # silently flattened every indented sub-item to top level. The
    # scheme below matches encode_list_diff's fresh-addition path and
    # KeepSyncV2._align_local_to_server exactly, and reduces to the
    # previous [idx] for a list with no indentation at all.
    last_parent_idx = 0
    child_counter = 0
    for idx, item in enumerate(new_items):
        cbx_id = item.cbx_id or _mint_cbx_id()
        # Keep only supports one level of nesting (deeper 400s), and a
        # leading child has no parent to attach to.
        indent = 0 if idx == 0 else min(1, item.indent)
        if indent == 0:
            last_parent_idx = idx
            child_counter = 0
            pos = [idx]
        else:
            pos = [last_parent_idx, child_counter]
            child_counter += 1
        ops.append(["cbx-add", list_sct_id, cbx_id, pos])
        if item.text:
            ops.append([
                "docs-nestedModel",
                ["text", 0, list_sct_id, cbx_id],
                {"ty": "is", "ibi": 1, "s": item.text},
            ])
        if item.checked:
            ops.append([
                "cbx-p", list_sct_id, cbx_id, [], ["cb:ck", True],
            ])
    return ops


def _mint_cbx_id() -> str:
    """Random checkbox id matching Keep's `cbx.xxxxxxxxxxxx` shape."""
    import secrets
    # Keep uses 12 lowercase alphanumerics — base36-ish. token_hex gives
    # us hex which is a strict subset; the server accepts it.
    return "cbx." + secrets.token_hex(6)


def encode_list_diff(
    list_sct_id: str,
    old_items: list["CheckboxItem"],
    new_items: list["CheckboxItem"],
) -> list:
    """Encode minimal cbx ops to transform old_items into new_items.

    Collaboration-friendly: matches items by `cbx_id` so concurrent
    web edits to a row's text or checked state are preserved by the
    server's OT engine. Items in `new_items` without a `cbx_id` are
    treated as fresh additions and get a freshly-minted id.

    Emits, in order:
      1. `cbx-rm` for each removed item (id present in old, absent in new).
      2. For each common item (matched by id):
         - `docs-nestedModel is/ds` ops to align text.
         - `cbx-p [cb:ck, bool]` if checked-state changed.
      2b. `cbx-mv` ops to reorder surviving items into their target
         order. Items whose indent level changed are converted to
         remove+add (Keep's API rejects cross-level cbx-mv with 400).
      3. `cbx-add` (+ text insert + cbx-p if checked) for each new item.

    Returns a flat op list (caller wraps in `docs-mlti`).
    """
    import difflib

    old_by_id: dict[str, "CheckboxItem"] = {it.cbx_id: it for it in old_items if it.cbx_id}
    new_by_id: dict[str, "CheckboxItem"] = {}
    fresh_items: list["CheckboxItem"] = []

    # Partition new_items into "matches existing" vs "fresh".
    for it in new_items:
        if it.cbx_id and it.cbx_id in old_by_id:
            new_by_id[it.cbx_id] = it
        else:
            fresh_items.append(it)

    # Promote indent-changers to remove+add. Cross-level cbx-mv (e.g.
    # [2] -> [1, 0]) is unreliable on Keep's server — sometimes 400s,
    # sometimes silently rejected — so we sidestep it entirely by
    # treating an indent change as "this is a new row; the old one
    # went away". We keep the original cbx_id on the re-add so the
    # row's identity survives across the rewrite.
    indent_changed: list[str] = []
    for cbx_id in list(new_by_id.keys()):
        old_indent = max(0, len(tuple(old_by_id[cbx_id].position)) - 1)
        new_indent = max(0, len(tuple(new_by_id[cbx_id].position)) - 1)
        if old_indent != new_indent:
            indent_changed.append(cbx_id)
    for cbx_id in indent_changed:
        new_it = new_by_id.pop(cbx_id)
        # Mint a fresh id — re-using a just-removed id risks the server
        # treating the cbx-add as a no-op against the prior state.
        fresh_items.append(CheckboxItem(
            cbx_id="",
            text=new_it.text,
            checked=new_it.checked,
            position=new_it.position,
        ))

    ops: list = []

    # 1. Removals — anything in old but not referenced by new (or
    # promoted to a re-add by the indent-change pass). Emit cbx-rm in
    # DESCENDING position order so each rm doesn't shift the indices
    # used by later rms.
    removed_ids: set[str] = set()
    rm_targets: list[tuple[tuple[int, ...], str]] = []
    for cbx_id, old_it in old_by_id.items():
        if cbx_id not in new_by_id:
            rm_targets.append((tuple(old_it.position) or (0,), cbx_id))
            removed_ids.add(cbx_id)
    rm_targets.sort(key=lambda t: t[0], reverse=True)
    for pos, cbx_id in rm_targets:
        ops.append([
            "cbx-rm", list_sct_id, cbx_id, list(pos), 0,
        ])

    # 2. In-place text/check edits for matched items (no position
    # changes yet — those are computed below in a single pass).
    for cbx_id, new_it in new_by_id.items():
        old_it = old_by_id[cbx_id]

        # --- text diff inside the row ---
        if old_it.text != new_it.text:
            sm = difflib.SequenceMatcher(
                a=old_it.text, b=new_it.text, autojunk=False,
            )
            row_target = ["text", 0, list_sct_id, cbx_id]
            # Walk back-to-front so each op's positions remain valid
            # in the current (pre-op) text. The server applies them
            # in array order; later positions are touched first so
            # earlier positions don't shift. Mirrors encode_text_diff.
            for tag, i1, i2, j1, j2 in reversed(sm.get_opcodes()):
                if tag == "equal":
                    continue
                # Same codepoint -> UTF-16 conversion the text-note
                # encoder does: a checklist row holding an emoji has
                # the identical surrogate-splitting failure otherwise.
                if tag in ("delete", "replace") and i2 > i1:
                    ds_si, ds_ei = cp_span_to_u16(old_it.text, i1 + 1, i2)
                    ops.append([
                        "docs-nestedModel", row_target,
                        {"ty": "ds", "si": ds_si, "ei": ds_ei},
                    ])
                if tag in ("insert", "replace") and j2 > j1:
                    ops.append([
                        "docs-nestedModel", row_target,
                        {"ty": "is",
                         "ibi": cp_to_u16_pos(old_it.text, i1 + 1),
                         "s": new_it.text[j1:j2]},
                    ])

        # --- checked toggle ---
        if bool(old_it.checked) != bool(new_it.checked):
            ops.append([
                "cbx-p", list_sct_id, cbx_id, [],
                ["cb:ck", bool(new_it.checked)],
            ])

    # 2b. Reorder pass — cbx-mv ops apply sequentially. After indent
    # changes have been promoted to rm+add (above), every surviving
    # item has the same indent level it had on the server. We can
    # therefore reduce the move problem to:
    #   * top-level reorder among parents
    #   * sibling reorder within each parent's children
    # Each is a flat list and the standard "walk left-to-right, find
    # the wanted id, emit cbx-mv [j] [i]" sequential algorithm works.
    surviving_old = sorted(
        (old_by_id[cid] for cid in old_by_id if cid not in removed_ids),
        key=lambda it: tuple(it.position),
    )
    target_order = [it for it in new_items if it.cbx_id in new_by_id]

    def _emit_flat_moves(cur: list[str], tgt: list[str], pos_prefix: list[int]) -> None:
        """Emit cbx-mv ops to transform ``cur`` (cbx_ids in current
        tree order) into ``tgt``. Positions are encoded as
        ``pos_prefix + [i]`` — empty prefix for top-level, ``[parent_i]``
        for a parent's children. Mutates ``cur`` to track the running
        server-side state as each move is applied."""
        if cur == tgt:
            return
        for i in range(len(tgt)):
            if i < len(cur) and cur[i] == tgt[i]:
                continue
            try:
                j = cur.index(tgt[i], i)
            except ValueError:
                # Shouldn't happen if cur and tgt have the same set of
                # ids, but bail rather than emit invalid positions.
                continue
            ops.append([
                "cbx-mv", list_sct_id,
                pos_prefix + [j], pos_prefix + [i],
            ])
            cur.insert(i, cur.pop(j))

    # Top-level reorder among parents.
    cur_top = [it.cbx_id for it in surviving_old if len(tuple(it.position)) <= 1]
    tgt_top = [it.cbx_id for it in target_order if len(tuple(it.position)) <= 1]
    if set(cur_top) == set(tgt_top):
        _emit_flat_moves(list(cur_top), tgt_top, [])

    # Per-parent sibling reorder. A parent's "current" children are
    # whatever items currently live under index `parent_idx` in the
    # post-top-level-reorder server state; their target children are
    # whatever lives under that parent's NEW index in target_order.
    # We key by parent cbx_id so the reorder survives a shifted parent.
    def _children_by_parent(items, ids_in_top_order):
        out: dict[str, list[str]] = {p: [] for p in ids_in_top_order}
        parent_lookup = list(ids_in_top_order)
        for it in items:
            pos = tuple(it.position)
            if len(pos) <= 1:
                continue
            parent_idx = pos[0]
            if 0 <= parent_idx < len(parent_lookup):
                out.setdefault(parent_lookup[parent_idx], []).append(it.cbx_id)
        return out

    cur_kids = _children_by_parent(surviving_old, cur_top)
    tgt_kids = _children_by_parent(target_order, tgt_top)
    for parent_id, tgt_ids in tgt_kids.items():
        cur_ids = cur_kids.get(parent_id, [])
        if set(cur_ids) != set(tgt_ids):
            # Set differs — child added or removed at this level.
            # Skip: those rows were already handled by removals or
            # the indent-change rm+add path.
            continue
        # The parent's NEW top-level index drives the cbx-mv prefix.
        try:
            parent_idx = tgt_top.index(parent_id)
        except ValueError:
            continue
        _emit_flat_moves(list(cur_ids), tgt_ids, [parent_idx])

    # 3. Fresh additions.
    for it in fresh_items:
        cbx_id = it.cbx_id or _mint_cbx_id()
        pos = list(it.position) if it.position else [len(old_items)]
        ops.append(["cbx-add", list_sct_id, cbx_id, pos])
        if it.text:
            ops.append([
                "docs-nestedModel",
                ["text", 0, list_sct_id, cbx_id],
                {"ty": "is", "ibi": 1, "s": it.text},
            ])
        if it.checked:
            ops.append([
                "cbx-p", list_sct_id, cbx_id, [], ["cb:ck", True],
            ])

    return ops

