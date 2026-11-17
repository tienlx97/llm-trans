#!/usr/bin/env python
"""Coding-agent harness. Uses stdlib only; never invokes translation or model APIs implicitly."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.harness.workflow import STATUSES, doctor, feature_list, git, set_feature, verify


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="Read local state, validate setup, show the existing diff")
    sub.add_parser("doctor", help="Validate repository harness; no network or dependency installation")
    check = sub.add_parser("verify", help="Run offline checks and save source-bound evidence")
    check.add_argument("--profile", choices=("python", "all"), default="python")
    feature = sub.add_parser("feature", help="Update feature status; passing requires current evidence")
    feature.add_argument("id")
    feature.add_argument("--status", choices=sorted(STATUSES), required=True)
    feature.add_argument("--evidence", type=Path)
    return result


def run(args: argparse.Namespace, root: Path = ROOT) -> int:
    if args.command in {"start", "doctor"}:
        print(json.dumps(doctor(root), ensure_ascii=False, indent=2))
        if args.command == "start":
            print("\nExisting working tree (preserve these changes):")
            print(git(root, "status", "--short"))
            for relative in ("harness/progress.md", "harness/handoff.md"):
                print((root / relative).read_text(encoding="utf-8"))
            print("Features:")
            for feature in feature_list(root)["features"]:
                print(f"  {feature['id']}: {feature['status']} — {feature['title']}")
        return 0
    if args.command == "verify":
        report, path = verify(root, args.profile)
        for check in report["checks"]:
            print(f"[{check['name']}] exit={check['exit_code']}")
            print(check["output"])
        print(f"Evidence: {path}")
        print("PASS" if report["passed"] else "FAIL (inspect evidence; source may have changed during checks)")
        return 0 if report["passed"] else 1
    if args.command == "feature":
        result = set_feature(root, args.id, args.status, args.evidence)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    # Entry-point-only UTF-8 setup, without importing pipeline config or loading .env.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        return run(parser().parse_args(argv))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Harness: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
