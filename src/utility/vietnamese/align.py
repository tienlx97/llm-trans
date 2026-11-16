"""Block-level JP <-> translation alignment, used to trace QC findings back
to the Japanese source and to cut chapters into reviewable segments.

Hard anchors are structure the translation must preserve one-to-one: scene
breaks, illustration tags and headings. Between two anchors, prose blocks are
mapped proportionally by cumulative length, which is robust to the merges and
splits a literary translation legitimately makes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

SCENE_BREAK_RE = re.compile(r"^\s*(\*\s*\*\s*\*|[＊*◆◇★☆▼▽●○※§◎■□]{1,5}(?:[\s　]*[＊*◆◇★☆▼▽●○※§◎■□]{1,5}){0,5})\s*$")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)|\[ILLUSTRATION:[^\]]+\]")
HEADING_RE = re.compile(r"^#{1,6}\s")


@dataclass
class Block:
    index: int
    kind: str      # heading | break | image | text
    text: str


def split_blocks(markdown: str) -> List[Block]:
    blocks: List[Block] = []
    for raw in re.split(r"\n\s*\n", (markdown or "").replace("\r\n", "\n")):
        text = raw.strip()
        if not text:
            continue
        if SCENE_BREAK_RE.match(text):
            kind = "break"
        elif IMAGE_RE.fullmatch(text):
            kind = "image"
        elif HEADING_RE.match(text):
            kind = "heading"
        else:
            kind = "text"
        blocks.append(Block(index=len(blocks), kind=kind, text=text))
    return blocks


def _sections(blocks: List[Block]) -> List[List[Block]]:
    sections: List[List[Block]] = [[]]
    for block in blocks:
        if block.kind in ("break", "image"):
            sections.append([])
        elif block.kind == "text":
            sections[-1].append(block)
    return sections


def _proportional(src: List[Block], dst: List[Block]) -> List[Tuple[List[int], List[int]]]:
    """Map each source block to the destination blocks overlapping its share
    of the section's length."""
    if not src:
        return [([], [b.index for b in dst])] if dst else []
    if not dst:
        return [([b.index], []) for b in src]
    src_total = sum(len(b.text) for b in src) or 1
    dst_total = sum(len(b.text) for b in dst) or 1
    dst_bounds, acc = [], 0
    for b in dst:
        start = acc / dst_total
        acc += len(b.text)
        dst_bounds.append((start, acc / dst_total, b.index))
    pairs, acc = [], 0
    for b in src:
        start = acc / src_total
        acc += len(b.text)
        end = acc / src_total
        hits = [idx for (s, e, idx) in dst_bounds if e > start + 1e-9 and s < end - 1e-9]
        if not hits:
            hits = [min(dst_bounds, key=lambda t: abs(t[0] - start))[2]]
        pairs.append(([b.index], hits))
    return pairs


def align(jp_markdown: str, tr_markdown: str) -> Tuple[List[Block], List[Block], List[Tuple[List[int], List[int]]], bool]:
    """``(jp_blocks, tr_blocks, pairs, anchored)``. ``anchored`` is False when
    the anchor structure differs (then the whole chapter is one section and
    locations are approximate)."""
    jp_blocks, tr_blocks = split_blocks(jp_markdown), split_blocks(tr_markdown)
    jp_secs, tr_secs = _sections(jp_blocks), _sections(tr_blocks)
    anchored = len(jp_secs) == len(tr_secs)
    if not anchored:
        jp_secs = [[b for b in jp_blocks if b.kind == "text"]]
        tr_secs = [[b for b in tr_blocks if b.kind == "text"]]
    pairs: List[Tuple[List[int], List[int]]] = []
    for jp_sec, tr_sec in zip(jp_secs, tr_secs):
        pairs.extend(_proportional(jp_sec, tr_sec))
    return jp_blocks, tr_blocks, pairs, anchored


def jp_blocks_for_tr(pairs: List[Tuple[List[int], List[int]]], tr_index: int) -> List[int]:
    out: List[int] = []
    for jp_idx, tr_idx in pairs:
        if tr_index in tr_idx:
            out.extend(jp_idx)
    return sorted(set(out))


def segments(jp_blocks: List[Block], tr_blocks: List[Block], pairs, max_chars: int = 2500) -> List[Tuple[List[int], List[int]]]:
    """Group consecutive pairs into review windows of ~max_chars JP chars."""
    jp_len = {b.index: len(b.text) for b in jp_blocks}
    out: List[Tuple[List[int], List[int]]] = []
    cur_jp: List[int] = []
    cur_tr: List[int] = []
    size = 0
    for jp_idx, tr_idx in pairs:
        cur_jp.extend(jp_idx)
        cur_tr.extend(i for i in tr_idx if i not in cur_tr)
        size += sum(jp_len.get(i, 0) for i in jp_idx)
        if size >= max_chars:
            out.append((cur_jp, cur_tr))
            cur_jp, cur_tr, size = [], [], 0
    if cur_jp or cur_tr:
        out.append((cur_jp, cur_tr))
    return out
