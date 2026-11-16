"""Explicit context caching for the Gemini batch path (long-context optimization).

Every request in a batch wave shares one prefix: the system instruction plus
the conversation ledger's replayed chapters. Put that prefix in ONE
``CachedContent`` and each request carries only its own chapter envelope plus
``cached_content=<name>``; the prefix is billed at the cache-read rate instead
of in full, once per request. This is the Gemini counterpart of the Anthropic
route's shared wave prefix, and like it, the prefix is at once the continuity
carrier and the cache key.

Explicit caching is a ``generateContent`` feature only -- the Interactions API
does not support it, and relies on implicit caching through
``previous_interaction_id`` instead -- so the sync path never comes here.
Gemini 4 Argon never comes here either: explicit caching is unsupported on
it at all, on either API, and every Argon call rides implicit caching
(prefixes of 6,144+ tokens; "durable caching enabled by default" per the
Gemini Enterprise Argon guide -- the Developer API's ZDR page instead
describes implicit caching as RAM-only with a 24-hour TTL).

Storage is billed per hour, and that decides everything. At 3.8 Flash's intro
rates a cached token costs $0.50/MTok/hour to keep, while each batch request
reading it saves (0.375 - 0.0375) = $0.3375/MTok. A four-chapter wave that
lands in an hour saves 1.35 against 0.50 of storage; the same wave left
running for a day loses money. So a cache is created only when the estimated
saving clears the storage cost by a margin, its TTL is kept short and
extended while the job is still running, and it is deleted the moment the
wave lands. Implicit caching still applies to batch traffic when no explicit
cache is made, at no storage cost.

A request that names ``cached_content`` must not also carry
``system_instruction`` (the instruction lives in the cache), so the batch
builder drops it whenever a cache name is returned here.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.Deepseek.common.atomic_io import atomic_write_json
from src.Deepseek.common.token_telemetry import _rates_for_model

logger = logging.getLogger(__name__)

# Below this many tokens the API refuses to create an EXPLICIT cache. 4,096
# for every model this module ever serves; Gemini 4 Argon never reaches it
# (explicit caching unsupported) and its separate implicit floor, 6,144, is
# IMPLICIT_CACHE_MIN_TOKENS in client.py.
DEFAULT_MIN_CACHE_TOKENS = 4096


class GeminiContextCache:
    def __init__(self, client, *, state_path: Path, volume_id: str, model: str, config: Dict[str, Any]):
        self.client = client
        self.state_path = Path(state_path)
        self.volume_id = str(volume_id)
        self.model = str(model)
        mode = str(config.get("mode", "auto") or "auto").strip().lower()
        self.mode = mode if mode in ("auto", "always", "never") else "auto"
        if not bool(config.get("enabled", True)):
            self.mode = "never"
        self.min_tokens = max(DEFAULT_MIN_CACHE_TOKENS, int(config.get("min_tokens", DEFAULT_MIN_CACHE_TOKENS) or 0))
        self.initial_ttl_seconds = max(300, int(config.get("ttl_seconds", 3600) or 3600))
        self.expected_wave_hours = max(0.1, float(config.get("expected_wave_hours", 1.0) or 1.0))
        # Required saving-to-storage ratio before a cache is worth creating.
        self.break_even_margin = max(1.0, float(config.get("break_even_margin", 1.5) or 1.5))
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state: Dict[str, Any] = {"volume_id": self.volume_id, "model": self.model, "caches": []}
        self._load()

    # ── persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict) and str(loaded.get("volume_id")) == self.volume_id:
            self.state = loaded
            self.state.setdefault("caches", [])

    def _save(self) -> None:
        atomic_write_json(self.state_path, self.state)

    def _entry(self, name: str) -> Optional[Dict[str, Any]]:
        return next((entry for entry in self.state["caches"] if entry.get("name") == name), None)

    # ── decision ───────────────────────────────────────────────────────────

    def should_cache(self, prefix_tokens: int, request_count: int) -> bool:
        if self.mode == "never" or prefix_tokens < self.min_tokens:
            return False
        if self.mode == "always":
            return True
        rates = _rates_for_model(self.model, input_tokens=prefix_tokens)
        # Batch requests bill the uncached prefix at half the input rate.
        uncached_rate = float(rates["cache_miss"]) * 0.5
        cached_rate = float(rates["cache_hit"]) * (1.0 if self.model.startswith("gemini-3.1-pro") else 0.5)
        storage_rate = float(rates.get("cache_storage_hr", 0.0) or 0.0)
        saving = request_count * prefix_tokens * max(0.0, uncached_rate - cached_rate) / 1_000_000
        storage = prefix_tokens * storage_rate * self.expected_wave_hours / 1_000_000
        worth_it = saving >= storage * self.break_even_margin
        logger.info(
            "[GEMINI-CACHE] prefix≈%d tokens × %d request(s): est. saving $%.4f vs storage $%.4f "
            "(%.1fh) -> %s",
            prefix_tokens, request_count, saving, storage, self.expected_wave_hours,
            "explicit cache" if worth_it else "implicit caching only",
        )
        return worth_it

    def count_tokens(self, system_instruction: str, contents: List[Dict[str, Any]]) -> int:
        """The server's own token count for the prefix, with a character
        estimate as the fallback (counting is free, but never worth failing a
        wave over)."""
        try:
            result = self.client.models.count_tokens(model=self.model, contents=contents or "")
            counted = int(getattr(result, "total_tokens", 0) or 0)
            return counted + max(1, len(system_instruction) // 3)
        except Exception as exc:  # noqa: BLE001 - estimate is good enough to gate on
            logger.debug("[GEMINI-CACHE] count_tokens failed (%s); estimating", exc)
            chars = len(system_instruction) + sum(
                len(str(part.get("text") or "")) for item in contents for part in item.get("parts", [])
            )
            return max(1, chars // 3)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def create(self, system_instruction: str, contents: List[Dict[str, Any]], *, wave: int, prefix_tokens: int) -> str:
        from google.genai import types

        fingerprint = hashlib.sha256(
            json.dumps([self.model, system_instruction, contents], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        cache = self.client.caches.create(
            model=self.model,
            config=types.CreateCachedContentConfig(
                display_name=f"mtls-{self.volume_id}-wave{wave}"[:128],
                system_instruction=system_instruction,
                contents=contents or None,
                ttl=f"{self.initial_ttl_seconds}s",
            ),
        )
        name = str(getattr(cache, "name", "") or "")
        self.state["caches"].append(
            {
                "name": name,
                "wave": int(wave),
                "fingerprint": fingerprint,
                "prefix_tokens": int(prefix_tokens),
                "created_at": _now(),
                "status": "live",
                "deleted_at": None,
            }
        )
        self._save()
        logger.info("[GEMINI-CACHE] created %s for wave %d (≈%d tokens, ttl %ds)",
                    name, wave, prefix_tokens, self.initial_ttl_seconds)
        return name

    def extend(self, name: str) -> None:
        """Push the expiry out by another TTL while a job is still running --
        a cache that expires under a pending request fails that request."""
        if not name:
            return
        from google.genai import types

        try:
            self.client.caches.update(
                name=name, config=types.UpdateCachedContentConfig(ttl=f"{self.initial_ttl_seconds}s")
            )
        except Exception as exc:  # noqa: BLE001 - the job may still finish inside the old TTL
            logger.warning("[GEMINI-CACHE] TTL extension failed for %s: %s", name, exc)

    def release(self, name: str) -> float:
        """Delete the cache and return its estimated storage cost in USD."""
        entry = self._entry(name)
        if not name or entry is None or entry.get("status") != "live":
            return 0.0
        try:
            self.client.caches.delete(name=name)
        except Exception as exc:  # noqa: BLE001 - it expires on its own; record and move on
            logger.warning("[GEMINI-CACHE] delete failed for %s (it will expire on its TTL): %s", name, exc)
        entry["status"] = "deleted"
        entry["deleted_at"] = _now()
        self._save()
        hours = max(1.0 / 60.0, _hours_between(entry.get("created_at"), entry["deleted_at"]))
        rates = _rates_for_model(self.model, input_tokens=int(entry.get("prefix_tokens") or 0))
        cost = int(entry.get("prefix_tokens") or 0) * float(rates.get("cache_storage_hr", 0.0) or 0.0) * hours / 1_000_000
        entry["storage_cost_usd"] = round(cost, 6)
        self._save()
        logger.info("[GEMINI-CACHE] released %s after %.2fh (storage ≈ $%.4f)", name, hours, cost)
        return cost

    def release_stale(self, keep: Optional[set] = None) -> None:
        """Delete caches a previous, interrupted run left live -- except any
        still referenced by an open batch job (``keep``)."""
        keep = keep or set()
        for entry in list(self.state["caches"]):
            if entry.get("status") == "live" and entry.get("name") not in keep:
                self.release(str(entry.get("name") or ""))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hours_between(start: Any, end: Any) -> float:
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        a = datetime.strptime(str(start), fmt)
        b = datetime.strptime(str(end), fmt)
        return max(0.0, (b - a).total_seconds() / 3600.0)
    except (TypeError, ValueError):
        return 0.0
