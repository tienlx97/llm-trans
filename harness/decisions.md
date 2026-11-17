# Project decisions

## Development workflow — 2026-11-17

The user requested removal of all dedicated memory integrations. Session
continuity uses repository progress, feature state, decisions and handoff files.
No memory database, index, model, API credential or service is required.

The coding harness retains startup validation, source-bound verification reports,
feature acceptance criteria and explicit handoffs. It remains independent of
translation prompts and providers.

## Translation continuity

Python owns orchestration. Each workspace records its target language.
Vietnamese names, address pairs and terms live in the volume bible, with separate
series inheritance. Incremental mode analyzes and translates pending windows,
using per-chapter hashes to resume analysis. Download and prep retain their
existing source selection.
