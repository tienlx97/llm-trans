# Project progress

## Current state

- Pipeline: Japanese to English/Vietnamese; EPUB and Syosetu/Kakuyomu input.
- Previous work: incremental translation, bilingual readmes and coding harness.
- Current change: remove all dedicated memory integrations at the user's request.
- Harness retains instructions, feature state, verification evidence and handoff files.
- Startup: `python scripts/harness.py start`.
- Verification: `python scripts/harness.py verify --profile python`.

## Session record — 2026-11-17

- Removed storage adapters, memory CLI/profile, setup config and related tests/docs.
- Existing translation changes preserved.
- Verification passed: doctor + full pytest, 81 passed, 2 skipped.
- Evidence: `.harness/runs/20261117T022911-f271223c/report.json`.
- Next action: continue user-requested work using repository notes directly.
