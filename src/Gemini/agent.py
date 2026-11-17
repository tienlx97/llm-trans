"""Gemini Phase 2 translator facade with the established filesystem contract.

Same contract as every other provider route: JP/ in, EN/<id>_EN.md and
THINKING/<id>_THINKING.md out, manifest.json stamped per chapter, the
fidelity gate deciding what counts as completed, and a refused chapter routed
to the DeepSeek safety fallback rather than retried.

Sync path: Gemini Interactions API, one server-side chain per volume
(``previous_interaction_id``), rebased from the local ledger whenever the
chain is unusable. Batch path: generateContent Batch API in waves, each
wave's shared prefix in one explicit CachedContent when that pays.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from src.Deepseek.common.chapter_order import in_reading_order, preceding_chapter_id
from src.Deepseek.common.config import cfg_int
from src.Deepseek.common.atomic_io import atomic_write_json, atomic_write_text
from src.Deepseek.common.chapter_signals import build_chapter_signal_guidance, parse_chapter_signals
from src.Deepseek.common.config import PIPELINE_ROOT, WORK_DIR, get_safety_fallback_config
from src.Deepseek.common.illustration_reinjection import reinject
from src.Deepseek.common.language import (
    DEFAULT_LANGUAGE,
    language_name,
    localize_guidance,
    require_provider_support,
    resolve_master_prompt,
    target_language_block,
    translated_dir,
    translated_path,
    translated_suffix,
    volume_target_language,
)
from src.Deepseek.common.llm_types import LLMResponse, LLMTermination
from src.Deepseek.common.safety_fallback import fallback_translate_chapter
from src.Deepseek.common.structural_fidelity import DEFAULT_RATIO_TOLERANCE, length_ratio, structural_report
from src.Deepseek.common.token_telemetry import log_call
from src.Deepseek.common.verbatim_anchors import (
    anchors_in_source,
    merge_bible_anchors,
    observe_prior_usage,
    parse_anchors,
    reconcile_chapter,
)
from src.Deepseek.translator.config import get_thinking_log_config
from src.Deepseek.translator.thinking_output import merge_thinking_log, sanitize_chapter_output
from src.Gemini.client import GeminiClient, lower_thinking_level
from src.Gemini.config import (
    get_gemini_batch_config,
    get_gemini_caching_config,
    get_gemini_config,
    get_gemini_continuation_config,
    get_gemini_conversation_config,
    get_gemini_fidelity_config,
    get_gemini_optimization_config,
    get_gemini_prompt_path,
    get_gemini_telemetry_config,
)
from src.Gemini.context import (
    derive_chapter_eps_band,
    parse_character_roster_handles,
    parse_eps_signals,
    parse_voice_fingerprints,
    parse_volume_type,
    resolve_voice_aliases,
)
from src.Gemini.errors import (
    GeminiAPIError,
    GeminiInteractionNotFoundError,
    GeminiInvalidRequestError,
    GeminiSafetyBlockError,
)
from src.Gemini.optimization import build_chapter_guidance
from src.Gemini.prompt_loader import (
    build_chapter_message,
    build_continuation_message,
    build_system_segments,
    join_system_segments,
)
from src.Gemini.response import thinking_tally

logger = logging.getLogger(__name__)


def _block_on_setting(cfg: Dict[str, Any]) -> List[str]:
    """fidelity_gate.block_on, defaulting only when the key is absent or null.

    An explicit ``[]`` means "record findings, never block" (config.yaml);
    ``cfg.get("block_on") or [...]`` turned it into "block on everything".
    """
    value = cfg.get("block_on")
    if value is None:
        return ["missing", "drifted", "structural"]
    return list(value) if isinstance(value, (list, tuple, set)) else [value]
# Japanese script only. The rest of U+FF00-FFEF is left alone: a
# translation_policy can require a fullwidth glyph verbatim (see the Anthropic
# route's note on volume 3e35fe's ＊).
_CJK_LEAK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")
_CHAPTER_INDEX_RE = re.compile(r"^(.*?)(\d+)$")
_BLOCKED_TERMINATIONS = (LLMTermination.SAFETY_BLOCKED, LLMTermination.REFUSED)


class GeminiTranslator:
    """Translate a volume through the Gemini API while preserving MTLS's shared
    filesystem contract (EN/, THINKING/, manifest.json)."""

    def __init__(
        self,
        work_dir: Path,
        volume_id: str,
        config: Optional[Dict[str, Any]] = None,
        thinking_log_enabled: Optional[bool] = None,
        dry_run: bool = False,
    ):
        self.work_dir = Path(work_dir)
        self.volume_id = str(volume_id)
        # The workspace's own language (manifest). English keeps every
        # historical path, prompt and the English verbatim-anchor locks; a
        # non-English target must have a Gemini route in project.languages.
        self.target_language = volume_target_language(self.work_dir)
        require_provider_support("gemini", self.target_language)
        self._is_english = self.target_language == DEFAULT_LANGUAGE
        self._last_truncated = False
        self._config = config or get_gemini_config()
        self.dry_run = dry_run
        self.client = GeminiClient(dry_run=dry_run)
        conversation_cfg = get_gemini_conversation_config()
        if conversation_cfg.get("enabled", True):
            self.client.attach_conversation(
                work_dir=self.work_dir, volume_id=self.volume_id, conversation_config=conversation_cfg
            )
        from src.Deepseek.translator.context_manager import load_context_xml

        context_xml = load_context_xml(self.work_dir)
        self._voice_profiles = resolve_voice_aliases(
            parse_voice_fingerprints(context_xml), parse_character_roster_handles(context_xml)
        )
        self._eps_signals = parse_eps_signals(context_xml)
        self._chapter_signals = parse_chapter_signals(context_xml)
        self._volume_type = parse_volume_type(context_xml)
        # Verbatim anchors lock ENGLISH surfaces; enforcing them on another
        # language would demand English phrases inside the translation. The
        # Vietnamese glossary/locks in bible_vi.json play that role instead.
        self._anchors = (
            merge_bible_anchors(parse_anchors(context_xml), self._series_bible_anchors_path())
            if self._is_english else []
        )
        if self._anchors:
            logger.info("[GEMINI-ANCHORS] %d verbatim anchor(s) loaded for %s", len(self._anchors), self.volume_id)
        self.system_segments = build_system_segments(
            prompt_path=resolve_master_prompt("gemini", get_gemini_prompt_path(), self.target_language),
            context_xml=context_xml,
        )
        self.system_instruction = join_system_segments(self.system_segments)
        if not dry_run:
            self.client.log_implicit_cache_plan(self.system_instruction)
        if self.client.conversation_manager is not None:
            self.client.conversation_manager.bind_system(self.system_segments)
            self.client.conversation_manager.language_name = language_name(self.target_language)
        self._previous_guidance_text: Optional[str] = None
        self._previous_tail_chars = max(0, cfg_int(conversation_cfg, "previous_chapter_tail_chars", 1200))
        continuation = get_gemini_continuation_config()
        self.continuation_enabled = bool(continuation.get("enabled", True))
        self.max_continuations = max(0, cfg_int(continuation, "max_continuations", 3))
        # An `incomplete` interaction with NO visible text means thinking used
        # the whole 64K budget; a continuation cannot rescue that. Retry the
        # chapter once at the next lower thinking level instead.
        self.retry_lower_thinking = bool(continuation.get("retry_lower_thinking_on_empty", True))
        thinking_log_cfg = get_thinking_log_config()
        self.thinking_log_enabled = (
            thinking_log_cfg.get("enabled", True) if thinking_log_enabled is None else thinking_log_enabled
        )
        self.thinking_log_dir_name = thinking_log_cfg.get("output_dir", "THINKING")
        self.thinking_density_enabled = thinking_log_cfg.get("density_map", {}).get("enabled", True)

    # ── context helpers (same contract as the Anthropic route) ─────────────

    def _series_bible_anchors_path(self) -> Path:
        series_id = ""
        try:
            manifest = json.loads((self.work_dir / "manifest.json").read_text(encoding="utf-8"))
            series_id = str(((manifest.get("pipeline_state") or {}).get("prep") or {}).get("series_id") or "")
        except (OSError, ValueError, AttributeError):
            series_id = ""
        return PIPELINE_ROOT / "bibles" / (series_id or "_absent") / "verbatim_anchors.json"

    def _anchor_rows(self, chapter_id: str, jp_source: str) -> List[Dict[str, Any]]:
        if not self._anchors or not jp_source:
            return []
        present = anchors_in_source(self._anchors, jp_source)
        if not present:
            return []
        rows: List[Dict[str, Any]] = []
        for observation in observe_prior_usage(present, translated_dir(self.work_dir, self.target_language), exclude_chapter_id=chapter_id):
            anchor = observation.anchor
            rows.append(
                {
                    "anchor_id": anchor.anchor_id,
                    "jp": anchor.jp,
                    "en": anchor.en,
                    "source_occurrences": observation.source_occurrences,
                    "forbidden_synonyms": list(anchor.forbidden_synonyms),
                    "established_in_chapter": observation.established_in_chapter,
                    "citation": observation.citations[0] if observation.citations else "",
                }
            )
        return rows

    def _preceding_chapter_id(self, chapter_id: str) -> Optional[str]:
        return preceding_chapter_id(chapter_id)

    def _chapter_tail_from_disk(self, chapter_id: str) -> str:
        if self._previous_tail_chars <= 0:
            return ""
        try:
            text = self._output_path(chapter_id).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return text if len(text) <= self._previous_tail_chars else text[-self._previous_tail_chars :].lstrip()

    def _continuity_brief(self, chapter_id: str) -> Optional[Dict[str, Any]]:
        manager = self.client.conversation_manager
        if manager is None:
            return None
        state = manager.continuity_state()
        verbatim = list(state.get("verbatim_chapter_ids") or [])
        summarized = list(state.get("summarized_chapter_ids") or [])
        brief: Dict[str, Any] = {"verbatim_chapter_ids": verbatim, "summarized_chapter_ids": summarized}
        previous_id = self._preceding_chapter_id(chapter_id)
        if not previous_id:
            return brief
        brief["previous_chapter_id"] = previous_id
        if previous_id in verbatim:
            brief["previous_chapter_status"] = "translated_in_full_in_this_request"
            return brief
        brief["previous_chapter_status"] = "summarized" if previous_id in summarized else "absent"
        tail = self._chapter_tail_from_disk(previous_id)
        if tail:
            brief["previous_chapter_tail"] = tail
        return brief

    def _build_chapter_prompt(self, chapter_id: str, chapter_path: Path, *, sequential: bool = False,
                              chapter_meta: Optional[Dict[str, Any]] = None) -> str:
        meta = chapter_meta or {}
        jp_source = Path(chapter_path).read_text(encoding="utf-8")
        signals = self._eps_signals.get(chapter_id, [])
        eps_band = str(meta.get("eps_band") or derive_chapter_eps_band(signals))
        active_characters = meta.get("active_characters")
        if active_characters is None:
            active_characters = [
                {"name": signal["name"], "fingerprint": self._voice_profiles.get(signal["name"], {})}
                for signal in signals
                if signal.get("name")
            ]
        guidance = build_chapter_guidance(
            eps_band, active_characters, get_gemini_optimization_config(), self._volume_type
        )
        signal_guidance = build_chapter_signal_guidance(self._chapter_signals.get(chapter_id))
        if signal_guidance:
            guidance = "\n\n".join(part for part in (guidance, signal_guidance) if part)
        guidance = localize_guidance(guidance, self.target_language)
        prompt = build_chapter_message(
            chapter_id,
            jp_source,
            guidance,
            language_name=language_name(self.target_language),
            localization_block=target_language_block(self.work_dir, self.target_language, chapter_id, jp_source),
            # Strictly sequential dedup; a batch wave has no single predecessor.
            previous_guidance_text=self._previous_guidance_text if sequential else None,
            continuity=self._continuity_brief(chapter_id),
            anchor_rows=self._anchor_rows(chapter_id, jp_source),
        )
        if sequential:
            self._previous_guidance_text = guidance.strip() if guidance else None
        return prompt

    # ── sync path ──────────────────────────────────────────────────────────

    def _generate_planned(self, prompt: str, *, thinking_level: Optional[str] = None) -> Tuple[LLMResponse, str]:
        """One chapter request against the conversation chain, rebasing once
        if the server no longer holds the chain head. Returns (response, the
        input actually sent)."""
        manager = self.client.conversation_manager
        plan = manager.plan_request(prompt) if manager is not None and not self.dry_run else None
        sent = plan.input if plan is not None else prompt
        previous = plan.previous_interaction_id if plan is not None else None
        if plan is not None and plan.rebased:
            self.client.reset_chain_telemetry()
        try:
            response = self.client.generate(
                input=sent,
                system_instruction=self.system_segments,
                previous_interaction_id=previous,
                thinking_level=thinking_level,
                dry_run=self.dry_run,
            )
        except GeminiInvalidRequestError as exc:
            # The documented loss of a chain head is a retention expiry, but
            # neither its status code nor its message is pinned down, so ANY
            # rejected chained request gets one rebase. A request rejected
            # outright is not billed; the rebased retry fails loudly on its own
            # if the fault was in the envelope rather than the chain.
            if manager is None or not previous:
                raise
            if not isinstance(exc, GeminiInteractionNotFoundError):
                logger.warning("[GEMINI] chained request rejected (%s); rebasing once from the ledger", exc)
            manager.note_chain_lost(str(exc)[:200])
            plan = manager.plan_request(prompt, compact=False)
            sent = plan.input
            self.client.reset_chain_telemetry()
            response = self.client.generate(
                input=sent, system_instruction=self.system_segments, thinking_level=thinking_level
            )
        return response, sent

    def translate_chapter(self, chapter_path: Path, chapter_meta: Optional[Dict[str, Any]] = None) -> str:
        meta = chapter_meta or {}
        chapter_id = str(meta.get("chapter_id", chapter_path.stem))
        prompt = self._build_chapter_prompt(chapter_id, chapter_path, sequential=True, chapter_meta=meta)
        response, _sent = self._generate_planned(prompt)
        if response.provider_metadata.get("dry_run"):
            from src.Deepseek.translator.dry_run import write_dry_run_prompt

            path = write_dry_run_prompt(
                work_dir=self.work_dir,
                volume_id=self.volume_id,
                chapter_id=chapter_id,
                payload=response.provider_metadata["payload"],
                provider="gemini",
            )
            return f"[DRY RUN — no translation performed. Payload written to {path}]"

        if response.termination == LLMTermination.MAX_OUTPUT and not response.content.strip():
            self._log_usage(f"{chapter_id}#thinking-exhausted", response)
            lower = lower_thinking_level(self.client.model, self.client.thinking_level)
            if not (self.retry_lower_thinking and lower):
                raise GeminiAPIError(
                    f"{chapter_id}: the output ceiling was spent entirely on thinking (no visible text). "
                    "Lower translation.gemini.thinking.level or enable continuation.retry_lower_thinking_on_empty."
                )
            logger.warning(
                "[GEMINI] %s: thinking consumed the whole %d-token output budget with no text; "
                "retrying once at thinking_level=%s", chapter_id, self.client.output_ceiling, lower,
            )
            response, _sent = self._generate_planned(prompt, thinking_level=lower)

        if response.termination in _BLOCKED_TERMINATIONS:
            self._log_usage(f"{chapter_id}#blocked", response)
            return self._safety_fallback_translate(
                chapter_path, chapter_id, _block_error(chapter_id, response, "translation")
            )
        if response.termination == LLMTermination.ERROR:
            errors = response.provider_metadata.get("errors") or []
            raise GeminiAPIError(f"Gemini interaction for {chapter_id} ended {response.raw_finish_reason}: {errors}")
        self._log_usage(chapter_id, response)

        responses = [response]
        # Separator placed BEFORE each response's text. A long-decode resume
        # picks up mid-stream, so it joins with "" -- a newline there could
        # split a sentence. A prompted continuation starts a fresh turn.
        separators = [""]
        continuation_count = 0
        while (
            response.termination == LLMTermination.MAX_OUTPUT
            and self.continuation_enabled
            and continuation_count < self.max_continuations
            and response.response_id
        ):
            continuation_count += 1
            token = str(response.provider_metadata.get("continuation_token") or "")
            resumed = None
            if token and self.client.long_decode:
                try:
                    resumed = self.client.generate(
                        input="",
                        system_instruction=self.system_segments,
                        continuation_token=token,
                    )
                except GeminiInvalidRequestError as exc:
                    # The resume envelope is not pinned down in the docs; a
                    # refused one is not billed, so fall back to prompting.
                    logger.warning("[GEMINI-LONG-DECODE] %s: continuation_token resume refused (%s); "
                                   "continuing by prompt instead", chapter_id, exc)
            response = resumed or self.client.generate(
                input=build_continuation_message(chapter_id, language_name=language_name(self.target_language)),
                system_instruction=self.system_segments,
                previous_interaction_id=response.response_id,
            )
            separators.append("" if resumed is not None else "\n")
            label = f"{chapter_id}#continue-{continuation_count}"
            if response.termination in _BLOCKED_TERMINATIONS:
                self._log_usage(label, response)
                return self._safety_fallback_translate(
                    chapter_path, chapter_id, _block_error(chapter_id, response, "continuation")
                )
            self._log_usage(label, response)
            responses.append(response)
        self._last_truncated = response.termination == LLMTermination.MAX_OUTPUT
        if self._last_truncated:
            logger.warning(
                "[GEMINI] %s: still incomplete after %d continuation(s); the fidelity gate decides "
                "whether the chapter can stand", chapter_id, continuation_count,
            )

        raw_text = ""
        for separator, r in zip(separators, responses):
            if r.content:
                raw_text += (separator if raw_text else "") + r.content
        text, leaked_blocks = sanitize_chapter_output(raw_text)
        if self._is_english:
            text = _CJK_LEAK_RE.sub("", text)
        if not text.strip():
            raise GeminiAPIError(f"Gemini returned no visible translation text for {chapter_id}.")
        self._maybe_write_thinking_log(
            chapter_id=chapter_id,
            api_thinking="\n\n".join(r.thinking_content for r in responses if r.thinking_content) or None,
            leaked_blocks=leaked_blocks,
            thinking_blocks=thinking_tally(responses),
        )
        manager = self.client.conversation_manager
        if manager is not None:
            # The chapter's LAST interaction is the new chain head, so the next
            # chapter extends from where this one actually finished.
            manager.commit(
                chapter_id=chapter_id,
                user_text=prompt,
                model_text=text,
                output_path=self._output_path(chapter_id),
                interaction_id=responses[-1].response_id,
                # Not input_tokens: a long decode sums that across hops.
                input_tokens=int(responses[-1].provider_metadata.get(
                    "context_input_tokens", responses[-1].input_tokens)),
            )
        return text

    def _safety_fallback_translate(self, chapter_path: Path, chapter_id: str, exc: GeminiAPIError) -> str:
        """Do NOT retry a content block -- the same input blocks the same way.
        Switch to DeepSeek with decision inheritance (see
        src/Deepseek/common/safety_fallback.py)."""
        cfg = get_safety_fallback_config()
        if not cfg.get("enabled", True):
            logger.warning("[GEMINI-SAFETY] %s — safety fallback disabled; re-raising", chapter_id)
            raise exc
        try:
            return fallback_translate_chapter(
                work_dir=self.work_dir,
                volume_id=self.volume_id,
                chapter_path=chapter_path,
                chapter_id=chapter_id,
                refusal=exc,
                source_provider="Gemini",
                refusal_code_default="safety_block",
                dry_run=self.dry_run,
            )
        except GeminiAPIError:
            raise
        except Exception as fallback_exc:  # noqa: BLE001 - surface any fallback failure loudly
            logger.error("[GEMINI-SAFETY] %s — DeepSeek fallback failed (%s); re-raising", chapter_id, fallback_exc)
            raise exc from fallback_exc

    def _log_usage(self, call_label: str, response: LLMResponse) -> None:
        """One token-log row per call. fresh = input minus cached (cached is a
        subset of input on Gemini); output already includes thinking."""
        if not bool(get_gemini_telemetry_config().get("enabled", True)):
            return
        try:
            metrics = dict(response.provider_metadata.get("cache_telemetry") or {})
            economics = dict(metrics.get("cache_economics") or {})
            quality = bool(get_gemini_telemetry_config().get("cache_quality", True)) and bool(metrics)
            log_call(
                phase="translator",
                volume_id=self.volume_id,
                call_label=call_label,
                model=response.model,
                provider="gemini",
                cache_hit_tokens=response.cached_tokens,
                cache_write_tokens=0,
                fresh_tokens=max(0, response.input_tokens - response.cached_tokens),
                output_tokens=response.output_tokens,
                cost_usd=response.total_cost_usd,
                batch=bool(response.batch_pricing),
                breakpoint_success_rate=metrics.get("breakpoint_success_rate") if quality else None,
                prefix_recovery=metrics.get("prefix_recovery") if quality else None,
                total_cache_coverage=metrics.get("total_cache_coverage") if quality else None,
                cache_net_savings_usd=economics.get("net_input_savings_usd") if quality else None,
            )
        except Exception as exc:  # telemetry must never prevent a translation
            logger.warning("[GEMINI] token log failed for %s: %s", call_label, exc)

    def _log_storage(self, label: str, cost_usd: float) -> None:
        if cost_usd <= 0 or not bool(get_gemini_telemetry_config().get("enabled", True)):
            return
        try:
            log_call(
                phase="translator", volume_id=self.volume_id, call_label=label, model=self.client.model,
                provider="gemini", cache_hit_tokens=0, fresh_tokens=0, output_tokens=0, cost_usd=cost_usd,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[GEMINI] storage log failed for %s: %s", label, exc)

    def _maybe_write_thinking_log(
        self,
        *,
        chapter_id: str,
        api_thinking: Optional[str],
        leaked_blocks: List[str],
        thinking_blocks: Optional[Tuple[int, int]] = None,
    ) -> None:
        """Archive thought summaries to THINKING/<chapter_id>_THINKING.md.

        Gemini returns summaries, never raw thoughts, and only when
        thinking.summaries is "auto". A chapter whose thought steps all came
        back signature-only under "auto" is warned about, not silently
        skipped: that is the summary being withheld, not the model declining
        to think."""
        if not self.thinking_log_enabled:
            return
        total, with_text = thinking_blocks or (0, 0)
        if self.client.thinking_summaries == "auto" and total and not with_text:
            logger.warning(
                "[GEMINI-THINKING] %s: %d thought step(s) carried no summary under thinking_summaries=auto",
                chapter_id, total,
            )
        merged = merge_thinking_log(api_thinking, leaked_blocks, chapter_id=chapter_id)
        if not merged:
            return
        thinking_dir = self.work_dir / self.thinking_log_dir_name
        thinking_dir.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Thinking Process — {chapter_id}\n\n"
            f"- **Chapter:** {chapter_id}\n"
            f"- **Model:** {self.client.model}\n"
            f"- **Provider:** Gemini\n"
            f"- **Thinking level:** {self.client.thinking_level}\n"
            f"- **Timestamp:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n"
            f"## Gemini Reasoning (thought summaries)\n\n"
        )
        atomic_write_text(thinking_dir / f"{chapter_id}_THINKING.md", header + merged + "\n")
        if self.thinking_density_enabled:
            from src.Deepseek.translator.thinking_density import build_density_report

            try:
                build_density_report(self.work_dir, self.volume_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[GEMINI-THINKING] %s — density map rebuild failed: %s", self.volume_id, exc)

    # ── persistence and fidelity ───────────────────────────────────────────

    def _peer_length_ratios(self, chapter_id: str) -> List[float]:
        ratios: List[float] = []
        en_dir = translated_dir(self.work_dir, self.target_language)
        suffix = translated_suffix(self.target_language)
        if not en_dir.is_dir():
            return ratios
        for en_path in sorted(en_dir.glob("*" + suffix)):
            peer_id = en_path.name[: -len(suffix)]
            if peer_id == chapter_id:
                continue
            try:
                ratio = length_ratio(
                    (self.work_dir / "JP" / f"{peer_id}.md").read_text(encoding="utf-8"),
                    en_path.read_text(encoding="utf-8"),
                )
            except OSError:
                continue
            if ratio:
                ratios.append(ratio)
        return ratios

    def _reinject_illustrations(self, chapter_id: str, en_text: str) -> str:
        try:
            jp_source = (self.work_dir / "JP" / f"{chapter_id}.md").read_text(encoding="utf-8")
        except OSError:
            return en_text
        assets_dir = self.work_dir / "assets" / "illustrations"
        assets = {p.name for p in assets_dir.iterdir()} if assets_dir.is_dir() else None
        repaired, plan = reinject(jp_source, en_text, available_assets=assets)
        if plan.nothing_to_do:
            return en_text
        if plan.ok:
            logger.warning(
                "[GEMINI-ILLUSTRATION] %s: model dropped %d plate(s); re-injected (ratio %.3f): %s",
                chapter_id, len(plan.plates), plan.ratio, ", ".join(p.target for p in plan.plates),
            )
            return repaired
        logger.error(
            "[GEMINI-ILLUSTRATION] %s: plate(s) dropped and re-injection REFUSED (ratio %.3f): %s",
            chapter_id, plan.ratio, "; ".join(plan.refusals),
        )
        return en_text

    def _check_chapter_fidelity(self, chapter_id: str, en_text: str) -> List[str]:
        """Verify a finished chapter against its source; returns blocking
        reasons. Anchor half plus structural half, same as the Anthropic
        route, written to .context/anchor_reconciliation.json."""
        cfg = get_gemini_fidelity_config()
        gate_enabled = bool(cfg.get("enabled", True))
        block_on = {str(item).strip().lower() for item in _block_on_setting(cfg)}
        try:
            tolerance = float(cfg.get("ratio_tolerance", DEFAULT_RATIO_TOLERANCE) or DEFAULT_RATIO_TOLERANCE)
        except (TypeError, ValueError):
            tolerance = DEFAULT_RATIO_TOLERANCE
        try:
            jp_source = (self.work_dir / "JP" / f"{chapter_id}.md").read_text(encoding="utf-8")
        except OSError:
            logger.warning("[GEMINI-FIDELITY] %s: JP source unreadable; completeness NOT verified", chapter_id)
            return []

        anchor_rows = (
            reconcile_chapter(self._anchors, chapter_id=chapter_id, jp_source=jp_source, en_text=en_text)
            if self._anchors else []
        )
        structural_rows = structural_report(jp_source, en_text, self._peer_length_ratios(chapter_id), tolerance=tolerance)

        path = self.work_dir / ".context" / "anchor_reconciliation.json"
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing.setdefault("volume_id", self.volume_id)
        existing.setdefault("chapters", {})
        existing["chapters"][chapter_id] = {"anchors": anchor_rows, "structural": structural_rows}
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, existing)

        blocking: List[str] = []
        anchor_flagged = [row for row in anchor_rows if str(row.get("status")) in ("missing", "drifted")]
        if anchor_flagged:
            logger.warning(
                "[GEMINI-FIDELITY] %s: %d anchor(s) not honoured: %s", chapter_id, len(anchor_flagged),
                ", ".join(f"{row['anchor_id']}({row['status']})" for row in anchor_flagged),
            )
        blocking.extend(
            f"anchor {row.get('anchor_id')} {row.get('status')} ({row.get('en')!r})"
            for row in anchor_flagged if str(row.get("status")) in block_on
        )
        self._write_anchor_warnings(existing.get("chapters") or {})
        for row in structural_rows:
            status = str(row.get("status") or "")
            if row.get("check") == "illustration_parity" and status == "mismatch":
                detail = (
                    f"illustration parity: source has {row.get('jp_count')}, output has {row.get('en_count')}; "
                    f"missing={row.get('missing')} unexpected={row.get('unexpected')}"
                )
                logger.error("[GEMINI-FIDELITY] %s: %s", chapter_id, detail)
                if "structural" in block_on:
                    blocking.append(detail)
            elif row.get("check") == "length_ratio" and status == "outlier":
                logger.warning(
                    "[GEMINI-FIDELITY] %s: translated bulk is an outlier (ratio=%s, median=%s, deviation=%s) "
                    "— worth a look, not blocking",
                    chapter_id, row.get("ratio"), row.get("median"), row.get("deviation"),
                )
        if blocking and not gate_enabled:
            logger.warning(
                "[GEMINI-FIDELITY] %s: %d problem(s) found but the gate is disabled: %s",
                chapter_id, len(blocking), "; ".join(blocking),
            )
            return []
        return blocking

    def _write_anchor_warnings(self, chapters: Dict[str, Any]) -> None:
        sections: List[str] = []
        for chapter_id in sorted(chapters):
            rows = [
                row for row in (chapters[chapter_id].get("anchors") or [])
                if str(row.get("status")) in ("missing", "drifted")
            ]
            if not rows:
                continue
            lines = [f"## {chapter_id}"]
            for row in rows:
                lines.append(
                    f"- `{row.get('anchor_id')}` **{row.get('status')}** — JP: {row.get('jp')} | expected EN: "
                    f"{row.get('en')!r} (source_occurrences={row.get('source_occurrences')}, "
                    f"en_occurrences={row.get('en_occurrences')})"
                )
            sections.append("\n".join(lines))
        path = self.work_dir / "QC" / "anchor_warnings.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not sections:
            if path.exists():
                path.unlink()
            return
        header = (
            f"# Anchor Fidelity Warnings — {self.volume_id}\n\n"
            "Non-blocking findings from the anchor-lock check (`translation.gemini.fidelity_gate`). "
            "These chapters were NOT held back for them -- the QC pass judges each.\n"
        )
        path.write_text(header + "\n" + "\n\n".join(sections) + "\n", encoding="utf-8")

    def _output_path(self, chapter_id: str) -> Path:
        return translated_path(self.work_dir, chapter_id, self.target_language)

    def _write_chapter(self, chapter_id: str, text: str) -> Tuple[Path, List[str]]:
        """Persist one chapter and verify it. The text is always written; the
        blocking reasons decide whether the caller may record it as done."""
        output_path = self._output_path(chapter_id)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        text = self._reinject_illustrations(chapter_id, text)
        atomic_write_text(output_path, text)
        blocking = self._check_chapter_fidelity(chapter_id, text)
        truncated, self._last_truncated = self._last_truncated, False
        if truncated and not self._is_english:
            blocking.append("output truncated at the model's output limit")
        return output_path, blocking

    def translate_and_persist_chapter(self, chapter_path: Path, chapter_meta: Optional[Dict[str, Any]] = None) -> Path:
        chapter_id = str((chapter_meta or {}).get("chapter_id", chapter_path.stem))
        text = self.translate_chapter(chapter_path, {**(chapter_meta or {}), "chapter_id": chapter_id})
        if self.dry_run:
            return self.work_dir / "DRY_RUN"
        output_path, blocking = self._write_chapter(chapter_id, text)
        if blocking:
            logger.error(
                "[GEMINI-FIDELITY] %s: %d fidelity problem(s); written to %s but NOT marked completed: %s",
                chapter_id, len(blocking), output_path, "; ".join(blocking),
            )
            _update_manifest_after_translation(self.work_dir, set(), {chapter_id: "; ".join(blocking)})
            return output_path
        _update_manifest_after_translation(self.work_dir, {chapter_id})
        return output_path

    def translate_all(self, chapter_files: List[Path]) -> Dict[str, Path]:
        return {
            path.stem: self.translate_and_persist_chapter(path, {"chapter_id": path.stem})
            for path in in_reading_order(chapter_files)
        }

    # ── batch path ─────────────────────────────────────────────────────────

    def translate_volume_batch(self, chapter_files: List[Path]) -> Dict[str, Path]:
        """Translate pending chapters through the Batch API in waves.

        A batch submits every request before any runs, so chapter N cannot
        carry chapter N-1's translation. Waves are the compromise the Anthropic
        route established: every request in a wave shares one byte-identical
        prefix (system instruction + the ledger's replayed chapters, frozen
        for the wave) and the ledger grows between waves. On Gemini that
        prefix can go into one explicit CachedContent, so the continuity
        carrier and the cache key are the same bytes here too.

        Every job is recorded before polling, and jobs a previous run left
        open are collected first -- resubmitting would pay twice.
        """
        from src.Gemini.batch import BatchLedger, retrieve_batch_results, submit_batch
        from src.Gemini.caching import GeminiContextCache

        batch_cfg = get_gemini_batch_config()
        pending = in_reading_order(chapter_files)
        if not pending:
            return {}
        manager = self.client.conversation_manager
        wave_size = max(1, int(batch_cfg.get("wave_size", 4) or 4))
        poll_seconds = max(5, int(batch_cfg.get("poll_seconds", 60) or 60))
        deadline_seconds = _completion_window_seconds(batch_cfg.get("completion_window", "48h"))
        inline_limit = int(batch_cfg.get("inline_limit_bytes", 16 * 1024 * 1024) or 16 * 1024 * 1024)

        written: Dict[str, Path] = {}
        unfinished: Dict[str, str] = {}
        ledger = BatchLedger(self._state_path(batch_cfg, "gemini_batch_state.json"),
                             volume_id=self.volume_id, model=self.client.model)
        cache = GeminiContextCache(
            self.client._client,
            state_path=self._state_path(get_gemini_caching_config(), "gemini_cache_state.json"),
            volume_id=self.volume_id, model=self.client.model, config=get_gemini_caching_config(),
        )
        # A cache an interrupted run left live bills storage until its TTL;
        # keep only the ones an open job still reads.
        cache.release_stale(keep={str(entry.get("cache_name") or "") for entry in ledger.open_batches()})

        settled = self._drain_open_batches(
            ledger, cache, manager, {path.stem: path for path in pending}, written, unfinished,
            poll_seconds=poll_seconds, deadline_seconds=deadline_seconds,
        )
        if settled:
            pending = [path for path in pending if path.stem not in settled]
            if not pending:
                self._finish_batch_run(written, unfinished)
                return written

        seed_count = max(0, int(batch_cfg.get("seed_chapters", 1) or 0))
        seed_wave: List[Path] = []
        if manager is not None and not manager.turns and seed_count:
            seed_wave, pending = pending[:seed_count], pending[seed_count:]
            if not bool(batch_cfg.get("seed_via_batch", True)):
                for path in seed_wave:
                    logger.info("[GEMINI-BATCH] seeding continuity synchronously: %s", path.stem)
                    written[path.stem] = self.translate_and_persist_chapter(path, {"chapter_id": path.stem})
                seed_wave = []
        waves: List[List[Path]] = ([seed_wave] if seed_wave else []) + list(_chunk(pending, wave_size))

        for wave_number, wave in enumerate(waves, start=1):
            if manager is not None:
                manager.prepare_prefix()
            history = manager.history_contents() if manager is not None else []
            prompts = {path.stem: self._build_chapter_prompt(path.stem, path) for path in wave}

            cache_name = ""
            # Argon is implicit-cache only; asking for a CachedContent there
            # is a guaranteed refusal, so it is not attempted. The wave's
            # requests still share system + history byte for byte, which is
            # exactly what implicit caching keys on.
            cacheable = cache.mode != "never" and self.client.supports_explicit_caching
            if not self.client.supports_explicit_caching:
                # Character lower bound, no count_tokens round trip for a log line.
                approx = (len(self.system_instruction) + sum(
                    len(str(part.get("text") or "")) for item in history for part in item.get("parts", []))) // 4
                floor = self.client.implicit_cache_min_tokens
                logger.info(
                    "[GEMINI-CACHE] wave %d: implicit caching only, shared prefix ≈%d tokens %s the %d-token floor",
                    wave_number, approx, "clears" if approx >= floor else "is under", floor,
                )
            prefix_tokens = cache.count_tokens(self.system_instruction, history) if cacheable else 0
            if prefix_tokens and cache.should_cache(prefix_tokens, len(wave)):
                try:
                    cache_name = cache.create(self.system_instruction, history, wave=wave_number,
                                              prefix_tokens=prefix_tokens)
                except Exception as exc:  # noqa: BLE001 - uncached is slower, not wrong
                    logger.warning("[GEMINI-CACHE] create failed (%s); wave %d runs on implicit caching",
                                   exc, wave_number)
                    cache_name = ""

            requests = []
            for path in wave:
                chapter_id = path.stem
                prompt_turn = [{"role": "user", "parts": [{"text": prompts[chapter_id]}]}]
                neutral = self.client.build_generate_content_request(
                    contents=prompt_turn if cache_name else [*history, *prompt_turn],
                    system_instruction=None if cache_name else self.system_instruction,
                    cached_content=cache_name or None,
                )
                requests.append({"key": chapter_id, "neutral": neutral})

            ids = [path.stem for path in wave]
            submitted = submit_batch(
                self.client, requests, model=self.client.model,
                display_name=f"mtls-{self.volume_id}-wave{wave_number}", inline_limit_bytes=inline_limit,
            )
            job_name = submitted["job_name"]
            ledger.record_submitted(job_name, wave=wave_number, chapter_ids=ids,
                                    src_kind=submitted["src_kind"], cache_name=cache_name)
            logger.info(
                "[GEMINI-BATCH] wave %d submitted %s (%d chapter(s), %d history turn(s), %s)",
                wave_number, job_name, len(ids), len(manager.turns) if manager is not None else 0,
                f"cache {cache_name}" if cache_name else "no explicit cache",
            )
            state = self._await_batch(job_name, cache, cache_name,
                                      poll_seconds=poll_seconds, deadline_seconds=deadline_seconds)
            if state is None:
                # Left open in the ledger, cache kept alive, so the next run
                # collects it. Later waves would be built on a ledger that
                # never received this one's chapters, so they wait too.
                logger.error("[GEMINI-BATCH] %s did not end within the completion window; stopping", job_name)
                for chapter_id in ids:
                    unfinished.setdefault(chapter_id, f"batch {job_name} still open at deadline")
                break
            ledger.record_ended(job_name, state)
            self._log_storage(f"wave{wave_number}#cache-storage", cache.release(cache_name))
            outcome = retrieve_batch_results(self.client, job_name, model=self.client.model, chapter_ids=ids)
            unfinished.update(outcome.unfinished)
            self._absorb_results(outcome, wave, prompts, manager, written, unfinished)

        self._finish_batch_run(written, unfinished)
        return written

    def _finish_batch_run(self, written: Dict[str, Path], unfinished: Dict[str, str]) -> None:
        if written or unfinished:
            _update_manifest_after_translation(self.work_dir, set(written), unfinished)
        if unfinished:
            logger.warning(
                "[GEMINI-BATCH] %d chapter(s) left pending: %s", len(unfinished),
                "; ".join(f"{cid} ({reason})" for cid, reason in sorted(unfinished.items())),
            )

    def _state_path(self, cfg: Dict[str, Any], default_name: str) -> Path:
        raw = Path(str(cfg.get("persistence_file", f".context/{default_name}")))
        return raw if raw.is_absolute() else self.work_dir / raw

    def _drain_open_batches(
        self, ledger, cache, manager, jp_by_id: Dict[str, Path], written: Dict[str, Path],
        unfinished: Dict[str, str], *, poll_seconds: int, deadline_seconds: float,
    ) -> set:
        """Collect jobs a previous run submitted but never retrieved. Returns
        the chapter ids that must NOT be resubmitted this run."""
        from src.Gemini.batch import retrieve_batch_results

        settled: set = set()
        for entry in ledger.open_batches():
            job_name = str(entry.get("job_name") or "")
            cache_name = str(entry.get("cache_name") or "")
            chapter_ids = [cid for cid in entry.get("chapter_ids") or [] if cid in jp_by_id]
            if not job_name:
                continue
            if not chapter_ids:
                ledger.record_abandoned(job_name, "no pending chapters remain for this job")
                cache.release(cache_name)
                continue
            logger.info("[GEMINI-BATCH] recovering open job %s (wave %s, %d chapter(s))",
                        job_name, entry.get("wave"), len(chapter_ids))
            state = self._await_batch(job_name, cache, cache_name,
                                      poll_seconds=poll_seconds, deadline_seconds=deadline_seconds)
            if state is None:
                for chapter_id in chapter_ids:
                    unfinished[chapter_id] = f"batch {job_name} still open"
                settled.update(chapter_ids)
                continue
            ledger.record_ended(job_name, state)
            self._log_storage(f"recovered-{job_name}#cache-storage", cache.release(cache_name))
            outcome = retrieve_batch_results(
                self.client, job_name, model=self.client.model, chapter_ids=list(entry.get("chapter_ids") or [])
            )
            unfinished.update({cid: reason for cid, reason in outcome.unfinished.items() if cid in jp_by_id})
            paths = [jp_by_id[cid] for cid in chapter_ids]
            prompts = {path.stem: self._build_chapter_prompt(path.stem, path) for path in paths}
            self._absorb_results(outcome, paths, prompts, manager, written, unfinished)
            settled.update(cid for cid in chapter_ids if cid not in outcome.retryable)
        return settled

    def _absorb_results(
        self, outcome, wave: List[Path], prompts: Dict[str, str], manager,
        written: Dict[str, Path], unfinished: Dict[str, str],
    ) -> None:
        """Persist one job's succeeded results and commit them to the ledger,
        in chapter order. Shared by the wave loop and recovery."""
        for path in wave:
            chapter_id = path.stem
            response = outcome.succeeded.get(chapter_id)
            if response is None:
                continue
            self._log_usage(chapter_id, response)
            if response.termination in _BLOCKED_TERMINATIONS:
                try:
                    text = self._safety_fallback_translate(
                        path, chapter_id, _block_error(chapter_id, response, "batch translation")
                    )
                except Exception as exc:  # noqa: BLE001 - chapter stays pending, loudly
                    unfinished[chapter_id] = f"blocked; safety fallback failed: {exc}"
                    continue
                fallback_path, fallback_blocking = self._write_chapter(chapter_id, text)
                if fallback_blocking:
                    unfinished[chapter_id] = "fidelity gate: " + "; ".join(fallback_blocking)
                    continue
                written[chapter_id] = fallback_path
                unfinished.pop(chapter_id, None)
                continue
            if response.termination == LLMTermination.MAX_OUTPUT:
                # A batch job cannot continue mid-flight. Persisting the
                # fragment would stamp it completed forever, so it stays
                # pending for the synchronous path, which can continue.
                unfinished[chapter_id] = "max_output — truncated, not persisted; re-run synchronously"
                logger.error("[GEMINI-BATCH] %s hit the output ceiling; NOT persisted", chapter_id)
                continue
            text, leaked_blocks = sanitize_chapter_output(response.content)
            if self._is_english:
                text = _CJK_LEAK_RE.sub("", text)
            if not text.strip():
                unfinished[chapter_id] = "no visible translation text"
                logger.error("[GEMINI-BATCH] %s returned no visible text; NOT persisted", chapter_id)
                continue
            self._maybe_write_thinking_log(
                chapter_id=chapter_id, api_thinking=response.thinking_content,
                leaked_blocks=leaked_blocks, thinking_blocks=thinking_tally([response]),
            )
            output_path, blocking = self._write_chapter(chapter_id, text)
            if blocking:
                # Not committed either: a chapter that lost content must not
                # become the continuity its successors are built on.
                unfinished[chapter_id] = "fidelity gate: " + "; ".join(blocking)
                logger.error("[GEMINI-FIDELITY] %s: %s; written but NOT completed or committed",
                             chapter_id, "; ".join(blocking))
                continue
            written[chapter_id] = output_path
            if manager is not None:
                # No interaction id: the server never saw this chapter, so the
                # chain stays broken and the next sync call rebases.
                manager.commit(chapter_id=chapter_id, user_text=prompts[chapter_id], model_text=text,
                               output_path=output_path)

    def _await_batch(self, job_name: str, cache, cache_name: str, *, poll_seconds: int,
                     deadline_seconds: float) -> Optional[str]:
        """Poll until the job reaches a terminal state; None if the completion
        window elapses first. Extends the wave's cache while the job runs."""
        from src.Gemini.batch import TERMINAL_STATES, poll_batch

        started = time.monotonic()
        last_extend = started
        extend_every = max(poll_seconds, cache.initial_ttl_seconds // 2)
        while True:
            state = poll_batch(self.client, job_name)
            if state in TERMINAL_STATES:
                return state
            now = time.monotonic()
            if now - started >= deadline_seconds:
                return None
            if cache_name and now - last_extend >= extend_every:
                cache.extend(cache_name)
                last_extend = now
            time.sleep(poll_seconds)


def _block_error(chapter_id: str, response: LLMResponse, what: str) -> GeminiSafetyBlockError:
    """The block's own reason (SAFETY, RECITATION, PROHIBITED_CONTENT, a
    failed-interaction status) as the error code, so the safety fallback's
    audit trail records which filter fired rather than a generic label."""
    reason = str(response.raw_finish_reason or "safety").upper()
    return GeminiSafetyBlockError(
        f"Gemini withheld the {what} of {chapter_id} ({reason}).", code=reason or "SAFETY"
    )


def _completion_window_seconds(raw: Any, default_seconds: float = 48 * 3600.0) -> float:
    """Parse "48h" / "90m" / "3600s" / bare seconds. The default is Google's
    own 48-hour expiry (JOB_STATE_EXPIRED), not the 24-hour target."""
    text = str(raw or "").strip().lower()
    if not text:
        return default_seconds
    units = {"h": 3600.0, "m": 60.0, "s": 1.0}
    multiplier = units.get(text[-1], 1.0)
    number = text[:-1] if text[-1] in units else text
    try:
        value = float(number)
    except ValueError:
        logger.warning("[GEMINI-BATCH] unparseable completion_window %r; using %.0fs", raw, default_seconds)
        return default_seconds
    return value * multiplier if value > 0 else default_seconds


def _chunk(items: List[Path], size: int) -> Iterator[List[Path]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _update_manifest_after_translation(
    work_dir: Path, translated_stems: set, blocked: Optional[Dict[str, str]] = None,
) -> None:
    """Stamp the manifest: "completed", or "blocked" (rendering on disk that
    failed the gate) / "pending" (nothing persisted) with the reason."""
    path = Path(work_dir) / "manifest.json"
    if not path.exists():
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    blocked = blocked or {}
    chapters = manifest.get("chapters", [])
    for entry in chapters if isinstance(chapters, list) else []:
        if not isinstance(entry, dict):
            continue
        stem = Path(str(entry.get("source_file", ""))).stem
        if stem in translated_stems:
            entry["translation_status"] = "completed"
            entry.pop("last_failure", None)
        elif stem in blocked:
            rendered = translated_path(Path(work_dir), stem).exists()
            entry["translation_status"] = "blocked" if rendered else "pending"
            entry["last_failure"] = blocked[stem]
    completed = sum(1 for e in chapters if isinstance(e, dict) and e.get("translation_status") == "completed")
    manifest.setdefault("pipeline_state", {})["translator"] = {
        "status": "completed" if chapters and completed == len(chapters) else "in_progress",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chapters_completed": completed,
        "chapters_total": len(chapters),
        "target_language": volume_target_language(Path(work_dir)),
    }
    atomic_write_json(path, manifest)


def reconcile_manifest_from_disk(work_dir: Path) -> Dict[str, str]:
    """Replay the fidelity gate against renderings already on disk, with no API
    call, so a chapter blocked by a since-fixed cause is stamped completed
    instead of being paid for again. Never downgrades a completed chapter."""
    path = Path(work_dir) / "manifest.json"
    if not path.exists():
        return {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    chapters = manifest.get("chapters", [])
    if not isinstance(chapters, list):
        return {}
    try:
        context_xml = (Path(work_dir) / "context.xml").read_text(encoding="utf-8")
    except OSError:
        context_xml = ""
    is_english = volume_target_language(Path(work_dir)) == DEFAULT_LANGUAGE
    anchors = parse_anchors(context_xml) if is_english else []
    cfg = get_gemini_fidelity_config()
    block_on = {str(item).strip().lower() for item in _block_on_setting(cfg)}
    try:
        tolerance = float(cfg.get("ratio_tolerance", DEFAULT_RATIO_TOLERANCE) or DEFAULT_RATIO_TOLERANCE)
    except (TypeError, ValueError):
        tolerance = DEFAULT_RATIO_TOLERANCE

    pairs: Dict[str, Tuple[str, str]] = {}
    for entry in chapters:
        if not isinstance(entry, dict):
            continue
        stem = Path(str(entry.get("source_file", ""))).stem
        jp_path = Path(work_dir) / "JP" / (stem + ".md")
        en_path = translated_path(Path(work_dir), stem)
        if jp_path.exists() and en_path.exists():
            try:
                pairs[stem] = (jp_path.read_text(encoding="utf-8"), en_path.read_text(encoding="utf-8"))
            except OSError:
                continue

    healed: List[str] = []
    still_blocked: Dict[str, str] = {}
    for entry in chapters:
        if not isinstance(entry, dict) or entry.get("translation_status") == "completed":
            continue
        stem = Path(str(entry.get("source_file", ""))).stem
        if stem not in pairs:
            continue
        if not is_english and "truncated" in str(entry.get("last_failure") or ""):
            # A truncated rendering cannot be healed by re-checking it.
            still_blocked[stem] = str(entry.get("last_failure"))
            continue
        jp_source, en_text = pairs[stem]
        peers = [
            ratio for other, (other_jp, other_en) in pairs.items() if other != stem
            for ratio in [length_ratio(other_jp, other_en)] if ratio is not None
        ]
        reasons = [
            "anchor %s %s" % (row.get("anchor_id"), row.get("status"))
            for row in reconcile_chapter(anchors, chapter_id=stem, jp_source=jp_source, en_text=en_text)
            if str(row.get("status")) in ("missing", "drifted") and str(row.get("status")) in block_on
        ]
        if "structural" in block_on:
            reasons.extend(
                "illustration parity: source %s, output %s" % (row.get("jp_count"), row.get("en_count"))
                for row in structural_report(jp_source, en_text, peers, tolerance=tolerance)
                if row.get("check") == "illustration_parity" and str(row.get("status")) == "mismatch"
            )
        if reasons:
            entry["translation_status"] = "blocked"
            entry["last_failure"] = "; ".join(reasons)
            still_blocked[stem] = entry["last_failure"]
        else:
            entry["translation_status"] = "completed"
            entry.pop("last_failure", None)
            healed.append(stem)
    if not (healed or still_blocked):
        return {}
    completed = sum(1 for e in chapters if isinstance(e, dict) and e.get("translation_status") == "completed")
    manifest.setdefault("pipeline_state", {})["translator"] = {
        "status": "completed" if chapters and completed == len(chapters) else "in_progress",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chapters_completed": completed,
        "chapters_total": len(chapters),
        "target_language": volume_target_language(Path(work_dir)),
    }
    atomic_write_json(path, manifest)
    if healed:
        logger.info("[GEMINI-MANIFEST] reconciled from disk: %d chapter(s) stamped completed (%s)",
                    len(healed), ", ".join(sorted(healed)))
    if still_blocked:
        logger.warning("[GEMINI-MANIFEST] %d rendering(s) on disk still fail the gate: %s", len(still_blocked),
                       "; ".join("%s (%s)" % (cid, reason) for cid, reason in sorted(still_blocked.items())))
    return still_blocked


def _filter_completed_chapters(work_dir: Path, chapter_files: List[Path]) -> List[Path]:
    path = Path(work_dir) / "manifest.json"
    if not path.exists():
        return chapter_files
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return chapter_files
    completed = {
        Path(str(entry.get("source_file", ""))).stem
        for entry in manifest.get("chapters", [])
        if isinstance(entry, dict) and entry.get("translation_status") == "completed"
    }
    return [path for path in chapter_files if path.stem not in completed]


def translate_volume(
    volume_id: str,
    chapters: Optional[List[str]] = None,
    thinking_log_enabled: Optional[bool] = None,
    dry_run: bool = False,
) -> Dict[str, Path]:
    work_dir = WORK_DIR / volume_id
    jp_dir = work_dir / "JP"
    if not jp_dir.is_dir():
        raise FileNotFoundError(f"No JP/ directory for volume {volume_id!r} at {jp_dir}")
    chapter_files = in_reading_order(jp_dir.glob("CHAPTER_*.md"))
    if chapters:
        wanted = set(chapters)
        chapter_files = [p for p in chapter_files if p.stem in wanted or p.stem.split("_")[-1] in wanted]
    if not dry_run:
        reconcile_manifest_from_disk(work_dir)
        chapter_files = _filter_completed_chapters(work_dir, chapter_files)
    translator = GeminiTranslator(work_dir, volume_id, thinking_log_enabled=thinking_log_enabled, dry_run=dry_run)
    batch_cfg = translator._config.get("batch", {}) or {}
    if bool(batch_cfg.get("enabled", False)) and not dry_run:
        return translator.translate_volume_batch(chapter_files)
    return translator.translate_all(chapter_files)
