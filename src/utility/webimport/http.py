"""Polite HTTP fetching: robots.txt (with ``*``/``$`` wildcards), per-host
delay, bounded retries, and hard stops at access controls.

There is deliberately no way to skip robots.txt, solve a CAPTCHA, log in, or
pass an age gate: any of those raises :class:`AccessDenied` and the import
stops with a message for the operator.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """A page could not be fetched after the allowed retries."""


class AccessDenied(FetchError):
    """robots.txt, a login/age gate, a CAPTCHA, or an HTTP 401/403."""


# ── robots.txt ────────────────────────────────────────────────────────────

@dataclass
class _Group:
    agents: List[str] = field(default_factory=list)
    rules: List[Tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    crawl_delay: Optional[float] = None


class RobotsRules:
    """RFC 9309 matching: the most specific user-agent group applies; within
    it the longest matching pattern wins and Allow wins a tie."""

    def __init__(self, text: str = "", *, allow_all: bool = False, deny_all: bool = False, reason: str = ""):
        self.allow_all = allow_all
        self.deny_all = deny_all
        self.reason = reason
        self.groups: List[_Group] = []
        if text:
            self._parse(text)

    def _parse(self, text: str) -> None:
        current: Optional[_Group] = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    self.groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if current is None:
                continue
            if key in ("allow", "disallow"):
                if value or key == "allow":
                    current.rules.append((key == "allow", value))
            elif key == "crawl-delay":
                try:
                    current.crawl_delay = float(value)
                except ValueError:
                    pass

    def _group_for(self, user_agent: str) -> Optional[_Group]:
        ua = user_agent.lower()
        product = ua.split("/", 1)[0].strip()
        best: Optional[_Group] = None
        best_len = -1
        for group in self.groups:
            for agent in group.agents:
                if agent == "*":
                    if best_len < 0:
                        best, best_len = group, 0
                elif agent and (product.startswith(agent) or agent.startswith(product)) and len(agent) > best_len:
                    best, best_len = group, len(agent)
        return best

    @staticmethod
    def _matches(pattern: str, path: str) -> int:
        """Length of the pattern if it matches ``path``, else -1."""
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        regex = "^" + regex + ("$" if anchored else "")
        return len(pattern) if re.match(regex, path) else -1

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.deny_all:
            return False
        if self.allow_all:
            return True
        group = self._group_for(user_agent)
        if group is None:
            return True
        parts = urlsplit(url)
        path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        best_len, allowed = -1, True
        for allow, pattern in group.rules:
            length = self._matches(pattern, path)
            if length > best_len or (length == best_len and allow):
                best_len, allowed = length, allow
        return allowed

    def crawl_delay(self, user_agent: str) -> Optional[float]:
        group = self._group_for(user_agent)
        return group.crawl_delay if group else None


# ── fetcher ───────────────────────────────────────────────────────────────

_GATE_MARKERS = (
    "g-recaptcha", "h-captcha", "cf-challenge", "challenge-platform",
    "年齢確認", "18歳以上ですか",
)
_RETRY_STATUSES = {429, 500, 502, 503, 504}

# Transport: (url, headers, timeout) -> (status, final_url, text, headers)
Transport = Callable[[str, Dict[str, str], float], Tuple[int, str, str, Dict[str, str]]]


def _urllib_transport(url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, str, str, Dict[str, str]]:
    """Standard-library GET (follows redirects). HTTP errors are returned as a
    status, not raised, so the fetcher decides how to treat them."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return response.status, response.geturl(), body.decode(charset, errors="replace"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return exc.code, exc.geturl() or url, body.decode("utf-8", errors="replace"), dict(exc.headers or {})


class PoliteFetcher:
    def __init__(
        self,
        *,
        user_agent: str,
        min_delay: float = 2.0,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: Optional[Transport] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.user_agent = user_agent
        self.min_delay = max(1.0, float(min_delay))
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._transport = transport or _urllib_transport
        self._sleep = sleep
        self._clock = clock
        self._robots: Dict[str, RobotsRules] = {}
        self._last_request: Dict[str, float] = {}
        self.requests_made = 0

    def _headers(self) -> Dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept-Language": "ja,en;q=0.5"}

    def _wait_turn(self, host: str) -> None:
        delay = self.min_delay
        rules = self._robots.get(host)
        if rules is not None:
            delay = max(delay, rules.crawl_delay(self.user_agent) or 0.0)
        last = self._last_request.get(host)
        if last is not None:
            remaining = delay - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request[host] = self._clock()

    def robots_for(self, url: str) -> RobotsRules:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if host in self._robots:
            return self._robots[host]
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        self._wait_turn(host)
        try:
            status, _final, text, _headers = self._transport(robots_url, self._headers(), self.timeout)
            self.requests_made += 1
        except Exception as exc:  # noqa: BLE001 - network failure: be conservative
            logger.warning("[WEB-IMPORT] robots.txt for %s unreachable (%s); not fetching from this host", host, exc)
            rules = RobotsRules(deny_all=True, reason=f"robots.txt unreachable ({exc})")
        else:
            if 200 <= status < 300:
                rules = RobotsRules(text)
            elif 400 <= status < 500:
                rules = RobotsRules(allow_all=True)   # no robots.txt: crawling is unrestricted
            else:
                rules = RobotsRules(deny_all=True, reason=f"robots.txt returned HTTP {status}")  # assume full disallow
        self._robots[host] = rules
        return rules

    def fetch(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise FetchError(f"Refusing non-HTTP URL: {url}")
        host = parts.netloc.lower()
        rules = self.robots_for(url)
        if not rules.can_fetch(self.user_agent, url):
            raise AccessDenied(f"{rules.reason or 'robots.txt disallows'}: not fetching {url}")
        attempt = 0
        while True:
            self._wait_turn(host)
            try:
                status, final_url, text, headers = self._transport(url, self._headers(), self.timeout)
                self.requests_made += 1
            except Exception as exc:  # noqa: BLE001
                if attempt >= self.max_retries:
                    raise FetchError(f"{url}: {exc}") from exc
                attempt += 1
                self._sleep(min(60.0, self.min_delay * (2 ** attempt)))
                continue
            if status in (401, 403):
                raise AccessDenied(f"{url}: HTTP {status} — access is restricted; not bypassing it")
            if status in _RETRY_STATUSES and attempt < self.max_retries:
                attempt += 1
                retry_after = _retry_after_seconds(headers)
                self._sleep(retry_after if retry_after is not None else min(60.0, self.min_delay * (2 ** attempt)))
                continue
            if status >= 400:
                raise FetchError(f"{url}: HTTP {status}")
            if _looks_gated(final_url, text):
                raise AccessDenied(f"{url}: login, age gate or CAPTCHA page returned; not bypassing it")
            return text


def _retry_after_seconds(headers: Dict[str, str]) -> Optional[float]:
    for key, value in (headers or {}).items():
        if key.lower() == "retry-after":
            try:
                return min(300.0, max(0.0, float(value)))
            except ValueError:
                return None
    return None


def _looks_gated(final_url: str, text: str) -> bool:
    path = urlsplit(final_url).path.lower()
    if "/login" in path or "/auth/" in path:
        return True
    head = (text or "")[:20000]
    return any(marker in head for marker in _GATE_MARKERS) and "p-novel__body" not in head and "widget-episodeBody" not in head
