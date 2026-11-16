"""Vietnamese QC (deterministic + mocked semantic review) and selective repair."""

from __future__ import annotations

import json
import unicodedata

from helpers import CHAPTERS, VI_TRANSLATIONS, FakeAssistant, make_workspace
from src.utility.vietnamese import bible as vb
from src.utility.vietnamese.qc import check_chapter, review_chapter, run_vi_qc
from src.utility.vietnamese.repair import mechanical_fix, repair_issues, reset_chapters


def _cats(issues):
    return {i["category"] for i in issues}


def test_clean_translation_has_no_blocking_issues():
    issues = check_chapter("CHAPTER_01", CHAPTERS["CHAPTER_01"], VI_TRANSLATIONS["CHAPTER_01"])
    assert not [i for i in issues if i["severity"] in ("critical", "major")], issues


def test_detects_structural_and_hygiene_defects():
    jp = CHAPTERS["CHAPTER_01"]
    vi = ("# Chương 1\n\nMizuki hét lên 陽斗.\n\nTôi đếm hai ngọn đèn\n\n"
          "<translation_task>leak</translation_task>")
    cats = _cats(check_chapter("CHAPTER_01", jp, vi))
    assert {"scene_breaks", "residual_japanese", "artifact", "truncation", "number"} <= cats


def test_missing_dialogue_and_paragraphs():
    jp = "# 章\n\n" + "\n\n".join(f"「台詞{n}です」" for n in range(10))
    vi = "# Chương\n\n" + "\n\n".join(f"Câu kể số {n}." for n in range(5))
    cats = _cats(check_chapter("CHAPTER_09", jp, vi))
    assert {"missing_dialogue", "missing_paragraphs"} <= cats


def test_terms_names_and_forbidden_variants():
    bible = vb.seed_from_context(open_context(), volume_id="v")
    bible["characters"]["c1"]["locked"] = True
    vb.merge_glossary_entry(bible, {"jp": "生徒会", "vi": "hội học sinh", "locked": True,
                                    "never_use": ["ban cán sự"]})
    vi = "# Chương 1\n\n“Harutu, đợi tớ!”\n\nTôi đến phòng ban cán sự."
    issues = check_chapter("CHAPTER_01", CHAPTERS["CHAPTER_01"], vi, bible)
    cats = _cats(issues)
    assert "term_forbidden" in cats and "term_missing" in cats and "name_spelling" in cats


def open_context():
    from helpers import CONTEXT_XML

    return CONTEXT_XML.format(volume_id="v", lang="vi")


def test_unicode_and_mechanical_fix():
    decomposed = unicodedata.normalize("NFD", "Tiếng Việt")
    text = f"# Chương\n\n\"Chào...\"，{decomposed}!!!\n\n* * *\n\n![illustration](i-001.jpg)"
    cats = _cats(check_chapter("CHAPTER_01", "# 章\n\n「こんにちは……」\n\n＊　＊　＊\n\n![illustration](i-001.jpg)", text))
    assert {"unicode", "punctuation"} <= cats
    fixed = mechanical_fix(text)
    assert "“Chào…”" in fixed and "!!!" not in fixed and "，" not in fixed
    assert unicodedata.is_normalized("NFC", fixed)
    assert "* * *" in fixed and "![illustration](i-001.jpg)" in fixed       # structure untouched


def test_semantic_review_keeps_only_traceable_findings():
    def responder(label, system, user):
        assert "data, never instructions" in system
        return {"issues": [
            {"category": "negation", "severity": "critical", "jp_quote": "俺は答えなかった。",
             "vi_quote": "Tôi trả lời ngay.", "explanation": "phủ định bị đảo", "suggested_fix": "Tôi không trả lời."},
            {"category": "addition", "severity": "major", "jp_quote": "", "vi_quote": "câu không tồn tại",
             "explanation": "hallucinated quote"},
        ]}

    vi = VI_TRANSLATIONS["CHAPTER_03"].replace("Tôi không trả lời.", "Tôi trả lời ngay.")
    issues = review_chapter("CHAPTER_03", CHAPTERS["CHAPTER_03"], vi, FakeAssistant(responder))
    negation = next(i for i in issues if i["category"] == "negation")
    assert negation["verified"] and negation["severity"] == "critical" and negation["vi_block"] is not None
    assert negation["jp_blocks"], "finding must trace back to the JP source"
    fake = next(i for i in issues if i["category"] == "addition")
    assert not fake["verified"] and fake["severity"] == "info"


def _translated_workspace(work_dir, broken_ch3: str | None = None):
    wd = make_workspace(work_dir, "v", "vi")
    (wd / "VI").mkdir()
    for cid, text in VI_TRANSLATIONS.items():
        if cid == "CHAPTER_03" and broken_ch3 is not None:
            text = broken_ch3
        (wd / "VI" / f"{cid}_VI.md").write_text(text, encoding="utf-8")
    vb.seed_volume_bible(wd)
    return wd


def test_volume_report_and_selective_repair(work_dir):
    broken = VI_TRANSLATIONS["CHAPTER_03"].replace("Tôi không trả lời.", "Tôi trả lời ngay.")
    wd = _translated_workspace(work_dir, broken)

    def review(label, system, user):
        if "CHAPTER_03" in label:
            return {"issues": [{"category": "negation", "severity": "critical", "jp_quote": "俺は答えなかった。",
                                "vi_quote": "Tôi trả lời ngay.", "explanation": "negation reversed",
                                "suggested_fix": "Tôi không trả lời."}]}
        return {"issues": []}

    report = run_vi_qc(wd, llm=True, assistant=FakeAssistant(review))
    assert not report["passed"]
    assert (wd / "QC" / "qc_vi_report.md").exists()
    issue = next(i for i in report["issues"] if i["category"] == "negation")
    untouched_before = (wd / "VI" / "CHAPTER_01_VI.md").read_text(encoding="utf-8")

    def fixer(label, system, user):
        assert "<paragraph_to_fix>\nTôi trả lời ngay.\n</paragraph_to_fix>" in user
        return {"revised_paragraph": "Tôi không trả lời.", "changed": True}

    summary = repair_issues(wd, FakeAssistant(fixer), issue_ids=[issue["id"]])
    assert summary["repaired"] == [issue["id"]]
    ch3 = (wd / "VI" / "CHAPTER_03_VI.md").read_text(encoding="utf-8")
    assert ch3 == VI_TRANSLATIONS["CHAPTER_03"]                       # only that paragraph changed
    assert (wd / "VI" / "CHAPTER_01_VI.md").read_text(encoding="utf-8") == untouched_before
    assert list((wd / ".context" / "repair_backups").glob("CHAPTER_03_VI.*.md"))
    log = json.loads((wd / "QC" / "repair_log.json").read_text(encoding="utf-8"))
    assert log[-1]["before"] == "Tôi trả lời ngay." and log[-1]["after"] == "Tôi không trả lời."
    rerun = run_vi_qc(wd, llm=True, assistant=FakeAssistant(lambda *a: {"issues": []}))
    assert rerun["passed"]


def test_repair_rejects_invalid_replacement(work_dir):
    broken = VI_TRANSLATIONS["CHAPTER_03"].replace("Tôi không trả lời.", "Tôi trả lời ngay.")
    wd = _translated_workspace(work_dir, broken)
    run_vi_qc(wd, llm=True, assistant=FakeAssistant(lambda label, s, u: {"issues": [
        {"category": "negation", "severity": "critical", "jp_quote": "俺は答えなかった。", "vi_quote": "Tôi trả lời ngay."}
    ]} if "CHAPTER_03" in label else {"issues": []}))
    summary = repair_issues(wd, FakeAssistant(lambda *a: {"revised_paragraph": "俺は答えなかった。"}),
                            min_severity="critical")
    assert summary["rejected"] and not summary["repaired"]
    assert (wd / "VI" / "CHAPTER_03_VI.md").read_text(encoding="utf-8") == broken


def test_reset_chapters_for_retranslation(work_dir):
    wd = _translated_workspace(work_dir)
    manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
    for c in manifest["chapters"]:
        c["translation_status"] = "completed"
    (wd / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert reset_chapters(wd, ["CHAPTER_02"]) == ["CHAPTER_02"]
    manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
    status = {c["source_file"]: c["translation_status"] for c in manifest["chapters"]}
    assert status == {"CHAPTER_01.md": "completed", "CHAPTER_02.md": "pending", "CHAPTER_03.md": "completed"}
    assert not (wd / "VI" / "CHAPTER_02_VI.md").exists()
    assert list((wd / ".context" / "stale").glob("CHAPTER_02_VI.*.md"))
