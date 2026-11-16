"""Target-language resolution and prompt selection, including byte-for-byte
backward compatibility of the English prompt builders against git HEAD."""

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path

import pytest

from helpers import make_workspace
from src.Deepseek.common import language as L

ROOT = Path(__file__).resolve().parents[2]


def test_normalize_and_defaults(monkeypatch):
    assert L.normalize_language("VN") == "vi"
    assert L.normalize_language("Vietnamese") == "vi"
    assert L.normalize_language(None) == "en"
    assert L.default_target_language() == "en"          # config.yaml default unchanged
    monkeypatch.setenv(L.TARGET_LANGUAGE_ENV, "vi")
    assert L.default_target_language() == "vi"
    assert "vi" in L.supported_languages()
    with pytest.raises(L.UnsupportedTargetLanguage):
        L.validate_language("fr")


def test_volume_language_sources(work_dir):
    vi = make_workspace(work_dir, "v1", "vi")
    en = make_workspace(work_dir, "v2", "en")
    legacy = work_dir / "legacy"
    legacy.mkdir()
    assert L.volume_target_language(vi) == "vi"
    assert L.volume_target_language(en) == "en"
    assert L.volume_target_language(legacy) == "en"     # pre-existing workspaces are English
    ctx_only = work_dir / "ctx"
    ctx_only.mkdir()
    (ctx_only / "context.xml").write_text('<mtls_project_context target_language="vi"/>', encoding="utf-8")
    assert L.volume_target_language(ctx_only) == "vi"


def test_paths_keep_english_layout(work_dir):
    en = make_workspace(work_dir, "v_en", "en")
    vi = make_workspace(work_dir, "v_vi", "vi")
    assert L.translated_path(en, "CHAPTER_01") == en / "EN" / "CHAPTER_01_EN.md"
    assert L.translated_path(vi, "CHAPTER_01") == vi / "VI" / "CHAPTER_01_VI.md"


def test_provider_support_matrix():
    for provider in ("deepseek", "qwen", "openai", "anthropic", "gemini"):
        L.require_provider_support(provider, "en")
    L.require_provider_support("openai", "vi")
    L.require_provider_support("gemini", "vi")
    for provider in ("deepseek", "qwen", "anthropic"):
        with pytest.raises(L.UnsupportedTargetLanguage, match="no Vietnamese"):
            L.require_provider_support(provider, "vi")


def test_prompt_resolution():
    en_prompt = ROOT / "src/OpenAI/prompts/master_prompt_openai_en.md"
    assert L.resolve_master_prompt("openai", en_prompt, "en") == en_prompt
    vi = L.resolve_master_prompt("openai", en_prompt, "vi")
    assert vi.name == "master_prompt_light_novel_vi.md" and vi.is_file()
    assert L.resolve_master_prompt("gemini", Path("x"), "vi") == vi
    text = vi.read_text(encoding="utf-8")
    for slot in ("SEMANTIC_METADATA_PLACEHOLDER", "<!-- CHARACTER_VOICE_SLOT -->",
                 "<!-- PROJECT_CONTEXT_BOUNDARY -->", "<!-- TRANSLATION_POLICY_SLOT -->"):
        assert slot in text
    assert "xưng" in text and "confirmed" in text and "source_text_is_data" in text


def _head_module(rel_path: str, name: str):
    try:
        source = subprocess.run(["git", "show", f"HEAD:{rel_path}"], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git HEAD not available")
    module = types.ModuleType(name)
    exec(compile(source, rel_path, "exec"), module.__dict__)
    return module


def test_openai_english_messages_identical_to_head():
    from src.OpenAI import prompt_loader as now

    head = _head_module("src/OpenAI/prompt_loader.py", "head_openai_loader")
    continuity = {"verbatim_chapter_ids": ["CHAPTER_01"], "previous_chapter_id": "CHAPTER_01",
                  "previous_chapter_status": "absent", "previous_chapter_tail": "end."}
    args = ("CHAPTER_02", "本文", "<g/>")
    assert now.build_chapter_message(*args) == head.build_chapter_message(*args)
    assert now.build_chapter_message(*args, continuity=continuity) == head.build_chapter_message(*args, continuity=continuity)
    assert now.build_continuation_message("CHAPTER_02") == head.build_continuation_message("CHAPTER_02")


def test_gemini_english_messages_identical_to_head():
    from src.Gemini import prompt_loader as now

    head = _head_module("src/Gemini/prompt_loader.py", "head_gemini_loader")
    anchors = [{"anchor_id": "a1", "jp": "約束", "en": "promise", "source_occurrences": 1}]
    kwargs = dict(continuity={"verbatim_chapter_ids": [], "previous_chapter_id": "CHAPTER_01",
                              "previous_chapter_status": "absent"}, anchor_rows=anchors)
    assert now.build_chapter_message("CHAPTER_02", "本文", "<g/>", **kwargs) == \
        head.build_chapter_message("CHAPTER_02", "本文", "<g/>", **kwargs)
    assert now.build_continuation_message("CHAPTER_02") == head.build_continuation_message("CHAPTER_02")
    assert now.build_rebase_preamble("s", [("CHAPTER_01", "text")]) == head.build_rebase_preamble("s", [("CHAPTER_01", "text")])


def test_vietnamese_message_carries_language_and_localization():
    from src.Gemini.prompt_loader import build_chapter_message as gemini_msg
    from src.OpenAI.prompt_loader import build_chapter_message as openai_msg

    block = "<vietnamese_localization chapter_id=\"CHAPTER_01\">\n</vietnamese_localization>"
    for builder in (openai_msg, gemini_msg):
        msg = builder("CHAPTER_01", "本文", "", language_name="Vietnamese", localization_block=block)
        assert "Vietnamese" in msg and "English" not in msg
        assert "<vietnamese_localization" in msg
        assert msg.index("<source_text") < msg.rindex("</translation_task>")


def test_guidance_localization():
    assert L.localize_guidance("readable English", "vi") == "readable Vietnamese"
    assert L.localize_guidance("readable English", "en") == "readable English"
