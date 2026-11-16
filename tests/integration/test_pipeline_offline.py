"""Multi-phase pipeline tests with mocked providers (no network, no paid API).

Covers: Japanese EPUB extraction into a Vietnamese workspace, bible seeding,
OpenAI and Gemini translation through their real agents (only the client's
``generate`` is scripted), interruption + resume, truncated-output recovery,
Vietnamese QC, EPUB3 build and validation, and English backward
compatibility on the same code paths.
"""

from __future__ import annotations

import json
import re
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

from helpers import (CHAPTERS, CONTEXT_XML, VI_TRANSLATIONS, ScriptedGenerate, gemini_response,
                     make_workspace, openai_response)
from src.builder.agent import run_builder
from src.builder.validation import validate_epub_structure
from src.Deepseek.common.language import UnsupportedTargetLanguage
from src.Deepseek.common.llm_types import LLMTermination
from src.utility.vietnamese.bible import save_bible, seed_volume_bible, volume_bible_path

pytestmark = pytest.mark.integration

EN_TRANSLATIONS = {
    "CHAPTER_01": "# Chapter 1: The Promise\n\n\"Haruto, wait!\"\n\nMizuki shouted.\n\nI counted three lanterns.\n\n* * *\n\nThe next morning I went to the student council room.",
    "CHAPTER_02": "# Chapter 2: Shadow\n\nThe figure said nothing.\n\n\"Who are you?\"\n\nI asked.\n\nTwelve students were waiting.",
    "CHAPTER_03": "# Chapter 3: Confession\n\n\"I like you, Haruto.\"\n\nMizuki smiled.\n\nI did not answer.",
}


@pytest.fixture
def openai_sync(monkeypatch):
    """Real OpenAI agent/config with batch and model fallback off; returns
    a function installing a scripted generate()."""
    import src.OpenAI.agent as agent
    from src.OpenAI.client import OpenAIClient

    base = deepcopy(agent.get_openai_config())
    base.setdefault("batch", {})["enabled"] = False
    base.setdefault("fallback", {})["enabled"] = False
    monkeypatch.setattr(agent, "get_openai_config", lambda source_config=None: deepcopy(base) if source_config is None else source_config)

    def install(script: ScriptedGenerate) -> ScriptedGenerate:
        monkeypatch.setattr(OpenAIClient, "generate", lambda self, **kw: script(**kw))
        return script

    return install


def _prepped(wd: Path, lang: str) -> None:
    """Stand in for prep: a populated context.xml and a completed prep stamp."""
    (wd / "context.xml").write_text(CONTEXT_XML.format(volume_id=wd.name, lang=lang), encoding="utf-8")
    manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
    manifest.setdefault("pipeline_state", {})["prep"] = {"status": "completed"}
    (wd / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def _statuses(wd: Path):
    manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
    return {Path(c["source_file"]).stem: c["translation_status"] for c in manifest["chapters"]}


def _jp_epub(tmp_path: Path) -> Path:
    from src.utility.webimport.epub_synth import build_epub
    from src.utility.webimport.models import Episode, EpisodeRef, WorkInfo

    work = WorkInfo(site="local", work_id="t", url="urn:test", title="灯火の約束", author="試験 作者")
    episodes = []
    for n, (cid, md) in enumerate(CHAPTERS.items(), start=1):
        lines = [line for line in md.split("\n\n") if not line.startswith("#")]
        title = md.split("\n", 1)[0].lstrip("# ")
        episodes.append(Episode(ref=EpisodeRef(episode_id=str(n), url="", title=title, index=n), title=title, body=lines))
    return build_epub(work, episodes, tmp_path / "source.epub")


def test_japanese_epub_to_vietnamese_epub(tmp_path, work_dir, openai_sync):
    from src.OpenAI.agent import translate_volume
    from src.utility.librarian.agent import run_librarian
    from src.utility.qc.agent import run_qc

    manifest = run_librarian(epub_path=_jp_epub(tmp_path), volume_id="lantern_vi", target_lang="vi", force_rerun=True)
    wd = work_dir / manifest.volume_id
    assert sorted(p.stem for p in (wd / "JP").glob("*.md")) == ["CHAPTER_01", "CHAPTER_02", "CHAPTER_03"]
    _prepped(wd, "vi")
    bible, path = seed_volume_bible(wd)
    from src.utility.vietnamese.bible import merge_relationship

    merge_relationship(bible, {"from": "c2", "to": "c1", "relation": "bạn thuở nhỏ", "address": [
        {"self": "tớ", "other": "cậu", "from_chapter": "CHAPTER_01", "evidence": "inferred"}]})
    bible["metadata"]["title_vi"] = "Lời hứa ngọn đèn"
    save_bible(path, bible)

    script = openai_sync(ScriptedGenerate(VI_TRANSLATIONS, openai_response))
    results = translate_volume("lantern_vi")
    assert sorted(results) == ["CHAPTER_01", "CHAPTER_02", "CHAPTER_03"]
    assert all(p.parent.name == "VI" and p.name.endswith("_VI.md") for p in results.values())
    assert not (wd / "EN").exists()
    assert set(_statuses(wd).values()) == {"completed"}

    first = script.calls[0]
    assert "Vietnamese literary translation" in first["prompt"]
    assert 'self="tớ" other="cậu"' in first["prompt"]                 # pronoun matrix reached the model
    assert "<vietnamese_localization" in first["prompt"]
    assert "xưng" in first["system_instruction"]                       # VI master prompt in use
    assert "The Lantern Promise" in first["system_instruction"]       # project context still injected

    report = run_qc("lantern_vi")
    assert report["passed"], report["recommendations"]

    result = run_builder("lantern_vi", output_base=tmp_path / "out")
    assert result.success, result.error
    check = validate_epub_structure(result.output_path)
    assert check.ok, check.errors
    assert check.language == "vi"
    with zipfile.ZipFile(result.output_path) as zf:
        names = zf.namelist()
        opf = zf.read(next(n for n in names if n.endswith(".opf"))).decode("utf-8")
        css = zf.read(next(n for n in names if n.endswith(".css"))).decode("utf-8")
        chapters = "".join(zf.read(n).decode("utf-8") for n in names if n.endswith(".xhtml"))
    assert "Lời hứa ngọn đèn" in opf and "The Lantern Promise" not in opf
    assert "hyphens: none" in css and ":lang(vi)" in css
    assert 'class="dialogue"' in chapters and "“Haruto, đợi tớ với!”" in chapters
    assert re.search(r'xml:lang="vi"|lang="vi"', chapters)


def test_interrupted_run_resumes_without_retranslating(work_dir, openai_sync):
    from src.OpenAI.agent import translate_volume

    wd = make_workspace(work_dir, "vol_resume", "vi")
    seed_volume_bible(wd)
    openai_sync(ScriptedGenerate(VI_TRANSLATIONS, openai_response, fail_on="CHAPTER_02"))
    with pytest.raises(RuntimeError, match="simulated interruption"):
        translate_volume("vol_resume")
    assert _statuses(wd) == {"CHAPTER_01": "completed", "CHAPTER_02": "pending", "CHAPTER_03": "pending"}

    script = openai_sync(ScriptedGenerate(VI_TRANSLATIONS, openai_response))
    results = translate_volume("vol_resume")
    assert sorted(results) == ["CHAPTER_02", "CHAPTER_03"]                 # chapter 1 not paid for twice
    translated = {re.findall(r'chapter_id="(CHAPTER_\d+)"', c["prompt"])[-1] for c in script.calls}
    assert translated == {"CHAPTER_02", "CHAPTER_03"}
    assert set(_statuses(wd).values()) == {"completed"}


def test_truncated_output_continues_or_blocks(work_dir, openai_sync):
    from src.OpenAI.agent import translate_volume

    wd = make_workspace(work_dir, "vol_trunc", "vi")
    head, tail = VI_TRANSLATIONS["CHAPTER_01"][:40], VI_TRANSLATIONS["CHAPTER_01"][40:]
    cut = [openai_response("# Chương 2\n\nNgười đó", LLMTermination.MAX_OUTPUT, f"t{i}") for i in range(10)]
    script = openai_sync(ScriptedGenerate(VI_TRANSLATIONS, openai_response, overrides={
        "CHAPTER_01": [openai_response(head, LLMTermination.MAX_OUTPUT, "a1"), openai_response(tail, response_id="a2")],
        "CHAPTER_02": cut,
    }))
    translate_volume("vol_trunc")
    ch1 = (wd / "VI" / "CHAPTER_01_VI.md").read_text(encoding="utf-8")
    assert ch1.replace("\n", "") == VI_TRANSLATIONS["CHAPTER_01"].replace("\n", "")   # continuation recovered
    status = _statuses(wd)
    assert status["CHAPTER_01"] == "completed" and status["CHAPTER_03"] == "completed"
    assert status["CHAPTER_02"] == "blocked"                                          # never marked done
    manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
    assert "truncated" in next(c for c in manifest["chapters"] if c["source_file"] == "CHAPTER_02.md")["last_failure"]
    assert any("translation_continuation" in str(c.get("prompt")) for c in script.calls)


def test_gemini_vietnamese_route(work_dir, monkeypatch):
    import src.Gemini.agent as gagent
    from src.Gemini.client import GeminiClient

    wd = make_workspace(work_dir, "vol_gem", "vi")
    bible, path = seed_volume_bible(wd)
    leaky = dict(VI_TRANSLATIONS, CHAPTER_02=VI_TRANSLATIONS["CHAPTER_02"].replace("Người đó", "Người đó 影"))
    script = ScriptedGenerate(leaky, gemini_response)
    monkeypatch.setattr(GeminiClient, "generate", lambda self, **kw: script(**kw))
    cfg = deepcopy(gagent.get_gemini_config())
    cfg.setdefault("batch", {})["enabled"] = False
    monkeypatch.setattr(gagent, "get_gemini_config", lambda: deepcopy(cfg))
    results = gagent.translate_volume("vol_gem")
    assert all(p.parent.name == "VI" for p in results.values())
    system = script.calls[0]["system_instruction"]
    system_text = "\n".join(system) if isinstance(system, list) else str(system)
    assert "xưng" in system_text and "Vietnamese" in script.calls[0]["input"]
    # Japanese left in the output is kept (not silently deleted) so QC can point at it.
    assert "影" in (wd / "VI" / "CHAPTER_02_VI.md").read_text(encoding="utf-8")
    from src.utility.qc.agent import run_qc

    report = run_qc("vol_gem")
    assert any(i["category"] == "residual_japanese" and i["chapter_id"] == "CHAPTER_02" for i in report["issues"])


def test_english_pipeline_unchanged(tmp_path, work_dir, openai_sync):
    from src.OpenAI.agent import translate_volume
    from src.utility.qc.agent import run_qc

    wd = make_workspace(work_dir, "vol_en", "en")
    _prepped(wd, "en")
    leaky = dict(EN_TRANSLATIONS, CHAPTER_02=EN_TRANSLATIONS["CHAPTER_02"] + " 影")
    script = openai_sync(ScriptedGenerate(leaky, openai_response))
    results = translate_volume("vol_en")
    assert {p.relative_to(wd).as_posix() for p in results.values()} == {
        "EN/CHAPTER_01_EN.md", "EN/CHAPTER_02_EN.md", "EN/CHAPTER_03_EN.md"}
    en_prompt = (Path(__file__).resolve().parents[2] / "src/OpenAI/prompts/master_prompt_openai_en.md").read_text(encoding="utf-8")
    assert script.calls[0]["system_instruction"].startswith(en_prompt.split("SEMANTIC_METADATA_PLACEHOLDER")[0][:2000])
    assert "<vietnamese_localization" not in script.calls[0]["prompt"]
    assert "English literary translation" in script.calls[0]["prompt"]
    assert "影" not in (wd / "EN" / "CHAPTER_02_EN.md").read_text(encoding="utf-8")   # historical CJK strip kept
    report = run_qc("vol_en")
    assert set(report) >= {"passed", "completeness", "truncation_flags", "token_outliers", "name_drifts"}
    assert "issues" not in report                                                    # original EN report shape
    result = run_builder("vol_en", output_base=tmp_path / "out")
    assert result.success, result.error
    check = validate_epub_structure(result.output_path)
    assert check.ok, check.errors
    assert check.language == "en"
    with zipfile.ZipFile(result.output_path) as zf:
        opf = zf.read(next(n for n in zf.namelist() if n.endswith(".opf"))).decode("utf-8")
        css = zf.read(next(n for n in zf.namelist() if n.endswith(".css"))).decode("utf-8")
    assert "The Lantern Promise" in opf and ":lang(vi)" not in css


def test_providers_without_vietnamese_route_refuse_before_any_call(work_dir):
    from src.Anthropic.agent import translate_volume as anthropic_tv
    from src.Deepseek.translator.agent import translate_volume as deepseek_tv
    from src.Qwen.agent import translate_volume as qwen_tv

    make_workspace(work_dir, "vol_guard", "vi")
    for tv in (deepseek_tv, qwen_tv, anthropic_tv):
        with pytest.raises(UnsupportedTargetLanguage):
            tv("vol_guard")
    assert not (work_dir / "vol_guard" / "EN").exists()


def test_dry_run_sends_nothing(work_dir, monkeypatch):
    from src.OpenAI.agent import translate_volume

    wd = make_workspace(work_dir, "vol_dry", "vi")
    monkeypatch.delenv("OPENAI_API_KEY")
    results = translate_volume("vol_dry", dry_run=True)
    assert len(results) == 3
    payloads = list((wd / "DRY_RUN").rglob("*"))
    assert payloads, "dry run must write payloads"
    assert not (wd / "VI").exists()
    assert set(_statuses(wd).values()) == {"pending"}
