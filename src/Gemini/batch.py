"""Gemini Batch API -- submit/poll/retrieve for volume-level batch translation.

The Interactions API has no batch mode, so this path runs on the
``generateContent`` Batch API (``client.batches``): half-price, a 24-hour
turnaround target, and ``JOB_STATE_EXPIRED`` after 48 hours pending/running.
Results are retained for six weeks.

Requests are keyed by chapter id. Inline submissions carry it as
``metadata={"key": chapter_id}`` (``InlinedResponse.metadata`` echoes it back);
a submission too large for an inline request goes through an uploaded JSONL
file, whose ``key`` field round-trips by contract. Results are never matched by
position unless no key survived at all.

Opt-in via ``translation.gemini.batch.enabled``; the synchronous Interactions
path stays the default.
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.Deepseek.common.atomic_io import atomic_write_json
from src.Deepseek.common.llm_types import LLMResponse
from src.Gemini.response import as_dict, generate_content_to_llm_response

logger = logging.getLogger(__name__)

TERMINAL_STATES = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
# Inline batch requests share a 20MB request ceiling; leave headroom for
# the SDK's own envelope.
DEFAULT_INLINE_LIMIT_BYTES = 16 * 1024 * 1024


@dataclass
class BatchOutcome:
    """One job's results, partitioned by what has to happen next. A request
    we built wrong (HTTP 400) is permanent: resubmitting it unchanged pays
    again for the identical failure. Everything else is retryable."""

    succeeded: Dict[str, LLMResponse] = field(default_factory=dict)
    retryable: Dict[str, str] = field(default_factory=dict)
    permanent: Dict[str, str] = field(default_factory=dict)

    @property
    def unfinished(self) -> Dict[str, str]:
        return {**self.retryable, **self.permanent}


class BatchLedger:
    """Durable record of submitted jobs. A job is billed when it runs and its
    results stay retrievable for six weeks, so a job id held only in memory
    is paid work nobody can collect after a crash. Written the instant the
    job name comes back, before polling starts."""

    SCHEMA_VERSION = "1.0"

    def __init__(self, path: Path, *, volume_id: str, model: str):
        self.path = Path(path)
        self.volume_id = str(volume_id)
        self.model = str(model)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state: Dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "volume_id": self.volume_id,
            "model": self.model,
            "batches": [],
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("[GEMINI-BATCH] unreadable batch state at %s; starting a fresh ledger", self.path)
            return
        if (
            isinstance(loaded, dict)
            and str(loaded.get("volume_id")) == self.volume_id
            and str(loaded.get("model")) == self.model
        ):
            self.state = loaded
            self.state.setdefault("batches", [])

    def _save(self) -> None:
        atomic_write_json(self.path, self.state)

    @property
    def batches(self) -> List[Dict[str, Any]]:
        return self.state.setdefault("batches", [])

    def _find(self, job_name: str) -> Optional[Dict[str, Any]]:
        return next((entry for entry in self.batches if entry.get("job_name") == job_name), None)

    def record_submitted(
        self, job_name: str, *, wave: int, chapter_ids: List[str], src_kind: str, cache_name: str = ""
    ) -> None:
        self.batches.append(
            {
                "job_name": job_name,
                "wave": int(wave),
                # Submission order, kept as the last-resort key when a result
                # comes back with no metadata at all.
                "chapter_ids": list(chapter_ids),
                "src_kind": src_kind,
                "cache_name": cache_name,
                "submitted_at": _now(),
                "status": "submitted",
                "resolved_at": None,
                "note": "",
            }
        )
        self._save()
        logger.info("[GEMINI-BATCH] recorded %s (wave %d, %d chapters, %s) in %s",
                    job_name, wave, len(chapter_ids), src_kind, self.path)

    def record_ended(self, job_name: str, state: str) -> None:
        self._resolve(job_name, "ended", state)

    def record_abandoned(self, job_name: str, note: str) -> None:
        self._resolve(job_name, "abandoned", note)

    def _resolve(self, job_name: str, status: str, note: str) -> None:
        entry = self._find(job_name)
        if entry is None:
            return
        entry["status"] = status
        entry["resolved_at"] = _now()
        entry["note"] = note
        self._save()

    def open_batches(self) -> List[Dict[str, Any]]:
        return sorted(
            (entry for entry in self.batches if entry.get("status") == "submitted"),
            key=lambda entry: (int(entry.get("wave") or 0), str(entry.get("submitted_at") or "")),
        )


def build_inline_request(neutral: Dict[str, Any], key: str) -> Dict[str, Any]:
    """SDK-shaped inline request (``types.InlinedRequest`` fields)."""
    config: Dict[str, Any] = {}
    if neutral.get("cached_content"):
        config["cached_content"] = neutral["cached_content"]
    elif neutral.get("system_instruction"):
        config["system_instruction"] = neutral["system_instruction"]
    if neutral.get("max_output_tokens"):
        config["max_output_tokens"] = int(neutral["max_output_tokens"])
    thinking: Dict[str, Any] = {"include_thoughts": bool(neutral.get("include_thoughts", True))}
    if neutral.get("thinking_level"):
        thinking["thinking_level"] = neutral["thinking_level"]
    config["thinking_config"] = thinking
    if neutral.get("safety_settings"):
        config["safety_settings"] = neutral["safety_settings"]
    return {"contents": neutral["contents"], "config": config, "metadata": {"key": key}}


def build_jsonl_line(neutral: Dict[str, Any], key: str) -> Dict[str, Any]:
    """REST-shaped (camelCase) request for the file-based batch form."""
    request: Dict[str, Any] = {"contents": neutral["contents"]}
    if neutral.get("cached_content"):
        request["cachedContent"] = neutral["cached_content"]
    elif neutral.get("system_instruction"):
        request["systemInstruction"] = {"parts": [{"text": neutral["system_instruction"]}]}
    generation: Dict[str, Any] = {
        "thinkingConfig": {"includeThoughts": bool(neutral.get("include_thoughts", True))}
    }
    if neutral.get("thinking_level"):
        generation["thinkingConfig"]["thinkingLevel"] = str(neutral["thinking_level"]).upper()
    if neutral.get("max_output_tokens"):
        generation["maxOutputTokens"] = int(neutral["max_output_tokens"])
    request["generationConfig"] = generation
    if neutral.get("safety_settings"):
        request["safetySettings"] = [
            {"category": item["category"], "threshold": item["threshold"]} for item in neutral["safety_settings"]
        ]
    return {"key": key, "request": request}


def submit_batch(
    client,
    requests: List[Dict[str, Any]],
    *,
    model: str,
    display_name: str,
    inline_limit_bytes: int = DEFAULT_INLINE_LIMIT_BYTES,
) -> Dict[str, str]:
    """Submit one job. ``requests`` are ``{"key": chapter_id, "neutral": {...}}``.

    Returns ``{"job_name": ..., "src_kind": "inline"|"file"}``.
    """
    inline = [build_inline_request(item["neutral"], item["key"]) for item in requests]
    size = len(json.dumps(inline, ensure_ascii=False).encode("utf-8"))
    sdk = client._client
    if size <= inline_limit_bytes:
        job = sdk.batches.create(model=model, src=inline, config={"display_name": display_name})
        return {"job_name": str(getattr(job, "name", "") or ""), "src_kind": "inline"}

    logger.info("[GEMINI-BATCH] payload %.1f MB exceeds the inline limit; submitting via JSONL file",
                size / 1_048_576)
    from google.genai import types

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{display_name}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for item in requests:
                handle.write(json.dumps(build_jsonl_line(item["neutral"], item["key"]), ensure_ascii=False) + "\n")
        uploaded = sdk.files.upload(
            file=str(path), config=types.UploadFileConfig(display_name=display_name, mime_type="jsonl")
        )
    job = sdk.batches.create(model=model, src=str(uploaded.name), config={"display_name": display_name})
    return {"job_name": str(getattr(job, "name", "") or ""), "src_kind": "file"}


def poll_batch(client, job_name: str) -> str:
    """One non-blocking status check; returns the job state name."""
    job = client._client.batches.get(name=job_name)
    state = getattr(job, "state", None)
    return str(getattr(state, "name", state) or "")


def retrieve_batch_results(client, job_name: str, *, model: str, chapter_ids: List[str]) -> BatchOutcome:
    """Decode every result into the same ``LLMResponse`` the sync path makes,
    keyed by chapter id and sorted into succeeded / retryable / permanent."""
    outcome = BatchOutcome()
    sdk = client._client
    job = sdk.batches.get(name=job_name)
    state = str(getattr(getattr(job, "state", None), "name", "") or "")
    if state != "JOB_STATE_SUCCEEDED":
        reason = f"job {state or 'unknown state'}"
        for chapter_id in chapter_ids:
            outcome.retryable[chapter_id] = reason
        logger.warning("[GEMINI-BATCH] %s ended %s; %d chapter(s) marked retryable", job_name, state, len(chapter_ids))
        return outcome

    dest = getattr(job, "dest", None)
    entries: List[Dict[str, Any]] = []
    inlined = getattr(dest, "inlined_responses", None) if dest is not None else None
    if inlined:
        for index, item in enumerate(inlined):
            payload = as_dict(item)
            key = str((payload.get("metadata") or {}).get("key") or "")
            entries.append({"index": index, "key": key, "response": getattr(item, "response", None),
                            "error": payload.get("error")})
    elif dest is not None and getattr(dest, "file_name", None):
        content = sdk.files.download(file=dest.file_name)
        text = content.decode("utf-8") if isinstance(content, (bytes, bytearray)) else str(content)
        for index, line in enumerate(line for line in text.splitlines() if line.strip()):
            record = json.loads(line)
            entries.append({"index": index, "key": str(record.get("key") or ""),
                            "response": record.get("response"), "error": record.get("error") or record.get("status")})

    positional = all(not entry["key"] for entry in entries) and len(entries) == len(chapter_ids)
    if positional and entries:
        logger.warning("[GEMINI-BATCH] %s: no result carried its key; matching by submission order", job_name)
    for entry in entries:
        chapter_id = entry["key"] or (chapter_ids[entry["index"]] if positional else "")
        if not chapter_id:
            logger.warning("[GEMINI-BATCH] %s: result #%d has no key; cannot attribute it", job_name, entry["index"])
            continue
        if entry["error"]:
            error = as_dict(entry["error"])
            code = error.get("code")
            detail = str(error.get("message") or error)[:300]
            reason = f"errored/{code}: {detail}"
            if str(code) in ("400", "3", "INVALID_ARGUMENT"):
                outcome.permanent[chapter_id] = reason
                logger.error("[GEMINI-BATCH] %s %s -- fix the request before resubmitting", chapter_id, reason)
            else:
                outcome.retryable[chapter_id] = reason
                logger.warning("[GEMINI-BATCH] %s %s -- safe to resubmit", chapter_id, reason)
            continue
        if entry["response"] is None:
            outcome.retryable[chapter_id] = "no response body"
            continue
        outcome.succeeded[chapter_id] = generate_content_to_llm_response(entry["response"], model=model, batch=True)

    for chapter_id in chapter_ids:
        if chapter_id not in outcome.succeeded and chapter_id not in outcome.unfinished:
            outcome.retryable[chapter_id] = "missing from results"
    logger.info("[GEMINI-BATCH] %s decoded: %d succeeded, %d retryable, %d permanent",
                job_name, len(outcome.succeeded), len(outcome.retryable), len(outcome.permanent))
    return outcome


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
