"""HTML paragraph -> plain Japanese text, with semantic ruby kept.

Ruby policy:
    * plain furigana (a hiragana reading over kanji) is kept only on its
      FIRST occurrence in the work (``finalize_furigana``) — that is where an
      author introduces how a name or rare word is read, which the translator
      needs to romanize it — and dropped on every repeat;
    * a reading that carries meaning (katakana/latin/kanji reading over kanji,
      e.g. 本気《マジ》, 魔法《マジック》, or ruby over kana) is kept inline as
      ``base《reading》`` — the notation the Vietnamese prompt documents;
    * emphasis dots (傍点: a reading made only of ・﹅ etc.) become *base*.

Everything else in the page (scripts, links, buttons, markup) is discarded:
only text nodes survive, and ASCII angle brackets are turned into their
fullwidth forms so no scraped text can close or open a prompt envelope tag.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional

from bs4 import NavigableString, Tag

_HIRAGANA_RE = re.compile(r"^[ぁ-ゟー\s]+$")
_KANJI_RE = re.compile(r"[㐀-䶿一-鿿々〆ヶ]")
_BOUTEN_RE = re.compile(r"^[・﹅﹆●◦。、゛\s]+$")
# Private-use delimiters marking plain furigana until the whole work has been
# read in order (see finalize_furigana). They never reach the output.
_FURI_OPEN, _FURI_SEP, _FURI_CLOSE = "\ue000", "\ue001", "\ue002"
_FURI_RE = re.compile("\ue000([^\ue000-\ue002]*)\ue001([^\ue000-\ue002]*)\ue002")


def _ruby_parts(tag: Tag) -> tuple[str, str]:
    base_parts: List[str] = []
    reading_parts: List[str] = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            base_parts.append(str(child))
        elif isinstance(child, Tag):
            name = child.name.lower()
            if name == "rt":
                reading_parts.append(child.get_text())
            elif name == "rp":
                continue
            else:
                base_parts.append(child.get_text())
    return "".join(base_parts).strip(), "".join(reading_parts).strip()


def render_ruby(base: str, reading: str) -> str:
    if not reading:
        return base
    if _BOUTEN_RE.match(reading):
        return f"*{base}*"
    if _KANJI_RE.search(base) and _HIRAGANA_RE.match(reading):
        return f"{_FURI_OPEN}{base}{_FURI_SEP}{reading}{_FURI_CLOSE}"
    return f"{base}《{reading}》"


def node_text(node: Tag) -> str:
    out: List[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(str(child))
        elif isinstance(child, Tag):
            name = child.name.lower()
            if name in ("script", "style", "button", "noscript", "template", "iframe"):
                continue
            if name == "br":
                out.append("\n")
            elif name == "ruby":
                out.append(render_ruby(*_ruby_parts(child)))
            elif name in ("em", "strong", "b"):
                inner = node_text(child)
                out.append(f"*{inner}*" if inner.strip() else inner)
            else:
                out.append(node_text(child))
    return "".join(out)


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("<", "＜").replace(">", "＞")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip("\n").rstrip()


def paragraph_texts(container: Optional[Tag]) -> List[str]:
    """One string per <p> (a web-novel line). Blank lines become "" so
    deliberate spacing can be kept as paragraph gaps."""
    if container is None:
        return []
    lines: List[str] = []
    for p in container.find_all("p"):
        text = clean_text(node_text(p))
        # The leading fullwidth space is the author's typed indent; the EPUB
        # stylesheet indents paragraphs itself.
        lines.append(text.lstrip(" 　") if text.strip(" 　\n") else "")
    return lines


def finalize_furigana(text: str, seen: set) -> str:
    """Resolve furigana markers in reading order: the first time a
    (base, reading) pair appears it is kept as ``base《reading》``, after
    that only the base remains. ``seen`` is shared across the whole work."""
    def repl(match: "re.Match[str]") -> str:
        key = (match.group(1), match.group(2))
        if key in seen:
            return match.group(1)
        seen.add(key)
        return f"{match.group(1)}《{match.group(2)}》"

    return _FURI_RE.sub(repl, text)
