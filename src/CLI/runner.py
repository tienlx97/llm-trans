"""One-shot completions through the official Codex and Claude Code CLIs.

    codex exec  --ephemeral --ignore-user-config --sandbox read-only --json
                -c model_instructions_file="<system prompt file>" -o <answer file> -
    claude -p   --output-format json --system-prompt-file <file> --tools ""
                --setting-sources "" --strict-mcp-config --no-session-persistence

The user message always goes through stdin (no Windows command-line length
limit) and the system prompt through a file, both inside a throw-away
directory that is also the CLI's working directory, so the agent sees no
project files and has no tools that could act on them (Claude: no tools at
all; Codex: read-only sandbox in an empty directory).

Billing: with ``subscription_only`` (the default) the child process does not
inherit ANTHROPIC_API_KEY / OPENAI_API_KEY / CODEX_API_KEY, so the CLI uses
the account it is signed in with (`claude` -> /login, `codex login`) rather
than silently switching to pay-per-token API billing. Keys are never logged.

Errors: an exhausted plan limit raises ``CliQuotaError`` at once (no retry
loop against a hard limit), a missing login raises ``CliAuthError``; other
failures are retried a bounded number of times.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.CLI import CLI_PROVIDERS

logger = logging.getLogger(__name__)

_API_KEY_ENV = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"),
}
_QUOTA_RE = re.compile(
    r"usage limit|hit your limit|limit reached|rate[ _-]?limit|quota|too many requests|\b429\b|resets? at",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"not logged in|please run /login|codex login|invalid api key|unauthori[sz]ed|\b401\b|authentication",
    re.IGNORECASE,
)
_LOGIN_HINT = {
    "claude": "run `claude` once and sign in with /login (Claude Pro/Max account)",
    "codex": "run `codex login` and sign in with your ChatGPT account",
}


class CliError(RuntimeError):
    pass


class CliQuotaError(CliError):
    """The plan's usage limit is exhausted; resume after it resets."""


class CliAuthError(CliError):
    """The CLI is not signed in."""


@dataclass
class CliResult:
    text: str
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    model: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        return self.stop_reason in {"max_tokens", "max_output_tokens", "length"}


def cli_settings(provider: str) -> Dict[str, Any]:
    """config.yaml ``cli.<provider>`` merged over ``cli`` shared keys."""
    from src.Deepseek.common.config import get_config_section

    section = get_config_section("cli") or {}
    shared = {k: v for k, v in section.items() if not isinstance(v, dict)}
    return {**shared, **(section.get(provider) or {})}


def _tail(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


@dataclass
class CliRunner:
    provider: str
    model: Optional[str] = None
    effort: Optional[str] = None
    timeout: float = 1800.0
    max_retries: int = 2
    retry_delay: float = 15.0
    executable: Optional[str] = None
    subscription_only: bool = True
    max_output_tokens: Optional[int] = None
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        self.provider = str(self.provider).strip().lower()
        if self.provider not in CLI_PROVIDERS:
            raise CliError(f"Unknown CLI provider {self.provider!r} (expected one of {', '.join(CLI_PROVIDERS)})")

    @classmethod
    def from_config(cls, provider: str, **overrides: Any) -> "CliRunner":
        cfg = cli_settings(str(provider).strip().lower())
        values = dict(
            model=cfg.get("model") or None,
            effort=cfg.get("effort") or None,
            timeout=float(cfg.get("timeout_seconds", 1800) or 1800),
            max_retries=int(cfg.get("max_retries", 2) if cfg.get("max_retries") is not None else 2),
            executable=cfg.get("executable") or None,
            subscription_only=bool(cfg.get("subscription_only", True)),
            max_output_tokens=int(cfg["max_output_tokens"]) if cfg.get("max_output_tokens") else None,
        )
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(provider=provider, **values)

    @property
    def label(self) -> str:
        return f"{self.provider}-cli" + (f":{self.model}" if self.model else "")

    def resolve_executable(self) -> str:
        if self.executable:
            return str(self.executable)
        found = shutil.which(self.provider)
        if not found:
            raise CliError(
                f"`{self.provider}` CLI not found on PATH. Install it, or set cli.{self.provider}.executable "
                "in config.yaml to its full path."
            )
        return found

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.subscription_only:
            for name in _API_KEY_ENV[self.provider]:
                env.pop(name, None)
        if self.provider == "claude" and self.max_output_tokens:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self.max_output_tokens)
        env.setdefault("NO_COLOR", "1")
        return env

    def _command(self, exe: str, workdir: Path, system_file: Path, answer_file: Path) -> List[str]:
        if self.provider == "claude":
            cmd = [exe, "-p", "--output-format", "json", "--system-prompt-file", str(system_file),
                   "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                   "--disable-slash-commands", "--no-session-persistence"]
            if self.model:
                cmd += ["--model", self.model]
            if self.effort:
                cmd += ["--effort", self.effort]
            return cmd
        instructions = system_file.as_posix().replace("\\", "\\\\").replace('"', '\\"')
        cmd = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
               "--sandbox", "read-only", "--color", "never", "--json", "-C", str(workdir),
               "-o", str(answer_file), "-c", f'model_instructions_file="{instructions}"']
        if self.model:
            # Accept the picker's display name too: "GPT-6.1 Terra" -> "gpt-6.1-terra".
            cmd += ["-m", re.sub(r"\s+", "-", self.model.strip()).lower()]
        if self.effort:
            cmd += ["-c", f'model_reasoning_effort="{self.effort.strip().lower()}"']
        return cmd + ["-"]

    def complete(self, system: str, user: str, *, label: str = "") -> CliResult:
        exe = self.resolve_executable()
        attempts = max(1, int(self.max_retries) + 1)
        last: Optional[CliError] = None
        for attempt in range(1, attempts + 1):
            try:
                return self._complete_once(exe, system, user)
            except (CliQuotaError, CliAuthError):
                raise
            except CliError as exc:
                last = exc
                if attempt < attempts:
                    delay = self.retry_delay * attempt
                    logger.warning("[%s] %s failed (attempt %d/%d): %s — retrying in %.0fs",
                                   self.label, label or "call", attempt, attempts, exc, delay)
                    self.sleep(delay)
        raise last or CliError("CLI call failed")

    def _complete_once(self, exe: str, system: str, user: str) -> CliResult:
        with tempfile.TemporaryDirectory(prefix="mtl_cli_") as tmp:
            workdir = Path(tmp)
            system_file = workdir / "system_prompt.md"
            answer_file = workdir / "answer.md"
            system_file.write_text(system, encoding="utf-8")
            cmd = self._command(exe, workdir, system_file, answer_file)
            try:
                proc = self.run(cmd, input=user.encode("utf-8"), capture_output=True, cwd=str(workdir),
                                env=self._env(), timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                raise CliError(f"{self.label} timed out after {self.timeout:.0f}s") from exc
            except OSError as exc:
                raise CliError(f"could not start {exe}: {exc}") from exc
            stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            answer = answer_file.read_text(encoding="utf-8") if answer_file.exists() else ""
        if self.provider == "claude":
            return self._parse_claude(proc.returncode, stdout, stderr)
        return self._parse_codex(proc.returncode, stdout, stderr, answer)

    def _raise_for(self, message: str) -> None:
        if _AUTH_RE.search(message):
            raise CliAuthError(f"{self.label} is not signed in: {_tail(message, 300)} — {_LOGIN_HINT[self.provider]}")
        if _QUOTA_RE.search(message):
            raise CliQuotaError(f"{self.label} usage limit reached: {_tail(message, 300)}")
        raise CliError(f"{self.label} failed: {_tail(message)}")

    def _parse_claude(self, code: int, stdout: str, stderr: str) -> CliResult:
        data: Dict[str, Any] = {}
        for line in reversed(stdout.strip().splitlines()):
            try:
                candidate = json.loads(line)
            except ValueError:
                continue
            if isinstance(candidate, dict) and candidate.get("type") == "result":
                data = candidate
                break
        if not data:
            try:
                data = json.loads(stdout)
            except ValueError:
                data = {}
        if not isinstance(data, dict) or not data:
            self._raise_for(stderr or stdout or f"exit code {code}")
        if data.get("is_error") or data.get("subtype") not in (None, "success") or code != 0:
            self._raise_for(str(data.get("result") or data.get("api_error_status") or stderr or f"exit code {code}"))
        usage = data.get("usage") or {}
        cached = int(usage.get("cache_read_input_tokens", 0) or 0)
        fresh = int(usage.get("input_tokens", 0) or 0) + int(usage.get("cache_creation_input_tokens", 0) or 0)
        models = list((data.get("modelUsage") or {}).keys())
        return CliResult(
            text=str(data.get("result") or ""),
            input_tokens=fresh + cached,
            cached_tokens=cached,
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            stop_reason=str(data.get("stop_reason") or ""),
            model=models[0] if models else (self.model or "claude-cli"),
            raw={k: data.get(k) for k in ("session_id", "num_turns", "duration_ms", "stop_reason")},
        )

    def _parse_codex(self, code: int, stdout: str, stderr: str, answer: str) -> CliResult:
        usage: Dict[str, Any] = {}
        messages: List[str] = []
        errors: List[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "turn.completed":
                usage = event.get("usage") or {}
            elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
                messages.append(str(event["item"].get("text") or ""))
            elif kind in ("error", "turn.failed"):
                err = event.get("error") if isinstance(event.get("error"), dict) else {}
                errors.append(str(event.get("message") or err.get("message") or event))
        if errors or code != 0:
            self._raise_for(" | ".join(errors) or stderr or f"exit code {code}")
        text = answer if answer.strip() else (messages[-1] if messages else "")
        if not text.strip():
            raise CliError(f"{self.label} returned no answer")
        return CliResult(
            text=text,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            cached_tokens=int(usage.get("cached_input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0)
            + int(usage.get("reasoning_output_tokens", 0) or 0),
            model=self.model or "codex-cli",
        )
