"""Lossless normalization of Gemini Interactions and generateContent payloads.

Two wire shapes land here and leave as the pipeline's one ``LLMResponse``:

* An ``Interaction`` (sync path): a ``status`` plus a chronological ``steps``
  array -- ``thought`` steps (signature + optional summary) and
  ``model_output`` steps (content blocks). Visible text is built from
  ``model_output`` steps only, never from the convenience ``output_text``.
* A ``GenerateContentResponse`` (Batch API): ``candidates[0].content.parts``
  with ``thought: true`` on reasoning parts, a ``finish_reason``, and
  ``usage_metadata``. Batch results decoded from a downloaded JSONL file
  arrive in REST camelCase, so every lookup accepts both spellings.

Token accounting -- two ways in which Gemini is NOT the Messages API:

1. Thought tokens are reported apart from output tokens (``total_thought_tokens``
   / ``thoughts_token_count``) but billed at the output rate. They are folded
   into ``output_tokens`` here, or every chapter undercounts by its whole
   reasoning spend. ``usage.reasoning_tokens`` keeps the split visible.
2. Cached tokens are a SUBSET of input tokens (``total_cached_tokens`` within
   ``total_input_tokens``), so costs use ``cache_read_included_in_input=True``
   -- the opposite of the Anthropic route.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from src.Deepseek.common.llm_types import (
    LLMApiFamily,
    LLMContentBlock,
    LLMResponse,
    LLMTermination,
    LLMUsage,
)
from src.Deepseek.common.token_telemetry import cost_breakdown_usd

logger = logging.getLogger(__name__)

# Interaction.status -> MTLS control flow. "incomplete" is documented as
# "completed, but contains incomplete results (e.g. hitting max_tokens)".
_INTERACTION_STATUS = {
    "completed": LLMTermination.COMPLETED,
    "incomplete": LLMTermination.MAX_OUTPUT,
    "requires_action": LLMTermination.TOOL_USE,
    "failed": LLMTermination.ERROR,
    "cancelled": LLMTermination.ERROR,
    # Present in the SDK's status literal though not in the API reference's
    # prose; a spend/agent budget stop is not a truncation we can continue.
    "budget_exceeded": LLMTermination.ERROR,
    "in_progress": LLMTermination.UNKNOWN,
    "queued": LLMTermination.UNKNOWN,
}

# generateContent finish reasons that withheld output on content grounds.
# RECITATION matters more than it looks for this pipeline: a light novel
# with a licensed English edition can trip it on a faithful translation.
_SAFETY_FINISH_REASONS = {
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
    "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION",
}
_SAFETY_TEXT_MARKERS = ("safety", "recitation", "prohibited", "blocklist", "spii", "blocked")

# Service tiers are priced as multiples of Standard. Flex is documented at the
# Batch rate; Priority's published rows are 1.8x Standard (0.75 -> 1.35).
_TIER_MULTIPLIER = {"standard": 1.0, "flex": 0.5, "priority": 1.8}


def interaction_to_llm_response(
    interaction: Any,
    *,
    model: str,
    streamed: bool,
    service_tier: str = "standard",
) -> LLMResponse:
    raw = as_dict(interaction)
    status = str(raw.get("status") or "").lower()
    steps = [as_dict(step) for step in (raw.get("steps") or [])]

    blocks: List[LLMContentBlock] = []
    text_parts: List[str] = []
    summaries: List[str] = []
    step_errors: List[Dict[str, Any]] = []
    thought_total = thought_with_text = 0
    for step in steps:
        step_type = str(step.get("type") or "")
        if step_type == "thought":
            thought_total += 1
            summary_text = "\n".join(
                str(item.get("text") or "")
                for item in (as_dict(part) for part in (step.get("summary") or []))
                if item.get("type") == "text" and item.get("text")
            )
            if summary_text:
                thought_with_text += 1
                summaries.append(summary_text)
            blocks.append(LLMContentBlock(type="reasoning", text=summary_text, payload=step))
        elif step_type == "model_output":
            if step.get("error"):
                step_errors.append(as_dict(step.get("error")))
            for item in (as_dict(part) for part in (step.get("content") or [])):
                if item.get("type") == "text":
                    text_parts.append(str(item.get("text") or ""))
                    blocks.append(LLMContentBlock(type="text", text=str(item.get("text") or ""), payload=item))
                else:
                    blocks.append(LLMContentBlock(type="provider_block", payload=item))
        elif step_type == "user_input":
            continue  # only present on interactions.get; it is our own envelope
        else:
            blocks.append(LLMContentBlock(type="provider_block", payload=step))

    visible_text = "".join(text_parts)
    errors = [as_dict(error) for error in (raw.get("errors") or [])] + step_errors
    termination = _INTERACTION_STATUS.get(status, LLMTermination.UNKNOWN)
    if _looks_like_safety_block(errors, visible_text, termination):
        # The Interactions API exposes no safety_settings and documents no
        # block status. Observed shapes are a failed status, or an error on
        # the model_output step, whose message names the filter.
        termination = LLMTermination.SAFETY_BLOCKED
        logger.warning("[GEMINI-SAFETY] output withheld: %s", _error_summary(errors) or status)

    usage_raw = as_dict(raw.get("usage") or {})
    input_tokens = _as_int(usage_raw.get("total_input_tokens"))
    raw_cached = _as_int(usage_raw.get("total_cached_tokens"))
    thought_tokens = _as_int(usage_raw.get("total_thought_tokens"))
    output_only = _as_int(usage_raw.get("total_output_tokens"))
    _check_accounting(
        input_tokens, raw_cached, output_only, thought_tokens,
        # Tool-use tokens are counted in total_tokens but belong to none of the
        # three classes checked, so they come off the total, not onto it.
        max(0, _as_int(usage_raw.get("total_tokens")) - _as_int(usage_raw.get("total_tool_use_tokens"))),
    )
    cached_tokens = min(input_tokens, raw_cached) if input_tokens else raw_cached
    billed_output = output_only + thought_tokens

    return _build_response(
        model=str(raw.get("model") or model),
        api_family=LLMApiFamily.GEMINI_INTERACTIONS,
        response_id=str(raw.get("id") or ""),
        raw_finish_reason=status,
        termination=termination,
        visible_text=visible_text,
        blocks=blocks,
        summaries=summaries,
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=billed_output,
        thought_tokens=thought_tokens,
        batch=False,
        service_tier=service_tier,
        metadata={
            "interaction_id": str(raw.get("id") or ""),
            "previous_interaction_id": raw.get("previous_interaction_id"),
            "status": status,
            "streamed": streamed,
            "errors": errors,
            "service_tier": service_tier,
            "thinking_blocks": {"total": thought_total, "with_text": thought_with_text},
            "usage_raw": usage_raw,
            # Long Decode: set on an `incomplete` interaction whose decoding
            # can be resumed by passing it back unchanged on the next create.
            "continuation_token": str(raw.get("continuation_token") or ""),
        },
    )


def generate_content_to_llm_response(response: Any, *, model: str, batch: bool = True) -> LLMResponse:
    raw = as_dict(response)
    candidates = _get(raw, "candidates") or []
    candidate = as_dict(candidates[0]) if candidates else {}
    content = as_dict(_get(candidate, "content") or {})
    parts = [as_dict(part) for part in (_get(content, "parts") or [])]

    blocks: List[LLMContentBlock] = []
    text_parts: List[str] = []
    summaries: List[str] = []
    thought_total = thought_with_text = 0
    for part in parts:
        text = str(part.get("text") or "")
        if part.get("thought"):
            thought_total += 1
            if text:
                thought_with_text += 1
                summaries.append(text)
            blocks.append(LLMContentBlock(type="reasoning", text=text, payload=part))
        elif "text" in part:
            text_parts.append(text)
            blocks.append(LLMContentBlock(type="text", text=text, payload=part))
        else:
            blocks.append(LLMContentBlock(type="provider_block", payload=part))

    finish = str(_get(candidate, "finish_reason", "finishReason") or "").upper()
    prompt_feedback = as_dict(_get(raw, "prompt_feedback", "promptFeedback") or {})
    block_reason = str(_get(prompt_feedback, "block_reason", "blockReason") or "").upper()
    if block_reason and block_reason != "BLOCKED_REASON_UNSPECIFIED":
        termination = LLMTermination.SAFETY_BLOCKED
        finish = finish or f"PROMPT_BLOCKED:{block_reason}"
    elif finish in _SAFETY_FINISH_REASONS:
        termination = LLMTermination.SAFETY_BLOCKED
    elif finish == "MAX_TOKENS":
        termination = LLMTermination.MAX_OUTPUT
    elif finish == "STOP":
        termination = LLMTermination.COMPLETED
    elif not candidates:
        termination = LLMTermination.ERROR
    else:
        termination = LLMTermination.UNKNOWN if finish else LLMTermination.COMPLETED
    if termination == LLMTermination.SAFETY_BLOCKED:
        logger.warning("[GEMINI-SAFETY] batch output withheld: finish=%s block=%s", finish, block_reason or "-")

    usage_raw = as_dict(_get(raw, "usage_metadata", "usageMetadata") or {})
    input_tokens = _as_int(_get(usage_raw, "prompt_token_count", "promptTokenCount"))
    cached_tokens = _as_int(_get(usage_raw, "cached_content_token_count", "cachedContentTokenCount"))
    thought_tokens = _as_int(_get(usage_raw, "thoughts_token_count", "thoughtsTokenCount"))
    billed_output = _as_int(_get(usage_raw, "candidates_token_count", "candidatesTokenCount")) + thought_tokens

    return _build_response(
        model=str(_get(raw, "model_version", "modelVersion") or model),
        api_family=LLMApiFamily.GEMINI_GENERATE_CONTENT,
        response_id=str(_get(raw, "response_id", "responseId") or ""),
        raw_finish_reason=finish,
        termination=termination,
        visible_text="".join(text_parts),
        blocks=blocks,
        summaries=summaries,
        input_tokens=input_tokens,
        cached_tokens=min(input_tokens, cached_tokens) if input_tokens else cached_tokens,
        output_tokens=billed_output,
        thought_tokens=thought_tokens,
        batch=batch,
        service_tier="batch" if batch else "standard",
        metadata={
            "finish_reason": finish,
            "block_reason": block_reason,
            "thinking_blocks": {"total": thought_total, "with_text": thought_with_text},
            "usage_raw": usage_raw,
        },
    )


def _build_response(
    *,
    model: str,
    api_family: LLMApiFamily,
    response_id: str,
    raw_finish_reason: str,
    termination: LLMTermination,
    visible_text: str,
    blocks: List[LLMContentBlock],
    summaries: List[str],
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    thought_tokens: int,
    batch: bool,
    service_tier: str,
    metadata: Dict[str, Any],
) -> LLMResponse:
    usage = LLMUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cached_tokens,
        cache_write_tokens=0,
        reasoning_tokens=thought_tokens,
    )
    tier = str(service_tier or "standard").lower()
    costs = cost_breakdown_usd(
        model_name=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cached_tokens,
        cache_read_included_in_input=True,
        batch=batch or tier == "flex",
    )
    multiplier = _TIER_MULTIPLIER.get(tier, 1.0) if tier == "priority" else 1.0
    cost_fields = {
        key: round(float(costs[key]) * multiplier, 8)
        for key in ("input_cost_usd", "output_cost_usd", "cache_read_cost_usd", "total_cost_usd")
    }
    return LLMResponse(
        content=visible_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cache_creation_tokens=0,
        finish_reason=raw_finish_reason,
        raw_finish_reason=raw_finish_reason,
        termination=termination,
        model=model,
        provider="gemini",
        api_family=api_family,
        response_id=response_id,
        content_blocks=blocks,
        usage=usage,
        thinking_content="\n\n".join(summaries) or None,
        input_cost_usd=cost_fields["input_cost_usd"],
        output_cost_usd=cost_fields["output_cost_usd"],
        cache_read_cost_usd=cost_fields["cache_read_cost_usd"],
        cache_creation_cost_usd=0.0,
        total_cost_usd=cost_fields["total_cost_usd"],
        batch_pricing=bool(batch or tier == "flex"),
        provider_metadata=metadata,
    )


_ACCOUNTING_WARNED = {"done": False}


def _check_accounting(input_tokens: int, cached: int, output: int, thought: int, total_plus_tools: int) -> None:
    """Say so, once per process, if the usage block breaks the two identities
    this module's billing rests on: cached <= input (cache is a subset), and
    total == input + output + thought (thought is apart from output). The
    figures are then still clamped and billed, but a broken identity means the
    cost column is wrong and the assumption needs revisiting -- the kind of
    error that otherwise hides forever inside a plausible-looking number."""
    if _ACCOUNTING_WARNED["done"] or not input_tokens:
        return
    problems = []
    if cached > input_tokens:
        problems.append(f"cached {cached} > input {input_tokens}")
    expected = input_tokens + output + thought
    if total_plus_tools and total_plus_tools not in (expected, 0) and abs(total_plus_tools - expected) > 1:
        problems.append(f"total {total_plus_tools} != input+output+thought {expected}")
    if problems:
        _ACCOUNTING_WARNED["done"] = True
        logger.warning("[GEMINI-BILLING] usage identity violated (%s); cost figures may be off. "
                       "Check src/Gemini/response.py's accounting assumptions.", "; ".join(problems))


def _looks_like_safety_block(errors: List[Dict[str, Any]], visible_text: str, termination: LLMTermination) -> bool:
    if termination not in (LLMTermination.ERROR, LLMTermination.COMPLETED, LLMTermination.UNKNOWN):
        return False
    if termination == LLMTermination.COMPLETED and visible_text.strip():
        return False
    blob = " ".join(
        f"{error.get('code', '')} {error.get('status', '')} {error.get('message', '')}" for error in errors
    ).lower()
    return any(marker in blob for marker in _SAFETY_TEXT_MARKERS)


def _error_summary(errors: List[Dict[str, Any]]) -> str:
    return "; ".join(str(error.get("message") or error.get("code") or "") for error in errors if error)


def thinking_tally(responses: List[LLMResponse]) -> Tuple[int, int]:
    total = with_text = 0
    for response in responses:
        counts = (getattr(response, "provider_metadata", None) or {}).get("thinking_blocks") or {}
        total += int(counts.get("total", 0) or 0)
        with_text += int(counts.get("with_text", 0) or 0)
    return total, with_text


def as_dict(value: Any) -> Dict[str, Any]:
    """Turn SDK data models into JSON-compatible dictionaries, dropping unset
    optionals so a missing field and an absent one read the same."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(mode="json", exclude_none=True)
        except TypeError:
            dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, dict) else {"value": dumped}
    if hasattr(value, "to_json_dict"):
        dumped = value.to_json_dict()
        return dict(dumped) if isinstance(dumped, dict) else {"value": dumped}
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {"value": value}


def _get(mapping: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    if len(names) == 1:
        camel = _camel(names[0])
        return mapping.get(camel)
    return None


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
