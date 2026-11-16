"""XML prompt assembly for the Gemini translation route.

Layout follows Google's Gemini 3 long-context guidance rather than the
Anthropic route's: the system instruction carries the role, constraints, and
the volume's project context (stable for the whole volume, so it is the
cacheable prefix), and every user turn supplies its data FIRST and its task
LAST -- "supply all the context first. Place your specific instructions or
questions at the very end of the prompt" -- bridged by an explicit anchor
phrase ("Based on the source text above...").
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_SEMANTIC_METADATA_PLACEHOLDER = "SEMANTIC_METADATA_PLACEHOLDER"
_CHARACTER_VOICE_SLOT_COMMENT = "<!-- CHARACTER_VOICE_SLOT -->"
_TRANSLATION_POLICY_SLOT_COMMENT = "<!-- TRANSLATION_POLICY_SLOT -->"
# The static craft policy sits above this marker, the volume's project context
# below it. The marker is consumed on assembly and never reaches the model.
_PROJECT_CONTEXT_BOUNDARY = "<!-- PROJECT_CONTEXT_BOUNDARY -->"

_TRANSLATION_POLICY_RE = re.compile(
    r"^[ \t]*<translation_policy\b.*?</translation_policy>[ \t]*\r?\n?",
    re.DOTALL | re.MULTILINE,
)


def load_master_prompt(prompt_path: Path) -> str:
    return Path(prompt_path).read_text(encoding="utf-8")


def split_translation_policy(context_xml: Optional[str]) -> Tuple[Optional[str], str]:
    """Lift this volume's <translation_policy> out of the context document.

    Same contract as the Anthropic route: the policy is a constraint, not
    metadata, so it gets its own slot ahead of the project-context data and is
    removed from the data dump on the way past (a second copy would read as a
    second, possibly conflicting rule set).
    """
    if not context_xml:
        return context_xml, ""
    match = _TRANSLATION_POLICY_RE.search(context_xml)
    if match is None:
        return context_xml, ""
    remaining = context_xml[: match.start()] + context_xml[match.end() :]
    return remaining, match.group(0).strip()


def _fill_slot(prompt: str, slot: str, value: Optional[str]) -> str:
    block = (value or "").strip()
    if not block:
        return prompt.replace(slot + "\n", "").replace(slot, "")
    return prompt.replace(slot, block)


def build_system_segments(
    *, prompt_path: Path, context_xml: Optional[str] = None, voice_block: Optional[str] = None
) -> List[str]:
    """``[static_policy, project_context]`` -- or one segment when the prompt
    carries no boundary marker.

    The split is not a cache-breakpoint mechanism here as it is on the
    Messages API (Gemini caches by prefix, implicitly or through one
    CachedContent): it exists so the conversation ledger can fingerprint the
    two layers and so a dry-run payload shows which bytes are volume-specific.
    """
    prompt = load_master_prompt(prompt_path)
    context_xml, policy_block = split_translation_policy(context_xml)
    prompt = _fill_slot(prompt, _TRANSLATION_POLICY_SLOT_COMMENT, policy_block)
    prompt = prompt.replace(_SEMANTIC_METADATA_PLACEHOLDER, (context_xml or "").strip())
    prompt = _fill_slot(prompt, _CHARACTER_VOICE_SLOT_COMMENT, voice_block)
    index = prompt.find(_PROJECT_CONTEXT_BOUNDARY)
    if index < 0:
        return [prompt.strip()]
    static_policy = prompt[:index].rstrip()
    project_context = prompt[index + len(_PROJECT_CONTEXT_BOUNDARY) :].strip()
    return [segment for segment in (static_policy, project_context) if segment] or [prompt.strip()]


def join_system_segments(segments: Sequence[str]) -> str:
    """The single ``system_instruction`` string the wire accepts."""
    return "\n\n".join(str(segment).strip() for segment in segments if str(segment).strip())


def _format_chapter_ids(chapter_ids: Any) -> str:
    """``none`` is deliberate: an empty element reads as an unfilled slot, and
    the model must be able to tell "nothing is here" from "I was not told"."""
    ids = [str(chapter_id).strip() for chapter_id in (chapter_ids or []) if str(chapter_id).strip()]
    return ", ".join(ids) if ids else "none"


def build_continuity_block(chapter_id: str, continuity: Dict[str, Any]) -> List[str]:
    """Declare, by chapter id, what continuity THIS request carries -- never
    more. On the batch path a wave's members are mutually blind, and a model
    told its predecessor is present will guess at a callback it cannot see."""
    verbatim = continuity.get("verbatim_chapter_ids") or []
    summarized = continuity.get("summarized_chapter_ids") or []
    previous_id = str(continuity.get("previous_chapter_id") or "").strip()
    previous_status = str(continuity.get("previous_chapter_status") or "").strip() or "unknown"
    previous_tail = str(continuity.get("previous_chapter_tail") or "").strip()

    lines = [
        f'  <continuity_state chapter_id="{chapter_id}">',
        "    This block describes the continuity present in THIS request, and nothing beyond it.",
        f"    <translated_in_full>{_format_chapter_ids(verbatim)}</translated_in_full>",
        f"    <translated_summarized_only>{_format_chapter_ids(summarized)}</translated_summarized_only>",
    ]
    if previous_id:
        lines.append(f'    <immediately_preceding chapter_id="{previous_id}" status="{previous_status}" />')
    if previous_tail:
        lines.extend(
            [
                f'    <preceding_chapter_ending chapter_id="{previous_id}">',
                previous_tail,
                "    </preceding_chapter_ending>",
            ]
        )
    if previous_id and previous_status == "absent":
        lines.extend(
            [
                "    <gap_rule>",
                f"      {previous_id} is not in this request. The source text is authoritative: translate",
                "      what this chapter's source says. Resolve every locked term, name, honorific, and",
                "      signature phrase from anchor_locks and from the project context, never by inventing",
                "      a bridge rendering. Where a callback depends on wording you cannot see, phrase it so",
                "      it stays true whichever wording was used.",
                "    </gap_rule>",
            ]
        )
    lines.append("  </continuity_state>")
    return lines


def build_anchor_block(chapter_id: str, anchor_rows: List[Dict[str, Any]]) -> List[str]:
    """Locked phrases this chapter's source actually contains, with how earlier
    chapters already rendered them (harvested from EN/). Salience, not
    duplication: naming the two or three locks this chapter collides with,
    beside the source that collides with them, is a different act from having
    them somewhere in a long system instruction."""
    if not anchor_rows:
        return []
    lines = [
        f'  <anchor_locks chapter_id="{chapter_id}">',
        "    Series-locked renderings for phrases in this chapter's source. Use the locked English",
        "    surface exactly; do not coin a variant or substitute a synonym for variety.",
    ]
    for row in anchor_rows:
        lines.append(
            f'    <lock id="{row.get("anchor_id", "")}" '
            f'occurrences_in_this_source="{row.get("source_occurrences", 0)}">'
        )
        lines.append(f'      <jp>{row.get("jp", "")}</jp>')
        lines.append(f'      <en>{row.get("en", "")}</en>')
        forbidden = row.get("forbidden_synonyms") or []
        if forbidden:
            lines.append(f'      <never_render_as>{", ".join(forbidden)}</never_render_as>')
        established = row.get("established_in_chapter")
        if established:
            lines.append(f'      <as_used_in chapter_id="{established}">{row.get("citation", "")}</as_used_in>')
        else:
            lines.append(
                '      <as_used_in status="no_prior_use">No earlier chapter has used this surface yet. '
                "You are establishing it; use the locked form exactly.</as_used_in>"
            )
        lines.append("    </lock>")
    lines.append("  </anchor_locks>")
    return lines


def build_chapter_message(
    chapter_id: str,
    jp_source: str,
    guidance: str = "",
    previous_guidance_text: Optional[str] = None,
    continuity: Optional[Dict[str, Any]] = None,
    anchor_rows: Optional[List[Dict[str, Any]]] = None,
    language_name: str = "English",
    localization_block: str = "",
) -> str:
    """One chapter's user turn: reference data, then source, then the task.

    The order is the optimization. Gemini attends best to an instruction that
    arrives after the material it governs, so the continuity declaration,
    anchor locks, and chapter guidance lead; the source text follows; and the
    task -- restated in full every turn because it is the one part the model
    must act on -- closes the envelope.
    """
    parts = [f'<translation_task chapter_id="{chapter_id}">']
    if continuity:
        parts.extend(build_continuity_block(chapter_id, continuity))
    if anchor_rows:
        parts.extend(build_anchor_block(chapter_id, anchor_rows))
    if localization_block.strip():
        parts.extend("  " + line for line in localization_block.strip().splitlines())
    guidance_text = guidance.strip()
    if guidance_text:
        if previous_guidance_text and guidance_text == previous_guidance_text:
            parts.extend(
                [
                    "  <chapter_guidance>",
                    "    Same emotional band and active voice set as the preceding chapter.",
                    "  </chapter_guidance>",
                ]
            )
        else:
            parts.extend(["  <chapter_guidance>", guidance_text, "  </chapter_guidance>"])
    parts.extend(
        [
            f'  <source_text chapter_id="{chapter_id}">',
            jp_source.strip(),
            "  </source_text>",
            "  <task>",
            f"    Based on the source text above, translate chapter {chapter_id} into finished {language_name}",
            "    literary prose, following every rule in the system instruction. Translate only this",
            "    source_text; earlier chapters in this conversation are already translated and are",
            f"    continuity, not targets. Begin with the chapter heading. Return only the {language_name}",
            "    chapter in Markdown, with no preamble, notes, or commentary.",
            "  </task>",
            "</translation_task>",
        ]
    )
    return "\n".join(parts)


def build_rebase_preamble(summary: str, recent_turns: Sequence[Tuple[str, str]], language_name: str = "English") -> str:
    """Continuity for a request that starts a new server-side chain.

    A chain breaks on compaction, on a turn committed by the batch path (which
    has no interaction id), on a retention expiry, or on a model change. The
    replacement chain opens with ONE user input that carries the continuity as
    data -- prior chapters' accepted English, framed as precedent -- rather
    than with fabricated ``model_output`` steps: stateless mode requires
    thought steps to be resent exactly as received, and a synthesized model
    turn has none to send.
    """
    lines = ["<conversation_continuity>"]
    lines.append(
        f"  The accepted {language_name} of earlier chapters in this volume. It is binding precedent for names,"
    )
    lines.append("  terms, voices, and devices. It is already translated; never translate it again.")
    if summary.strip():
        lines.extend(["  <continuity_summary>", summary.strip(), "  </continuity_summary>"])
    for chapter_id, english in recent_turns:
        if not str(english).strip():
            continue
        lines.extend(
            [f'  <accepted_chapter chapter_id="{chapter_id}">', str(english).strip(), "  </accepted_chapter>"]
        )
    lines.append("</conversation_continuity>")
    return "\n".join(lines)


def build_continuation_message(chapter_id: str, language_name: str = "English") -> str:
    """Ask for only the unseen tail after an ``incomplete`` interaction."""
    return "\n".join(
        [
            f'<translation_continuation chapter_id="{chapter_id}">',
            "  Your previous response for this chapter stopped before the end of the source text.",
            "  Continue exactly at its final emitted point. Do not repeat earlier prose, the chapter",
            f"  heading, or scene markers already emitted. Return only the remaining {language_name} Markdown.",
            "</translation_continuation>",
        ]
    )
