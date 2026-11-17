"""Reading order for chapter files, shared by every route.

The Librarian pads chapter numbers to two digits (CHAPTER_01), so plain string
order breaks past 99 chapters: CHAPTER_100 sorts before CHAPTER_11. Web-novel
imports routinely exceed that, and every route's continuity (conversation
ledger, previous-chapter tail, batch waves) assumes chapters arrive in story
order.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Optional, Union

_DIGITS = re.compile(r"(\d+)")


def reading_order(path: Union[str, Path]) -> list:
    """Sort key: CHAPTER_9 < CHAPTER_10 < CHAPTER_100; _PART_ suffixes in order."""
    stem = Path(str(path)).stem
    # (0, int) / (1, str) keeps the key comparable when two stems differ in
    # whether a position holds digits or text.
    return [(0, int(part)) if part.isdigit() else (1, part) for part in _DIGITS.split(stem)]


def in_reading_order(paths: Iterable[Path]) -> List[Path]:
    return sorted(paths, key=reading_order)


_TRAILING_INDEX = re.compile(r"^(.*?)(\d+)$")


def preceding_chapter_id(chapter_id: str) -> Optional[str]:
    """The id immediately before ``chapter_id`` (None for the first, or for an
    id with no trailing number).

    Re-padded the way the Librarian names files (two digits minimum), NOT to
    the width of the input: CHAPTER_100's predecessor is CHAPTER_99, and
    padding to three digits ("CHAPTER_099") names a file that never exists.
    """
    match = _TRAILING_INDEX.match(str(chapter_id))
    if match is None:
        return None
    index = int(match.group(2))
    return None if index <= 1 else f"{match.group(1)}{index - 1:02d}"
