"""LLM-assisted build-out of the Vietnamese bible, chapter by chapter.

The volume is read in order in batches of chapters. Each batch sees the bible
as it stands after the previous batches (never later chapters), so every
proposal is tagged with the chapter it first appears in and the per-chapter
prompt block never reveals it earlier.

Evidence discipline:
    * a proposal marked ``confirmed`` must quote a span that literally occurs
      in the batch's Japanese text; otherwise it is downgraded to ``inferred``;
    * a Vietnamese address pair (xưng/gọi) is a translation decision, not a
      fact of the source, so it is recorded as ``inferred`` no matter what the
      model says; only the operator locks it;
    * merges go through bible.merge_* so locked and stronger entries are never
      overwritten — disagreements become open ambiguities.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.Deepseek.common.chapter_order import in_reading_order
from src.utility.vietnamese.bible import (
    load_volume_bible,
    merge_character,
    merge_glossary_entry,
    merge_relationship,
    add_ambiguity,
    save_bible,
    volume_bible_path,
    empty_bible,
)

logger = logging.getLogger(__name__)

ANALYZE_SYSTEM = """You build the Vietnamese localization bible for a Japanese light novel
that will be translated into Vietnamese. You read a batch of chapters in story
order plus the bible as established so far, and you propose additions.

Rules:
- Report only what this batch supports. Do not use knowledge of the series from
  outside the given text, and do not anticipate later events.
- Evidence levels: "confirmed" = the text states it; you MUST then give a
  "quote" copied exactly from the Japanese text. "inferred" = a reasoned
  reading. "unknown" = not established. Never infer gender from occupation,
  hobbies, emotional expression or politeness alone; use "unknown".
- Names: give a Vietnamese-reader form. Japanese names in Hepburn romaji without
  macrons (ou/uu for long vowels), in the order the text uses. A hiragana reading
  shown as 漢字《よみ》 is the author's reading: follow it.
- Terms (magic, skills, items, titles, organizations, places, world terms):
  propose natural Vietnamese; Hán Việt only where it is the genre convention.
- Address (xưng/gọi): for each speaker→addressee pair that actually talks in
  this batch, propose how the speaker should refer to themself ("self") and to
  the addressee ("other") in Vietnamese, given age/rank, intimacy, setting and
  the Japanese first-person and honorific signals. Give "from_chapter" = the
  chapter id where this pair (or a change in it) first appears, and a short
  "quote" of a line that shows the dynamic. Add "context" only when the pair
  is setting-specific (e.g. "trước mặt người ngoài").
- Record genuine open questions (unclear gender, unclear relation, ambiguous
  term) under "ambiguities" instead of guessing.
- Use existing character ids from the bible; for new characters use new ids
  of the form "v<number>".

Return ONLY a JSON object:
{"characters": [{"id","jp","aliases_jp":[],"reading","vi","gender":{"value":"male|female|unknown","evidence","quote"},
                 "age_or_rank":{"value","evidence","quote"},"role","first_chapter"}],
 "relationships": [{"from","to","relation","intimacy","social","evidence","quote",
                    "address":[{"self","other","from_chapter","context","quote"}]}],
 "glossary": [{"jp","vi","category","policy":"translate|keep|transliterate","evidence","first_chapter","notes"}],
 "recurring_phrases": [{"jp","vi","speaker","first_chapter","notes"}],
 "timeline": [{"chapter","event","evidence"}],
 "ambiguities": [{"topic","detail","chapter"}]}"""


def _verify(evidence: Any, quote: Any, source: str) -> str:
    level = str(evidence or "inferred").strip().lower()
    if level not in ("confirmed", "inferred", "unknown"):
        level = "inferred"
    if level == "confirmed" and not (quote and str(quote).strip() and str(quote).strip() in source):
        return "inferred"
    return level


def _bible_summary(bible: Dict[str, Any]) -> str:
    compact = {
        "characters": {cid: {k: c.get(k) for k in ("jp", "vi", "gender", "aliases_jp", "locked")}
                       for cid, c in (bible.get("characters") or {}).items()},
        "relationships": [{k: r.get(k) for k in ("from", "to", "relation", "address")} for r in bible.get("relationships") or []],
        "glossary": [{k: g.get(k) for k in ("jp", "vi", "locked")} for g in bible.get("glossary") or [] if g.get("vi")],
    }
    return json.dumps(compact, ensure_ascii=False)


def apply_proposals(bible: Dict[str, Any], proposals: Dict[str, Any], *, batch_source: str,
                    chapter_ids: List[str]) -> Dict[str, Any]:
    known = set(chapter_ids)
    default_chapter = chapter_ids[0] if chapter_ids else None

    def chap(value: Any) -> Optional[str]:
        value = str(value or "").strip()
        return value if value in known else default_chapter

    for raw in proposals.get("characters") or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        gender = raw.get("gender") or {}
        age = raw.get("age_or_rank") or {}
        incoming = {
            "jp": str(raw.get("jp") or ""), "aliases_jp": [a for a in raw.get("aliases_jp") or [] if a],
            "reading": str(raw.get("reading") or ""), "vi": str(raw.get("vi") or ""), "name_policy": "keep",
            "locked": False, "role": str(raw.get("role") or ""), "provenance": "analysis",
            "gender": {"value": str(gender.get("value") or "unknown"),
                       "evidence": _verify(gender.get("evidence"), gender.get("quote"), batch_source),
                       "source": str(gender.get("quote") or "")[:200]},
            "age_or_rank": {"value": str(age.get("value") or ""),
                            "evidence": _verify(age.get("evidence"), age.get("quote"), batch_source)},
            "introduced_in": chap(raw.get("first_chapter")),
        }
        if incoming["gender"]["value"] not in ("male", "female"):
            incoming["gender"] = {"value": "unknown", "evidence": "unknown", "source": ""}
        merge_character(bible, str(raw["id"]), incoming, raised_in=incoming["introduced_in"])

    for raw in proposals.get("relationships") or []:
        if not isinstance(raw, dict) or not raw.get("from") or not raw.get("to"):
            continue
        if raw["from"] not in bible.get("characters", {}) or raw["to"] not in bible.get("characters", {}):
            continue
        states = []
        for state in raw.get("address") or []:
            if not isinstance(state, dict) or not (state.get("self") or state.get("other")):
                continue
            states.append({"self": str(state.get("self") or ""), "other": str(state.get("other") or ""),
                           "from_chapter": chap(state.get("from_chapter")), "context": str(state.get("context") or ""),
                           "evidence": "inferred", "quote": str(state.get("quote") or "")[:200]})
        merge_relationship(bible, {
            "from": raw["from"], "to": raw["to"], "relation": str(raw.get("relation") or ""),
            "intimacy": str(raw.get("intimacy") or ""), "social": str(raw.get("social") or ""),
            "evidence": _verify(raw.get("evidence"), raw.get("quote"), batch_source), "address": states,
            "provenance": "analysis", "notes": "",
        }, raised_in=default_chapter)

    for list_name in ("glossary", "recurring_phrases"):
        for raw in proposals.get(list_name) or []:
            if not isinstance(raw, dict) or not raw.get("jp"):
                continue
            if str(raw["jp"]) not in batch_source:
                continue  # a term the batch does not contain cannot be tagged to it
            merge_glossary_entry(bible, {
                "jp": str(raw["jp"]), "vi": str(raw.get("vi") or ""),
                "category": str(raw.get("category") or ("recurring_phrase" if list_name == "recurring_phrases" else "other")),
                "policy": str(raw.get("policy") or "translate"), "locked": False, "never_use": [],
                "evidence": "inferred", "introduced_in": chap(raw.get("first_chapter")),
                "speaker": str(raw.get("speaker") or ""), "provenance": "analysis", "notes": str(raw.get("notes") or ""),
            }, list_name=list_name, raised_in=chap(raw.get("first_chapter")))

    for raw in proposals.get("timeline") or []:
        if isinstance(raw, dict) and raw.get("event"):
            item = {"chapter": chap(raw.get("chapter")), "event": str(raw["event"])[:300],
                    "evidence": _verify(raw.get("evidence"), None, batch_source)}
            if item not in bible.setdefault("timeline", []):
                bible["timeline"].append(item)
    for raw in proposals.get("ambiguities") or []:
        if isinstance(raw, dict) and raw.get("topic"):
            add_ambiguity(bible, str(raw["topic"]), str(raw.get("detail") or ""), chap(raw.get("chapter")))
    return bible


def analyze_volume(work_dir: Path, assistant, *, batch_chars: int = 40000,
                   chapters: Optional[List[str]] = None, force: bool = False) -> Dict[str, Any]:
    work_dir = Path(work_dir)
    bible = load_volume_bible(work_dir) or empty_bible(volume_id=work_dir.name)
    # Story order (CHAPTER_9 < CHAPTER_10 < CHAPTER_100): each batch must see
    # only what earlier chapters established.
    jp_files = in_reading_order((work_dir / "JP").glob("*.md"))
    if chapters is not None:
        wanted = set(chapters)
        jp_files = [p for p in jp_files if p.stem in wanted]
    # Keep progress in the same atomic file as the proposals: a failed batch
    # must never be stamped as done. Hash the text actually sent to the model.
    progress = bible.setdefault("analysis_chapters", {})
    sources = {p: p.read_text(encoding="utf-8") for p in jp_files}
    hashes = {p.stem: hashlib.sha256(text.encode("utf-8")).hexdigest()
              for p, text in sources.items()}
    jp_files = [p for p in jp_files if force or progress.get(p.stem) != hashes[p.stem]]
    logger.info("[VI-BIBLE] %d chapter(s) need analysis", len(jp_files))
    batches: List[List[Path]] = [[]]
    size = 0
    for path in jp_files:
        length = len(sources[path])
        if batches[-1] and size + length > batch_chars:
            batches.append([])
            size = 0
        batches[-1].append(path)
        size += length
    for n, batch in enumerate(b for b in batches if b):
        texts = {p.stem: sources[p] for p in batch}
        source = "\n\n".join(texts.values())
        user = (f"<bible_so_far>\n{_bible_summary(bible)}\n</bible_so_far>\n\n" + "\n\n".join(
            f"<chapter id='{cid}'>\n{text}\n</chapter>" for cid, text in texts.items()))
        result = assistant.complete_json(system=ANALYZE_SYSTEM, user=user, label=f"bible-vi-batch{n + 1}")
        if result.get("dry_run"):
            logger.info("[VI-BIBLE] dry run: batch %d payload at %s", n + 1, result.get("payload_path"))
            continue
        apply_proposals(bible, result, batch_source=source, chapter_ids=list(texts))
        progress.update({cid: hashes[cid] for cid in texts})
        save_bible(volume_bible_path(work_dir), bible)   # checkpoint after every batch
    return bible


def lock_established(bible: Dict[str, Any]) -> int:
    """Operator approval: lock every character name and term that has a
    Vietnamese form, so later analysis can never change them."""
    count = 0
    for char in (bible.get("characters") or {}).values():
        if char.get("vi") and not char.get("locked"):
            char["locked"] = True
            count += 1
    for list_name in ("glossary", "recurring_phrases"):
        for entry in bible.get(list_name) or []:
            if entry.get("vi") and not entry.get("locked"):
                entry["locked"] = True
                count += 1
    return count
