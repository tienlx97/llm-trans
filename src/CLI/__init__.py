"""Subscription CLI providers: Codex CLI (`codex exec`) and Claude Code (`claude -p`).

These routes drive the official command-line tools non-interactively, so a
translation bills against the ChatGPT or Claude plan the CLI is signed in
with instead of an API key. See runner.py for the exact invocation.
"""

CLI_PROVIDERS = ("codex", "claude")
