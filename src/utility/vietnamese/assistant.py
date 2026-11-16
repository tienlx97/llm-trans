"""One-shot JSON calls for bible analysis, semantic QC and repair.

Native APIs only, no compatibility layer: OpenAI Responses
(``responses.create``) and Gemini ``models.generate_content`` -- or the
signed-in Codex CLI / Claude Code (``codex`` / ``claude``, see src/CLI/). Only
parameters every model of those families accepts are sent (model,
instructions/system instruction, input, max output tokens); no reasoning or
sampling knobs whose support varies by model. The SDK's own bounded retry
handles rate limits (``assistant.max_retries``).

Every call is logged to the volume's token ledger. ``dry_run`` writes the
request to work/<vol>/DRY_RUN/assistant/ instead of sending it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

logger = logging.getLogger(__name__)


class AssistantError(RuntimeError):
    pass


class JsonAssistant(Protocol):
    def complete_json(self, *, system: str, user: str, label: str) -> Dict[str, Any]: ...


def parse_json_object(text: str) -> Dict[str, Any]:
    """The first complete JSON object in ``text`` (tolerates code fences)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start < 0:
        raise AssistantError("Model returned no JSON object")
    depth, in_str, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start : i + 1])
                except ValueError as exc:
                    raise AssistantError(f"Model returned malformed JSON: {exc}") from exc
                if not isinstance(value, dict):
                    raise AssistantError("Model JSON is not an object")
                return value
    raise AssistantError("Model JSON object is incomplete (output truncated?)")


@dataclass
class ProviderAssistant:
    provider: str
    model: str
    volume_id: str
    work_dir: Path
    max_output_tokens: int = 16000
    max_retries: int = 3
    timeout: float = 600.0
    dry_run: bool = False

    def _dry_run(self, label: str, system: str, user: str) -> Dict[str, Any]:
        out = Path(self.work_dir) / "DRY_RUN" / "assistant"
        out.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.-]+", "_", label)
        path = out / f"{safe}.json"
        path.write_text(json.dumps({"provider": self.provider, "model": self.model, "system": system, "user": user},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        return {"dry_run": True, "payload_path": str(path)}

    def _log(self, label: str, input_tokens: int, cached: int, output_tokens: int) -> None:
        try:
            from src.Deepseek.common.token_telemetry import log_call

            log_call(phase="vi_assistant", volume_id=self.volume_id, call_label=label, model=self.model,
                     provider=self.provider, cache_hit_tokens=cached, fresh_tokens=max(0, input_tokens - cached),
                     output_tokens=output_tokens)
        except Exception as exc:  # noqa: BLE001 - telemetry must never fail the call
            logger.warning("[VI-ASSISTANT] token log failed for %s: %s", label, exc)

    def complete_json(self, *, system: str, user: str, label: str) -> Dict[str, Any]:
        if self.dry_run:
            return self._dry_run(label, system, user)
        if self.provider == "openai":
            text, usage = self._openai(system, user)
        elif self.provider == "gemini":
            text, usage = self._gemini(system, user)
        elif self.provider in ("codex", "claude"):
            text, usage = self._cli(system, user, label)
        else:
            raise AssistantError(f"Unsupported assistant provider {self.provider!r} (openai | gemini | codex | claude)")
        self._log(label, *usage)
        return parse_json_object(text)

    def _openai(self, system: str, user: str):
        from openai import OpenAI

        from src.Deepseek.common.config import get_config_section

        cfg = (get_config_section("translation").get("openai") or {})
        key = os.environ.get(str(cfg.get("api_key_env") or "OPENAI_API_KEY"), "")
        if not key:
            raise AssistantError(f"{cfg.get('api_key_env') or 'OPENAI_API_KEY'} is not set")
        client = OpenAI(api_key=key, base_url=cfg.get("endpoint") or None, max_retries=self.max_retries, timeout=self.timeout)
        response = client.responses.create(model=self.model, instructions=system, input=user,
                                           max_output_tokens=self.max_output_tokens)
        if getattr(response, "status", "") == "incomplete":
            raise AssistantError("OpenAI response incomplete (output limit reached); raise assistant.max_output_tokens")
        usage = getattr(response, "usage", None)
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
        return (response.output_text or "", (int(getattr(usage, "input_tokens", 0) or 0), int(cached),
                                              int(getattr(usage, "output_tokens", 0) or 0)))

    def _cli(self, system: str, user: str, label: str):
        from src.CLI.runner import CliError, CliRunner

        runner = CliRunner.from_config(self.provider, model=self.model or None)
        try:
            result = runner.complete(system, user + "\n\nReturn only the JSON object.", label=label)
        except CliError as exc:
            raise AssistantError(str(exc)) from exc
        if result.truncated:
            raise AssistantError(f"{runner.label} stopped at its output limit")
        return result.text, (result.input_tokens, result.cached_tokens, result.output_tokens)

    def _gemini(self, system: str, user: str):
        from google import genai
        from google.genai import types

        from src.Deepseek.common.config import get_config_section

        cfg = (get_config_section("translation").get("gemini") or {})
        key = os.environ.get(str(cfg.get("api_key_env") or "GEMINI_API_KEY")) or os.environ.get("GOOGLE_API_KEY", "")
        if not key:
            raise AssistantError(f"{cfg.get('api_key_env') or 'GEMINI_API_KEY'} is not set")
        client = genai.Client(api_key=key, http_options=types.HttpOptions(
            timeout=int(self.timeout * 1000),
            retry_options=types.HttpRetryOptions(attempts=max(1, self.max_retries)),
        ))
        response = client.models.generate_content(
            model=self.model, contents=user,
            config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                               max_output_tokens=self.max_output_tokens),
        )
        meta = getattr(response, "usage_metadata", None)
        finish = str(getattr((response.candidates or [None])[0], "finish_reason", "") or "")
        if "MAX_TOKENS" in finish:
            raise AssistantError("Gemini response hit the output limit; raise assistant.max_output_tokens")
        return (response.text or "", (int(getattr(meta, "prompt_token_count", 0) or 0),
                                       int(getattr(meta, "cached_content_token_count", 0) or 0),
                                       int(getattr(meta, "candidates_token_count", 0) or 0)
                                       + int(getattr(meta, "thoughts_token_count", 0) or 0)))


def make_assistant(work_dir: Path, *, dry_run: bool = False, provider: Optional[str] = None,
                   model: Optional[str] = None) -> ProviderAssistant:
    from src.Deepseek.common.config import get_config_section

    cfg = get_config_section("assistant") or {}
    chosen = str(provider or cfg.get("provider") or "openai").strip().lower()
    chosen_model = model or cfg.get("model") or (get_config_section("translation").get(chosen) or {}).get("model")
    if chosen in ("codex", "claude"):
        from src.CLI.runner import cli_settings

        # A CLI may run its own default model; "" means "let the CLI pick".
        chosen_model = model or cfg.get("model") or cli_settings(chosen).get("model") or ""
    elif not chosen_model:
        raise AssistantError(f"No model configured for assistant provider {chosen!r}")
    return ProviderAssistant(
        provider=chosen, model=str(chosen_model), volume_id=Path(work_dir).name, work_dir=Path(work_dir),
        max_output_tokens=int(cfg.get("max_output_tokens", 16000) or 16000),
        max_retries=int(cfg.get("max_retries", 3) or 3), timeout=float(cfg.get("timeout_seconds", 600) or 600),
        dry_run=dry_run,
    )

