"""Conversation state for the Gemini route: a server-side chain plus a local ledger.

The Interactions API can hold a conversation server-side: each chapter's call
passes the previous interaction's id as ``previous_interaction_id`` and the
server replays the history, thought signatures included, with implicit
caching over it. That is the cheap, faithful path, and the default.

A server-side chain is not something this pipeline can rely on alone:

* It cannot be compacted. Every chained call carries the whole history, so a
  long volume eventually pushes context pressure (and, on 3.1 Pro, the 200k
  price tier) past what the run should pay.
* It expires. Stored interactions are retained 1 day on the free tier and up
  to 55 days on paid projects; a volume resumed after that has no chain.
* The batch path cannot extend it. Batch runs on ``generateContent``, which
  has no ``previous_interaction_id``, so a batch-committed chapter has no id.

So every accepted chapter is ALSO recorded locally: the envelope we sent and
the English that came back. When the chain is unusable, the next request
REBASES: it starts a new chain with one user input that carries a continuity
summary plus the retained chapters' English as data
(``prompt_loader.build_rebase_preamble``), ahead of the chapter envelope.
Model turns are never fabricated -- stateless mode requires thought steps to
be resent exactly as received, and a synthesized model turn has none.

The batch path replays the same local ledger as plain ``contents`` turns.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.Deepseek.common.atomic_io import atomic_write_json
from src.Gemini.prompt_loader import build_rebase_preamble

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestPlan:
    """What the next Interactions request sends.

    ``input`` is a single user-input string. ``previous_interaction_id`` is
    set only when the request extends a live chain; ``rebased`` marks a
    request that opens a new chain from the local ledger.
    """

    input: str
    previous_interaction_id: Optional[str]
    rebased: bool


class GeminiConversationManager:
    SCHEMA_VERSION = "1.0"

    def __init__(self, work_dir: Path, volume_id: str, model: str, config: Dict[str, Any]):
        self.work_dir = Path(work_dir)
        self.volume_id = str(volume_id)
        self.model = str(model)
        self.enabled = bool(config.get("enabled", True))
        self.recent_verbatim_chapters = max(1, int(config.get("recent_verbatim_chapters", 3) or 3))
        self.context_window = max(1, int(config.get("context_window", 1_000_000) or 1_000_000))
        # Long Context (Gemini 4 Argon): one window shared by input, thinking
        # and output, so history is budgeted against what the output
        # reservation leaves. 0 elsewhere -- the window is input-only there.
        self.output_reserve_tokens = max(0, int(config.get("output_reserve_tokens", 0) or 0))
        self.input_budget = max(1, self.context_window - self.output_reserve_tokens)
        self.soft_notice_ratio = float(config.get("soft_notice_ratio", 0.50) or 0.50)
        self.compact_ratio = float(config.get("compact_ratio", 0.70) or 0.70)
        # An absolute ceiling on chained input, independent of the window. A
        # chained call re-reads its whole history (cached, but billed), and on
        # gemini-3.1-pro-preview a prompt over 200k tokens moves the ENTIRE
        # request to the higher price tier -- so the default there sits under
        # it. 0 disables the ceiling.
        default_ceiling = 180_000 if self.model.startswith("gemini-3.1-pro") else 0
        raw_ceiling = config.get("max_chain_input_tokens", default_ceiling)
        self.max_chain_input_tokens = max(0, int(default_ceiling if raw_ceiling is None else raw_ceiling))
        self.summary_head_chars = max(0, int(config.get("summary_head_chars", 240) or 240))
        self.summary_tail_chars = max(0, int(config.get("summary_tail_chars", 480) or 480))
        raw_path = Path(str(config.get("persistence_file", ".context/gemini_conversation.json")))
        self.state_path = raw_path if raw_path.is_absolute() else self.work_dir / raw_path
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._new_state()
        self._load()

    # ── persistence ────────────────────────────────────────────────────────

    def _new_state(self) -> Dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "volume_id": self.volume_id,
            "model": self.model,
            "turns": [],
            "summary": "",
            "compaction_events": [],
            # The interaction the next sync request may extend, or "" when the
            # chain is broken and the next request must rebase.
            "chain_head": "",
            # total_input_tokens the server reported for the chain head's call:
            # the exact context-pressure signal, where one exists.
            "chain_input_tokens": 0,
            "system_fingerprint": "",
        }

    def _load(self) -> None:
        if not self.enabled or not self.state_path.exists():
            return
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("[GEMINI-CONVERSATION] unreadable ledger at %s; starting fresh", self.state_path)
            return
        if not isinstance(loaded, dict) or str(loaded.get("volume_id")) != self.volume_id:
            return
        if str(loaded.get("model")) != self.model:
            # A different model cannot extend another model's server chain,
            # but the accepted English is still this volume's precedent: keep
            # the turns, break the chain, and let the next request rebase.
            logger.warning(
                "[GEMINI-CONVERSATION] ledger was written by %s, now running %s; keeping the "
                "accepted chapters and rebasing the server-side chain",
                loaded.get("model"), self.model,
            )
            loaded["model"] = self.model
            loaded["chain_head"] = ""
            loaded["chain_input_tokens"] = 0
        self.state = {**self._new_state(), **loaded}
        self.state.setdefault("turns", [])

    def _save(self) -> None:
        atomic_write_json(self.state_path, self.state)

    @property
    def turns(self) -> List[Dict[str, Any]]:
        return self.state.setdefault("turns", [])

    @property
    def chain_head(self) -> str:
        return str(self.state.get("chain_head") or "")

    # ── system binding ─────────────────────────────────────────────────────

    def bind_system(self, system_segments: Sequence[str]) -> None:
        """Record the system prefix fingerprint.

        Unlike Anthropic's preserved-thinking models, Gemini does not bind a
        stored turn to the system instruction it was produced under:
        ``system_instruction`` is interaction-scoped and resent on every call,
        so a context.xml rewrite (the DeepSeek safety fallback's decision
        inheritance) needs no recovery here. The fingerprint is kept so the
        explicit-cache layer and a reader of the ledger can see it changed.
        """
        import hashlib

        fingerprint = hashlib.sha256("\x00".join(map(str, system_segments)).encode("utf-8")).hexdigest()
        if self.state.get("system_fingerprint") and self.state["system_fingerprint"] != fingerprint:
            logger.info("[GEMINI-CONVERSATION] system instruction changed since the ledger was written")
        self.state["system_fingerprint"] = fingerprint

    # ── sync path ──────────────────────────────────────────────────────────

    def plan_request(self, user_prompt: str, *, compact: bool = True) -> RequestPlan:
        """Decide whether the next chapter extends the chain or rebases it."""
        if not self.enabled:
            return RequestPlan(input=user_prompt, previous_interaction_id=None, rebased=False)
        if compact:
            self._maybe_compact()
        head = self.chain_head
        last = self.turns[-1] if self.turns else None
        if head and last is not None and str(last.get("interaction_id") or "") == head:
            return RequestPlan(input=user_prompt, previous_interaction_id=head, rebased=False)
        if not self.turns and not str(self.state.get("summary") or "").strip():
            return RequestPlan(input=user_prompt, previous_interaction_id=None, rebased=False)
        preamble = build_rebase_preamble(
            str(self.state.get("summary") or ""),
            [(str(turn.get("chapter_id") or ""), str(turn.get("model_text") or "")) for turn in self.turns],
            language_name=getattr(self, "language_name", "English"),
        )
        logger.info(
            "[GEMINI-CONVERSATION] opening a new server-side chain from the local ledger "
            "(%d retained chapter(s)%s)",
            len(self.turns), ", plus summary" if str(self.state.get("summary") or "").strip() else "",
        )
        return RequestPlan(input=f"{preamble}\n\n{user_prompt}", previous_interaction_id=None, rebased=True)

    def note_chain_lost(self, reason: str) -> None:
        """The server no longer holds the chain head (retention expiry or a
        deletion). The next plan_request rebases."""
        if self.chain_head:
            logger.warning("[GEMINI-CONVERSATION] chain head %s lost (%s); next request rebases", self.chain_head, reason)
        self.state["chain_head"] = ""
        self.state["chain_input_tokens"] = 0
        self._save()

    def commit(
        self,
        *,
        chapter_id: str,
        user_text: str,
        model_text: str,
        output_path: Path,
        interaction_id: str = "",
        input_tokens: int = 0,
    ) -> None:
        """Record an accepted chapter.

        ``interaction_id`` is the LAST interaction of the chapter's chain
        (after any continuations), so the next chapter extends from the point
        where this one actually finished. A batch-committed chapter passes ""
        and leaves the chain broken: the server never saw it.
        """
        if not self.enabled:
            return
        if not str(model_text).strip():
            raise ValueError("Gemini conversation commit requires the chapter's English text")
        self.turns.append(
            {
                "chapter_id": chapter_id,
                "user_text": user_text,
                "model_text": model_text,
                "interaction_id": interaction_id,
                "output_path": str(output_path),
            }
        )
        self.state["chain_head"] = interaction_id
        self.state["chain_input_tokens"] = int(input_tokens) if interaction_id else 0
        self._maybe_compact()
        self._save()

    # ── batch path ─────────────────────────────────────────────────────────

    def prepare_prefix(self) -> None:
        """Run the compaction check once ahead of a batch wave, so every
        request in the wave is built against one frozen ledger state."""
        if not self.enabled:
            return
        before = (len(self.turns), str(self.state.get("summary") or ""))
        self._maybe_compact()
        if (len(self.turns), str(self.state.get("summary") or "")) != before:
            self._save()

    def history_contents(self) -> List[Dict[str, Any]]:
        """The ledger as ``generateContent`` contents: summary pair, then each
        retained chapter as a user/model pair of plain text.

        Byte-stable between compactions -- the history only grows by
        appending -- which is what lets a wave share one cached prefix.
        Prior model turns carry no thought signature: generateContent
        enforces signatures on function-call turns, not on plain text ones.
        """
        if not self.enabled:
            return []
        contents: List[Dict[str, Any]] = []
        summary = str(self.state.get("summary") or "").strip()
        if summary:
            contents.append(_text_content("user", f"<continuity_summary>\n{summary}\n</continuity_summary>"))
            contents.append(_text_content("model", "Understood. Continuing with that continuity established."))
        for turn in self.turns:
            contents.append(_text_content("user", str(turn.get("user_text") or "")))
            contents.append(_text_content("model", str(turn.get("model_text") or "")))
        return contents

    # ── shared ─────────────────────────────────────────────────────────────

    def continuity_state(self) -> Dict[str, Any]:
        """Which chapters the next request carries verbatim, and which only as
        a summary line. Every retained turn is carried verbatim, whether in
        the live chain, in a rebase preamble, or in batch contents -- the
        compaction that drops a turn also breaks the chain, so the server
        never holds a chapter the ledger has folded."""
        verbatim = [str(turn.get("chapter_id") or "").strip() for turn in self.turns]
        verbatim = [chapter_id for chapter_id in verbatim if chapter_id]
        summarized: List[str] = []
        for line in str(self.state.get("summary") or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                chapter_id = stripped[2:].split(":", 1)[0].strip()
                if chapter_id and chapter_id not in summarized:
                    summarized.append(chapter_id)
        return {
            "verbatim_chapter_ids": verbatim,
            "summarized_chapter_ids": [c for c in summarized if c not in verbatim],
        }

    def estimated_tokens(self) -> int:
        """Context pressure in tokens: the server's own count for the chain
        head when there is one, else a character estimate of what a rebase
        would send. Japanese source runs denser than 4 chars/token, so the
        estimate counts the user envelopes at 2 chars/token."""
        observed = int(self.state.get("chain_input_tokens") or 0)
        if observed and self.chain_head:
            return observed
        english = sum(len(str(turn.get("model_text") or "")) for turn in self.turns)
        envelopes = sum(len(str(turn.get("user_text") or "")) for turn in self.turns)
        return max(1, english // 4 + envelopes // 2 + len(str(self.state.get("summary") or "")) // 4)

    def _maybe_compact(self) -> None:
        if len(self.turns) <= self.recent_verbatim_chapters:
            return
        tokens = self.estimated_tokens()
        ratio = tokens / float(self.input_budget)
        over_ceiling = bool(self.max_chain_input_tokens) and tokens >= self.max_chain_input_tokens
        if ratio >= self.compact_ratio or over_ceiling:
            folded = self.turns[: -self.recent_verbatim_chapters]
            self._fold_turns_into_summary(folded)
            self.turns[:] = self.turns[-self.recent_verbatim_chapters :]
            # The server chain still holds the folded chapters; extending it
            # would keep paying for them. Break it so the next call rebases.
            self.state["chain_head"] = ""
            self.state["chain_input_tokens"] = 0
            self.state.setdefault("compaction_events", []).append(
                {
                    "reason": "token_ceiling" if over_ceiling and ratio < self.compact_ratio else "compact",
                    "estimated_tokens": tokens,
                    "ratio": round(ratio, 4),
                    "folded": len(folded),
                    "remaining_turns": len(self.turns),
                }
            )
            logger.info(
                "[GEMINI-CONVERSATION] compacted at ~%d tokens (ratio %.3f): folded %d chapter(s), "
                "retained %d verbatim; next request rebases the chain",
                tokens, ratio, len(folded), len(self.turns),
            )
            return
        if ratio >= self.soft_notice_ratio:
            logger.info("[GEMINI-CONVERSATION] context pressure ratio=%.2f tokens≈%d", ratio, tokens)

    def _fold_turns_into_summary(self, turns: Iterable[Dict[str, Any]]) -> None:
        """Keep each folded chapter's opening AND its ending -- the ending is
        what the next chapter reaches back for."""
        lines: List[str] = []
        for turn in turns:
            visible = " ".join(str(turn.get("model_text") or "").split())
            chapter_id = str(turn.get("chapter_id") or "unknown")
            if not visible:
                lines.append(f"- {chapter_id}: (no visible text recorded)")
                continue
            if len(visible) <= self.summary_head_chars or not self.summary_tail_chars:
                lines.append(f"- {chapter_id}: {visible[: self.summary_head_chars] or visible}")
                continue
            head = visible[: self.summary_head_chars].rstrip()
            tail_start = max(self.summary_head_chars, len(visible) - self.summary_tail_chars)
            lines.append(f"- {chapter_id}: OPENS: {head} [...] ENDS: {visible[tail_start:].lstrip()}")
        if not lines:
            return
        prior = str(self.state.get("summary") or "").strip()
        folded = "Prior chapter continuity:\n" + "\n".join(lines)
        self.state["summary"] = (prior + "\n" + folded).strip() if prior else folded


def _text_content(role: str, text: str) -> Dict[str, Any]:
    return {"role": role, "parts": [{"text": text}]}
