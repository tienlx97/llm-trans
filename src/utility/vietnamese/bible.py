"""Vietnamese Series Bible: characters, relationship/pronoun matrix, glossary.

Storage (never touches the English bible files):
    volume: work/<vol>/.context/bible_vi.json
    series: bibles/<series_id>/vi/bible_vi.json

Every fact carries an evidence level:
    confirmed — the source text states it (an LLM proposal is only accepted
                as confirmed when it quotes a span that literally occurs in
                the JP source; see analyze.py)
    inferred  — a reasoned reading; a default the source may override
    unknown   — not established; the translator must not resolve it

Merge rules (volume -> series, analysis -> volume):
    * a locked entry is never changed; a disagreeing proposal is recorded as
      an open ambiguity instead
    * a weaker evidence level never overwrites a stronger one
    * two different values at the same level keep the existing value (the
      established choice wins) and record an ambiguity

Spoiler control: entries carry ``introduced_in`` ("series" for anything
inherited from an earlier volume, else a chapter id). The per-chapter block
only includes entries introduced at or before that chapter, and — for
characters, terms and relationships — only those whose JP surface occurs in
the chapter's source.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 1
EVIDENCE_RANK = {"unknown": 0, "inferred": 1, "confirmed": 2}
SERIES = "series"
VOLUME_BIBLE_REL = Path(".context") / "bible_vi.json"

GLOSSARY_CATEGORIES = (
    "name", "place", "organization", "title", "magic", "skill", "item",
    "world_term", "honorific", "wordplay", "recurring_phrase", "other",
)
NAME_POLICIES = ("keep", "transliterate", "translate")


# ── construction / io ─────────────────────────────────────────────────────

def empty_bible(*, volume_id: str = "", series_id: str = "") -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "target_language": "vi",
        "series_id": series_id,
        "volume_id": volume_id,
        # Vietnamese book metadata for the built EPUB; empty -> JP original title.
        "metadata": {"title_vi": "", "series_vi": "", "author_vi": ""},
        "honorific_policy": "localize",   # localize | keep
        "dialogue_style": "quotes",       # quotes | dash
        "narration": {"pov_character": None, "narrator_self": "tôi"},
        "characters": {},
        "relationships": [],
        "glossary": [],
        "recurring_phrases": [],
        "voice_fingerprints": {},
        "worldbuilding": [],
        "timeline": [],
        "ambiguities": [],
        "decisions": [],
        "volumes": [],
    }


def volume_bible_path(work_dir: Path) -> Path:
    return Path(work_dir) / VOLUME_BIBLE_REL


def series_bible_path(bible_root: Path, series_id: str) -> Path:
    if not series_id or not re.fullmatch(r"[\w.\-]+", series_id):
        raise ValueError(f"Invalid series id {series_id!r}")
    return Path(bible_root) / series_id / "vi" / "bible_vi.json"


def load_bible(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    base = empty_bible()
    base.update(data)
    return base


def save_bible(path: Path, bible: Dict[str, Any]) -> Path:
    from src.Deepseek.common.atomic_io import atomic_write_json

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, bible)
    return path


# ── ordering ──────────────────────────────────────────────────────────────

def chapter_order(chapter_id: Optional[str]) -> Tuple[int, int, str]:
    """``series`` < any chapter; chapters sort by their trailing number."""
    if not chapter_id or chapter_id == SERIES:
        return (0, 0, "")
    digits = re.findall(r"\d+", str(chapter_id))
    return (1, int(digits[-1]) if digits else 0, str(chapter_id))


def effective_at(introduced_in: Optional[str], chapter_id: Optional[str]) -> bool:
    if chapter_id is None:
        return True
    return chapter_order(introduced_in) <= chapter_order(chapter_id)


def _rank(evidence: Any) -> int:
    return EVIDENCE_RANK.get(str(evidence or "unknown").strip().lower(), 0)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── seeding from context.xml ──────────────────────────────────────────────

_MACRONS = {"ō": "ou", "ū": "uu", "ā": "aa", "ē": "ei", "ī": "ii",
            "Ō": "Ou", "Ū": "Uu", "Ā": "Aa", "Ē": "Ei", "Ī": "Ii"}
_GENDER_EVIDENCE = {"explicit": "confirmed", "inferred": "inferred", "unspecified": "unknown"}
_PRONOUN_GENDER = {"he/him": "male", "she/her": "female", "they/them": "unknown"}


def romaji_without_macrons(name: str) -> str:
    return "".join(_MACRONS.get(ch, ch) for ch in str(name or ""))


def _child_text(node: Optional[ET.Element], tag: str) -> str:
    if node is None:
        return ""
    child = node.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def seed_from_context(context_xml: str, *, volume_id: str = "", series_id: str = "") -> Dict[str, Any]:
    """Deterministic seed: characters (with prep's gender evidence), name
    surfaces and relationship dyads. No Vietnamese address pair is invented
    here — those come from analysis or the operator."""
    bible = empty_bible(volume_id=volume_id, series_id=series_id)
    try:
        root = ET.fromstring(context_xml) if context_xml else None
    except ET.ParseError:
        root = None
    if root is None:
        return bible

    roster = root.find("character_roster")
    jp_to_id: Dict[str, str] = {}
    canonical_to_id: Dict[str, str] = {}
    for node in (roster.findall("character") if roster is not None else []):
        cid = str(node.get("id") or "").strip()
        if not cid:
            continue
        identity = node.find("identity")
        jp_name = _child_text(identity, "jp_name")
        canonical = str(node.get("canonical_name") or "").strip()
        gender_node = identity.find("gender") if identity is not None else None
        pronouns = (gender_node.get("pronouns") if gender_node is not None else "") or ""
        evidence = _GENDER_EVIDENCE.get(
            str((gender_node.get("evidence") if gender_node is not None else "") or "unspecified").lower(), "unknown"
        )
        gender_value = _PRONOUN_GENDER.get(pronouns.strip().lower(), "unknown")
        if gender_value == "unknown":
            evidence = "unknown"
        is_pov = str(node.get("is_pov") or "").lower() == "true"
        bible["characters"][cid] = {
            "jp": jp_name,
            "aliases_jp": _jp_aliases(jp_name),
            "reading": _child_text(identity, "reading"),
            "vi": romaji_without_macrons(canonical),
            "name_policy": "keep",
            "locked": False,
            "gender": {
                "value": gender_value,
                "evidence": evidence,
                "source": ((gender_node.text or "").strip() if gender_node is not None else "")[:200],
            },
            "age_or_rank": {"value": _child_text(identity, "age"), "evidence": "inferred" if _child_text(identity, "age") else "unknown"},
            "role": _child_text(identity, "role"),
            "pov": is_pov,
            "narration_ref": "",
            "introduced_in": None,
            "provenance": "prep",
            "notes": "",
        }
        if is_pov and not bible["narration"]["pov_character"]:
            bible["narration"]["pov_character"] = cid
        if jp_name:
            jp_to_id[jp_name] = cid
        if canonical:
            canonical_to_id[canonical] = cid

    name_map = root.find("name_map")
    for entry in (name_map.findall("entry") if name_map is not None else []):
        jp, en = str(entry.get("jp") or "").strip(), str(entry.get("en") or "").strip()
        if not jp:
            continue
        owner = jp_to_id.get(jp) or next((cid for name, cid in jp_to_id.items() if jp in name or name in jp), None)
        if owner:
            aliases = bible["characters"][owner]["aliases_jp"]
            if jp not in aliases and jp != bible["characters"][owner]["jp"]:
                aliases.append(jp)
            continue
        # Non-character proper nouns: their English rendering is not a
        # Vietnamese one (生徒会 -> "Student Council"), so only the JP
        # surface is recorded; the Vietnamese form is left for analysis.
        bible["glossary"].append({
            "jp": jp, "vi": "", "category": "other", "policy": "translate", "locked": False,
            "never_use": [], "evidence": "unknown", "introduced_in": None, "provenance": "prep",
            "notes": f"English edition: {en}" if en else "",
        })

    graph = root.find("relationship_graph")
    for dyad in (graph.findall("dyad") if graph is not None else []):
        a = _resolve_character(dyad.get("character_a"), bible, canonical_to_id, jp_to_id)
        b = _resolve_character(dyad.get("character_b"), bible, canonical_to_id, jp_to_id)
        if not a or not b or a == b:
            continue
        bible["relationships"].append({
            "from": a, "to": b, "relation": str(dyad.get("type") or ""), "intimacy": "", "social": "",
            "evidence": "inferred", "address": [], "provenance": "prep", "notes": "",
        })
    return bible


def _jp_aliases(jp_name: str) -> List[str]:
    """Family and given name as separate surfaces (山田太郎 is addressed as 山田
    or 太郎 far more often than in full). Only split on an explicit space or
    middle dot; guessing a split point inside unspaced kanji is not safe."""
    parts = [p for p in re.split(r"[\s　・=＝]+", jp_name or "") if p]
    return parts if len(parts) > 1 else []


def _resolve_character(raw: Any, bible: Dict[str, Any], canonical_to_id: Dict[str, str], jp_to_id: Dict[str, str]) -> Optional[str]:
    value = str(raw or "").strip()
    if not value:
        return None
    if value in bible["characters"]:
        return value
    return canonical_to_id.get(value) or jp_to_id.get(value)


# ── merging ───────────────────────────────────────────────────────────────

def add_ambiguity(bible: Dict[str, Any], topic: str, detail: str, raised_in: Optional[str] = None) -> None:
    for existing in bible.setdefault("ambiguities", []):
        if existing.get("topic") == topic and existing.get("detail") == detail:
            return
    bible["ambiguities"].append({
        "id": f"amb{len(bible['ambiguities']) + 1}", "topic": topic, "detail": detail,
        "status": "open", "raised_in": raised_in, "resolution": "", "created": _now(),
    })


def _merge_fact(current: Dict[str, Any], incoming: Dict[str, Any], *, topic: str, bible: Dict[str, Any],
                locked: bool, raised_in: Optional[str]) -> Dict[str, Any]:
    """Merge one {value, evidence, ...} fact under the evidence rules."""
    current = dict(current or {"value": "", "evidence": "unknown"})
    incoming = dict(incoming or {})
    cur_val, new_val = current.get("value"), incoming.get("value")
    if not new_val or new_val == "unknown":
        return current
    if new_val == cur_val:
        if _rank(incoming.get("evidence")) > _rank(current.get("evidence")):
            current.update(incoming)
        return current
    if not cur_val or cur_val == "unknown":
        return incoming
    if locked:
        add_ambiguity(bible, topic, f"locked {cur_val!r} kept; proposal {new_val!r} ignored", raised_in)
        return current
    if _rank(incoming.get("evidence")) > _rank(current.get("evidence")):
        return incoming
    add_ambiguity(
        bible, topic,
        f"kept {current.get('value')!r} ({current.get('evidence')}); conflicting {incoming.get('value')!r} ({incoming.get('evidence')})",
        raised_in,
    )
    return current


def merge_character(bible: Dict[str, Any], cid: str, incoming: Dict[str, Any], raised_in: Optional[str] = None) -> None:
    chars = bible.setdefault("characters", {})
    if cid not in chars:
        chars[cid] = copy.deepcopy(incoming)
        chars[cid].setdefault("introduced_in", raised_in)
        return
    current = chars[cid]
    locked = bool(current.get("locked"))
    for fact in ("gender", "age_or_rank"):
        if fact in incoming:
            current[fact] = _merge_fact(current.get(fact) or {}, incoming[fact], topic=f"{cid}.{fact}",
                                        bible=bible, locked=locked, raised_in=raised_in)
    new_vi = str(incoming.get("vi") or "").strip()
    if new_vi and new_vi != current.get("vi"):
        if locked:
            add_ambiguity(bible, f"{cid}.vi", f"locked name {current.get('vi')!r} kept; proposal {new_vi!r} ignored", raised_in)
        elif not current.get("vi"):
            current["vi"] = new_vi
    for alias in incoming.get("aliases_jp") or []:
        if alias and alias not in current.setdefault("aliases_jp", []) and alias != current.get("jp"):
            current["aliases_jp"].append(alias)
    for key in ("narration_ref", "role", "notes"):
        if incoming.get(key) and not current.get(key):
            current[key] = incoming[key]


def _glossary_key(entry: Dict[str, Any]) -> str:
    return unicodedata.normalize("NFC", str(entry.get("jp") or "")).strip()


def merge_glossary_entry(bible: Dict[str, Any], incoming: Dict[str, Any], *, list_name: str = "glossary",
                         raised_in: Optional[str] = None) -> None:
    key = _glossary_key(incoming)
    if not key:
        return
    entries = bible.setdefault(list_name, [])
    for current in entries:
        if _glossary_key(current) != key:
            continue
        new_vi = str(incoming.get("vi") or "").strip()
        old_vi = str(current.get("vi") or "").strip()
        if new_vi and new_vi != old_vi:
            if current.get("locked"):
                add_ambiguity(bible, f"{list_name}:{key}", f"locked {old_vi!r} kept; proposal {new_vi!r} ignored", raised_in)
            elif not old_vi or _rank(incoming.get("evidence")) > _rank(current.get("evidence")):
                current["vi"] = new_vi
                current["evidence"] = incoming.get("evidence", current.get("evidence"))
            else:
                add_ambiguity(bible, f"{list_name}:{key}", f"kept {old_vi!r}; alternative {new_vi!r}", raised_in)
        for bad in incoming.get("never_use") or []:
            if bad and bad not in current.setdefault("never_use", []):
                current["never_use"].append(bad)
        for field in ("category", "notes"):
            if incoming.get(field) and not current.get(field):
                current[field] = incoming[field]
        return
    entry = copy.deepcopy(incoming)
    entry.setdefault("introduced_in", raised_in)
    entry.setdefault("locked", False)
    entry.setdefault("never_use", [])
    entries.append(entry)


def _relationship_key(rel: Dict[str, Any]) -> Tuple[str, str]:
    return (str(rel.get("from") or ""), str(rel.get("to") or ""))


def merge_relationship(bible: Dict[str, Any], incoming: Dict[str, Any], raised_in: Optional[str] = None) -> None:
    key = _relationship_key(incoming)
    if not all(key):
        return
    for current in bible.setdefault("relationships", []):
        if _relationship_key(current) != key:
            continue
        for field in ("relation", "intimacy", "social", "notes"):
            if incoming.get(field) and not current.get(field):
                current[field] = incoming[field]
        if _rank(incoming.get("evidence")) > _rank(current.get("evidence")):
            current["evidence"] = incoming["evidence"]
        for state in incoming.get("address") or []:
            _merge_address_state(bible, current, state, raised_in)
        return
    rel = copy.deepcopy(incoming)
    rel["address"] = []
    bible["relationships"].append(rel)
    for state in incoming.get("address") or []:
        _merge_address_state(bible, rel, state, raised_in)


def _merge_address_state(bible: Dict[str, Any], rel: Dict[str, Any], state: Dict[str, Any], raised_in: Optional[str]) -> None:
    state = dict(state)
    state.setdefault("from_chapter", raised_in or SERIES)
    state.setdefault("context", "")
    if not (state.get("self") or state.get("other")):
        return
    for current in rel.setdefault("address", []):
        same_slot = current.get("from_chapter") == state["from_chapter"] and current.get("context", "") == state["context"]
        if not same_slot:
            continue
        if (current.get("self"), current.get("other")) == (state.get("self"), state.get("other")):
            if _rank(state.get("evidence")) > _rank(current.get("evidence")):
                current["evidence"] = state.get("evidence")
                if state.get("quote"):
                    current["quote"] = state["quote"]
            return
        if current.get("locked"):
            add_ambiguity(bible, f"address {rel['from']}->{rel['to']}",
                          f"locked {current.get('self')}/{current.get('other')} kept; proposal {state.get('self')}/{state.get('other')} ignored",
                          raised_in)
            return
        if _rank(state.get("evidence")) > _rank(current.get("evidence")):
            current.update(state)
            return
        add_ambiguity(bible, f"address {rel['from']}->{rel['to']}",
                      f"kept {current.get('self')}/{current.get('other')}; alternative {state.get('self')}/{state.get('other')}",
                      raised_in)
        return
    rel["address"].append(state)


def merge_bible(base: Dict[str, Any], incoming: Dict[str, Any], *, raised_in: Optional[str] = None) -> Dict[str, Any]:
    """Merge ``incoming`` into a copy of ``base`` under the evidence rules."""
    out = copy.deepcopy(base)
    for cid, char in (incoming.get("characters") or {}).items():
        merge_character(out, cid, char, raised_in)
    for rel in incoming.get("relationships") or []:
        merge_relationship(out, rel, raised_in)
    for entry in incoming.get("glossary") or []:
        merge_glossary_entry(out, entry, raised_in=raised_in)
    for entry in incoming.get("recurring_phrases") or []:
        merge_glossary_entry(out, entry, list_name="recurring_phrases", raised_in=raised_in)
    for cid, voice in (incoming.get("voice_fingerprints") or {}).items():
        out.setdefault("voice_fingerprints", {}).setdefault(cid, voice)
    for list_name in ("worldbuilding", "timeline", "decisions"):
        seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in out.get(list_name) or []}
        for item in incoming.get(list_name) or []:
            if json.dumps(item, sort_keys=True, ensure_ascii=False) not in seen:
                out.setdefault(list_name, []).append(copy.deepcopy(item))
    for amb in incoming.get("ambiguities") or []:
        if amb.get("status", "open") == "open":
            add_ambiguity(out, str(amb.get("topic") or ""), str(amb.get("detail") or ""), amb.get("raised_in"))
    return out


def inherit_series(series_bible: Dict[str, Any], *, volume_id: str) -> Dict[str, Any]:
    """A series bible as the starting point of a new volume: everything it
    holds was established in an earlier volume, so it is effective from the
    first chapter ("series")."""
    out = copy.deepcopy(series_bible)
    out["volume_id"] = volume_id
    for char in (out.get("characters") or {}).values():
        char["introduced_in"] = SERIES
    for list_name in ("glossary", "recurring_phrases", "worldbuilding"):
        for entry in out.get(list_name) or []:
            entry["introduced_in"] = SERIES
    for rel in out.get("relationships") or []:
        if rel.get("address"):
            # Only the state the previous volume ENDED in carries over.
            latest = max(rel["address"], key=lambda s: (chapter_order(s.get("from_chapter")), s.get("context") == ""))
            carried = [dict(s, from_chapter=SERIES) for s in rel["address"]
                       if s.get("from_chapter") == latest.get("from_chapter")]
            rel["address"] = carried
    return out


def promote_to_series(volume_bible: Dict[str, Any], series_bible: Optional[Dict[str, Any]], *, volume_id: str) -> Dict[str, Any]:
    """Fold a finished volume into the series bible. Volume-local chapter
    positions collapse to the volume: in the next volume they are history."""
    base = series_bible or empty_bible(series_id=volume_bible.get("series_id", ""))
    merged = merge_bible(base, volume_bible, raised_in=volume_id)
    volumes = merged.setdefault("volumes", [])
    if volume_id not in volumes:
        volumes.append(volume_id)
    merged["volume_id"] = ""
    return merged


# ── per-chapter rendering ─────────────────────────────────────────────────

def _surfaces(char: Dict[str, Any]) -> List[str]:
    return [s for s in [char.get("jp"), *(char.get("aliases_jp") or [])] if s]


def _present(surfaces: Iterable[str], source: Optional[str]) -> bool:
    if source is None:
        return True
    return any(s and s in source for s in surfaces)


def active_characters(bible: Dict[str, Any], chapter_id: Optional[str], source: Optional[str]) -> List[str]:
    pov = (bible.get("narration") or {}).get("pov_character")
    ids = []
    for cid, char in (bible.get("characters") or {}).items():
        if not effective_at(char.get("introduced_in"), chapter_id):
            continue
        if cid == pov or char.get("pov") or _present(_surfaces(char), source):
            ids.append(cid)
    return ids


def current_address(rel: Dict[str, Any], chapter_id: Optional[str]) -> List[Dict[str, Any]]:
    """The address states in force at ``chapter_id``: per context, the latest
    one whose from_chapter is not after it."""
    by_context: Dict[str, Dict[str, Any]] = {}
    for state in rel.get("address") or []:
        if not effective_at(state.get("from_chapter"), chapter_id):
            continue
        ctx = str(state.get("context") or "")
        prior = by_context.get(ctx)
        if prior is None or chapter_order(state.get("from_chapter")) >= chapter_order(prior.get("from_chapter")):
            by_context[ctx] = state
    return [by_context[k] for k in sorted(by_context)]


def render_localization_block(
    bible: Optional[Dict[str, Any]],
    *,
    chapter_id: Optional[str] = None,
    source: Optional[str] = None,
    max_entries: int = 60,
    dialogue_style: Optional[str] = None,
) -> str:
    """The ``<vietnamese_localization>`` block for one chapter turn."""
    bible = bible or empty_bible()
    style = dialogue_style or bible.get("dialogue_style") or "quotes"
    lines = [
        f'<vietnamese_localization chapter_id="{escape(str(chapter_id or ""))}">',
        f'  <policy honorifics="{escape(str(bible.get("honorific_policy") or "localize"))}" '
        f'dialogue_style="{escape(str(style))}" '
        f'narrator_self="{escape(str((bible.get("narration") or {}).get("narrator_self") or "tôi"))}" />',
    ]
    chars = bible.get("characters") or {}
    active = active_characters(bible, chapter_id, source)[:max_entries]
    if active:
        lines.append("  <characters>")
        for cid in active:
            c = chars[cid]
            gender = c.get("gender") or {}
            attrs = [
                f'id="{escape(cid)}"', f'jp="{escape(str(c.get("jp") or ""))}"',
                f'vi="{escape(str(c.get("vi") or ""))}"',
                f'gender="{escape(str(gender.get("value") or "unknown"))}"',
                f'gender_evidence="{escape(str(gender.get("evidence") or "unknown"))}"',
            ]
            if c.get("locked"):
                attrs.append('locked="true"')
            if c.get("pov") or cid == (bible.get("narration") or {}).get("pov_character"):
                attrs.append('pov="true"')
            rank = c.get("age_or_rank") or {}
            if rank.get("value"):
                attrs.append(f'age_or_rank="{escape(str(rank["value"]))}" age_evidence="{escape(str(rank.get("evidence") or "inferred"))}"')
            if c.get("narration_ref"):
                attrs.append(f'narration_ref="{escape(str(c["narration_ref"]))}"')
            aliases = ", ".join(c.get("aliases_jp") or [])
            if aliases:
                attrs.append(f'aliases_jp="{escape(aliases)}"')
            lines.append(f"    <character {' '.join(attrs)} />")
        lines.append("  </characters>")

    active_set = set(active)
    rel_lines: List[str] = []
    for rel in bible.get("relationships") or []:
        if rel.get("from") not in active_set or rel.get("to") not in active_set:
            continue
        states = current_address(rel, chapter_id)
        attrs = (f'from="{escape(str(rel["from"]))}" to="{escape(str(rel["to"]))}" '
                 f'relation="{escape(str(rel.get("relation") or ""))}" evidence="{escape(str(rel.get("evidence") or "unknown"))}"')
        if rel.get("intimacy"):
            attrs += f' intimacy="{escape(str(rel["intimacy"]))}"'
        if rel.get("social"):
            attrs += f' social="{escape(str(rel["social"]))}"'
        if not states:
            rel_lines.append(f"    <pair {attrs} />")
            continue
        rel_lines.append(f"    <pair {attrs}>")
        for s in states:
            ctx = f' context="{escape(str(s.get("context")))}"' if s.get("context") else ""
            lock = ' locked="true"' if s.get("locked") else ""
            rel_lines.append(
                f'      <address self="{escape(str(s.get("self") or ""))}" other="{escape(str(s.get("other") or ""))}" '
                f'evidence="{escape(str(s.get("evidence") or "inferred"))}" since="{escape(str(s.get("from_chapter") or SERIES))}"{ctx}{lock} />'
            )
        rel_lines.append("    </pair>")
    if rel_lines:
        lines.append("  <pronoun_matrix>")
        lines.append("    from = speaker, to = addressee; self = how the speaker refers to themself (xưng), other = how they address the addressee (gọi).")
        lines.extend(rel_lines)
        lines.append("  </pronoun_matrix>")

    term_lines: List[str] = []
    for list_name in ("glossary", "recurring_phrases"):
        for entry in bible.get(list_name) or []:
            if not entry.get("vi") or not effective_at(entry.get("introduced_in"), chapter_id):
                continue
            if not _present([entry.get("jp")], source):
                continue
            attrs = (f'jp="{escape(str(entry.get("jp")))}" vi="{escape(str(entry.get("vi")))}" '
                     f'category="{escape(str(entry.get("category") or list_name))}" '
                     f'evidence="{escape(str(entry.get("evidence") or "inferred"))}"')
            if entry.get("locked"):
                attrs += ' locked="true"'
            if entry.get("never_use"):
                attrs += f' never_use="{escape(", ".join(entry["never_use"]))}"'
            term_lines.append(f"    <term {attrs} />")
            if len(term_lines) >= max_entries:
                break
    if term_lines:
        lines.append("  <terms>")
        lines.extend(term_lines)
        lines.append("  </terms>")

    open_amb = [a for a in bible.get("ambiguities") or []
                if a.get("status", "open") == "open" and effective_at(a.get("raised_in") if str(a.get("raised_in") or "").upper().startswith("CHAPTER") else None, chapter_id)]
    relevant = [a for a in open_amb if any(cid in str(a.get("topic")) for cid in active_set) or "gender" in str(a.get("topic"))][:10]
    if relevant:
        lines.append("  <unresolved>")
        lines.append("    Open questions: do not settle these on your own; keep the rendering neutral where the source allows.")
        for a in relevant:
            lines.append(f'    <item topic="{escape(str(a.get("topic")))}">{escape(str(a.get("detail")))}</item>')
        lines.append("  </unresolved>")
    lines.append("</vietnamese_localization>")
    return "\n".join(lines)


# ── volume convenience ────────────────────────────────────────────────────

def load_volume_bible(work_dir: Path) -> Optional[Dict[str, Any]]:
    return load_bible(volume_bible_path(work_dir))


def seed_volume_bible(work_dir: Path, *, bible_root: Optional[Path] = None, series_id: str = "",
                      force: bool = False) -> Tuple[Dict[str, Any], Path]:
    """Create (or refresh) work/<vol>/.context/bible_vi.json.

    Order: inherited series bible (if any) -> existing volume bible (operator
    edits survive a re-seed) -> fresh context.xml seed. Each merge obeys the
    evidence and lock rules, so a re-seed never overwrites a decision.
    """
    work_dir = Path(work_dir)
    volume_id = work_dir.name
    path = volume_bible_path(work_dir)
    existing = None if force else load_bible(path)
    try:
        context_xml = (work_dir / "context.xml").read_text(encoding="utf-8")
    except OSError:
        context_xml = ""
    seeded = seed_from_context(context_xml, volume_id=volume_id, series_id=series_id)

    base: Dict[str, Any] = empty_bible(volume_id=volume_id, series_id=series_id)
    if series_id and bible_root is not None:
        series = load_bible(series_bible_path(bible_root, series_id))
        if series:
            base = inherit_series(series, volume_id=volume_id)
    if existing:
        base = merge_bible(base, existing)
        for key in ("honorific_policy", "dialogue_style", "narration", "metadata"):
            if existing.get(key):
                base[key] = existing[key]
    merged = merge_bible(base, seeded)
    if not merged.get("narration", {}).get("pov_character") and seeded["narration"].get("pov_character"):
        merged["narration"]["pov_character"] = seeded["narration"]["pov_character"]
    merged["volume_id"] = volume_id
    merged["series_id"] = series_id or merged.get("series_id", "")
    save_bible(path, merged)
    return merged, path
