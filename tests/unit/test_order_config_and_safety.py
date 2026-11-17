"""Story-order sorting, explicit-zero config values, and the re-extraction guard."""

from __future__ import annotations

import json
from pathlib import Path

from src.Deepseek.common.chapter_order import in_reading_order, preceding_chapter_id, reading_order
from src.Deepseek.common.config import cfg_float, cfg_int
from src.utility.librarian.epub_extractor import EPUBExtractor


def test_reading_order_past_99_chapters_and_parts():
    names = ["CHAPTER_100", "CHAPTER_11", "CHAPTER_02", "CHAPTER_01_PART_02", "CHAPTER_01_PART_01", "CHAPTER_99"]
    ordered = [p.stem for p in in_reading_order(Path(n + ".md") for n in names)]
    assert ordered == ["CHAPTER_01_PART_01", "CHAPTER_01_PART_02", "CHAPTER_02", "CHAPTER_11", "CHAPTER_99", "CHAPTER_100"]
    # A translated file sorts with its chapter, whatever the suffix.
    assert reading_order("CHAPTER_100_EN.md") > reading_order("CHAPTER_99_EN.md")


def test_preceding_chapter_uses_librarian_padding():
    assert preceding_chapter_id("CHAPTER_07") == "CHAPTER_06"
    assert preceding_chapter_id("CHAPTER_10") == "CHAPTER_09"
    assert preceding_chapter_id("CHAPTER_100") == "CHAPTER_99"   # not CHAPTER_099
    assert preceding_chapter_id("CHAPTER_01") is None
    assert preceding_chapter_id("AFTERWORD") is None


def test_explicit_zero_is_kept_and_null_falls_back():
    assert cfg_int({"previous_chapter_tail_chars": 0}, "previous_chapter_tail_chars", 1200) == 0
    assert cfg_int({"max_retries": None}, "max_retries", 5) == 5
    assert cfg_int({}, "max_retries", 5) == 5
    assert cfg_float({"jitter_factor": 0}, "jitter_factor", 0.25) == 0.0


def test_reextraction_guard_reads_disk_not_just_manifest(tmp_path):
    extractor = EPUBExtractor(tmp_path)
    work = tmp_path / "vol"
    (work / "JP").mkdir(parents=True)
    (work / "VI").mkdir()
    (work / "JP" / "CHAPTER_01_PART_02.md").write_text("x", encoding="utf-8")
    assert extractor._check_existing_translations(work) is None  # source only: safe to replace

    (work / "VI" / "CHAPTER_01_VI.md").write_text("x", encoding="utf-8")
    assert extractor._check_existing_translations(work)["translated_total"] == 1  # no manifest at all

    (work / "manifest.json").write_text("{truncated", encoding="utf-8")
    assert extractor._check_existing_translations(work)["translated_dirs"] == {"VI": 1}  # corrupt manifest

    (work / "VI" / "CHAPTER_01_VI.md").unlink()
    (work / "manifest.json").write_text(json.dumps({"pipeline_state": {"prep": {"status": "completed"}}}), encoding="utf-8")
    assert extractor._check_existing_translations(work)["prep_completed"] is True  # paid prep is state too


def test_empty_block_on_means_never_block():
    from src.Anthropic.agent import _block_on_setting as anthropic_setting
    from src.Gemini.agent import _block_on_setting as gemini_setting

    for setting in (anthropic_setting, gemini_setting):
        assert setting({"block_on": []}) == []                      # record only, never block
        assert setting({"block_on": ["structural"]}) == ["structural"]
        assert setting({}) == ["missing", "drifted", "structural"]  # absent: full default
        assert setting({"block_on": None}) == ["missing", "drifted", "structural"]


def test_bible_chapter_order_handles_split_chapters():
    from src.utility.vietnamese.bible import chapter_order, effective_at

    assert chapter_order("CHAPTER_03") < chapter_order("CHAPTER_05_PART_01") < chapter_order("CHAPTER_05_PART_02")
    assert chapter_order("series") < chapter_order("CHAPTER_01")
    # An entry introduced in chapter 3 is visible to a later split chapter.
    assert effective_at("CHAPTER_03", "CHAPTER_05_PART_01")
    assert not effective_at("CHAPTER_07", "CHAPTER_05_PART_02")


def test_token_estimate_counts_japanese_densely():
    import tiktoken

    from src.Deepseek.common.token_estimate import estimate_tokens

    enc = tiktoken.get_encoding("o200k_base")
    japanese = "「陽斗、待ってよ！」美月が叫んだ。俺は三つの灯火を数えた。翌朝、生徒会室に向かった。" * 40
    english = "Haruto, wait! Mizuki shouted. I counted three lanterns and went to the council room. " * 40
    for text in (japanese, english):
        real = len(enc.encode(text))
        # Never under-count by more than 20%: compaction must not fire late.
        assert estimate_tokens(text) >= real * 0.8, (estimate_tokens(text), real)
    assert estimate_tokens(japanese) > len(japanese) // 4 * 2


def test_toc_parser_ignores_xml_comments(tmp_path):
    from src.utility.librarian.toc_parser import TOCParser

    nav = tmp_path / "nav.xhtml"
    nav.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        '<nav epub:type="toc"><!-- toc --><h1>目次</h1><ol><!-- chapters -->'
        '<li><a href="c1.xhtml">第一話</a></li><li><a href="c2.xhtml">第二話</a></li></ol></nav></body></html>',
        encoding="utf-8",
    )
    toc = TOCParser(tmp_path)._parse_nav_xhtml(nav)
    assert toc.title == "目次"
    assert [point.label for point in toc.nav_points] == ["第一話", "第二話"]


def test_qwen_cache_tokens_are_disjoint_from_input_tokens():
    # QwenCloud's documented Anthropic-compatible example: input_tokens 369
    # beside cache_read_input_tokens 896 -- the counts do not overlap.
    import pytest

    from src.Deepseek.common.token_telemetry import _rates_for_model
    from src.Qwen.client import QwenClient

    client = QwenClient(dry_run=True)
    usage = {"input_tokens": 369, "output_tokens": 100, "cache_read_input_tokens": 896, "cache_creation_input_tokens": 0}
    response = client._to_llm_response(raw_blocks=[{"type": "text", "text": "x"}], stop_reason="end_turn",
                                       response_id="r", usage_obj=usage, streamed=False)
    rates = _rates_for_model(client.model, cache_pricing_mode=client._cache_pricing_mode)
    assert response.input_cost_usd == pytest.approx(369 * rates["cache_miss"] / 1_000_000)
    assert response.provider_metadata["cache_telemetry"]["cache_hit_ratio"] == pytest.approx(896 / (369 + 896), abs=1e-4)


def test_spine_and_toc_hrefs_are_url_decoded(tmp_path):
    from src.utility.librarian.spine_parser import SpineParser
    from src.utility.librarian.toc_parser import TOCParser

    opf = tmp_path / "content.opf"
    opf.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<manifest><item id="c1" href="text/chapter%201.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>', encoding="utf-8")
    assert SpineParser(opf).parse().items[0].href == "text/chapter 1.xhtml"

    nav = tmp_path / "nav.xhtml"
    nav.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
        '<li><a href="text/chapter%201.xhtml">One</a></li></ol></nav></body></html>', encoding="utf-8")
    toc = TOCParser(tmp_path)._parse_nav_xhtml(nav)
    assert toc.nav_points[0].content_src.startswith("text/chapter 1.xhtml")


def test_deepseek_retries_transient_errors_and_never_duplicates_a_broken_stream(monkeypatch):
    import anthropic
    import httpx2 as httpx  # anthropic 1.x is built on httpx2

    from src.Deepseek.translator import deepseek_client as dc

    client = object.__new__(dc.DeepSeekClient)
    monkeypatch.setattr(dc, "get_deepseek_retry_config", lambda: {"max_retries": 2, "base_delay_ms": 0})
    monkeypatch.setattr(dc.time, "sleep", lambda _s: None)
    request = httpx.Request("POST", "https://api.deepseek.com/anthropic/v1/messages")
    calls = []

    def flaky(*, turn_kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise anthropic.APIConnectionError(request=request)
        return "ok"

    monkeypatch.setattr(client, "_run_turn_once", flaky)
    assert client._run_turn(turn_kwargs={}) == "ok" and len(calls) == 3

    # A 400 is not transient: no retry.
    calls.clear()

    def bad_request(*, turn_kwargs):
        calls.append(1)
        raise anthropic.BadRequestError("bad", response=httpx.Response(400, request=request), body=None)

    monkeypatch.setattr(client, "_run_turn_once", bad_request)
    import pytest

    with pytest.raises(anthropic.BadRequestError):
        client._run_turn(turn_kwargs={})
    assert len(calls) == 1


def test_failed_chapter_conversion_stops_extraction():
    import pytest

    from src.utility.librarian.agent import _raise_on_failed_chapters

    _raise_on_failed_chapters([])  # nothing failed: no error
    with pytest.raises(RuntimeError, match="1 chapter"):
        _raise_on_failed_chapters(["第三話: broken XHTML"])
