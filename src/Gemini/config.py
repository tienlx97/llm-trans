"""Gemini-provider configuration accessors."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from src.Deepseek.common.config import PIPELINE_ROOT, get_config_section


def get_gemini_config() -> Dict[str, Any]:
    """Return the isolated ``translation.gemini`` configuration block."""
    return get_config_section("translation").get("gemini", {}) or {}


def get_gemini_prompt_path() -> Path:
    cfg = get_gemini_config()
    relative = cfg.get("master_prompt", "src/Gemini/prompts/master_prompt_gemini_en.md")
    return PIPELINE_ROOT / str(relative)


def get_gemini_conversation_config() -> Dict[str, Any]:
    return get_gemini_config().get("conversation", {}) or {}


def get_gemini_optimization_config() -> Dict[str, Any]:
    return get_gemini_config().get("optimizations", {}) or {}


def get_gemini_continuation_config() -> Dict[str, Any]:
    return get_gemini_config().get("continuation", {}) or {}


def get_gemini_retry_config() -> Dict[str, Any]:
    return get_gemini_config().get("retry", {}) or {}


def get_gemini_batch_config() -> Dict[str, Any]:
    return get_gemini_config().get("batch", {}) or {}


def get_gemini_caching_config() -> Dict[str, Any]:
    """Explicit CachedContent for the batch path. The Interactions path has
    no explicit cache (docs: "not supported in the Interactions API") and
    relies on implicit caching through ``previous_interaction_id`` instead."""
    return get_gemini_config().get("caching", {}) or {}


def get_gemini_fidelity_config() -> Dict[str, Any]:
    """Post-translation completeness gate -- same contract as the Anthropic
    route's: deterministic, token-free, and the only layer that verifies a
    finished chapter against its source."""
    return get_gemini_config().get("fidelity_gate", {}) or {}


def get_gemini_long_context_config() -> Dict[str, Any]:
    """Long Context window budgeting. Honoured only on models listed in
    ``src.Gemini.client.LONG_CONTEXT_MODELS``; inert on every other model."""
    return get_gemini_config().get("long_context", {}) or {}


def get_gemini_long_decode_config() -> Dict[str, Any]:
    """Long Decode (background interactions past the 64K output ceiling).
    Honoured only on models listed in ``src.Gemini.client.LONG_DECODE_MODELS``."""
    return get_gemini_config().get("long_decode", {}) or {}


def get_gemini_telemetry_config() -> Dict[str, Any]:
    return get_gemini_config().get("telemetry", {}) or {}
