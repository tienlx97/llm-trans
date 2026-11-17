"""Repository checks, source-bound evidence and feature state for coding agents."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.Deepseek.common.atomic_io import atomic_write_json

STATUSES = {"not_started", "in_progress", "blocked", "passing"}
KNOWN_CHECKS = {"doctor", "python-tests", "ts-typecheck", "ts-tests"}
# These are updated after checks and are not source code under test.
STATE_FILES = {"harness/features.json", "harness/progress.md", "harness/handoff.md"}


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if result.returncode:
        raise RuntimeError("git failed: " + " ".join(args))
    return result.stdout.decode("utf-8", errors="replace")


def source_digest(root: Path) -> str:
    """Hash tracked and unignored new files, including deletions and dirty changes."""
    paths = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    digest = hashlib.sha256()
    for relative in sorted(set(paths) - STATE_FILES - {""}):
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<deleted>")
        digest.update(b"\0")
    return digest.hexdigest()


def feature_list(root: Path) -> dict:
    data = json.loads((root / "harness/features.json").read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("features"), list):
        raise ValueError("Invalid feature file schema")
    ids = set()
    for feature in data["features"]:
        name = feature.get("id")
        if not isinstance(name, str) or not name or name in ids:
            raise ValueError("Feature IDs must be nonempty and unique")
        ids.add(name)
        if feature.get("status") not in STATUSES:
            raise ValueError(f"Invalid feature status: {name}")
        if not feature.get("acceptance") or not feature.get("required_checks"):
            raise ValueError(f"Missing acceptance/required checks: {name}")
        if not set(feature["required_checks"]).issubset(KNOWN_CHECKS):
            raise ValueError(f"Unknown required check: {name}")
        if feature["status"] == "passing" and not feature.get("evidence"):
            raise ValueError(f"Passing feature has no evidence: {name}")
    if sum(f["status"] == "in_progress" for f in data["features"]) > 1:
        raise ValueError("Keep at most one feature in_progress")
    return data


def doctor(root: Path) -> dict:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10+ required")
    for relative in ("AGENTS.md", "CLAUDE.md", "harness/README.md", "harness/progress.md", "harness/handoff.md"):
        if not (root / relative).is_file():
            raise ValueError(f"Missing harness file: {relative}")
    data = feature_list(root)
    return {"python": sys.version.split()[0], "features": len(data["features"])}


def run_check(root: Path, name: str, argv: list[str], cwd: Path, timeout: float = 600) -> dict:
    start = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout)
        code = result.returncode
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        code = 124
        output = ((exc.stdout or b"") + (exc.stderr or b"")).decode("utf-8", errors="replace")
        output += "\nCheck timed out.\n"
    except OSError as exc:
        code = 127
        output = f"Could not start {name}: {type(exc).__name__}\n"
    return {"name": name, "argv": argv, "cwd": str(cwd.relative_to(root)) or ".",
            "exit_code": code, "seconds": round(time.monotonic() - start, 3), "output": output}


def verify(root: Path, profile: str = "python") -> tuple[dict, Path]:
    if profile not in {"python", "all"}:
        raise ValueError("Unsupported verification profile")
    before = source_digest(root)
    checks = []
    try:
        result = doctor(root)
        checks.append({"name": "doctor", "exit_code": 0, "output": json.dumps(result)})
    except (ValueError, OSError, RuntimeError) as exc:
        checks.append({"name": "doctor", "exit_code": 1, "output": str(exc)})
    checks.append(run_check(root, "python-tests", [sys.executable, "-m", "pytest", "-q"], root))
    if profile == "all":
        npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm") or "npm"
        checks.append(run_check(root, "ts-typecheck", [npm, "run", "typecheck"], root / "ts"))
        checks.append(run_check(root, "ts-tests", [npm, "test"], root / "ts"))
    after = source_digest(root)
    stamp = datetime.now(timezone.utc).isoformat()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    path = root / ".harness/runs" / run_id / "report.json"
    report = {"schema_version": 1, "run_id": run_id, "created_at": stamp,
              "profile": profile, "head": git(root, "rev-parse", "HEAD").strip(),
              "source_digest": before, "source_digest_after": after,
              "passed": before == after and all(c["exit_code"] == 0 for c in checks),
              "checks": checks}
    atomic_write_json(path, report)
    return report, path


def set_feature(root: Path, feature_id: str, status: str, evidence: Path | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError("Invalid feature status")
    data = feature_list(root)
    feature = next((f for f in data["features"] if f["id"] == feature_id), None)
    if feature is None:
        raise ValueError(f"Unknown feature: {feature_id}")
    if status == "in_progress" and any(f["status"] == "in_progress" and f is not feature for f in data["features"]):
        raise ValueError("Another feature is already in_progress")
    if status == "passing":
        if evidence is None:
            raise ValueError("Passing requires --evidence from a successful verify run")
        path = evidence if evidence.is_absolute() else root / evidence
        report = json.loads(path.read_text(encoding="utf-8"))
        checks = report.get("checks", [])
        passed = {c["name"] for c in checks if c.get("exit_code") == 0}
        if (report.get("schema_version") != 1 or report.get("passed") is not True
                or any(c.get("exit_code") != 0 for c in checks)
                or not set(feature["required_checks"]).issubset(passed)):
            raise ValueError("Evidence failed or is missing required checks")
        current = source_digest(root)
        if report.get("source_digest") != current or report.get("source_digest_after") != current:
            raise ValueError("Evidence is stale: source changed; run verify again")
        feature["evidence"] = [{"run_id": report["run_id"], "created_at": report["created_at"],
                                "source_digest": current, "checks": sorted(passed),
                                "report": str(path.resolve().relative_to(root.resolve())).replace("\\", "/")}]
    feature["status"] = status
    atomic_write_json(root / "harness/features.json", data)
    return feature
