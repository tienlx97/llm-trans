"""Codex CLI / Claude Code providers. No CLI is executed: ``subprocess.run``
is replaced by a fake that inspects the exact command, stdin and env."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List

import pytest

from helpers import CHAPTERS, CONTEXT_XML, make_workspace
from src.CLI.agent import CliTranslator, split_source, translate_volume
from src.CLI.runner import CliAuthError, CliError, CliQuotaError, CliResult, CliRunner


# ── runner ──────────────────────────────────────────────────────────────

class FakeProc:
    """Records each call; answers from a queue of callables(cmd, stdin, env)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: List[dict] = []

    def __call__(self, cmd, input=None, capture_output=None, cwd=None, env=None, timeout=None):
        system_file = None
        if "--system-prompt-file" in cmd:
            system_file = Path(cmd[cmd.index("--system-prompt-file") + 1])
        for arg in cmd:
            if isinstance(arg, str) and arg.startswith("model_instructions_file="):
                system_file = Path(arg.split("=", 1)[1].strip('"'))
        call = {"cmd": cmd, "stdin": input.decode("utf-8"), "env": env, "cwd": cwd,
                "system": system_file.read_text(encoding="utf-8") if system_file else None}
        self.calls.append(call)
        return self.answers.pop(0)(call)


def claude_ok(text, stop_reason="end_turn"):
    def answer(call):
        body = {"type": "result", "subtype": "success", "is_error": False, "result": text,
                "stop_reason": stop_reason,
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100,
                          "output_tokens": 42},
                "modelUsage": {"claude-test-model": {}}}
        return subprocess.CompletedProcess(call["cmd"], 0, json.dumps(body).encode(), b"")
    return answer


def claude_error(message, code=1):
    def answer(call):
        body = {"type": "result", "subtype": "success", "is_error": True, "result": message}
        return subprocess.CompletedProcess(call["cmd"], code, json.dumps(body).encode(), b"")
    return answer


def codex_ok(text):
    def answer(call):
        out = Path(call["cmd"][call["cmd"].index("-o") + 1])
        out.write_text(text, encoding="utf-8")
        events = [{"type": "thread.started"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                  {"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 700,
                                                       "output_tokens": 30, "reasoning_output_tokens": 5}}]
        return subprocess.CompletedProcess(call["cmd"], 0, "\n".join(map(json.dumps, events)).encode(), b"")
    return answer


def codex_error(message):
    def answer(call):
        events = [{"type": "error", "message": message}]
        return subprocess.CompletedProcess(call["cmd"], 1, "\n".join(map(json.dumps, events)).encode(), b"")
    return answer


def runner(provider, fake, **kw):
    return CliRunner(provider=provider, executable=provider, run=fake, sleep=lambda s: None, **kw)


def test_claude_command_stdin_env_and_parse(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-the-cli")
    fake = FakeProc(claude_ok("“Chào buổi sáng,” cô ấy nói."))
    result = runner("claude", fake, model="sonnet", max_output_tokens=64000).complete("SYS 指示", "「おはよう」", label="t")
    call = fake.calls[0]
    assert call["stdin"] == "「おはよう」" and call["system"] == "SYS 指示"
    for flag in ("-p", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in call["cmd"]
    assert call["cmd"][call["cmd"].index("--tools") + 1] == ""          # no tools at all
    assert call["cmd"][call["cmd"].index("--model") + 1] == "sonnet"
    assert "ANTHROPIC_API_KEY" not in call["env"]                       # bills the signed-in plan
    assert call["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"
    assert Path(call["cwd"]).name.startswith("mtl_cli_")               # empty throw-away dir
    assert result.text.startswith("“Chào") and not result.truncated
    assert (result.input_tokens, result.cached_tokens, result.output_tokens) == (115, 100, 42)
    assert result.model == "claude-test-model"


def test_codex_command_uses_instruction_file_and_answer_file(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-reach-the-cli")
    fake = FakeProc(codex_ok("“Đi thôi.”"))
    result = runner("codex", fake, effort="high").complete("Hệ thống", "「行こう」")
    call = fake.calls[0]
    assert call["cmd"][:2] == ["codex", "exec"] and call["cmd"][-1] == "-"
    assert call["cmd"][call["cmd"].index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in call["cmd"] and "--ignore-user-config" in call["cmd"]
    assert 'model_reasoning_effort="high"' in call["cmd"]
    assert call["system"] == "Hệ thống" and call["stdin"] == "「行こう」"
    assert "OPENAI_API_KEY" not in call["env"]
    assert result.text == "“Đi thôi.”"
    assert (result.input_tokens, result.cached_tokens, result.output_tokens) == (900, 700, 35)


def test_subscription_only_false_keeps_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-explicit")
    fake = FakeProc(claude_ok("ok"))
    runner("claude", fake, subscription_only=False).complete("s", "u")
    assert fake.calls[0]["env"]["ANTHROPIC_API_KEY"] == "sk-explicit"


def test_plan_limit_stops_without_retrying():
    fake = FakeProc(claude_error("You've hit your limit · resets 3pm"))
    with pytest.raises(CliQuotaError):
        runner("claude", fake, max_retries=3).complete("s", "u")
    assert len(fake.calls) == 1


def test_not_signed_in_is_reported_with_login_hint():
    fake = FakeProc(codex_error("Not logged in"))
    with pytest.raises(CliAuthError, match="codex login"):
        runner("codex", fake).complete("s", "u")


def test_transient_failure_is_retried_then_bounded():
    fake = FakeProc(codex_error("stream disconnected"), codex_ok("xong"))
    assert runner("codex", fake, max_retries=1).complete("s", "u").text == "xong"
    fake = FakeProc(codex_error("boom"), codex_error("boom"))
    with pytest.raises(CliError):
        runner("codex", fake, max_retries=1).complete("s", "u")
    assert len(fake.calls) == 2


def test_claude_max_tokens_is_truncation():
    fake = FakeProc(claude_ok("half", stop_reason="max_tokens"))
    assert runner("claude", fake).complete("s", "u").truncated


# ── translator ──────────────────────────────────────────────────────────

class ScriptedRunner:
    """Stands in for CliRunner at the ``complete`` boundary."""

    model = None
    label = "fake-cli"

    def __init__(self, responder):
        self.responder = responder
        self.calls: List[dict] = []

    def complete(self, system, user, *, label=""):
        self.calls.append({"system": system, "user": user, "label": label})
        out = self.responder(len(self.calls), user, label)
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, CliResult) else CliResult(text=out, input_tokens=100, output_tokens=50)


def _vi_answer(n, user, label):
    import re
    cid = re.search(r'chapter_id="(CHAPTER_\d+)"', user).group(1)
    return f"# Chương {cid[-1]}\n\n" + ("Đây là bản dịch tiếng Việt khá dài cho chương này. " * 6)


def _manifest(wd):
    return {c["source_file"]: c for c in json.loads((wd / "manifest.json").read_text(encoding="utf-8"))["chapters"]}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_translate_volume_vi_writes_and_resumes(work_dir, provider):
    wd = make_workspace(work_dir)
    fake = ScriptedRunner(_vi_answer)
    results = translate_volume("vol_test", provider=provider, runner=fake)
    assert sorted(results) == ["CHAPTER_01", "CHAPTER_02", "CHAPTER_03"]
    assert (wd / "VI" / "CHAPTER_01_VI.md").read_text(encoding="utf-8").startswith("# Chương 1")
    assert not (wd / "EN").exists()
    assert all(c["translation_status"] == "completed" for c in _manifest(wd).values())
    first, second = fake.calls[0], fake.calls[1]
    assert "<vietnamese_localization" in first["user"] and "陽斗、待ってよ" in first["user"]
    assert "Vietnamese" in first["system"] and "<project_context>" in first["system"]
    assert "<ending_tail>" not in first["user"]
    assert "<ending_tail>" in second["user"] and "Đây là bản dịch" in second["user"]   # continuity from disk
    again = translate_volume("vol_test", provider=provider, runner=ScriptedRunner(_vi_answer))
    assert again == {}


def test_long_chapter_is_split_with_part_continuity(work_dir, monkeypatch):
    long_jp = "# 第一話\n\n" + "\n\n".join(f"段落{i}。" + "あ" * 300 for i in range(10))
    wd = make_workspace(work_dir, chapters={"CHAPTER_01": long_jp})
    import src.CLI.agent as agent

    monkeypatch.setattr(agent, "cli_settings", lambda p: {"max_chunk_chars": 1000, "previous_tail_chars": 200})
    fake = ScriptedRunner(lambda n, user, label: f"Phần {n}. " + "chữ " * 200)
    translate_volume("vol_test", provider="claude", runner=fake)
    assert len(fake.calls) > 1
    assert fake.calls[1]["label"] == "CHAPTER_01#part-2"
    assert "do not emit a heading" in fake.calls[1]["user"] and "<ending_tail>" in fake.calls[1]["user"]
    joined = "\n".join(c["user"] for c in fake.calls)
    for i in range(10):
        assert joined.count(f"段落{i}。") == 1                            # every paragraph sent exactly once
    text = (wd / "VI" / "CHAPTER_01_VI.md").read_text(encoding="utf-8")
    assert text.index("Phần 1.") < text.index("Phần 2.")


def test_truncated_chapter_is_blocked(work_dir):
    wd = make_workspace(work_dir)
    fake = ScriptedRunner(lambda n, user, label: CliResult(text="# Chương\n\nNửa chừng", stop_reason="max_tokens")
                          if n == 2 else _vi_answer(n, user, label))
    translate_volume("vol_test", provider="claude", runner=fake)
    status = _manifest(wd)
    assert status["CHAPTER_02.md"]["translation_status"] == "blocked"
    assert status["CHAPTER_01.md"]["translation_status"] == "completed"


def test_plan_limit_mid_volume_keeps_finished_chapters(work_dir):
    wd = make_workspace(work_dir)
    fake = ScriptedRunner(lambda n, user, label: CliQuotaError("limit") if n == 2 else _vi_answer(n, user, label))
    with pytest.raises(CliQuotaError):
        translate_volume("vol_test", provider="codex", runner=fake)
    status = _manifest(wd)
    assert status["CHAPTER_01.md"]["translation_status"] == "completed"
    assert status["CHAPTER_02.md"]["translation_status"] == "pending"
    rest = ScriptedRunner(_vi_answer)
    assert sorted(translate_volume("vol_test", provider="codex", runner=rest)) == ["CHAPTER_02", "CHAPTER_03"]


def test_dry_run_calls_nothing(work_dir):
    wd = make_workspace(work_dir)
    fake = ScriptedRunner(lambda *a: pytest.fail("dry run must not call the CLI"))
    translate_volume("vol_test", provider="claude", runner=fake, dry_run=True)
    assert list((wd / "DRY_RUN").rglob("*.md"))
    assert not (wd / "VI").exists()


def test_english_workspace_uses_english_prompt(work_dir):
    wd = make_workspace(work_dir, lang="en")
    fake = ScriptedRunner(lambda n, user, label: "# Chapter\n\n" + "English prose here. " * 20)
    translate_volume("vol_test", provider="claude", runner=fake)
    assert (wd / "EN" / "CHAPTER_01_EN.md").exists()
    assert "<vietnamese_localization" not in fake.calls[0]["user"]


def test_split_source_respects_paragraphs():
    text = "\n".join(f"line{i}" + "x" * 50 for i in range(40))
    parts = split_source(text, 500)
    assert all(len(p) <= 500 for p in parts)
    assert "\n".join(parts) == text


# ── prep and CLI wiring ─────────────────────────────────────────────────

def test_prep_through_cli(work_dir, monkeypatch):
    from conftest import repatch_dirs
    import src.utility.prep.agent as prep

    repatch_dirs(monkeypatch, work_dir)
    wd = make_workspace(work_dir)
    populated = CONTEXT_XML.format(volume_id="vol_test", lang="vi")
    seen = {}

    class PrepRunner:
        label = "claude-cli"
        timeout = 3600.0

        def complete(self, system, user, *, label=""):
            seen.update(system=system, user=user)
            return CliResult(text="```xml\n" + populated.split("\n", 1)[1] + "\n```", model="m")

    monkeypatch.setattr(CliRunner, "from_config", classmethod(lambda cls, provider, **kw: PrepRunner()))
    receipt = prep.run_prep("vol_test", provider="claude")
    assert receipt["volume_id"] == "vol_test"
    assert "<jp_chapters>" in seen["user"] and "美月が叫んだ" in seen["user"]
    manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pipeline_state"]["prep"]["status"] == "completed"
    assert 'generated_by="claude_cli_prep"' in (wd / "context.xml").read_text(encoding="utf-8")


def test_prep_provider_defaults_to_cli_translation_provider(monkeypatch):
    import scripts.mtl as mtl

    assert mtl._prep_provider(None, "claude") == "claude"
    assert mtl._prep_provider("codex", "claude") == "codex"
    assert mtl._prep_provider(None, "openai") is None          # config.yaml prep.provider
    args = mtl.build_parser().parse_args(["resume", "v", "--provider", "codex", "--prep-provider", "claude"])
    assert (args.provider, args.prep_provider) == ("codex", "claude")


def test_vietnamese_routes_declared_for_cli_providers():
    from src.Deepseek.common.language import provider_supports

    assert provider_supports("codex", "vi") and provider_supports("claude", "vi")


def test_reading_order_past_99_chapters():
    from src.CLI.agent import reading_order

    names = ["CHAPTER_100", "CHAPTER_11", "CHAPTER_09", "CHAPTER_101"]
    assert [n for n in sorted(names, key=lambda n: reading_order(Path(n + ".md")))] == \
        ["CHAPTER_09", "CHAPTER_11", "CHAPTER_100", "CHAPTER_101"]


def _cli_prep_fixture(work_dir, monkeypatch, reply_xml, settings=None):
    from conftest import repatch_dirs
    import src.CLI.runner as runner_mod
    import src.utility.prep.agent as prep

    repatch_dirs(monkeypatch, work_dir)
    seen = {}

    class PrepRunner:
        label = "codex-cli"
        timeout = 3600.0

        def complete(self, system, user, *, label=""):
            seen["user"] = user
            return CliResult(text=reply_xml, model="m")

    monkeypatch.setattr(CliRunner, "from_config", classmethod(lambda cls, provider, **kw: PrepRunner()))
    if settings is not None:
        monkeypatch.setattr(runner_mod, "cli_settings", lambda p: settings)
    return prep, seen


def test_cli_prep_sends_only_the_opening_of_a_long_volume(work_dir, monkeypatch):
    wd = make_workspace(work_dir)
    reply = CONTEXT_XML.format(volume_id="vol_test", lang="vi").split("\n", 1)[1]
    prep, seen = _cli_prep_fixture(work_dir, monkeypatch, reply, {"prep_max_chars": 60})
    prep.run_prep("vol_test", provider="codex")
    assert "<source_scope>" in seen["user"]
    assert '<chapter id="CHAPTER_01">' in seen["user"] and '<chapter id="CHAPTER_03">' not in seen["user"]


def test_cli_prep_without_characters_is_refused(work_dir, monkeypatch):
    wd = make_workspace(work_dir)
    before = (wd / "context.xml").read_text(encoding="utf-8")
    reply = ('<mtls_project_context schema_version="1.0"><volume_identity><title_jp>x</title_jp></volume_identity>'
             '<character_roster status="pending"><pending>Awaiting owning agent.</pending></character_roster>'
             '</mtls_project_context>')
    prep, _ = _cli_prep_fixture(work_dir, monkeypatch, reply)
    with pytest.raises(prep.PrepError, match="character_roster"):
        prep.run_prep("vol_test", provider="codex")
    assert (wd / "context.xml").read_text(encoding="utf-8") == before
