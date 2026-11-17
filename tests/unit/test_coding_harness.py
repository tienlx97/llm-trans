"""Offline tests of harness evidence enforcement and supported commands."""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import harness as cli
from src.harness import workflow

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "harness").mkdir()
    for relative in ("AGENTS.md", "CLAUDE.md", "README.md", "READ_ME.vi.md", "ARCHITECTURE.md",
                     "harness/README.md", "harness/decisions.md", "harness/progress.md", "harness/handoff.md",
                     "harness/features.json"):
        shutil.copyfile(REPO / relative, root / relative)
    # Fixtures must not inherit a previous session's completed feature state.
    features_path = root / "harness/features.json"
    data = json.loads(features_path.read_text(encoding="utf-8"))
    for index, feature in enumerate(data["features"]):
        feature["status"] = "in_progress" if index == 0 else "not_started"
        feature["evidence"] = []
    extra = copy.deepcopy(data["features"][0])
    extra.update(id="test-feature", status="not_started", evidence=[])
    data["features"].append(extra)
    features_path.write_text(json.dumps(data), encoding="utf-8")
    (root / ".gitignore").write_text(".harness/\n", encoding="utf-8")
    (root / "example.py").write_text("answer = 42\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root



def test_feature_completion_rejects_failed_missing_and_stale_evidence(project):
    current = workflow.source_digest(project)
    report = {"schema_version": 1, "passed": True, "source_digest": current,
              "source_digest_after": current, "run_id": "test", "created_at": "test-time",
              "checks": [{"name": "doctor", "exit_code": 0}, {"name": "python-tests", "exit_code": 0}]}
    evidence = project / ".harness/report.json"
    evidence.parent.mkdir()
    with pytest.raises(ValueError, match="requires"):
        workflow.set_feature(project, "coding-harness", "passing")
    failed = copy.deepcopy(report)
    failed["checks"][1]["exit_code"] = 1
    evidence.write_text(json.dumps(failed), encoding="utf-8")
    with pytest.raises(ValueError, match="failed"):
        workflow.set_feature(project, "coding-harness", "passing", evidence)
    missing = copy.deepcopy(report)
    missing["checks"] = missing["checks"][:1]
    evidence.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        workflow.set_feature(project, "coding-harness", "passing", evidence)
    evidence.write_text(json.dumps(report), encoding="utf-8")
    (project / "example.py").write_text("answer = 43", encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        workflow.set_feature(project, "coding-harness", "passing", evidence)
    (project / "example.py").write_text("answer = 42\n", encoding="utf-8")
    assert workflow.set_feature(project, "coding-harness", "passing", evidence)["status"] == "passing"
    assert workflow.doctor(project)["features"] == 2
    # State changes after verification do not invalidate the source snapshot.
    assert workflow.source_digest(project) == current


def test_verify_records_failure_without_marking_features_passing(project, monkeypatch):
    original_git = workflow.git
    monkeypatch.setattr(workflow, "git", lambda root, *args: "test-head" if args == ("rev-parse", "HEAD")
                        else original_git(root, *args))
    monkeypatch.setattr(workflow, "run_check", lambda root, name, argv, cwd: {
        "name": name, "argv": argv, "exit_code": 1, "output": "regression found"})
    report, path = workflow.verify(project)
    assert report["passed"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["checks"][1]["exit_code"] == 1
    assert workflow.feature_list(project)["features"][0]["status"] == "in_progress"


def test_doctor_rejects_passing_without_evidence_and_parallel_active_features(project):
    path = project / "harness/features.json"
    data = workflow.feature_list(project)
    data["features"][0]["status"] = "passing"
    data["features"][0]["evidence"] = []
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="no evidence"):
        workflow.doctor(project)
    data["features"][0]["status"] = "in_progress"
    data["features"][1]["status"] = "in_progress"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="one feature"):
        workflow.doctor(project)


def test_removed_commands_and_profiles_are_rejected():
    for arguments in (["memory", "health"], ["verify", "--profile", "memory"], ["verify", "--profile", "live"]):
        with pytest.raises(SystemExit):
            cli.parser().parse_args(arguments)
