"""Phase 2 translator through a subscription CLI (Codex CLI or Claude Code).

Same filesystem contract as the API routes: reads work/<vol>/JP/CHAPTER_*.md,
writes the workspace's translation file (EN/<id>_EN.md or VI/<id>_VI.md),
marks manifest chapters completed/blocked, and skips completed chapters on
resume. Prompts are the shared ones: the provider's VI master prompt (or the
OpenAI English master prompt) with context.xml injected, the per-chapter
<vietnamese_localization> block and the voice/EPS guidance.

Differences from the API routes, and why:
  * Every call is stateless (one CLI process per request), so continuity
    comes from the ending of the previous chapter, read back from disk.
  * A CLI exposes no "continue from here" turn, so long chapters are split
    at paragraph boundaries (``max_chunk_chars``) and each part carries the
    tail of the part before it. This keeps every answer well inside the
    output limit instead of relying on continuation.
  * A part is treated as truncated when Claude reports stop_reason
    max_tokens or the answer is implausibly short for its source; a
    truncated chapter is written for inspection but marked ``blocked``, so
    resume retries it.
  * Plan usage limits stop the run cleanly (CliQuotaError): finished
    chapters stay completed and ``resume`` continues later.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.CLI.runner import CliError, CliQuotaError, CliResult, CliRunner, cli_settings
from src.Deepseek.common.atomic_io import atomic_write_text
from src.Deepseek.common.chapter_order import reading_order
from src.Deepseek.common.chapter_signals import build_chapter_signal_guidance, parse_chapter_signals
from src.Deepseek.common.config import WORK_DIR
from src.Deepseek.common.language import (
    DEFAULT_LANGUAGE,
    language_name,
    localize_guidance,
    require_provider_support,
    resolve_master_prompt,
    target_language_block,
    translated_path,
    volume_target_language,
)
from src.Deepseek.common.token_telemetry import log_call
from src.Deepseek.translator.thinking_output import split_thinking_from_output
from src.OpenAI.config import get_openai_config, get_openai_optimization_config, get_openai_prompt_path
from src.OpenAI.context import (
    derive_chapter_eps_band,
    parse_character_roster_handles,
    parse_eps_signals,
    parse_voice_fingerprints,
    parse_volume_type,
    resolve_voice_aliases,
)
from src.OpenAI.optimization import build_chapter_guidance
from src.OpenAI.prompt_loader import build_chapter_message, build_system_instruction

logger = logging.getLogger(__name__)

_CJK_LEAK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)
# Japanese -> Vietnamese/English prose is always longer in characters than its
# source; an answer below this fraction of its source lost text.
_MIN_OUTPUT_RATIO = 0.4
_RATIO_MIN_SOURCE_CHARS = 400


def split_source(text: str, max_chars: int) -> List[str]:
    """Paragraph-boundary parts of at most ``max_chars`` (a single longer
    paragraph stays whole)."""
    text = text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    parts: List[str] = []
    current: List[str] = []
    size = 0
    for line in text.splitlines():
        if current and size + len(line) + 1 > max_chars and line.strip():
            parts.append("\n".join(current).strip())
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current and "\n".join(current).strip():
        parts.append("\n".join(current).strip())
    return parts


def clean_output(text: str) -> str:
    text = (text or "").strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    visible, _ = split_thinking_from_output(text)
    return visible.strip()


class CliTranslator:
    def __init__(
        self,
        work_dir: Path,
        volume_id: str,
        provider: str,
        *,
        dry_run: bool = False,
        runner: Optional[CliRunner] = None,
    ):
        self.work_dir = Path(work_dir)
        self.volume_id = str(volume_id)
        self.provider = str(provider).strip().lower()
        self.dry_run = dry_run
        self.target_language = volume_target_language(self.work_dir)
        require_provider_support(self.provider, self.target_language)
        settings = cli_settings(self.provider)
        self.max_chunk_chars = int(settings.get("max_chunk_chars", 9000) or 0)
        self.previous_tail_chars = max(0, int(settings.get("previous_tail_chars", 1200) or 0))
        self.runner = runner or CliRunner.from_config(self.provider)

        from src.Deepseek.translator.context_manager import load_context_xml

        context_xml = load_context_xml(self.work_dir)
        self._voice_profiles = resolve_voice_aliases(
            parse_voice_fingerprints(context_xml), parse_character_roster_handles(context_xml)
        )
        self._eps_signals = parse_eps_signals(context_xml)
        self._chapter_signals = parse_chapter_signals(context_xml)
        self._volume_type = parse_volume_type(context_xml)
        self._optimization = get_openai_optimization_config(get_openai_config())
        self.system_instruction = build_system_instruction(
            prompt_path=resolve_master_prompt(self.provider, get_openai_prompt_path(), self.target_language),
            context_xml=context_xml,
        )
        self.last_truncated = False

    # ── paths ────────────────────────────────────────────────────────────
    def output_path(self, chapter_id: str) -> Path:
        return translated_path(self.work_dir, chapter_id, self.target_language)

    def _previous_tail(self, chapter_id: str) -> str:
        match = re.match(r"^(.*?)(\d+)$", chapter_id)
        if not match or int(match.group(2)) <= 1 or self.previous_tail_chars <= 0:
            return ""
        previous = "%s%0*d" % (match.group(1), len(match.group(2)), int(match.group(2)) - 1)
        try:
            text = self.output_path(previous).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return text[-self.previous_tail_chars:].lstrip()

    # ── prompt ───────────────────────────────────────────────────────────
    def _guidance(self, chapter_id: str) -> str:
        signals = self._eps_signals.get(chapter_id, [])
        active = [
            {"name": s["name"], "fingerprint": self._voice_profiles.get(s["name"], {})}
            for s in signals if s.get("name")
        ]
        guidance = build_chapter_guidance(
            derive_chapter_eps_band(signals), active, self._optimization, self._volume_type
        )
        signal_guidance = build_chapter_signal_guidance(self._chapter_signals.get(chapter_id))
        if signal_guidance:
            guidance = "\n\n".join(part for part in (guidance, signal_guidance) if part)
        return localize_guidance(guidance, self.target_language)

    def build_messages(self, chapter_id: str, jp_source: str) -> List[Dict[str, Any]]:
        """One request per part; the tail of the part before is filled in at
        send time (``{previous}`` is only known after that part returns)."""
        parts = split_source(jp_source, self.max_chunk_chars)
        guidance = self._guidance(chapter_id)
        requests = []
        for index, part in enumerate(parts, start=1):
            part_guidance = guidance
            if len(parts) > 1:
                note = (
                    f"This source_text is part {index} of {len(parts)} of chapter {chapter_id}. "
                    + ("Emit the chapter heading as usual." if index == 1 else
                       "The chapter heading and earlier parts are already translated: do not emit a heading, "
                       "do not repeat the ending_tail, continue seamlessly after it.")
                )
                part_guidance = "\n\n".join(p for p in (f"<chunk_note>{note}</chunk_note>", guidance) if p)
            requests.append({
                "index": index, "count": len(parts), "source": part, "guidance": part_guidance,
            })
        return requests

    def _message(self, chapter_id: str, request: Dict[str, Any], tail: str) -> str:
        continuity = None
        if tail:
            first = request["index"] == 1
            continuity = {
                "previous_chapter_id": (self._previous_id(chapter_id) if first
                                        else f"{chapter_id} part {request['index'] - 1}"),
                "previous_chapter_status": "translated_earlier" if first else "translated_in_previous_request",
                "previous_chapter_tail": tail,
            }
        return build_chapter_message(
            chapter_id,
            request["source"],
            request["guidance"],
            continuity=continuity,
            language_name=language_name(self.target_language),
            localization_block=target_language_block(
                self.work_dir, self.target_language, chapter_id, request["source"]
            ),
        )

    @staticmethod
    def _previous_id(chapter_id: str) -> str:
        match = re.match(r"^(.*?)(\d+)$", chapter_id)
        if not match:
            return "none"
        return "%s%0*d" % (match.group(1), len(match.group(2)), int(match.group(2)) - 1)

    # ── translation ──────────────────────────────────────────────────────
    def translate_chapter(self, chapter_path: Path) -> str:
        chapter_id = chapter_path.stem
        jp_source = chapter_path.read_text(encoding="utf-8")
        requests = self.build_messages(chapter_id, jp_source)
        outputs: List[str] = []
        tail = self._previous_tail(chapter_id)
        self.last_truncated = False
        for request in requests:
            message = self._message(chapter_id, request, tail)
            label = chapter_id if request["count"] == 1 else f"{chapter_id}#part-{request['index']}"
            if self.dry_run:
                self._write_dry_run(label, message)
                outputs.append("")
                continue
            result = self.runner.complete(self.system_instruction, message, label=label)
            self._log_usage(label, result)
            text = clean_output(result.text)
            if not text:
                raise CliError(f"{self.runner.label} returned no translation text for {label}")
            if result.truncated or self._implausibly_short(request["source"], text):
                logger.warning("[CLI] %s looks truncated (stop_reason=%r, %d chars for %d source chars)",
                               label, result.stop_reason, len(text), len(request["source"]))
                self.last_truncated = True
            outputs.append(text)
            tail = text[-self.previous_tail_chars:].lstrip() if self.previous_tail_chars else ""
        if self.dry_run:
            return ""
        joined = "\n\n".join(outputs).strip() + "\n"
        if self.target_language == DEFAULT_LANGUAGE:
            joined = _CJK_LEAK_RE.sub("", joined)
        return joined

    @staticmethod
    def _implausibly_short(source: str, text: str) -> bool:
        body = len(re.sub(r"\s+", "", source))
        return body >= _RATIO_MIN_SOURCE_CHARS and len(re.sub(r"\s+", "", text)) < body * _MIN_OUTPUT_RATIO

    def _write_dry_run(self, label: str, message: str) -> None:
        from src.Deepseek.translator.dry_run import write_dry_run_prompt

        write_dry_run_prompt(
            work_dir=self.work_dir, volume_id=self.volume_id, chapter_id=label,
            payload={"model": self.runner.model or f"{self.provider} CLI default", "system": self.system_instruction,
                     "messages": [{"role": "user", "content": message}]},
            provider=f"{self.provider}-cli",
        )

    def _log_usage(self, label: str, result: CliResult) -> None:
        try:
            log_call(
                phase="translation", volume_id=self.volume_id, call_label=label,
                model=result.model or self.runner.label, provider=f"{self.provider}-cli",
                cache_hit_tokens=result.cached_tokens,
                fresh_tokens=max(0, result.input_tokens - result.cached_tokens),
                output_tokens=result.output_tokens,
                # Billed to the signed-in plan, not per token.
                cost_usd=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - telemetry never fails a chapter
            logger.warning("[CLI] token log failed for %s: %s", label, exc)

    def translate_all(self, chapter_files: List[Path],
                      on_chapter: Optional[Callable[[str, Path], None]] = None) -> Dict[str, Path]:
        from src.OpenAI.agent import _update_manifest_after_translation

        results: Dict[str, Path] = {}
        for path in sorted(chapter_files, key=reading_order):
            chapter_id = path.stem
            try:
                text = self.translate_chapter(path)
            except CliQuotaError:
                logger.error("[CLI] plan usage limit reached at %s; %d chapter(s) finished this run. "
                             "Run `resume` after the limit resets.", chapter_id, len(results))
                raise
            if self.dry_run:
                results[chapter_id] = self.work_dir / "DRY_RUN"
                continue
            out = self.output_path(chapter_id)
            out.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(out, text)
            if self.last_truncated:
                _update_manifest_after_translation(
                    self.work_dir, set(),
                    blocked={chapter_id: "output looks truncated (CLI); written for inspection, rerun resumes it"},
                )
            else:
                _update_manifest_after_translation(self.work_dir, {chapter_id})
            results[chapter_id] = out
            logger.info("[CLI] %s -> %s", chapter_id, out)
            if on_chapter:
                on_chapter(chapter_id, out)
        return results


def translate_volume(
    volume_id: str,
    chapters: Optional[List[str]] = None,
    thinking_log_enabled: Optional[bool] = None,  # accepted for CLI parity; CLIs expose no reasoning text
    dry_run: bool = False,
    *,
    provider: str,
    runner: Optional[CliRunner] = None,
) -> Dict[str, Path]:
    from src.OpenAI.agent import _filter_completed_chapters

    work_dir = WORK_DIR / volume_id
    jp_dir = work_dir / "JP"
    if not jp_dir.is_dir():
        raise FileNotFoundError(f"No JP/ directory for volume {volume_id!r} at {jp_dir}")
    chapter_files = sorted(jp_dir.glob("CHAPTER_*.md"), key=reading_order)
    if chapters:
        wanted = set(chapters)
        chapter_files = [p for p in chapter_files if p.stem in wanted or p.stem.split("_")[-1] in wanted]
    if not dry_run:
        chapter_files = _filter_completed_chapters(work_dir, chapter_files)
    translator = CliTranslator(work_dir, volume_id, provider, dry_run=dry_run, runner=runner)
    return translator.translate_all(chapter_files)
