"""Gemini client for the Phase 2 literary translator (Google GenAI SDK).

Two Google surfaces, one boundary object:

* The **Interactions API** (``client.interactions``) carries the synchronous
  path. Generally available since June 2026 and where new models and features
  launch first. Its server-side state -- ``previous_interaction_id`` over
  stored interactions -- is what lets a volume be one continuous
  conversation without resending the history, with implicit caching over it
  and thought signatures managed server-side.
* The **generateContent** surface carries what Interactions does not have
  yet: the Batch API and explicit context caching (``batch.py``,
  ``caching.py``). This client builds those requests; it does not send them.

Scoped to the Gemini 3 models this route is verified against. All three are
1M-token-context thinking models with a 64K output ceiling that INCLUDES
thinking tokens, controlled by ``thinking_level`` (``thinking_budget`` is the
2.5-era knob and is not sent). Sampling parameters are never sent: Google
recommends leaving temperature/top_p/top_k at their defaults on Gemini 3, and
lowering temperature can cause looping and degraded reasoning.

Sources: ai.google.dev/gemini-api/docs (interactions-overview, thinking,
whats-new-gemini-3.8, batch-api, caching), checked 2026-10-12, and the
installed google-genai 2.29.0 types.

Gemini 4 Argon adds two capabilities, gated to that model alone
(docs: long-decode and pricing, checked 2026-10-14):

* **Long Context** -- one 1,048,576-token window SHARED by input, thinking
  and output. The conversation ledger budgets history against the window
  minus the output reservation, not against the whole window.
* **Long Decode** -- ``background: true`` with ``max_output_tokens`` above
  65,536 (up to the window minus input). Only background interactions may
  exceed 64K; a foreground request above it is a 400. The interaction is
  created once and polled with ``interactions.get`` -- it can run for hours,
  and decodes in ~5K-token chunks after the first 64K. ``total_input_tokens``
  is summed across every hop, so it is billing truth but NOT a context size.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union

from src.Deepseek.common.config import cfg_float, cfg_int
from src.Deepseek.common.llm_types import LLMApiFamily, LLMResponse
from src.Deepseek.common.token_telemetry import cost_breakdown_usd
from src.Gemini.config import get_gemini_config, get_gemini_long_context_config, get_gemini_long_decode_config
from src.Gemini.errors import (
    GeminiLongDecodeError,
    RetryPolicy,
    call_with_retry,
    classify_exception,
    is_retryable,
)
from src.Gemini.prompt_loader import join_system_segments
from src.Gemini.response import as_dict, interaction_to_llm_response

logger = logging.getLogger(__name__)

SUPPORTED_MODELS = ("gemini-4-argon", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.1-pro-preview")

# Thinking levels each model accepts. "minimal" is a hard error on 3.8 Flash
# and absent from 3.1 Pro (docs: thinking#thinking-levels). Argon: low,
# medium (its default) and high per the thinking table, checked 2026-10-14;
# minimal is not listed, so it stays out.
THINKING_LEVELS: Dict[str, tuple] = {
    "gemini-4-argon": ("low", "medium", "high"),
    "gemini-3.8-flash": ("low", "medium", "high"),
    "gemini-3.6-flash": ("minimal", "low", "medium", "high"),
    "gemini-3.1-pro-preview": ("low", "medium", "high"),
}
_LEVEL_ORDER = ("minimal", "low", "medium", "high")

# Output ceiling, thinking included. A max_output_tokens above it is clamped.
# This is the ceiling for every foreground request on every model.
MAX_OUTPUT_TOKENS = 65_536

# ── Gemini 4 Argon: Long Context / Long Decode gate ─────────────────────────
LONG_CONTEXT_MODELS = ("gemini-4-argon",)
LONG_DECODE_MODELS = ("gemini-4-argon",)
LONG_CONTEXT_WINDOW = 1_048_576
DEFAULT_CONTEXT_WINDOW = 1_000_000
# The doc's own example budget. Deliberately not "the rest of the window":
# the model does not follow length targets, and a runaway decode bills
# every token at the output rate.
DEFAULT_LONG_DECODE_OUTPUT = 262_144
# Argon is implicit-cache only; a CachedContent create is refused.
EXPLICIT_CACHE_UNSUPPORTED = ("gemini-4-argon",)
# Shortest shared prefix implicit caching will hit, per model. Google's two
# doc sets disagree for Argon (checked 2026-10-14): the Gemini Enterprise
# Argon guide says "shared prompt prefixes of 6,144 tokens or more, with
# durable caching enabled by default"; the Developer API's Interactions
# caching table still lists 4,096. The higher figure is taken on purpose --
# if the real floor is 4,096, prefixes of 4,096-6,143 merely go unscored;
# the lower one would score uncacheable prefixes as misses and raise false
# alarms. Neither doc set names a request field for durable caching, so
# there is nothing to send: it is on by default and not configurable. This
# is NOT caching.min_tokens, which is the explicit-cache create floor.
IMPLICIT_CACHE_MIN_TOKENS: Dict[str, int] = {"gemini-4-argon": 6_144}
DEFAULT_IMPLICIT_CACHE_MIN_TOKENS = 4_096
# generateContent per-request output on Argon: 32K non-streaming (the Batch
# API's shape). Other models keep MAX_OUTPUT_TOKENS.
GENERATE_CONTENT_OUTPUT_CEILING: Dict[str, int] = {"gemini-4-argon": 32_768}
_BACKGROUND_PENDING = ("queued", "in_progress")

MODEL_ID_ALIASES = {
    "gemini-3.8": "gemini-3.8-flash",
    "gemini-3.6": "gemini-3.6-flash",
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
}

SERVICE_TIERS = ("standard", "flex", "priority")


def normalize_model_id(model: str) -> str:
    name = str(model or "").strip()
    return MODEL_ID_ALIASES.get(name, name)


def lower_thinking_level(model: str, level: str) -> Optional[str]:
    """The next level down that ``model`` accepts, or None at the floor."""
    allowed = THINKING_LEVELS.get(model, ("low", "medium", "high"))
    try:
        index = _LEVEL_ORDER.index(level)
    except ValueError:
        return None
    for candidate in reversed(_LEVEL_ORDER[:index]):
        if candidate in allowed:
            return candidate
    return None


@dataclass(frozen=True)
class GeminiCapabilities:
    provider_name: str = "gemini"
    api_family: LLMApiFamily = LLMApiFamily.GEMINI_INTERACTIONS
    supports_thinking_levels: bool = True
    supports_server_side_state: bool = True
    supports_implicit_caching: bool = True
    supports_explicit_caching: bool = True  # generateContent / batch path only
    supports_streaming: bool = True
    supports_batch: bool = True  # generateContent Batch API
    context_window: int = 1_000_000
    max_output_tokens: int = MAX_OUTPUT_TOKENS


GEMINI_CAPABILITIES = GeminiCapabilities()


class GeminiClient:
    """Provider-isolated client returning MTLS's shared ``LLMResponse``."""

    CAPABILITIES = GEMINI_CAPABILITIES

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, dry_run: bool = False):
        cfg = get_gemini_config()
        self._cfg = cfg
        configured = str(model or cfg.get("model", "gemini-3.8-flash"))
        self.model = normalize_model_id(configured)
        if self.model != configured:
            logger.info("[GEMINI] model %r resolved to %r", configured, self.model)
        if self.model not in SUPPORTED_MODELS:
            logger.warning(
                "[GEMINI] model %r is outside the supported set %s; this route is built and verified only "
                "for those models", self.model, SUPPORTED_MODELS,
            )
        self.thinking_level = self._validated_thinking_level(cfg)
        self.thinking_summaries = str((cfg.get("thinking", {}) or {}).get("summaries", "auto") or "auto")
        tier = str(cfg.get("service_tier", "standard") or "standard").strip().lower()
        if tier not in SERVICE_TIERS:
            raise ValueError(f"translation.gemini.service_tier must be one of {SERVICE_TIERS}, got {tier!r}")
        self.service_tier = tier
        configured_max = int((cfg.get("generation", {}) or {}).get("max_output_tokens", MAX_OUTPUT_TOKENS) or MAX_OUTPUT_TOKENS)
        if configured_max > MAX_OUTPUT_TOKENS:
            logger.warning("[GEMINI] max_output_tokens %d exceeds the %d ceiling; clamped", configured_max, MAX_OUTPUT_TOKENS)
        self.max_output_tokens = min(MAX_OUTPUT_TOKENS, max(1024, configured_max))
        self._init_long_context_and_decode()
        self.api_key_env = str(cfg.get("api_key_env", "GEMINI_API_KEY"))
        self.api_key = api_key or os.getenv(self.api_key_env) or os.getenv("GOOGLE_API_KEY")
        if not self.api_key and not dry_run:
            raise ValueError(f"Gemini API key not found. Set {self.api_key_env} or pass api_key explicitly.")
        self._client = None
        if not dry_run:
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:
                raise ImportError("GeminiClient requires the Google GenAI SDK (pip install 'google-genai>=2.3.0').") from exc
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=int(float(cfg.get("http_timeout_seconds", 900)) * 1000)),
            )
        self.conversation_manager = None
        self._cache_telemetry: Dict[str, Any] = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "thought_tokens": 0,
            "cache_read_tokens": 0,
            "expected_prefix_tokens": 0,
            "recovered_prefix_tokens": 0,
            "breakpoint_opportunities": 0,
            "breakpoint_successes": 0,
            "below_floor_calls": 0,
            "breakpoint_success_rate": 0.0,
            "prefix_recovery": 0.0,
            "total_cache_coverage": 0.0,
            "cache_hit_ratio": 0.0,
            "cache_economics": {
                "baseline_input_cost_usd": 0.0,
                "actual_input_cost_usd": 0.0,
                "net_input_savings_usd": 0.0,
            },
        }
        self._previous_chain_input = 0

    def _validated_thinking_level(self, cfg: Dict[str, Any]) -> str:
        """Fail at startup, not on the first chapter: an unsupported level is a
        400 on every request (``minimal`` on 3.8 Flash is the documented one)."""
        level = str((cfg.get("thinking", {}) or {}).get("level", "high") or "high").strip().lower()
        allowed = THINKING_LEVELS.get(self.model)
        if allowed and level not in allowed:
            raise ValueError(
                f"translation.gemini.thinking.level={level!r} is not supported by {self.model} "
                f"(accepted: {', '.join(allowed)})."
            )
        return level

    def _init_long_context_and_decode(self) -> None:
        """Resolve the Argon-only capabilities. Both are inert on any model
        outside LONG_CONTEXT_MODELS / LONG_DECODE_MODELS, whatever the config
        says -- the config enables them, the model gates them."""
        long_context_cfg = get_gemini_long_context_config()
        long_decode_cfg = get_gemini_long_decode_config()
        # The physical window is the model's, used for the output clamp even
        # when long_context budgeting is switched off.
        self.model_context_window = LONG_CONTEXT_WINDOW if self.model in LONG_CONTEXT_MODELS else DEFAULT_CONTEXT_WINDOW
        self.long_context = bool(long_context_cfg.get("enabled", True)) and self.model in LONG_CONTEXT_MODELS
        configured_window = int(long_context_cfg.get("context_window", LONG_CONTEXT_WINDOW) or LONG_CONTEXT_WINDOW)
        self.context_window = (
            min(self.model_context_window, max(1, configured_window)) if self.long_context else DEFAULT_CONTEXT_WINDOW
        )

        self.long_decode = bool(long_decode_cfg.get("enabled", True)) and self.model in LONG_DECODE_MODELS
        requested = int(long_decode_cfg.get("max_output_tokens", DEFAULT_LONG_DECODE_OUTPUT) or DEFAULT_LONG_DECODE_OUTPUT)
        if self.long_decode and requested <= MAX_OUTPUT_TOKENS:
            # Background with <= 64K is just a slower foreground call.
            logger.warning(
                "[GEMINI-LONG-DECODE] long_decode.max_output_tokens=%d does not exceed the %d standard "
                "ceiling; long decode disabled", requested, MAX_OUTPUT_TOKENS,
            )
            self.long_decode = False
        self.long_decode_max_output_tokens = min(self.model_context_window, requested) if self.long_decode else 0
        self.long_decode_poll_seconds = max(2.0, float(long_decode_cfg.get("poll_seconds", 10) or 10))
        # Background interactions can run up to 24 hours.
        self.long_decode_max_wait_seconds = max(
            60.0, float(long_decode_cfg.get("max_wait_hours", 24) or 24) * 3600.0)
        self.long_decode_max_poll_failures = max(
            1, int(long_decode_cfg.get("max_consecutive_poll_failures", 10) or 10))
        if self.long_context or self.long_decode:
            logger.info(
                "[GEMINI-LONG-CONTEXT] %s: window=%d long_decode=%s output_ceiling=%d",
                self.model, self.context_window, self.long_decode, self.output_ceiling,
            )

    @property
    def output_ceiling(self) -> int:
        """The most a single interaction can emit, thinking included."""
        return self.long_decode_max_output_tokens if self.long_decode else self.max_output_tokens

    @property
    def supports_explicit_caching(self) -> bool:
        return self.model not in EXPLICIT_CACHE_UNSUPPORTED

    @property
    def implicit_cache_min_tokens(self) -> int:
        return IMPLICIT_CACHE_MIN_TOKENS.get(self.model, DEFAULT_IMPLICIT_CACHE_MIN_TOKENS)

    def log_implicit_cache_plan(self, system_text: str) -> None:
        """Say once, up front, whether the static prefix alone can cache.

        A lower-bound estimate (4 chars/token): the question is "does it
        clear the floor", and an over-estimate would answer yes wrongly. The
        chain usually crosses the floor by the second chapter either way;
        this only tells the operator whether the FIRST call can hit."""
        estimate = len(system_text) // 4
        floor = self.implicit_cache_min_tokens
        logger.info(
            "[GEMINI-CACHE] %s: %s; static prefix ≈%d tokens %s the %d-token implicit floor",
            self.model,
            "implicit caching only (explicit unsupported)" if not self.supports_explicit_caching
            else "implicit caching (explicit on batch waves)",
            estimate, "clears" if estimate >= floor else "is under", floor,
        )

    def attach_conversation(self, *, work_dir, volume_id: str, conversation_config: Optional[Dict[str, Any]] = None):
        from src.Gemini.conversation import GeminiConversationManager

        config = dict(conversation_config or {})
        if self.long_context:
            # The window is shared with output, so history may only use what
            # the output reservation leaves. Derived from the same ceiling the
            # request sends, so compaction and the clamp cannot drift apart.
            config["context_window"] = self.context_window
            config["output_reserve_tokens"] = self.output_ceiling
        self.conversation_manager = GeminiConversationManager(work_dir, volume_id, self.model, config)
        return self.conversation_manager

    @property
    def cache_telemetry(self) -> Dict[str, Any]:
        return dict(self._cache_telemetry)

    # ── Interactions (sync path) ───────────────────────────────────────────

    def build_interaction_body(
        self,
        *,
        input: str,
        system_instruction: Union[str, Sequence[str]],
        previous_interaction_id: Optional[str] = None,
        thinking_level: Optional[str] = None,
        continuation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        system_text = system_instruction if isinstance(system_instruction, str) else join_system_segments(system_instruction)
        body: Dict[str, Any] = {
            "model": self.model,
            "input": input,
            # Interaction-scoped, like generation_config: previous_interaction_id
            # carries the conversation, never the instruction, so it is resent
            # on every call (and implicit caching makes the resend cheap).
            "system_instruction": system_text,
            "generation_config": {
                "thinking_level": thinking_level or self.thinking_level,
                "thinking_summaries": self.thinking_summaries,
                "max_output_tokens": self.max_output_tokens,
            },
            # store=True is required for previous_interaction_id, for the
            # authoritative interactions.get() after a stream, and for every
            # background interaction.
            "store": True,
        }
        if continuation_token:
            # Resuming a long decode: the opaque token carries the state, so
            # neither new input nor a chain link is sent.
            body.pop("input")
            body["continuation_token"] = continuation_token
        elif previous_interaction_id:
            body["previous_interaction_id"] = previous_interaction_id
        if self.long_decode:
            body["background"] = True
            body["generation_config"]["max_output_tokens"] = self._long_decode_budget(
                input if not continuation_token else "", system_text,
                chained=bool(previous_interaction_id or continuation_token),
            )
        if self.service_tier != "standard":
            body["service_tier"] = self.service_tier
        return body

    def _long_decode_budget(self, input_text: str, system_text: str, *, chained: bool) -> int:
        """Output budget for one background interaction: the configured
        ceiling, but never past the window minus this request's input.

        The input is a character estimate (2 chars/token, conservative for
        Japanese) plus the ledger's own estimate when the request extends a
        chain -- never the server's total_input_tokens, which long decode
        sums across hops and which overstates the real context."""
        estimate = (len(input_text) + len(system_text)) // 2
        if chained and self.conversation_manager is not None:
            estimate += self.conversation_manager.estimated_tokens()
        room = self.model_context_window - estimate
        budget = min(self.long_decode_max_output_tokens, room)
        if budget < self.long_decode_max_output_tokens:
            logger.warning(
                "[GEMINI-LONG-DECODE] output budget clamped to %d by ~%d input tokens in a %d window",
                max(1024, budget), estimate, self.model_context_window,
            )
        return max(1024, budget)

    def generate(
        self,
        *,
        input: str,
        system_instruction: Union[str, Sequence[str]],
        previous_interaction_id: Optional[str] = None,
        thinking_level: Optional[str] = None,
        dry_run: bool = False,
        stream: Optional[bool] = None,
        continuation_token: Optional[str] = None,
    ) -> LLMResponse:
        body = self.build_interaction_body(
            input=input,
            system_instruction=system_instruction,
            previous_interaction_id=previous_interaction_id,
            thinking_level=thinking_level,
            continuation_token=continuation_token,
        )
        streaming_cfg = self._cfg.get("streaming", {}) or {}
        # Long decode polls instead: a background interaction can outlive any
        # sane stream, and the stored record is what gets read either way.
        use_stream = False if self.long_decode else bool(
            streaming_cfg.get("enabled", True) if stream is None else stream)
        if dry_run:
            return LLMResponse(
                content="[DRY RUN]",
                model=self.model,
                provider="gemini",
                api_family=LLMApiFamily.GEMINI_INTERACTIONS,
                provider_metadata={"dry_run": True, "payload": {**body, "stream": use_stream}},
            )

        retry_cfg = self._cfg.get("retry", {}) or {}
        policy = RetryPolicy(
            max_retries=cfg_int(retry_cfg, "max_retries", 5),
            base_delay_ms=int(retry_cfg.get("base_delay_ms", 1000) or 1000),
            max_delay_ms=int(retry_cfg.get("max_delay_ms", 60_000) or 60_000),
            jitter_factor=cfg_float(retry_cfg, "jitter_factor", 0.25),
            max_429_retries=cfg_int(retry_cfg, "max_429_retries", 4),
            max_503_retries=cfg_int(retry_cfg, "max_503_retries", 4),
        )
        on_retry = lambda attempt, err, delay: logger.warning(  # noqa: E731
            "[GEMINI-RETRY] attempt=%d delay=%.2fs error=%s", attempt, delay, err
        )

        if self.long_decode:
            # Only the create is retried. Once an interaction exists, a retry
            # would start and bill a second one; polling absorbs its own blips.
            created = call_with_retry(lambda: self._create_background(body), policy, on_retry=on_retry)
            raw = self._await_background(created, body["generation_config"]["max_output_tokens"])
            response = interaction_to_llm_response(
                raw, model=self.model, streamed=False, service_tier=self.service_tier
            )
            self._finish_response(response, chained=bool(previous_interaction_id or continuation_token))
            return response

        def invoke() -> LLMResponse:
            try:
                raw = self._create_streaming(body) if use_stream else self._client.interactions.create(**body)
                response = interaction_to_llm_response(
                    raw, model=self.model, streamed=use_stream, service_tier=self.service_tier
                )
            except Exception as exc:  # one provider-neutral boundary for SDK failures
                raise classify_exception(exc) from exc
            self._finish_response(response, chained=bool(previous_interaction_id))
            return response

        return call_with_retry(invoke, policy, on_retry=on_retry)

    def _finish_response(self, response: LLMResponse, *, chained: bool) -> None:
        # The context size the ledger may trust. A long decode that ran past
        # one 64K hop reports total_input_tokens summed over every hop, which
        # overstates the context; 0 makes the ledger fall back to its own
        # estimate. Every other call is a single hop, so nothing changes.
        response.provider_metadata["context_input_tokens"] = (
            0 if int(response.output_tokens) > MAX_OUTPUT_TOKENS else int(response.input_tokens)
        )
        self._record_cache_telemetry(response, chained=chained)
        response.provider_metadata["cache_telemetry"] = self.cache_telemetry

    # ── Long Decode (background interactions) ──────────────────────────────

    def _create_background(self, body: Dict[str, Any]) -> Any:
        try:
            return self._client.interactions.create(**body)
        except Exception as exc:  # noqa: BLE001 - normalized for the retry policy
            raise classify_exception(exc) from exc

    def _await_background(self, created: Any, budget: int) -> Dict[str, Any]:
        """Poll a background interaction to a terminal status.

        Transient poll failures are absorbed here, up to
        long_decode.max_consecutive_poll_failures in a row; past that, or past
        long_decode.max_wait_hours, the interaction is cancelled (it is billed
        for work done either way) and GeminiLongDecodeError -- not retryable --
        is raised. Ctrl-C cancels it too rather than leaving it decoding.
        """
        raw = as_dict(created)
        interaction_id = str(raw.get("id") or "")
        if not interaction_id:
            raise GeminiLongDecodeError("Gemini background interaction was created without an id")
        logger.info("[GEMINI-LONG-DECODE] %s started (status=%s, max_output_tokens=%d)",
                    interaction_id, raw.get("status"), budget)
        started = time.monotonic()
        failures = 0
        status = str(raw.get("status") or "").lower()
        try:
            while status in _BACKGROUND_PENDING:
                if time.monotonic() - started >= self.long_decode_max_wait_seconds:
                    self._cancel_background(interaction_id, "wait deadline passed")
                    raise GeminiLongDecodeError(
                        f"Gemini background interaction {interaction_id} still {status} after "
                        f"{self.long_decode_max_wait_seconds / 3600.0:.1f}h; cancelled"
                    )
                time.sleep(self.long_decode_poll_seconds)
                try:
                    raw = as_dict(self._client.interactions.get(interaction_id))
                except Exception as exc:  # noqa: BLE001 - classified below
                    normalized = classify_exception(exc)
                    failures += 1
                    if not is_retryable(normalized) or failures >= self.long_decode_max_poll_failures:
                        self._cancel_background(interaction_id, f"polling failed: {normalized}")
                        raise GeminiLongDecodeError(
                            f"Gemini background interaction {interaction_id}: polling failed "
                            f"{failures} time(s): {normalized}", status_code=normalized.status_code,
                        ) from exc
                    logger.warning("[GEMINI-LONG-DECODE] %s poll failed (%d/%d): %s", interaction_id,
                                   failures, self.long_decode_max_poll_failures, normalized)
                    continue
                failures = 0
                new_status = str(raw.get("status") or "").lower()
                if new_status != status:
                    logger.info("[GEMINI-LONG-DECODE] %s %s -> %s (%.0fs)", interaction_id, status,
                                new_status, time.monotonic() - started)
                status = new_status
        except KeyboardInterrupt:
            self._cancel_background(interaction_id, "interrupted")
            raise
        return raw

    def _cancel_background(self, interaction_id: str, reason: str) -> None:
        try:
            self._client.interactions.cancel(interaction_id)
            logger.warning("[GEMINI-LONG-DECODE] cancelled %s (%s)", interaction_id, reason)
        except Exception as exc:  # noqa: BLE001 - best effort; the caller is already failing
            logger.error("[GEMINI-LONG-DECODE] cancel of %s failed (%s): %s", interaction_id, reason, exc)

    def _create_streaming(self, body: Dict[str, Any]) -> Any:
        """Stream for connection liveness, then fetch the stored interaction.

        A chapter can take minutes of thinking plus up to 64K output tokens;
        a blocking request sits idle on the wire that whole time. Streaming
        keeps bytes moving. The finished resource is then read back with
        ``interactions.get(id)`` -- the stored record is authoritative and
        lossless. If that read fails, the interaction is rebuilt from the
        stream itself: steps from ``step.start`` + ``step.delta`` by index,
        id/status/usage from ``interaction.completed`` (whose documented
        payload carries no steps, so it cannot stand alone).
        """
        interaction_id = ""
        completed: Dict[str, Any] = {}
        error_event: Any = None
        steps: Dict[int, Dict[str, Any]] = {}
        for event in self._client.interactions.create(**body, stream=True):
            event_type = str(getattr(event, "event_type", "") or "")
            if event_type in ("interaction.created", "interaction.completed"):
                interaction = as_dict(getattr(event, "interaction", None))
                interaction_id = str(interaction.get("id") or interaction_id)
                if event_type == "interaction.completed":
                    completed = interaction
            elif event_type == "step.start":
                steps[int(getattr(event, "index", len(steps)))] = as_dict(getattr(event, "step", None))
            elif event_type == "step.delta":
                _apply_step_delta(steps, int(getattr(event, "index", 0)), as_dict(getattr(event, "delta", None)))
            elif event_type == "error":
                error_event = getattr(event, "error", None)
        if error_event is not None and not completed:
            raise RuntimeError(f"Gemini stream error: {as_dict(error_event)}")
        if interaction_id:
            try:
                return self._client.interactions.get(interaction_id)
            except Exception as exc:  # noqa: BLE001 - rebuild from the stream instead
                logger.warning("[GEMINI] interactions.get(%s) after stream failed (%s); "
                               "using the streamed steps", interaction_id, exc)
        if not completed:
            raise RuntimeError("Gemini stream ended without an interaction.completed event")
        return {**completed, "steps": [steps[index] for index in sorted(steps)]}

    # ── generateContent (batch path) ───────────────────────────────────────

    def build_generate_content_request(
        self,
        *,
        contents: List[Dict[str, Any]],
        system_instruction: Optional[str] = None,
        cached_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Provider-neutral request the batch module renders to the inline SDK
        shape or a REST JSONL line. With ``cached_content`` the system
        instruction is dropped: it lives in the cache, and a request that sets
        both is rejected."""
        batch_cfg = self._cfg.get("batch", {}) or {}
        neutral: Dict[str, Any] = {
            "contents": contents,
            "thinking_level": self.thinking_level,
            "include_thoughts": self.thinking_summaries != "none",
            # Never the long-decode ceiling: that is background-Interactions
            # only. Argon's generateContent caps a non-streaming request lower.
            "max_output_tokens": min(
                self.max_output_tokens, GENERATE_CONTENT_OUTPUT_CEILING.get(self.model, MAX_OUTPUT_TOKENS)
            ),
        }
        if cached_content:
            neutral["cached_content"] = cached_content
        elif system_instruction:
            neutral["system_instruction"] = system_instruction
        # Interactions has no safety_settings; generateContent does. Applied
        # to the batch path only when configured -- an asymmetry worth
        # knowing when one chapter blocks on sync and passes on batch.
        safety = batch_cfg.get("safety_settings") or []
        if safety:
            neutral["safety_settings"] = [
                {"category": str(item.get("category")), "threshold": str(item.get("threshold"))}
                for item in safety if isinstance(item, dict)
            ]
        return neutral

    # ── telemetry ──────────────────────────────────────────────────────────

    def _record_cache_telemetry(self, response: LLMResponse, *, chained: bool) -> None:
        """Session cache economics for the implicit cache.

        On a chained call the cacheable prefix is everything the server held
        before this turn, which is the previous chained call's input. Prefix
        recovery is cached / that expectation, the direct measure of whether
        previous_interaction_id is actually buying cache hits.
        """
        telemetry = self._cache_telemetry
        usage = response.usage
        cached = int(usage.cache_read_tokens)
        total_input = int(usage.input_tokens)
        telemetry["calls"] = int(telemetry["calls"]) + 1
        telemetry["input_tokens"] = int(telemetry["input_tokens"]) + total_input
        telemetry["output_tokens"] = int(telemetry["output_tokens"]) + int(usage.output_tokens)
        telemetry["thought_tokens"] = int(telemetry["thought_tokens"]) + int(usage.reasoning_tokens)
        telemetry["cache_read_tokens"] = int(telemetry["cache_read_tokens"]) + cached

        # A prefix under the implicit floor cannot hit; scoring it would book a
        # miss the cache was never able to avoid. A gap past the cache TTL
        # (the Enterprise guide gives 15-20 minutes, e.g. after a long
        # decode) can still miss legitimately -- that one is scored.
        floor = self.implicit_cache_min_tokens
        cacheable = chained and self._previous_chain_input >= floor
        if chained and 0 < self._previous_chain_input < floor:
            telemetry["below_floor_calls"] = int(telemetry.get("below_floor_calls", 0)) + 1
        if cacheable:
            expected = self._previous_chain_input
            recovered = min(cached, expected)
            telemetry["breakpoint_opportunities"] = int(telemetry["breakpoint_opportunities"]) + 1
            telemetry["expected_prefix_tokens"] = int(telemetry["expected_prefix_tokens"]) + expected
            telemetry["recovered_prefix_tokens"] = int(telemetry["recovered_prefix_tokens"]) + recovered
            threshold = float(((self._cfg.get("caching", {}) or {}).get("cache_monitor", {}) or {}).get(
                "warn_threshold_prefix_recovery", 0.80) or 0.80)
            if recovered / float(expected) >= threshold:
                telemetry["breakpoint_successes"] = int(telemetry["breakpoint_successes"]) + 1
        # The next call's expected prefix is this call's real context, not a
        # multi-hop long decode's summed input (0 skips scoring the next call).
        self._previous_chain_input = int(response.provider_metadata.get("context_input_tokens", total_input))

        opportunities = int(telemetry["breakpoint_opportunities"])
        expected_total = int(telemetry["expected_prefix_tokens"])
        telemetry["breakpoint_success_rate"] = round(
            int(telemetry["breakpoint_successes"]) / float(opportunities) if opportunities else 0.0, 4)
        telemetry["prefix_recovery"] = round(
            int(telemetry["recovered_prefix_tokens"]) / float(expected_total) if expected_total else 0.0, 4)
        coverage = round(int(telemetry["cache_read_tokens"]) / float(max(1, int(telemetry["input_tokens"]))), 4)
        telemetry["total_cache_coverage"] = coverage
        telemetry["cache_hit_ratio"] = coverage

        baseline = cost_breakdown_usd(
            model_name=response.model or self.model,
            input_tokens=total_input,
            batch=bool(response.batch_pricing),
        )
        economics = telemetry["cache_economics"]
        economics["baseline_input_cost_usd"] = round(
            float(economics["baseline_input_cost_usd"]) + float(baseline["input_cost_usd"]), 8)
        economics["actual_input_cost_usd"] = round(
            float(economics["actual_input_cost_usd"]) + float(response.input_cost_usd) + float(response.cache_read_cost_usd), 8)
        economics["net_input_savings_usd"] = round(
            float(economics["baseline_input_cost_usd"]) - float(economics["actual_input_cost_usd"]), 8)

        monitor = ((self._cfg.get("caching", {}) or {}).get("cache_monitor", {}) or {})
        if bool(monitor.get("enabled", True)) and telemetry["calls"] >= 2 and cacheable:
            threshold = float(monitor.get("warn_threshold_cache_hit_ratio", 0.60) or 0.60)
            if coverage < threshold:
                logger.warning(
                    "[GEMINI-CACHE] implicit cache coverage %.2f below %.2f (cached=%d of %d input)",
                    coverage, threshold, telemetry["cache_read_tokens"], telemetry["input_tokens"],
                )

    def reset_chain_telemetry(self) -> None:
        """A rebase starts a new chain; its first call has no cached prefix to
        recover, and must not be scored against the old chain's size."""
        self._previous_chain_input = 0


def _apply_step_delta(steps: Dict[int, Dict[str, Any]], index: int, delta: Dict[str, Any]) -> None:
    """Fold one SSE ``step.delta`` into the step it belongs to: text deltas
    append to a model_output's content, thought_summary deltas to a thought's
    summary, and the signature delta (the last one before ``step.stop``)
    lands on the thought step."""
    step = steps.setdefault(index, {})
    kind = str(delta.get("type") or "")
    if kind == "text":
        step.setdefault("type", "model_output")
        content = step.setdefault("content", [])
        if content and content[-1].get("type") == "text":
            content[-1]["text"] = str(content[-1].get("text") or "") + str(delta.get("text") or "")
        else:
            content.append({"type": "text", "text": str(delta.get("text") or "")})
    elif kind == "thought_summary":
        step.setdefault("type", "thought")
        part = as_dict(delta.get("content"))
        summary = step.setdefault("summary", [])
        if part.get("type") == "text" and summary and summary[-1].get("type") == "text":
            summary[-1]["text"] = str(summary[-1].get("text") or "") + str(part.get("text") or "")
        elif part:
            summary.append(part)
    elif kind == "thought_signature":
        step.setdefault("type", "thought")
        step["signature"] = delta.get("signature")
