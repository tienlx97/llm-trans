"""Gemini API error classification and bounded retry policy.

Two SDK error families reach this module and both are normalized here:

* ``google.genai.errors.APIError`` (``ClientError`` / ``ServerError``) from the
  ``generateContent`` surface -- Batch API and explicit caching. Carries
  ``.code`` (HTTP status) and ``.status`` (the canonical gRPC name, e.g.
  ``RESOURCE_EXHAUSTED``).
* ``google.genai._gaos`` ``*ClientError`` / ``*ServerError`` from the
  Interactions surface. Carries the httpx ``raw_response``.

The SDK module is never imported at load time; classification works off
attributes and message text so a dry run needs no SDK at all.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


class GeminiAPIError(RuntimeError):
    """Base class for normalized Gemini transport failures."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code or "gemini_api_error"


class GeminiRateLimitError(GeminiAPIError):
    """HTTP 429 / ``RESOURCE_EXHAUSTED`` -- quota or per-minute rate limit."""


class GeminiOverloadedError(GeminiAPIError):
    """HTTP 503 / ``UNAVAILABLE`` -- Google's transient-capacity signal, the
    counterpart of Anthropic's 529. Tracked on its own ceiling so an overload
    storm backs off harder than an ordinary blip."""


class GeminiAuthError(GeminiAPIError):
    """HTTP 401/403 -- ``UNAUTHENTICATED`` / ``PERMISSION_DENIED``."""


class GeminiInvalidRequestError(GeminiAPIError):
    """HTTP 400/404/413/422 -- ``INVALID_ARGUMENT`` / ``NOT_FOUND`` /
    ``FAILED_PRECONDITION``. Not retryable: the request we built is wrong."""


class GeminiInteractionNotFoundError(GeminiInvalidRequestError):
    """A ``previous_interaction_id`` the server no longer holds -- the stored
    interaction aged out of its retention window (1 day free tier, up to 55
    days paid) or was deleted. Recoverable, but not by retrying: the caller
    rebases the conversation from the local ledger and starts a new chain."""


class GeminiSafetyBlockError(GeminiAPIError):
    """A completed transport call whose output was withheld by a safety,
    blocklist, prohibited-content, SPII, or recitation filter. Routed to the
    DeepSeek safety fallback rather than retried -- the same input blocks the
    same way."""


class GeminiLongDecodeError(GeminiAPIError):
    """A background (Long Decode) interaction that was created but could not
    be seen through: polling kept failing, or the wait deadline passed. Not
    retryable -- the interaction was already billed for the work it did, and
    a retry would create (and pay for) a second one."""


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    base_delay_ms: int = 1000
    max_delay_ms: int = 60_000
    jitter_factor: float = 0.25
    max_429_retries: int = 4
    max_503_retries: int = 4


def classify_exception(exc: BaseException) -> GeminiAPIError:
    """Normalize SDK and HTTP errors without importing the SDK at module load."""
    if isinstance(exc, GeminiAPIError):
        return exc
    status = _extract_status(exc)
    text = str(exc)
    lower = text.lower()
    grpc = _extract_grpc_status(exc, lower)
    if status == 429 or grpc == "RESOURCE_EXHAUSTED" or "resource_exhausted" in lower or "rate limit" in lower:
        return GeminiRateLimitError(text, status_code=status, code="RESOURCE_EXHAUSTED")
    if status == 503 or grpc == "UNAVAILABLE" or "overloaded" in lower:
        return GeminiOverloadedError(text, status_code=status, code="UNAVAILABLE")
    if status in {401, 403} or grpc in {"UNAUTHENTICATED", "PERMISSION_DENIED"}:
        return GeminiAuthError(text, status_code=status, code=grpc or "PERMISSION_DENIED")
    if status == 404 and "interaction" in lower:
        return GeminiInteractionNotFoundError(text, status_code=status, code="NOT_FOUND")
    if status in {400, 404, 409, 413, 422} or grpc in {"INVALID_ARGUMENT", "NOT_FOUND", "FAILED_PRECONDITION"}:
        return GeminiInvalidRequestError(text, status_code=status, code=grpc or "INVALID_ARGUMENT")
    return GeminiAPIError(text, status_code=status, code=grpc or None)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (GeminiAuthError, GeminiInvalidRequestError, GeminiSafetyBlockError, GeminiLongDecodeError)):
        return False
    status = _extract_status(exc)
    return status in {None, 408, 425, 429, 500, 502, 503, 504}


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Execute a request with exponential backoff and separate 429/503 ceilings."""
    attempts = 0
    rate_limit_attempts = 0
    overload_attempts = 0
    while True:
        try:
            return fn()
        # Exception, not BaseException: Ctrl+C (KeyboardInterrupt) and SystemExit
        # must abort the run, not be classified as a transport error and retried.
        except Exception as exc:  # retry policy works on provider-normalized exceptions
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            normalized = classify_exception(exc)
            if not is_retryable(normalized) or attempts >= max(0, policy.max_retries):
                raise normalized from exc
            if isinstance(normalized, GeminiRateLimitError):
                rate_limit_attempts += 1
                if rate_limit_attempts > max(0, policy.max_429_retries):
                    raise normalized from exc
            if isinstance(normalized, GeminiOverloadedError):
                overload_attempts += 1
                if overload_attempts > max(0, policy.max_503_retries):
                    raise normalized from exc
            attempts += 1
            delay = _backoff_seconds(attempts, policy)
            if on_retry is not None:
                on_retry(attempts, normalized, delay)
            sleep(delay)


def _backoff_seconds(attempt: int, policy: RetryPolicy) -> float:
    base = max(0, policy.base_delay_ms) / 1000.0
    ceiling = max(base, policy.max_delay_ms / 1000.0)
    delay = min(ceiling, base * (2 ** max(0, attempt - 1)))
    jitter = delay * max(0.0, min(1.0, policy.jitter_factor))
    return max(0.05, delay + random.uniform(-jitter, jitter))


def _extract_status(exc: BaseException) -> Optional[int]:
    # google.genai.errors.APIError keeps the HTTP status in ``code``; the
    # Interactions (_gaos) errors keep it on the httpx ``raw_response``.
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, bool):
            continue
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    for holder in ("raw_response", "response"):
        response = getattr(exc, holder, None)
        value = getattr(response, "status_code", None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_grpc_status(exc: BaseException, lower_text: str) -> str:
    status = getattr(exc, "status", None)
    if isinstance(status, str) and status:
        return status.upper()
    for name in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "UNAUTHENTICATED", "PERMISSION_DENIED",
                 "INVALID_ARGUMENT", "NOT_FOUND", "FAILED_PRECONDITION", "DEADLINE_EXCEEDED"):
        if name.lower() in lower_text:
            return name
    return ""
