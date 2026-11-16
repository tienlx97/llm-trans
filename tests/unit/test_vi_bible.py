"""Vietnamese bible: seeding, evidence/lock merge rules, spoiler gating,
cross-volume inheritance and LLM-proposal verification."""

from __future__ import annotations

import json

from helpers import CHAPTERS, CONTEXT_XML, make_workspace
from src.utility.vietnamese import bible as vb
from src.utility.vietnamese.analyze import apply_proposals, lock_established


def _seed():
    return vb.seed_from_context(CONTEXT_XML.format(volume_id="v", lang="vi"), volume_id="v")


def test_seed_maps_prep_evidence():
    bible = _seed()
    chars = bible["characters"]
    assert chars["c1"]["vi"] == "Satou Haruto"                       # macron -> ou, never a Hán Việt reading
    assert chars["c1"]["gender"] == {"value": "male", "evidence": "confirmed", "source": "俺は男子高校生だ"}
    assert chars["c2"]["gender"]["evidence"] == "inferred"
    assert chars["c3"]["gender"] == {"value": "unknown", "evidence": "unknown", "source": "never stated"}
    assert set(chars["c1"]["aliases_jp"]) >= {"佐藤", "陽斗"}
    assert bible["narration"]["pov_character"] == "c1"
    # Non-character proper noun: English rendering is NOT reused as Vietnamese.
    council = next(g for g in bible["glossary"] if g["jp"] == "生徒会")
    assert council["vi"] == "" and "Student Council" in council["notes"]
    assert bible["relationships"][0]["from"] == "c1" and bible["relationships"][0]["evidence"] == "inferred"


def test_locked_entries_never_overwritten():
    bible = _seed()
    bible["characters"]["c1"]["locked"] = True
    vb.merge_character(bible, "c1", {"vi": "Sato Haruto"}, raised_in="CHAPTER_02")
    assert bible["characters"]["c1"]["vi"] == "Satou Haruto"
    assert any("locked" in a["detail"] for a in bible["ambiguities"])
    vb.merge_glossary_entry(bible, {"jp": "灯火", "vi": "ngọn đèn", "locked": True})
    vb.merge_glossary_entry(bible, {"jp": "灯火", "vi": "đèn lồng", "evidence": "confirmed"})
    entry = next(g for g in bible["glossary"] if g["jp"] == "灯火")
    assert entry["vi"] == "ngọn đèn"


def test_weaker_evidence_cannot_override_stronger():
    bible = _seed()
    vb.merge_character(bible, "c1", {"gender": {"value": "female", "evidence": "inferred"}})
    assert bible["characters"]["c1"]["gender"]["value"] == "male"
    assert any(a["topic"] == "c1.gender" for a in bible["ambiguities"])
    # unknown -> filled by an inferred proposal
    vb.merge_character(bible, "c3", {"gender": {"value": "female", "evidence": "inferred"}})
    assert bible["characters"]["c3"]["gender"]["value"] == "female"
    # inferred -> upgraded by confirmed
    vb.merge_character(bible, "c2", {"gender": {"value": "female", "evidence": "confirmed"}})
    assert bible["characters"]["c2"]["gender"]["evidence"] == "confirmed"


def test_spoiler_gating_and_relationship_change():
    bible = _seed()
    vb.merge_relationship(bible, {"from": "c2", "to": "c1", "relation": "bạn thuở nhỏ", "address": [
        {"self": "tớ", "other": "cậu", "from_chapter": "CHAPTER_01", "evidence": "inferred"},
        {"self": "em", "other": "anh", "from_chapter": "CHAPTER_03", "evidence": "inferred"},
    ]})
    vb.merge_glossary_entry(bible, {"jp": "告白", "vi": "lời tỏ tình", "introduced_in": "CHAPTER_03"})
    ch1 = vb.render_localization_block(bible, chapter_id="CHAPTER_01", source=CHAPTERS["CHAPTER_01"])
    ch3 = vb.render_localization_block(bible, chapter_id="CHAPTER_03", source=CHAPTERS["CHAPTER_03"])
    assert 'self="tớ" other="cậu"' in ch1 and 'self="em"' not in ch1
    assert 'self="em" other="anh"' in ch3 and 'self="tớ"' not in ch3
    assert "lời tỏ tình" not in ch1                     # introduced later
    # Characters absent from the chapter are not injected (c3 = 影 only in ch2).
    assert 'id="c3"' not in ch1
    ch2 = vb.render_localization_block(bible, chapter_id="CHAPTER_02", source=CHAPTERS["CHAPTER_02"])
    assert 'id="c3"' in ch2 and 'gender="unknown" gender_evidence="unknown"' in ch2


def test_series_inheritance_and_promotion(tmp_path):
    vol1 = _seed()
    vol1["series_id"] = "lantern"
    vb.merge_relationship(vol1, {"from": "c2", "to": "c1", "address": [
        {"self": "tớ", "other": "cậu", "from_chapter": "CHAPTER_01"},
        {"self": "em", "other": "anh", "from_chapter": "CHAPTER_03"}]})
    series = vb.promote_to_series(vol1, None, volume_id="vol1")
    path = vb.save_bible(vb.series_bible_path(tmp_path, "lantern"), series)
    assert path.parts[-3:] == ("lantern", "vi", "bible_vi.json")      # never the English bible files
    vol2 = vb.inherit_series(vb.load_bible(path), volume_id="vol2")
    rel = next(r for r in vol2["relationships"] if r["from"] == "c2")
    assert [(s["self"], s["from_chapter"]) for s in rel["address"]] == [("em", "series")]
    assert all(c["introduced_in"] == "series" for c in vol2["characters"].values())


def test_seed_volume_bible_preserves_operator_edits(work_dir):
    wd = make_workspace(work_dir, "v", "vi")
    bible, path = vb.seed_volume_bible(wd)
    bible["characters"]["c1"]["vi"] = "Haruto"
    bible["characters"]["c1"]["locked"] = True
    bible["metadata"]["title_vi"] = "Lời hứa đèn lồng"
    vb.save_bible(path, bible)
    again, _ = vb.seed_volume_bible(wd)
    assert again["characters"]["c1"]["vi"] == "Haruto"
    assert again["metadata"]["title_vi"] == "Lời hứa đèn lồng"


def test_analysis_proposals_are_verified():
    bible = _seed()
    source = CHAPTERS["CHAPTER_01"]
    proposals = {
        "characters": [{"id": "c3", "jp": "影", "gender": {"value": "male", "evidence": "confirmed",
                                                           "quote": "影は男だった"}}],  # quote not in source
        "relationships": [{"from": "c2", "to": "c1", "relation": "bạn thân", "evidence": "confirmed",
                           "quote": "陽斗、待ってよ！",
                           "address": [{"self": "tớ", "other": "cậu", "from_chapter": "CHAPTER_01",
                                        "evidence": "confirmed"}]}],
        "glossary": [{"jp": "灯火", "vi": "ngọn đèn", "first_chapter": "CHAPTER_01"},
                     {"jp": "魔王", "vi": "Ma vương"}],                 # not in this batch -> dropped
    }
    apply_proposals(bible, proposals, batch_source=source, chapter_ids=["CHAPTER_01"])
    assert bible["characters"]["c3"]["gender"]["evidence"] == "inferred"   # unverifiable quote downgraded
    rel = next(r for r in bible["relationships"] if r["from"] == "c2")
    assert rel["evidence"] == "confirmed"                                  # quote verified
    assert rel["address"][0]["evidence"] == "inferred"                     # a decision, never a fact
    assert any(g["jp"] == "灯火" for g in bible["glossary"])
    assert not any(g["jp"] == "魔王" for g in bible["glossary"])
    assert lock_established(bible) >= 1 and next(g for g in bible["glossary"] if g["jp"] == "灯火")["locked"]
