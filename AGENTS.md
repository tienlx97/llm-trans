# Coding-agent harness

This repository translates Japanese light novels. The harness in `harness/`
governs development of the project, not translation prompts.

## Start a session

1. Read `harness/progress.md`, `harness/features.json` and `harness/handoff.md`.
2. Run `python scripts/harness.py start` using the development venv.
   Inspect the existing diff; preserve user changes and prior unfinished work.
3. Read relevant repository files and verify historical notes against current code.
4. Select the user-requested scope and define observable acceptance checks.
   Keep root instructions short; use `harness/README.md` for procedures.

## Work

- Python owns the pipeline. `ts/` is a client console; do not duplicate
  orchestration in the UI. Each provider owns its native route.
- Preserve English behavior and workspace-bound target languages. Vietnamese
  bible/checkpoints remain the authority for translation decisions.
- Keep credentials, novels, translation workspaces and full chat transcripts
  out of tracked development documents.
- Use relevant offline tests; model calls, network tests, and external services
  are separate checks. Report skipped or unavailable checks explicitly.
- Do not reset, stash, commit, push, install global tools, or change unrelated
  configuration simply to make the working tree appear clean.

## Finish a session

1. Run `python scripts/harness.py verify --profile python`. For console changes,
   use `--profile all` after installing `ts/` dependencies.
2. Inspect the diff. Feature completion requires a successful evidence report
   for the current source snapshot; use `feature ... --status passing` with
   `--evidence`.
3. Update `harness/progress.md` and `harness/handoff.md` with changes, evidence,
   limitations, and the next useful action. Preserve unrelated pending changes.
4. Tell the user what changed, what was checked, and what remains unverified.

## graphify

When the user types `/graphify`, read the installed graphify skill before any
other action. Use the existing graph if available; do not rebuild it implicitly.
