"""Synthetic fixtures. All Japanese sample text here is written for these
tests; none of it is copied from a published work."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.Deepseek.common.llm_types import LLMResponse, LLMTermination

CONTEXT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<mtls_project_context schema_version="1.0" volume_id="{volume_id}" target_language="{lang}" generated_by="test">
  <opf_metadata format="json">{{"title": "灯火の約束", "author": "試験 作者"}}</opf_metadata>
  <volume_identity>
    <title_jp>灯火の約束</title_jp>
    <title_en>The Lantern Promise</title_en>
    <series_en>The Lantern Promise</series_en>
    <author_jp>試験 作者</author_jp>
    <author_en>Shiken Sakusha</author_en>
    <publisher>Test Bunko</publisher>
    <volume_number>1</volume_number>
  </volume_identity>
  <character_roster>
    <character id="c1" canonical_name="Satō Haruto" is_pov="true">
      <identity><jp_name>佐藤 陽斗</jp_name><reading>さとう はると</reading><age>16</age><role>protagonist</role>
        <gender pronouns="he/him" evidence="explicit">俺は男子高校生だ</gender></identity>
    </character>
    <character id="c2" canonical_name="Mizuki" is_pov="false">
      <identity><jp_name>美月</jp_name><role>deuteragonist</role>
        <gender pronouns="she/her" evidence="inferred">register only</gender></identity>
    </character>
    <character id="c3" canonical_name="Kage" is_pov="false">
      <identity><jp_name>影</jp_name><role>antagonist</role>
        <gender pronouns="they/them" evidence="unspecified">never stated</gender></identity>
    </character>
  </character_roster>
  <name_map>
    <entry jp="佐藤" en="Satō"/>
    <entry jp="陽斗" en="Haruto"/>
    <entry jp="生徒会" en="Student Council"/>
  </name_map>
  <relationship_graph>
    <dyad character_a="c1" character_b="c2" type="childhood_friend"/>
  </relationship_graph>
</mtls_project_context>
"""

CHAPTERS = {
    "CHAPTER_01": "# 第一話　約束\n\n## 第一話　約束\n\n「陽斗、待ってよ！」\n\n美月が叫んだ。\n\n俺は三つの灯火を数えた。\n\n＊　＊　＊\n\n翌朝、生徒会室に向かった。",
    "CHAPTER_02": "# 第二話　影\n\n## 第二話　影\n\n影は何も言わなかった。\n\n「お前は誰だ」\n\n俺は聞いた。\n\n12人の生徒が待っていた。",
    "CHAPTER_03": "# 第三話　告白\n\n## 第三話　告白\n\n「好きだよ、陽斗」\n\n美月は笑った。\n\n俺は答えなかった。",
}

VI_TRANSLATIONS = {
    "CHAPTER_01": "# Chương 1: Lời hứa\n\n“Haruto, đợi tớ với!”\n\nMizuki hét lên.\n\nTôi đếm ba ngọn đèn.\n\n* * *\n\nSáng hôm sau, tôi đến phòng hội học sinh.",
    "CHAPTER_02": "# Chương 2: Cái bóng\n\nNgười đó không nói gì.\n\n“Ngươi là ai?”\n\nTôi hỏi.\n\n12 học sinh đang đợi.",
    "CHAPTER_03": "# Chương 3: Lời tỏ tình\n\n“Tớ thích cậu, Haruto.”\n\nMizuki mỉm cười.\n\nTôi không trả lời.",
}


def make_workspace(work: Path, volume_id: str = "vol_test", lang: str = "vi",
                   chapters: Optional[Dict[str, str]] = None, with_context: bool = True) -> Path:
    """A workspace in the Librarian's on-disk shape, without running it."""
    chapters = chapters or CHAPTERS
    wd = work / volume_id
    (wd / "JP").mkdir(parents=True)
    for cid, text in chapters.items():
        (wd / "JP" / f"{cid}.md").write_text(text, encoding="utf-8")
    manifest = {
        "version": "1.0",
        "volume_id": volume_id,
        "runtime_config": {"source_language": "ja", "target_language": lang,
                           "source_chapters_dir": "JP", "translated_chapters_dir": lang.upper()},
        "pipeline_state": {"librarian": {"status": "completed"}},
        "chapters": [
            {"id": cid.lower(), "source_file": f"{cid}.md", "translated_file": f"{cid}_{lang.upper()}.md",
             "title": cid, "word_count": len(text), "toc_order": n, "translation_status": "pending",
             "qc_status": "pending"}
            for n, (cid, text) in enumerate(chapters.items())
        ],
        "assets": {},
    }
    (wd / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if with_context:
        (wd / "context.xml").write_text(CONTEXT_XML.format(volume_id=volume_id, lang=lang), encoding="utf-8")
    return wd


def openai_response(text: str, termination=LLMTermination.COMPLETED, response_id: str = "resp_1") -> LLMResponse:
    return LLMResponse(
        content=text, input_tokens=100, output_tokens=50, model="gpt-6-sol", provider="openai",
        termination=termination, response_id=response_id,
        provider_metadata={"raw_output": [{"type": "message", "role": "assistant",
                                           "content": [{"type": "output_text", "text": text}]}]},
    )


def gemini_response(text: str, termination=LLMTermination.COMPLETED, response_id: str = "int_1") -> LLMResponse:
    return LLMResponse(content=text, input_tokens=100, output_tokens=50, model="gemini-4-argon", provider="gemini",
                       termination=termination, response_id=response_id, provider_metadata={})


class ScriptedGenerate:
    """Replaces a client's ``generate``: picks the chapter from the prompt and
    answers from ``translations``; records every call."""

    def __init__(self, translations: Dict[str, str], factory: Callable[..., LLMResponse],
                 overrides: Optional[Dict[str, List[LLMResponse]]] = None, fail_on: Optional[str] = None):
        self.translations = translations
        self.factory = factory
        self.overrides = {k: list(v) for k, v in (overrides or {}).items()}
        self.fail_on = fail_on
        self.calls: List[Dict] = []

    def _chapter(self, text: str) -> str:
        import re

        ids = re.findall(r'chapter_id="(CHAPTER_\d+)"', text or "")
        return ids[-1] if ids else ""

    def __call__(self, **kwargs) -> LLMResponse:
        text = kwargs.get("prompt") or kwargs.get("input") or ""
        if isinstance(text, list):
            text = json.dumps(text, ensure_ascii=False)
        self.calls.append(kwargs)
        chapter = self._chapter(text)
        if self.fail_on and chapter == self.fail_on:
            raise RuntimeError(f"simulated interruption at {chapter}")
        if chapter in self.overrides and self.overrides[chapter]:
            return self.overrides[chapter].pop(0)
        return self.factory(self.translations.get(chapter, "# ?\n\nKhông có."), response_id=f"r{len(self.calls)}")


class FakeAssistant:
    def __init__(self, responder):
        self.responder = responder
        self.calls: List[Dict] = []

    def complete_json(self, *, system: str, user: str, label: str):
        self.calls.append({"label": label, "system": system, "user": user})
        return self.responder(label, system, user)
