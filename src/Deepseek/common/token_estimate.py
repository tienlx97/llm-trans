"""Cheap, dependency-free token estimate for context-pressure decisions.

``len(text) // 4`` is a fair rule for English but not for Japanese: measured
with tiktoken's o200k_base, Japanese light-novel prose runs 1.1-1.4
characters per token, so the flat rule under-counts a JP-heavy conversation
ledger roughly 2.4x and its compaction ladder fires far later than configured.
CJK and kana characters are therefore counted separately.
"""

from __future__ import annotations

import re

# CJK punctuation (\u300c\u300d\u3001\u3002) and fullwidth forms (\uff01\uff1f) tokenise like the script around them.
_CJK_RE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
_CJK_CHARS_PER_TOKEN = 1.1   # measured 1.1-1.4 (dialogue-heavy text is densest): err towards compacting early
_OTHER_CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    text = text or ""
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, int(cjk / _CJK_CHARS_PER_TOKEN + other / _OTHER_CHARS_PER_TOKEN))
