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


def _decode_ops(ops: list[Any], revision: Optional[str] = None) -> StyledDoc:
    sct_id: Optional[str] = None
    text_chars: list[str] = []                  # 0-based, populated from `is` ops
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
            for i, ch in enumerate(s):
                text_chars.insert(insert_idx + i, ch)
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

    for i, ch in enumerate(text_chars):
        pos1 = i + 1   # 1-based
        if ch == "\n":
            _close_paragraph(pos1)
            continue
        style = text_styles[i] or {}
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

    `doc.sct_id` must match the existing note's sct id."""
    if not doc.sct_id:
        raise ValueError("StyledDoc.sct_id is required for encode_full_replace")
    target = ["text", 1, doc.sct_id]
    ops: list = []
    if current_text_length > 0:
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
            ops.append([
                "docs-nestedModel", target,
                {"ty": "ds", "si": i1 + 1, "ei": i2},
            ])
        if tag in ("insert", "replace") and j2 > j1:
            ops.append([
                "docs-nestedModel", target,
                {"ty": "is", "ibi": i1 + 1, "s": new_text[j1:j2]},
            ])

    # Style ops on inserted/changed regions in the NEW doc.
    changed_ranges: list[tuple[int, int]] = []
    for tag, _i1, _i2, j1, j2 in opcodes:
        if tag in ("insert", "replace") and j2 > j1:
            changed_ranges.append((j1 + 1, j2))   # 1-based inclusive

    if changed_ranges:
        ops.extend(_emit_styles_for_ranges(new_doc, target, changed_ranges))
    elif not _styles_equal(old_doc, new_doc):
        n = len(new_text)
        if n > 0:
            ops.extend(_emit_styles_for_ranges(new_doc, target, [(1, n)]))

    return ops


def _styles_equal(a: StyledDoc, b: StyledDoc) -> bool:
    """True iff a and b have identical per-character styles AND headings."""
    if len(a.paragraphs) != len(b.paragraphs):
        return False
    for pa, pb in zip(a.paragraphs, b.paragraphs):
        if pa.heading != pb.heading or pa.text != pb.text:
            return False
        a_flat = [(r.style_tuple(), len(r.text)) for r in pa.runs if r.text]
        b_flat = [(r.style_tuple(), len(r.text)) for r in pb.runs if r.text]
        if a_flat != b_flat:
            return False
    return True


def _emit_styles_for_ranges(
    doc: StyledDoc,
    target: list,
    ranges_1based: list[tuple[int, int]],
) -> list:
    ops: list = []
    pos = 1
    for p_idx, para in enumerate(doc.paragraphs):
        for run in para.runs:
            run_len = len(run.text)
            if run_len == 0:
                continue
            run_si = pos
            run_ei = pos + run_len - 1
            sm = _style_marker_dict(run)
            if sm and any(_range_intersects((run_si, run_ei), r) for r in ranges_1based):
                ops.append([
                    "docs-nestedModel", target,
                    {"ty": "as", "st": "text",
                     "si": run_si, "ei": run_ei, "sm": sm},
                ])
            pos += run_len
        if para.heading:
            anchor = pos
            if any(_range_intersects((anchor, anchor), r) for r in ranges_1based):
                sm_p = {
                    "ps_hd": para.heading,
                    "ps_hdid": para.heading_id or _mint_heading_id(),
                }
                ops.append([
                    "docs-nestedModel", target,
                    {"ty": "as", "st": "paragraph",
                     "si": anchor, "ei": anchor, "sm": sm_p},
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
                # Inclusive range [pos, pos + run_len - 1]
                ops.append([
                    "docs-nestedModel", target,
                    {
                        "ty": "as",
                        "st": "text",
                        "si": pos,
                        "ei": pos + run_len - 1,
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
            ops.append([
                "docs-nestedModel", target,
                {"ty": "as", "st": "paragraph", "si": anchor, "ei": anchor, "sm": sm},
            ])
        # Step over the paragraph terminator \n (except after the last para)
        if p_idx < len(doc.paragraphs) - 1:
            pos += 1
    return ops


def _style_marker_dict(run: "StyleRun") -> dict:
    """Build the `sm` dict for a styled text run.

    Per the wire protocol, each style key has a paired `<key>_i` flag
    indicating whether the value was inherited from a neighbouring run.
    For full-rewrite encoding we set `_i: false` (explicit) for every key
    we touch and omit keys whose value is the default (false)."""
    sm: dict = {}
    for name in ("bold", "italic", "underline", "strikethrough"):
        if getattr(run, name):
            wire = _TEXT_NAMES[name]
            sm[wire] = True
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
        parts.append(f"<{tag}>{''.join(inner) or '&nbsp;'}</{tag}>")
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
            chars: list[str] = b["text"]
            if ty == "is":
                ibi = int(body.get("ibi", 1))
                s = str(body.get("s", ""))
                insert_idx = max(0, ibi - 1)
                for i, ch in enumerate(s):
                    chars.insert(insert_idx + i, ch)
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
            text="".join(b["text"]),
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

    for idx, item in enumerate(new_items):
        cbx_id = item.cbx_id or _mint_cbx_id()
        ops.append(["cbx-add", list_sct_id, cbx_id, [idx]])
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
         - `cbx-mv` if its position changed.
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

    ops: list = []

    # 1. Removals — anything in old but not referenced by new. Emit
    # cbx-rm in DESCENDING position order so each rm doesn't shift
    # the indices used by later rms.
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
            text_ops: list = []
            for tag, i1, i2, j1, j2 in reversed(sm.get_opcodes()):
                if tag == "equal":
                    continue
                if tag in ("delete", "replace") and i2 > i1:
                    text_ops.append([
                        "docs-nestedModel", row_target,
                        {"ty": "ds", "si": i1 + 1, "ei": i2},
                    ])
                if tag in ("insert", "replace") and j2 > j1:
                    text_ops.append([
                        "docs-nestedModel", row_target,
                        {"ty": "is", "ibi": i1 + 1, "s": new_it.text[j1:j2]},
                    ])
            text_ops.reverse()
            ops.extend(text_ops)

        # --- checked toggle ---
        if bool(old_it.checked) != bool(new_it.checked):
            ops.append([
                "cbx-p", list_sct_id, cbx_id, [],
                ["cb:ck", bool(new_it.checked)],
            ])

    # 2b. Reorder pass — cbx-mv ops apply sequentially. Compute the
    # current order of surviving items (post-removals, pre-moves) and
    # the target order, then emit moves that incrementally transform
    # the former into the latter. Each cbx-mv shifts the positions of
    # everything between the source and the destination, so we walk
    # left-to-right and only move whichever item is in the wrong
    # cell at index `i`. Top-level rows only — nested indents fall
    # back to absolute-position emission below.
    surviving_old = sorted(
        (old_by_id[cid] for cid in old_by_id if cid not in removed_ids),
        key=lambda it: tuple(it.position),
    )
    target_order = [it for it in new_items if it.cbx_id in new_by_id]
    cur_ids = [it.cbx_id for it in surviving_old]
    tgt_ids = [it.cbx_id for it in target_order]
    only_top_level = (
        all(len(tuple(it.position)) <= 1 for it in surviving_old)
        and all(len(tuple(it.position)) <= 1 for it in target_order)
    )
    if only_top_level and set(cur_ids) == set(tgt_ids):
        for i in range(len(tgt_ids)):
            if cur_ids[i] == tgt_ids[i]:
                continue
            j = cur_ids.index(tgt_ids[i], i)
            ops.append([
                "cbx-mv", list_sct_id, [j], [i],
            ])
            cur_ids.insert(i, cur_ids.pop(j))
    else:
        # Indented or mismatched-set fallback: emit absolute-position
        # cbx-mv per matched item whose position changed. May produce
        # redundant ops but keeps backwards-compatible behaviour.
        for cbx_id, new_it in new_by_id.items():
            old_it = old_by_id[cbx_id]
            if tuple(old_it.position) != tuple(new_it.position):
                ops.append([
                    "cbx-mv", list_sct_id,
                    list(old_it.position),
                    list(new_it.position),
                ])

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

