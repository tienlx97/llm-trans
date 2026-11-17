"""Offline regression coverage for resumable bible analysis and chapter windows."""

import json

import pytest

from helpers import make_workspace
from scripts import mtl
from src.utility.vietnamese import analyze, assistant, bible as vb


class Recorder:
    def __init__(self, fail_at=None, dry_run=False):
        self.calls = []
        self.fail_at = fail_at
        self.dry_run = dry_run

    def complete_json(self, **kwargs):
        self.calls.append(kwargs["user"])
        if len(self.calls) == self.fail_at:
            raise RuntimeError("interrupted")
        return {"dry_run": True} if self.dry_run else {}


def test_analysis_checkpoint_survives_failure_and_reseed(work_dir):
    wd = make_workspace(work_dir, chapters={"CHAPTER_1": "one", "CHAPTER_2": "two"})
    recorder = Recorder(fail_at=2)
    with pytest.raises(RuntimeError, match="interrupted"):
        analyze.analyze_volume(wd, recorder, batch_chars=3)
    assert set(vb.load_volume_bible(wd)["analysis_chapters"]) == {"CHAPTER_1"}
    vb.seed_volume_bible(wd)
    recorder = Recorder()
    analyze.analyze_volume(wd, recorder)
    assert len(recorder.calls) == 1
    assert "id='CHAPTER_1'" not in recorder.calls[0]
    assert "id='CHAPTER_2'" in recorder.calls[0]
    analyze.analyze_volume(wd, recorder)
    assert len(recorder.calls) == 1


def test_changed_source_force_empty_selection_and_dry_run(work_dir):
    wd = make_workspace(work_dir, chapters={"CHAPTER_1": "one", "CHAPTER_2": "two"})
    analyze.analyze_volume(wd, Recorder())
    before = vb.volume_bible_path(wd).read_bytes()
    (wd / "JP" / "CHAPTER_2.md").write_text("updated", encoding="utf-8")
    recorder = Recorder(dry_run=True)
    analyze.analyze_volume(wd, recorder)
    assert "id='CHAPTER_1'" not in recorder.calls[0]
    assert vb.volume_bible_path(wd).read_bytes() == before
    recorder = Recorder()
    analyze.analyze_volume(wd, recorder, chapters=[])
    assert recorder.calls == []
    analyze.analyze_volume(wd, recorder)
    assert len(recorder.calls) == 1 and "updated" in recorder.calls[0]
    analyze.analyze_volume(wd, recorder, force=True)
    assert "id='CHAPTER_1'" in recorder.calls[-1]
    assert "id='CHAPTER_2'" in recorder.calls[-1]


def _fake_translate(wd, events, fail_on=None):
    def translate(volume_id, *, chapters, **kwargs):
        events.append(("translate", list(chapters)))
        if fail_on and fail_on in chapters:
            raise RuntimeError("quota")
        data = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
        for entry in data["chapters"]:
            if entry["source_file"].removesuffix(".md") in chapters:
                entry["translation_status"] = "completed"
        (wd / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        return {cid: wd / f"{cid}.md" for cid in chapters}
    return translate


def test_windows_interleave_in_story_order_and_resume(work_dir, monkeypatch):
    wd = make_workspace(work_dir, chapters={"CHAPTER_100": "three", "CHAPTER_9": "one", "CHAPTER_10": "two"})
    events = []
    recorder = Recorder()
    monkeypatch.setattr(assistant, "make_assistant", lambda *a, **kw: recorder)
    original = analyze.analyze_volume

    def record_analysis(*args, **kwargs):
        events.append(("analyze", kwargs["chapters"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(analyze, "analyze_volume", record_analysis)
    monkeypatch.setattr(mtl, "_resolve_translate_volume", lambda p: _fake_translate(wd, events, "CHAPTER_100"))
    with pytest.raises(RuntimeError, match="quota"):
        mtl._translate_incrementally(wd.name, "claude", 2, None)
    assert events == [
        ("analyze", ["CHAPTER_9", "CHAPTER_10"]),
        ("translate", ["CHAPTER_9", "CHAPTER_10"]),
        ("analyze", ["CHAPTER_100"]), ("translate", ["CHAPTER_100"])]
    calls = len(recorder.calls)
    events.clear()
    monkeypatch.setattr(mtl, "_resolve_translate_volume", lambda p: _fake_translate(wd, events))
    mtl._translate_incrementally(wd.name, "claude", 2, None)
    assert events == [("analyze", ["CHAPTER_100"]), ("translate", ["CHAPTER_100"])]
    assert len(recorder.calls) == calls  # analysis committed before translation failed


def test_unfinished_window_stops_before_next_analysis(work_dir, monkeypatch):
    wd = make_workspace(work_dir)
    recorder = Recorder()
    monkeypatch.setattr(assistant, "make_assistant", lambda *a, **kw: recorder)
    monkeypatch.setattr(mtl, "_resolve_translate_volume", lambda p: lambda *a, **kw: {})
    with pytest.raises(RuntimeError, match="unfinished"):
        mtl._translate_incrementally(wd.name, "claude", 1, None)
    assert len(recorder.calls) == 1


@pytest.mark.parametrize("command", ["run", "resume", "import-url"])
def test_incremental_cli_flags(command):
    args = mtl.build_parser().parse_args([command, "input", "--incremental", "--window", "1"])
    assert args.incremental and args.window == 1
    with pytest.raises(SystemExit):
        mtl.build_parser().parse_args([command, "input", "--window", "0"])


def test_pipeline_suppresses_full_analysis_even_after_prep(work_dir, monkeypatch):
    wd = make_workspace(work_dir)
    hooks = []
    monkeypatch.setattr(mtl, "run_prep", lambda *a, **kw: {"blocks_populated": [], "blocks_pending": []})
    monkeypatch.setattr(mtl, "_after_prep", lambda *a, **kw: hooks.append(kw))

    def stopped(*args):
        raise RuntimeError("quota")

    monkeypatch.setattr(mtl, "_translate_incrementally", stopped)
    monkeypatch.setattr(mtl, "run_qc", lambda *a: pytest.fail("must not QC after interruption"))
    assert mtl._continue_pipeline(wd.name, provider="claude", series_id=None,
                                  thinking_log_enabled=None, incremental=True) == 1
    assert hooks == [{"analyze": False}]
