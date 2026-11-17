# LLM Translator - DeepSeek/Qwen/OpenAI/Anthropic/Gemini-Powered

[English](README.md) | [Tiếng Việt](READ_ME.vi.md)

**Selectable-provider pipeline** for Japanese → English and Japanese → Vietnamese
light novel translation. Extract (an EPUB, or a Syosetu/Kakuyomu URL), prep,
translate, QC, build — and remember the series for next time.

Phase 2 selects one isolated translation route through `translation.provider`:
`deepseek`, `qwen`, `openai`, `anthropic`, or `gemini` (plus `codex` / `claude`
for the signed-in subscription CLIs). Each route owns its native
client, prompt, conversation ledger, cache policy, continuation behavior, and
error handling; selecting one does not silently alter any of the others.

Signed-in **Codex CLI** and **Claude Code** are also available through
`--provider codex` / `--provider claude`. For long Vietnamese web novels,
`--incremental --window 20` updates the character/terminology bible and translates
one group at a time; see [incremental translation](#incremental-vietnamese-translation-for-long-web-novels).

A content-safety refusal mid-volume does not end the run: the chapter is
handed to DeepSeek with the source provider's already-established translation
decisions inherited rather than reinvented. See
[Safety-Refusal Fallback](#safety-refusal-fallback).

---

## Navigation

- [LLM Translator - DeepSeek/Qwen/OpenAI/Anthropic/Gemini-Powered](#llm-translator---deepseekqwenopenaianthropicgemini-powered)
  - [Navigation](#navigation)
  - [What It Is](#what-it-is)
  - [What It Is NOT](#what-it-is-not)
  - [Quick Start](#quick-start)
  - [Architecture](#architecture)
    - [Providers](#providers)
    - [OpenAI Responses — Application and Cache Telemetry](#openai-responses--application-and-cache-telemetry)
    - [Proofreading Mode — Powered by Anthropic's Advisor Tool](#proofreading-mode--powered-by-anthropics-advisor-tool)
    - [Safety-Refusal Fallback](#safety-refusal-fallback)
    - [Prep — Cached Multi-turn Path](#prep--cached-multi-turn-path)
    - [Prep — OpenAI Responses / Batch Path](#prep--openai-responses--batch-path)
    - [Series Continuity — The Bible Writer](#series-continuity--the-bible-writer)
  - [Japanese → Vietnamese](#japanese--vietnamese)
    - [Windows quick start (Vietnamese)](#windows-quick-start-vietnamese)
    - [Input sources: EPUB, web novel URL, existing workspace](#input-sources-epub-web-novel-url-existing-workspace)
    - [Incremental Vietnamese translation for long web novels](#incremental-vietnamese-translation-for-long-web-novels)
    - [After the first run: check, correct, continue](#after-the-first-run-check-correct-continue)
    - [Without an API key: Codex CLI or Claude Code](#without-an-api-key-codex-cli-or-claude-code)
    - [How a workspace gets its language](#how-a-workspace-gets-its-language)
    - [Vietnamese Series Bible and xưng hô](#vietnamese-series-bible-and-xưng-hô)
    - [Vietnamese QC and selective repair](#vietnamese-qc-and-selective-repair)
    - [Vietnamese EPUB fonts](#vietnamese-epub-fonts)
    - [Web novel import (Syosetu / Kakuyomu)](#web-novel-import-syosetu--kakuyomu)
    - [Resume, truncation and dry runs](#resume-truncation-and-dry-runs)
    - [What is not supported yet](#what-is-not-supported-yet)
  - [Configuration](#configuration)
  - [File Structure](#file-structure)
  - [CLI Reference](#cli-reference)
  - [Coding-agent harness](#coding-agent-harness)
  - [Testing](#testing)
  - [MCP Tools](#mcp-tools)
  - [Requirements](#requirements)
  - [context.xml](#contextxml)

---

## What It Is

A self-contained translation pipeline that ingests a Japanese EPUB (or a
Syosetu/Kakuyomu web novel), builds a full character/voice/continuity
reference through one cache-oriented prep pass, translates every chapter
through a high-capacity language model with prefix caching, runs a QC gate,
and outputs a finished English or Vietnamese EPUB. On a sequel volume, it
recalls the previous volume's names and voices without being told twice.

## What It Is NOT

- It does **not** require vector stores or retrieval-augmented modules at
  runtime — the translation prompt carries all policy inline, and preparation
  is a bounded multi-turn metadata conversation, not a multi-phase pipeline.
- It does **not** run multi-model quality-evaluation fan-out — the post-
  translation gate is filesystem-only sanity checks with zero API cost.
- It does **not** maintain a publisher/author/anime-metadata database — series
  continuity is three flat JSON files (name locks, recurring phrase anchors,
  cross-volume state), a continuity aid, not a database.
- It does **not** mix translation providers within a volume by default —
  every chapter goes through the one configured `translation.provider` unless
  a content-safety refusal triggers the DeepSeek fallback. Prep has its own
  switch (`prep.provider`), so prep and translation can use different
  providers (the shipped config preps on OpenAI and translates on Anthropic).

## Quick Start

```bash
# 1. Clone or copy into LLM Translator/
cd LLM Translator

# 2. Create a Python venv
python -m venv venv
venv\Scripts\activate   # Windows
# source venv/bin/activate   # Linux/macOS

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Set your API key(s) in .env, matching whichever provider(s) you use:
#    DEEPSEEK_API_KEY, DASHSCOPE_API_KEY (Qwen), OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY
#    (or no key at all with --provider codex / --provider claude, see
#    "Without an API key" below)

# 5. Drop an EPUB in raw/
copy "C:\path\to\your-light-novel.epub" raw\      # Windows
# cp /path/to/your-light-novel.epub raw/            # Linux/macOS

# 6. Run the full pipeline
python scripts/mtl.py run raw/your-light-novel.epub

# Or step by step:
python scripts/mtl.py extract raw/your-light-novel.epub   # -> work/<vol_id>/JP/ + barebone context.xml
python scripts/mtl.py prep <vol_id>                        # -> context.xml via a cached multi-turn prep conversation
python scripts/mtl.py translate <vol_id>                   # -> work/<vol_id>/EN/
python scripts/mtl.py qc <vol_id>                           # -> QC report (stdout, zero API cost)
python scripts/mtl.py build <vol_id>                        # -> output/<vol_id>.epub
```

`--dry-run` on `translate`/`build` (and the equivalent MCP tool flags) assembles
the exact request payload without sending it or spending anything — no `EN/`
output, no manifest changes.

Writing the series bible (so the *next* volume in the series detects the
sequel automatically) is an MCP/IDE-agent action, not a CLI step — see
[Series Continuity](#series-continuity--the-bible-writer) below.

A TypeScript Ink/React terminal console (`ts/`) is available as an
alternative operator UI — `npm install` inside that directory, then
`mtl-ts.bat` / `mtl-ts.sh` from the project root.

---

## Architecture

```
raw/                          ← Drop EPUBs here
    │
    ▼
[Phase 1: Librarian]          ← src/utility/librarian/ (deterministic, zero LLM calls)
    JP chapter .md extraction, TOC parsing, illustration export
    Writes a barebone context.xml + manifest.json
    │
    ▼
[Prep: Cached Multi-turn]     ← src/utility/prep/
    One stable system prefix: prep policy + context shell + full JP source + bible + search evidence
    Sixteen JSON-node turns assembled deterministically into context.xml
    │
    ▼
[Phase 2: Translator]         ← src/Deepseek/, src/Qwen/, src/OpenAI/, src/Anthropic/, src/Gemini/, src/CLI/
    One isolated route per provider — own client, prompt, conversation ledger, cache policy
    (src/CLI/ drives the signed-in Codex CLI / Claude Code instead of an API)
    │
    ▼
[QC Gate]                     ← src/utility/qc/ (EN: filesystem-only) · src/utility/vietnamese/ (VI)
    Completeness, truncation, token-count outliers, name-spelling drift, structural checks
    VI adds locked-term/pronoun checks and an optional LLM semantic review (qc --llm)
    │
    ▼
[Bible Writer]                ← reads populated context.xml
    Merges into bibles/<series_id>/{term_lock,verbatim_anchors,series_pack}.json
    │
    ▼
[Phase 4: Builder]            ← src/builder/ (deterministic, zero LLM calls)
    EPUB3 packaging, XHTML, NCX/TOC
    │
    ▼
output/<vol_id>.epub          ← Finished English light novel (Vietnamese: output/<title>_VI.epub)
```

### Providers

llm-LN-translator runs Phase 2 on frontier-class LLMs, one isolated route per
provider, selected by `translation.provider`. Model IDs, context limits, and
reasoning settings change with every provider release, so this README does not
pin them. `config.yaml` is the source of truth for what each route is currently
configured to call.

| Provider | Route | Specialized tooling |
|---|---|---|
| DeepSeek | `src/Deepseek/` | DRDI: EPS-band reasoning directives injected into the user turn, standing in for a native effort parameter |
| Qwen | `src/Qwen/` | Native thinking with a configurable token budget |
| OpenAI | `src/OpenAI/` | Native Responses API (not a compatibility wrapper) with encrypted-reasoning replay and cache telemetry. See [OpenAI Responses](#openai-responses--application-and-cache-telemetry) |
| Anthropic | `src/Anthropic/` | **Proofreading Mode** (Anthropic's beta advisor tool) as an in-loop proofreader and validator, plus opt-in `web_search`. See [Proofreading Mode](#proofreading-mode--powered-by-anthropics-advisor-tool) |
| Google Gemini | `src/Gemini/` | Interactions API server-side conversation with implicit caching; Long Context + Long Decode (background, >64K output) on Gemini 4 Argon |
| Codex CLI / Claude Code | `src/CLI/` | `--provider codex` / `--provider claude`: drives the signed-in CLI non-interactively and bills your ChatGPT/Claude plan, no API key. See [Without an API key](#without-an-api-key-codex-cli-or-claude-code) |

The shipped `config.yaml` sets `translation.provider: anthropic` and
`prep.provider: openai`; `--provider` / `--prep-provider` override either one
for a single run.

Every route receives the same prepared `context.xml` (voice guidance,
terminology locks, scene/emotional guidance) and enforces the same
source-first contract; only the transport, cache policy, and reasoning
control differ, per provider API.

### OpenAI Responses — Application and Cache Telemetry

The OpenAI route is a native Responses API implementation under `src/OpenAI/`,
not an OpenAI-compatible wrapper around another provider. `agent.py` assembles
chapter requests and continuations, `client.py` owns the wire payload and cache
monitor, `conversation.py` maintains local replay state, and `response.py`
normalizes Responses output and usage into the shared MTLS boundary types.

**Request and conversation shape.** The route uses `store: false` and persists
the complete replay ledger at the configured path. Astra and fallback profiles use
model-scoped ledgers such as `.context/openai_conversation_gpt_6_astra.json`. Every request
starts with a developer message containing the OpenAI master prompt plus the
volume's prepared `context.xml`, followed by up to two recent chapter turns and
the current Japanese chapter. Replay retains complete output items—including
encrypted reasoning content required by the next Responses turn—while only
visible output text is written as translated prose.

**Explicit fixed-prefix caching.** The default cache policy is `explicit` with
a 30-minute TTL. MTLS adds one explicit breakpoint to the developer
`input_text` block and sends a deterministic `prompt_cache_key` derived from
the configured model, prompt profile, and that complete developer prefix. The key contains no
title or user-identifying text and changes automatically when the model,
master prompt, or injected context changes. Conversation replay and the
current chapter come after the breakpoint, so they remain ordinary input under
this policy. Consequently, low total cache coverage can be normal even when
the intended breakpoint is recovered perfectly.

OpenAI cache reads and writes are provider-side facts. MTLS reads
`usage.input_tokens_details.cached_tokens` and `cache_write_tokens` from the
API response; local tokenization cannot reconstruct either value.

| OpenAI metric | Calculation | Interpretation |
|---|---|---|
| **Breakpoint Success Rate** | Successful recoveries ÷ eligible repeat calls | Health of the explicit developer-prefix breakpoint. The cold first call is excluded. |
| **Prefix Recovery** | Recovered expected-prefix tokens ÷ total expected-prefix opportunity tokens | Weighted measure of how completely the fixed prefix was recovered. |
| **Total Cache Coverage** | Cache-read tokens ÷ all input tokens | Cost-coverage information, not an OpenAI health warning. |
| **Cache Economics** | Read, write, and ordinary input tokens plus baseline cost, actual cost, and net savings | Shows whether later reads repay the priced cache write. Also reports the read-to-write token ratio. |

The OpenAI monitor warns only when **Breakpoint Success Rate** or **Prefix
Recovery** falls below `0.90`, after at least two eligible repeat calls. It does
not apply the `0.70` total-hit threshold used by growing-prefix provider routes.
A healthy fixed-prefix run may therefore report 100% breakpoint success, 100%
prefix recovery, and only about 15% total coverage; those values describe a
narrow but fully functioning cache rather than a cache failure.

Astra remains selectable in `config.yaml`; GPT-6 Sol is the default and GPT-6
Luna is its fallback. An eligible access or transport failure can pin the
volume to Luna. The checked-in `allow_mixed_model_volume: true` permits a
chapter-boundary switch after committed Sol chapters, with a model-specific
conversation ledger. Set it to `false` to record `fallback_pending` instead;
`resume_pending: true` then explicitly resumes that boundary on Luna.

**Artifacts.** Each OpenAI translator call adds cache reads, cache writes,
ordinary input, output, the three cumulative ratios, net cache savings, and
total estimated cost to `LOG/token_log_<run-stamp>.md`. Credential-free dry
runs report the cache mode, a SHA-256 digest of the key, and explicit breakpoint
count without exposing the key itself. Dry runs remain single-turn and make no
API call, so they cannot report cache hits, writes, or billed savings.

Logs created before `cache_write_tokens` telemetry was added retain valid cache
read counts, but their write cost and net savings cannot be reconstructed
exactly. Use a new live run when billing-accurate cache economics are required.

**OpenAI Batch.** `translation.openai.batch.enabled` is on by default for the
GPT-6 family. Set it to `false` to force synchronous Responses calls. The
checked-in translation route uses `gpt-6-sol`, with `gpt-6-luna` as fallback.

Batch requests follow the Anthropic Fable 5.1 wave pattern while retaining
OpenAI's persistent Responses reasoning ledger. Each wave freezes one replay
prefix, submits chapter requests as JSONL to `/v1/responses`, and commits
completed raw output (including encrypted reasoning items) in chapter order
before the next wave is assembled. Chapters in the same wave intentionally do
not see one another; lower `batch.wave_size` when continuity matters more than
throughput. A one-chapter seed wave (`seed_chapters: 1`) establishes the first
translation decisions without abandoning Batch pricing.

`batch.persistence_file` stores submitted job IDs and file IDs atomically. An
interrupted run drains that ledger before submitting new work, so it does not
duplicate accepted jobs. Missing or transient result lines remain pending for a
later retry; model-access failures may switch at a clean boundary to the
configured GPT-6 Luna fallback. If a primary-model chapter has already committed and
mixed-model continuation is disabled, the route records `fallback_pending`
instead of silently combining incompatible reasoning state.

### Proofreading Mode — Powered by Anthropic's Advisor Tool

**Proofreading Mode** is the formal name for `translation.anthropic.advisor` —
the label an operator actually sees, in this README and in the TypeScript
console's Configuration screen (`Translation → Anthropic → Proofreading Mode`)
alike. Under the hood it is powered by Anthropic's beta `advisor_20260301`
tool: a second, higher-intelligence model consulted mid-generation by the
executor itself, mid-turn, on its own judgment of need — not a second pass,
not a separate QC stage. One consult serves both purposes Runs 1–11 validated
independently and then confirmed work together in the same call: real-world
reference grounding (does a claimed subculture reference, public figure, or
historical detail actually check out) and craft/proofreading judgment
(name-form drift, locked-anchor collisions, register calls the executor is too
close to its own prose to catch). `enabled: true` by default — eleven manual
dry runs closed out the validation this ships on. The plan and spec they
followed (`docs/anthropic-advisor-mode-plan.md`,
`docs/anthropic-advisor-mode-spec.md`) are referenced by the config comments
but are not included in this repository.

**Current configuration.** `config.yaml` ships with executor
`translation.anthropic.model: claude-sonnet-5-5` and advisor
`advisor.model: claude-opus-5-5`, with `advisor.enabled: true`. On a
`claude-sonnet-5-5` executor, every advisor result comes back encrypted
(`advisor_redacted_result`), whichever advisor is picked. The executor forces
this, not the advisor.

**Model choice.** `advisor.model` picks the model that gives the advice. The
pairing should be listed for the configured executor in
`ADVISOR_COMPATIBILITY` (`src/Anthropic/client.py`), which mirrors Anthropic's
executor/advisor compatibility table:

| Executor (`model`) | Valid advisors | Result type |
|---|---|---|
| `claude-sonnet-5-5` (current) | `claude-opus-5-5` (current), `claude-fable-5-1`, `claude-opus-5`, `claude-sonnet-5-5`, `claude-fable-5`, `claude-mythos-5`, `claude-mythos-5-1` | Always encrypted |
| `claude-sonnet-5` | `claude-opus-4-8`, `claude-sonnet-5`, `claude-opus-4-7`, `claude-opus-5`, `claude-opus-5-5`, `claude-fable-5`, `claude-fable-5-1`, `claude-mythos-5`, `claude-mythos-5-1` | Plaintext `advisor_result` for `claude-opus-4-8` / `claude-sonnet-5`; encrypted for the rest |
| `claude-opus-5-5` | `claude-opus-5-5` (confirmed in live use 2026-09-24, though Anthropic's table does not list it), `claude-fable-5-1`, `claude-mythos-5-1` | Encrypted |
| `claude-opus-5` | `claude-opus-5`, `claude-opus-5-5`, `claude-fable-5`, `claude-fable-5-1`, `claude-mythos-5`, `claude-mythos-5-1` | Encrypted |
| `claude-fable-5-1` | `claude-fable-5-1`, `claude-mythos-5-1` | Encrypted |

`claude-opus-4-8` is not used in the current configuration. It is the
plaintext advisor that Runs 8–11 validated, and it works **only** with a
`claude-sonnet-5` executor. `claude-sonnet-5-5` returns a 400 for Opus 4.8,
Opus 4.7, and Sonnet 5 advisors. To get readable plaintext advice back, set
`model: claude-sonnet-5` and `advisor.model: claude-opus-4-8`. For a Sonnet
executor, Anthropic recommends `claude-fable-5-1` as the advisor with the
largest quality gain.

An unlisted pairing is **not** refused. Startup logs a warning and lets the
API make the final call: Anthropic returns a 400 on the first request if it
rejects the pairing. The one advisor setting that still stops at startup is
an effort level below `"high"`.

**Economic gating.** `advisor.require_signal: true` (default) turns
Proofreading Mode on for a request only when the chapter actually carries a
`chapter_signals` risk entry — ambiguity, ateji, multi-speaker, voice-contrast,
or a subculture reference — combined with a WARM/HOT EPS band. EPS band alone
is deliberately not sufficient; this session's evidence measured that gate as
uneconomical and rejected it (`src/Anthropic/agent.py::_tools_qualify`,
`optimization.py::build_advisor_guidance`). `max_uses: 2` caps consults per
request; `max_tokens: 24000` bounds each consult's own output.

**Pause-turn resumption.** A deep consult can end the turn early with
`stop_reason: pause_turn` rather than completing generation. Both the
synchronous and Batch paths resend the unchanged assistant message
(`agent.py::_resolve_pauses`) until the turn actually finishes or
`max_pause_resumes` (default 2) is exhausted — content already written before
the pause is preserved and accumulated across every resumed response, never
discarded. Verified directly against Anthropic's Batch documentation: server
tools including `advisor` run inside Batch, and a batch-path pause is resolved
via one synchronous follow-up call per pause rather than a second batch job,
since Batch already runs more agentic-loop iterations before pausing than the
synchronous path does.

**Master-prompt coupling.** Proofreading Mode's craft judgment is measured
against the same standard the executor itself writes to: a
`<proofreading_discipline>` block in `master_prompt_anthropic_en.md` (a
sibling to `<anti_translationese>`) codifying recurring LLM-output patterns
this project's own QC audits have caught — em-dash overuse as a default
connector rather than a deliberate device, nonstandard ellipsis runs,
hedge-word monotony, reflexive "somehow" translations, italics doing double
duty for both interiority and emphasis, and uniform acknowledgment tags. The
advisor is directed to weigh in on these the same way it weighs in on
name-form drift or a locked-anchor collision.

**`web_search` (independent of Proofreading Mode).** A separate, non-beta
tool (`translation.anthropic.web_search`, type `web_search_20250305`) a
chapter can need independently of, alongside, or instead of Proofreading
Mode — Run 9 confirmed all four combinations behave correctly. The shipped
`config.yaml` has it **on** (`enabled: true`) and gated by the same
`require_signal` check as the advisor. Its $10/1,000-search cost and
query-level economy guardrail have not been validated to the standard
Proofreading Mode cleared, so set `enabled: false` if you want to avoid that
spend.

### Safety-Refusal Fallback

Any non-DeepSeek provider can refuse a chapter on content-safety grounds. The
refusal is treated as deterministic — retrying the identical payload against
the identical gate fails identically — so the route does not retry. Instead:

1. The refusal code is captured for the audit trail.
2. An **inheritance agent** (one DeepSeek call on the `prep` config) reads
   every EN chapter already produced and summarizes the decisions the source
   provider actually established — names, honorific practice as observed,
   landmarks, epithets, voice, terminology.
3. That summary is injected into a `<translation_inheritance>` block in
   `context.xml`, so the DeepSeek run that finishes the volume inherits those
   decisions instead of reinventing them.
4. If the inheritance step itself fails, the original refusal is re-raised
   rather than falling through to a cold translation — a run that stops is
   recoverable; a volume that silently drifts in terminology mid-book is not.

Every firing writes `work/<vol_id>/QC/inheritance_translator.json` — the exact
refusal, every chapter completed before it, and the governing config. Its
presence makes a cross-provider consistency copypass mandatory in QC.

On by default for every non-DeepSeek provider (`translation.safety_fallback.enabled`,
one shared switch, not per-provider) — see `src/Deepseek/common/safety_fallback.py`.

**Why refusals happen at all.** Each non-DeepSeek provider runs its own
content-safety classifier, tuned to that provider's own policy — this
pipeline does not control or configure it. Ordinary light-novel conventions
sit close enough to that boundary that behavior varies by provider and by
material: a comedic beat built on a risqué joke or teasing innuendo can trip
one provider's classifier while another treats it as unremarkable genre
content; material that reads as more explicitly sexual trips a refusal far
more consistently, across most providers. Neither outcome reflects a defect
in this pipeline — it is the provider's own policy surface, encountered live.

In practice, DeepSeek shows the lowest refusal rate of the API routes and
the highest tolerance for ordinary genre material — including, in observed
runs, chapters that a different route declined outright, which DeepSeek
completed as ordinary translation work with no intervention needed.

This is a current observation, not a guarantee. A provider's safety
classifier is that provider's own policy, on that provider's own release
schedule — not this codebase's. A route that handles a given volume cleanly
today is not bound to keep doing so after a future model or policy update,
on any provider, DeepSeek included.

### Prep — Cached Multi-turn Path

`src/utility/prep/agent.py::run_prep()` builds `context.xml` before
translation starts. `prep.provider` picks the route: `deepseek` (this
section), `openai` (next section; the shipped default), or `codex` / `claude`
(one long CLI call, see [Without an API key](#without-an-api-key-codex-cli-or-claude-code)).
On the DeepSeek route, multi-turn is the active default path
(`prep.multi_turn.enabled`); parallel cache-warmed fan-out remains available
as an explicitly selected recovery strategy, and a one-shot unified call is
the final fallback. The active path fails closed — a turn or assembly error
preserves its JSON artifacts for explicit retry rather than silently
spending on a second strategy.

**The 17-block shell.** The Librarian (`librarian/agent.py::_write_context_placeholder`)
writes `context.xml` immediately after extraction: `<opf_metadata>` populated
in place from the EPUB's own OPF data, plus seventeen further blocks seeded
`<pending/>`, each tagged with its owning agent. Of those seventeen, fifteen
are filled by the multi-turn prep conversation in one persisted sequential
DeepSeek conversation — one JSON-node turn per block
(`block_prompts.py::MULTITURN_BLOCK_ORDER`), assembled and schema-validated
into XML after every turn — plus `chapter_titles_en`, a block prep creates
fresh rather than inheriting from the Librarian's shell, for sixteen turns
total. The remaining two stay outside prep's own accounting: `character_attribute_anchors`
is never generated by any path in this lightweight client (a block name
inherited from the original heavier pipeline, permanently omitted here — see
`block_prompts.py`'s own ownership comment), and `translation_inheritance`
stays `<pending/>` unless the Safety-Refusal Fallback above actually fires.

### Prep — OpenAI Responses / Batch Path

Selecting `prep.provider: openai` uses the same `build_multiturn_system_prompt()`
and `build_multiturn_task_suffix()` contract as the DeepSeek prep pipeline. One
native Responses request is created for every active prep block; with
`prep.openai.batch.enabled: true`, all of them are submitted in one durable
OpenAI Batch job and tracked in `prep.openai.batch.persistence_file`. Completed
JSON nodes are written under `.context/openai_prep/`, so an interrupted run can
resume accepted work without resubmitting completed blocks. The shipped
config has the flag at `false`, which runs the same block loop through
synchronous Responses calls.

`prep --force-rerun` re-asks every block instead of reusing completed
artifacts under `work/<vol>/.context/multiturn/`; without it a repeat run pays
only for the turns that never finished.

**Metadata search.** Before the conversation starts, prep resolves the
volume's Japanese title/author against `web_search_chain.py`'s
`MetadataSearchChain` — retrieval kept deterministic and separate from model
reasoning, so blocks that need real-world grounding (series continuity,
publisher conventions, cast canon) work from actual search results rather
than a model guess. It tries the locked Bookwalker search URL directly first
(`bookwalker_direct`), then falls through configured providers in fixed
order on the first hit — Brave (queried as `site:bookwalker.jp <keyword>`),
Japan's National Diet Library catalog (NDL), SerpAPI, Tavily, Exa. Every
result is host-allowlisted to `bookwalker.jp` and `ndlsearch.ndl.go.jp`
only — no other domain is ever returned as evidence — and provider API keys
are read from environment variables only, never serialized into the
evidence trail. The chain's result folds into the conversation's stable
system prefix as grounding evidence and is persisted to
`.context/prep_web_search.json` for audit.

Every model turn — prep's sixteen and the search chain's provider calls
alike — writes a row to the per-volume cost ledger under
`work/<volume_id>/LOG/`.

### Series Continuity — The Bible Writer

`bibles/<series_id>/` is three flat, human-readable JSON files:

| File | Content |
|------|---------|
| `term_lock.json` | JP→EN name table for characters and every other locked term |
| `verbatim_anchors.json` | Recurring signature phrases and locked wording |
| `series_pack.json` | Volumes processed, merged voice fingerprints, last-known emotional-proximity band per character |

On the *next* volume of a series, prep matches its JP title against every
`series_pack.json`; a match loads the bible and injects it so the model
reuses locked names and voices instead of reinventing them. Writing the bible
is deliberately MCP/IDE-agent-only, not a CLI command.

> **Known gap.** The writer's implementation (`src/utility/bible/`) is not in
> this repository: an unanchored `bible/` rule in `.gitignore` excluded it
> from every commit (the rule is now `/bible/`). Until that package is
> committed, the `write_bible` MCP tool reports itself unavailable; the
> server and its other tools are unaffected, and prep still *reads* an
> existing `bibles/<series_id>/`.

---

## Japanese → Vietnamese

Vietnamese (`vi`) is a second, independent target language. It does not
replace or change the English route: an English workspace keeps `EN/`, the
provider's own English master prompt, verbatim anchors and the DeepSeek
safety fallback exactly as before (a test compares the English prompt
builders byte-for-byte against the previous release).

| Piece | Where |
|---|---|
| Language resolution, per-workspace paths, provider support | `src/Deepseek/common/language.py` |
| Vietnamese literary master prompt (OpenAI + Gemini) | `src/prompts/vi/master_prompt_light_novel_vi.md` |
| Series Bible, pronoun matrix, glossary, spoiler gating | `src/utility/vietnamese/bible.py` |
| LLM relationship / pronoun analysis | `src/utility/vietnamese/analyze.py` |
| QC (deterministic + semantic) | `src/utility/vietnamese/qc.py`, `align.py` |
| Selective repair | `src/utility/vietnamese/repair.py` |
| Web import | `src/utility/webimport/` |
| EPUB validation (built-in + optional epubcheck) | `src/builder/validation.py` |

**Providers.** Vietnamese supports **Codex CLI**, **Claude Code**, **OpenAI** (Responses API, conversation
state, Batch, caching, continuation: the same route as English) and
**Gemini** (Interactions API chain, Batch + explicit cache). DeepSeek, Qwen
and Anthropic keep their English routes; asked to translate a Vietnamese
workspace they stop **before any API call** with a clear message, instead of
writing English into it. Model ids, context windows and reasoning settings
come from the existing `translation.openai` / `translation.gemini` blocks, so
an Astra or Sol model is used exactly as configured for English. Nothing is
assumed about a model the config does not name.

### Windows quick start (Vietnamese)

PowerShell, from the repository folder:

```powershell
# 1. One-time setup (Python 3.10+ from https://www.python.org/downloads/, tick "Add to PATH")
py -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -r requirements.txt

# 2. EITHER an API key in .env (never commit it) for the provider you translate with ...
Set-Content .env "OPENAI_API_KEY=sk-..."      # or GEMINI_API_KEY=...
#    ... OR no key at all: sign in to Codex CLI / Claude Code and use
#    --provider codex / --provider claude (see "Without an API key" below)
codex login

# 3a. A Japanese EPUB -> Vietnamese EPUB
copy "C:\path\to\novel.epub" raw\
python scripts\mtl.py run raw\novel.epub --target vi --provider openai

# 3b. Or a web novel (episodes 1-30 become one volume)
python scripts\mtl.py import-url "https://ncode.syosetu.com/n0000aa/" --target vi --range 1-30
python scripts\mtl.py resume web_syosetu_n0000aa_0001-0030_vi --provider gemini

# 4. Interrupted? Continue exactly where it stopped (finished chapters are not paid for again)
python scripts\mtl.py resume <volume_id>
```

`cmd.exe` works the same with `.venv\Scripts\activate.bat`; `mtl.bat run ...`
is the existing launcher (it uses the system Python). The finished book is in
`output\` as `<title>_VI.epub`. Japanese titles and names print correctly in
any Windows console because the CLI switches its own output to UTF-8.

To make Vietnamese the default for every new workspace instead of passing
`--target vi`, set `project.target_language: vi` in `config.yaml`.

### Input sources: EPUB, web novel URL, existing workspace

Every input ends up as the same workspace (`work/<volume_id>/JP/CHAPTER_*.md`
plus `manifest.json` and `context.xml`), so prep, translation, QC and build
behave identically whatever the source.

| Input | Accepted form | One-step command | Step by step |
|---|---|---|---|
| Japanese EPUB | any path to a `.epub` (conventionally `raw\`) | `run raw\novel.epub --target vi --provider P` | `extract raw\novel.epub --target vi`, then `resume <vol> --provider P` |
| Syosetu | `https://ncode.syosetu.com/n1234ab/` (work) or `.../n1234ab/5/` (one episode) | `run "<url>" --target vi --range 1-30 --provider P` | `import-url "<url>" --target vi --range 1-30`, then `resume <vol> --provider P` |
| Kakuyomu | `https://kakuyomu.jp/works/<id>` or `.../works/<id>/episodes/<id>` | same as Syosetu | same as Syosetu |
| Existing workspace | a volume id from `python scripts\mtl.py list` | `resume <vol> --provider P` | `prep`, `translate`, `qc`, `build` one by one |

`P` is `openai`, `gemini`, `codex` or `claude` for Vietnamese (English also
accepts `deepseek`, `qwen`, `anthropic`). Quote URLs in PowerShell.

**EPUB**

```powershell
python scripts\mtl.py run "D:\Books\novel vol1.epub" --target vi --provider codex
python scripts\mtl.py extract raw\novel.epub --target vi --volume-id my_novel_v1_vi   # own id
```

Without `--volume-id` the id is derived from the EPUB plus `_vi`. Extracting
the same EPUB again over a workspace that already has translations creates a
new derived workspace instead of overwriting it.

**Web novel URL (Syosetu / Kakuyomu)**

```powershell
# Episodes 1-30 by reading order -> volume web_syosetu_n1234ab_0001-0030_vi
python scripts\mtl.py import-url "https://ncode.syosetu.com/n1234ab/" --target vi --range 1-30
python scripts\mtl.py resume web_syosetu_n1234ab_0001-0030_vi --provider codex

# Or everything in one go
python scripts\mtl.py run "https://kakuyomu.jp/works/1177354054000000000" --target vi --range 1-30 --provider claude
```

* `--range`: `1-30`, `31-` (from 31 to the latest), or `7` (one episode).
  Without it: the whole work, or only the episode named in an episode URL.
* Next batch of a long series: import the next range as its own volume
  (`--range 31-60` -> `..._0031-0060_vi`). The bible carries names and
  pronouns across volumes when they share a `--series-id`.
* New episodes on the site: run the same command again (`run "<url>" --target
  vi --provider P`). Without `--volume-id`, an import that starts at the same
  episode in the same language updates the existing workspace instead of
  creating a new one: new episodes are appended as new chapters, their
  characters are added to the bible (automatically with `codex`/`claude`,
  otherwise `bible-vi <vol> --analyze --chapters <new ones>`), and only they
  are translated. Add `--refresh` to also re-check old episodes for edits
  (edited ones go back to `pending`, the old translation is kept in
  `.context/stale/`). The workspace keeps its original id (e.g. `_0001-0104_vi`).
* `--no-extract` only downloads and builds the Japanese EPUB in `raw/web/`.
* Only public pages: login, age gates (`novel18.syosetu.com`), CAPTCHAs and
  paywalls stop the import (details in the web import section below).

**Existing workspace**

```powershell
python scripts\mtl.py list                       # volume ids in work/
python scripts\mtl.py status <vol>               # language, prep state, chapters done/blocked
python scripts\mtl.py resume <vol> --provider codex
python scripts\mtl.py translate <vol> --provider codex --chapters CHAPTER_03   # only some chapters
python scripts\mtl.py build <vol>                # rebuild the EPUB after editing VI\*.md by hand
```

The finished book is written to `output\<title>_VI.epub`.

### Incremental Vietnamese translation for long web novels

Download the selected episodes, then analyze and translate in windows instead
of analyzing every chapter before translation starts:

```powershell
python scripts/mtl.py run "<url>" --target vi --provider claude --incremental --window 20
python scripts/mtl.py resume <vol> --provider claude --incremental --window 20
```

For a work with 938 published episodes, a work-page URL without `--range`
selects all currently available episodes; add `--range 1-938` to select that
fixed range. An episode URL can select just that episode instead. If the work
is already downloaded, use `resume`; it does not fetch new episodes from the site.

Each window contains up to 20 pending chapters in reading order. The Vietnamese
bible is updated first, then those chapters are translated before the next
window is analyzed. Use `--window 1` for chapter-by-chapter updates. This also
works with `import-url ... --run --incremental --window 20`. Without
`--incremental`, pipeline phase ordering stays unchanged. Download and initial
`context.xml` prep still happen before the windows; this option changes the
bible-analysis/translation sequence, not prep's source selection.

Analysis saves a SHA-256 text hash per successful chapter in the workspace's
`.context/bible_vi.json` (`analysis_chapters`), atomically with the bible updates.
Repeated analysis skips unchanged chapters; interrupted batches and edited text
are retried. Dry runs never advance these checkpoints. Reseeding preserves the
workspace's checkpoints; they are not inherited by later volumes. Existing
bibles have no historical checkpoints, so their selected chapters are analyzed
once to establish them. Reanalysis merges through the existing evidence/lock
rules; it does not erase earlier decisions automatically.

If translation fails or leaves unfinished chapters, the pipeline stops before
analyzing the next window. Repeat `resume` with the same incremental flags:
completed translations are skipped and successful analysis is reused. Previously
completed chapters are not backfilled by incremental mode. QC and EPUB build run
after all windows finish.

For ongoing novels, repeat the URL command to import new episodes into the same
workspace (same novel, starting episode and target language). Keep the range
start fixed when extending it, e.g. `--range 1-50`, then `--range 1-100`.
To detect edits to already cached episodes as well as new releases:

```powershell
python scripts/mtl.py import-url "<url>" --target vi --refresh --run --provider claude --incremental --window 20
```

Changed chapters are reset to pending by import sync. Manually editing a JP
file does not reset its translation status; use `repair <vol> --reset CHAPTER_ID`
if that chapter also needs translating again.

To deliberately reanalyze unchanged text:

```powershell
python scripts/mtl.py bible-vi <vol> --analyze --force-analyze --provider claude
```

`--chapters CHAPTER_01 CHAPTER_02` can restrict this operation. For translation
providers other than `codex`/`claude`, bible analysis uses the configured
`assistant.provider`.

### After the first run: check, correct, continue

```powershell
# 1. Read the EPUB in output\ (opening, a few middle chapters, the end):
#    names, xưng hô, recurring terms.
# 2. Look at the bible, edit what is wrong, then lock it
python scripts\mtl.py bible-vi <vol> --show
notepad work\<vol>\.context\bible_vi.json
python scripts\mtl.py bible-vi <vol> --lock
# 3. Retranslate chapters that used old names (or every chapter: --reset all)
python scripts\mtl.py repair <vol> --reset CHAPTER_05 CHAPTER_12
python scripts\mtl.py resume <vol> --provider codex      # only those chapters, then QC + build
# 4. Optional checks
python scripts\mtl.py repair <vol> --mechanical           # punctuation only, no model call
python scripts\mtl.py qc <vol> --llm --provider codex     # semantic review -> QC\qc_vi_report.md
python scripts\mtl.py repair <vol> --severity major --provider codex
python scripts\mtl.py build <vol>                         # rebuild the EPUB after manual edits
# 5. Keep the bible for the next volume / next batch of episodes
python scripts\mtl.py bible-vi <vol> --promote --series-id <series>
```

New episodes on the website: run the same `run "<url>" --target vi ...`
command again (see the input section above).

### Without an API key: Codex CLI or Claude Code

A ChatGPT plan (Plus/Pro) or a Claude plan (Pro/Max) includes no API key, but
both include an official command-line tool. `--provider codex` and
`--provider claude` drive those tools non-interactively (`codex exec`,
`claude -p`), so prep, translation and the `qc --llm` / `bible-vi --analyze`
assistant bill against your plan instead of an API account:

```powershell
# One-time: install and sign in to the tool you will use
codex login          # Codex CLI, ChatGPT account
claude               # Claude Code; type /login, then /exit

python scripts\mtl.py run raw\novel.epub --target vi --provider claude
python scripts\mtl.py resume web_syosetu_n0000aa_0001-0030_vi --provider codex
python scripts\mtl.py resume <vol> --provider codex --prep-provider claude   # mix them
```

* With `--provider codex|claude`, prep uses the same CLI unless
  `--prep-provider` says otherwise; no `DEEPSEEK_API_KEY` is needed.
* `subscription_only: true` (default, `cli:` in `config.yaml`) keeps
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` away from the CLI, so it never
  silently switches to pay-per-token billing.
* Plans have usage limits. When one is reached the run stops cleanly; every
  finished chapter stays completed, and the same `resume` command continues
  after the limit resets. To stretch a plan, pick a smaller model:
  `cli.claude.model: sonnet`, or a model your Codex plan offers in
  `cli.codex.model` (`null` = the CLI's default).
* Each call is a fresh process in an empty temporary folder (Claude: no tools;
  Codex: read-only sandbox). There is no shared conversation, so continuity
  comes from the Vietnamese bible block and the ending of the previous
  chapter; chapters over `cli.max_chunk_chars` are split at paragraph
  boundaries. A chapter that comes back cut off is marked `blocked` and is
  retried by `resume`.
* `cli.codex.model` takes the model id (`gpt-6.1-terra`), not the picker's
  display name; a display name such as `GPT-6.1 Terra` is converted
  automatically. The ids your plan offers are listed in
  `%USERPROFILE%\.codex\models_cache.json`.
* Prep is one long call that prints nothing until it returns (often 5-20
  minutes). A long volume is cut to its opening chapters
  (`cli.prep_max_chars`, 120000 characters by default) because one answer
  cannot cover hundreds of thousands of characters; prep that comes back
  without a character roster is refused instead of written. For Vietnamese
  the bible analysis then runs automatically over the whole volume in
  batches, in story order, so characters introduced later are covered.
  Each translated chapter then prints a line.
* The other assistant commands take the same switch: `qc <vol> --llm
  --provider codex`, `repair <vol> --severity major --provider codex`,
  `bible-vi <vol> --analyze --provider codex`. Without it they use
  `assistant.provider` from `config.yaml`.
* Chapters are always processed in story order (`CHAPTER_9`, `CHAPTER_10`,
  ..., `CHAPTER_100`), so continuity and the bible never see a later chapter
  first.
* This is meant for your own use with your own account, within each plan's
  terms and limits.

### How a workspace gets its language

The language is chosen once, at `extract` / `import-url` / `run`, and stored in
the workspace (`manifest.json` → `runtime_config.target_language`, and the root
of `context.xml`). Every later command reads it from there:

```
work/<vol>_vi/
├── JP/CHAPTER_01.md            source (same as English)
├── VI/CHAPTER_01_VI.md         translation (English volumes: EN/CHAPTER_01_EN.md)
├── context.xml                 prep output (facts; English renderings ignored for vi)
├── .context/bible_vi.json      Vietnamese bible for this volume
└── QC/qc_vi_report.{json,md}   Vietnamese QC report
```

Without `--volume-id`, a non-English workspace gets the usual id plus `_vi`,
so the English and Vietnamese workspaces of one EPUB never overwrite each
other. Prep runs unchanged (it builds `context.xml`: characters, gender
evidence, relationships, voices); the Vietnamese prompt uses those facts and
ignores the English-specific renderings.

### Vietnamese Series Bible and xưng hô

`work/<vol>/.context/bible_vi.json` (per volume) and
`bibles/<series_id>/vi/bible_vi.json` (per series) hold: character profiles
(JP name, aliases, Vietnamese name, gender, age/rank, POV), the
**relationship/pronoun matrix** (speaker → addressee, `self`/`other` =
xưng/gọi, an optional setting such as "trước mặt người ngoài", and the
chapter each pair takes effect), the glossary (names, places, organizations,
titles, magic, skills, items, world terms, honorifics, wordplay) with
`locked` and `never_use`, recurring phrases, voice notes, worldbuilding,
timeline, open ambiguities, translation decisions and book metadata
(`metadata.title_vi`). The English bible files are never written.

Every fact has an evidence level: `confirmed` (stated in the text),
`inferred`, or `unknown`. The translator is told what each means, so an
`unknown` gender is rendered neutrally and never guessed.

* A locked entry is never changed by seeding, analysis or series merges; a
  disagreeing proposal becomes an open ambiguity for you to decide.
* A weaker evidence level never overwrites a stronger one.
* An LLM proposal marked `confirmed` must quote text that really occurs in
  the source, or it is downgraded to `inferred`. Vietnamese address pairs are
  translation decisions, so analysis always records them as `inferred`.
* Spoilers: each chapter receives only the entries introduced at or before
  it, and only characters/terms that occur in its source. A relationship
  change from chapter 7 (e.g. `tớ/cậu` → `em/anh`) is invisible to chapters 1–6.

```powershell
python scripts\mtl.py bible-vi <vol> --seed            # from context.xml (automatic after prep)
python scripts\mtl.py bible-vi <vol> --analyze         # LLM pass: relationships, xưng hô, terms (paid)
python scripts\mtl.py bible-vi <vol> --show
# edit work\<vol>\.context\bible_vi.json (names, pairs, title_vi), then approve:
python scripts\mtl.py bible-vi <vol> --lock            # lock every name/term that has a Vietnamese form
python scripts\mtl.py bible-vi <vol> --promote --series-id my_series   # carry it into the next volume
```

For the regular pipeline, `project.languages.vi.bible.auto_analyze: true`
enables analysis in the after-prep hook; a CLI assistant (`codex`/`claude`)
also enables it there by default. An already completed prep with an existing
bible is skipped by `resume`. Use `--incremental` to analyze pending translation
windows regardless of this setting, or `bible-vi --analyze` to explicitly resume
unfinished analysis. Analysis checkpoints skip unchanged chapters in either mode.

### Vietnamese QC and selective repair

`qc` on a Vietnamese workspace runs deterministic checks with no API call:
missing chapters, truncation, paragraph and dialogue-line parity, scene
breaks, illustration tags, leftover Japanese script, prompt or commentary
leaks, locked names and terms, forbidden variants, near-miss name spellings,
numbers (digits and spelled-out Vietnamese numbers against the source),
Unicode NFC, fullwidth punctuation, ellipsis/dash/exclamation inflation, and
overuse of "anh ta/cô ta". `qc --llm` adds a semantic review (omission,
addition, negation, quantity, time, causality, speaker, pronoun, names/terms,
mistranslation, naturalness) over aligned JP/VI windows. A reviewer finding
only counts as an issue when its Vietnamese quote exists in the text it was
shown. Severities are `critical` / `major` / `minor` / `info`; the gate is
`project.languages.vi.qc.block_on_severity`.

Every issue in `QC/qc_vi_report.md` points to the Vietnamese paragraph and the
aligned Japanese paragraph(s), with excerpts. QC never rewrites text.

```powershell
python scripts\mtl.py qc <vol> --llm                  # add --dry-run to only write the review payloads
python scripts\mtl.py repair <vol> --mechanical       # NFC, “quotes”, …, punctuation; wording untouched
python scripts\mtl.py repair <vol> --severity major   # LLM-fix only the located paragraphs
python scripts\mtl.py repair <vol> --issue CHAPTER_03-0012
python scripts\mtl.py repair <vol> --reset CHAPTER_05 # missing passages/truncation: retranslate that chapter only
```

A repair replaces one paragraph, and only after the replacement passes
validation (no Japanese script, one paragraph, sane length, same illustration
tags). The chapter is backed up to `.context/repair_backups/` and each change
is logged in `QC/repair_log.json`.

### Vietnamese EPUB fonts

Vietnamese books embed their fonts, so diacritics render the same on every
reader: **Literata** for the text (a serif designed for long reading on
screens) and **Inter** for chapter titles. Both are SIL OFL fonts with the
full Vietnamese alphabet, subset to Latin + Vietnamese (adds about 0.5 MB
to each EPUB). Files and licenses are in `fonts/`; change or turn
off embedding in `project.languages.vi.epub_fonts` (`embed: false` keeps only
the CSS font names). On Kindle, choose *Publisher Font* to see them. English
books are unchanged.

### Web novel import (Syosetu / Kakuyomu)

```powershell
python scripts\mtl.py import-url "https://ncode.syosetu.com/n0000aa/" --target vi --range 1-50
python scripts\mtl.py import-url "https://kakuyomu.jp/works/<work id>" --target vi --run
python scripts\mtl.py import-url "<same url>" --target vi --range 1-60 --volume-id <existing vol> --refresh
```

* One adapter per site (`syosetu.py`, `kakuyomu.py`); a work URL or an episode
  URL is accepted. Title, author, arc headings, preface/afterword and
  paragraph structure are kept.
* Ruby: a meaningful reading (`本気《マジ》`, katakana or kanji readings) is
  kept inline; a plain hiragana reading is kept only the first time it
  appears (where it tells the translator how to romanize a name); emphasis
  dots become emphasis.
* Downloaded text is data only: scripts and markup are dropped, `<`/`>` in the
  text become fullwidth so scraped content cannot open or close a prompt
  envelope, and the prompt states that source text is never an instruction.
* Episodes are cached under `raw/web/<site>_<id>/` the moment they are parsed;
  re-running an interrupted import fetches only what is missing. `--refresh`
  re-checks cached episodes; changed ones are reported, and if the workspace
  already exists the changed chapters go back to `pending` (old translation
  kept in `.context/stale/`) while new episodes are added as new chapters.
  Kakuyomu exposes only publish dates, so silent edits there are found only
  with `--refresh`.
* The episodes become a Japanese EPUB3 and go through the normal Librarian,
  so everything downstream is identical to an EPUB volume. A web novel has
  no publisher; the EPUB names the site instead (e.g. `Shōsetsuka ni Narō
  (ncode.syosetu.com)`).
* **Access rules:** robots.txt is always honoured (including `*`/`$` patterns
  and Crawl-delay), with at least 2 s between requests (configurable upward in
  `web_import`). A login page, an age gate (e.g. `novel18.syosetu.com`), a
  CAPTCHA, HTTP 401/403, or an unreachable robots.txt stops the import; there
  is no option to bypass any of them. Respect the authors' rights: translate
  for personal use unless you have permission to publish.

### Resume, truncation and dry runs

* `resume <vol>` runs only unfinished phases; `translate` skips chapters the
  manifest marks completed, so a crash or Ctrl+C never pays twice.
* Output cut off at the model limit is continued automatically (Responses /
  Interactions continuation). A Vietnamese chapter still incomplete after the
  allowed continuations is written but marked `blocked` ("output truncated")
  and is retried by the next `resume`.
* `translate --dry-run`, `qc --llm --dry-run`, `repair --dry-run` and
  `bible-vi --analyze --dry-run` write the exact request to `DRY_RUN/` and send
  nothing (translation dry runs need no API key).
* Japanese text left in a Vietnamese output is not silently deleted (English
  keeps its historical behaviour); QC reports it with its location.
* Every translation and assistant call is logged with input/output tokens and
  estimated cost in `work/<vol>/LOG/`. API keys are read from environment
  variables only and are never logged.

### What is not supported yet

* Vietnamese routes for DeepSeek, Qwen and Anthropic (the API route; the
  Claude Code route, `--provider claude`, does support Vietnamese). They refuse Vietnamese
  workspaces before any call; adding one means a prompt entry under
  `project.languages.vi.providers` plus that route's output-path wiring.
* A content-safety fallback for Vietnamese. The DeepSeek fallback is
  English-only, so a refused Vietnamese chapter stays pending with the refusal
  logged.
* Prep itself still writes English-oriented metadata (that is what
  `context.xml` holds for both languages); Vietnamese names and terms come
  from the bible seed, `--analyze`, and your edits.
* Deterministic QC cannot attribute a line to its speaker, so pronoun
  consistency between specific characters is checked only by `qc --llm`.

## Configuration

One control surface per provider in `config.yaml`, selected by
`translation.provider`:

```yaml
project:
  target_language: en       # default for NEW workspaces; --target overrides

prep:
  provider: openai          # deepseek | openai | codex | claude

translation:
  provider: anthropic       # deepseek | qwen | openai | anthropic | gemini | codex | claude

  deepseek:
    model: deepseek-v4-pro
  qwen:
    model: qwen3.7-max
  openai:
    model: gpt-6-sol        # GPT-6 Luna is the fallback
  anthropic:
    model: claude-sonnet-5-5
    advisor:                # Proofreading Mode
      enabled: true
      model: claude-opus-5-5
  gemini:
    model: gemini-4-argon

  safety_fallback:
    enabled: true            # default ON for every non-DeepSeek provider

assistant:
  provider: openai          # Vietnamese bible analysis, qc --llm, repair

cli:                        # --provider codex / claude
  subscription_only: true

builder:
  device_profile: standard  # passthrough | standard | xteink-x3 | xteink-x4
```

These are the values `config.yaml` ships with. See `config.yaml` itself for
the full documented parameter set per provider — generation limits,
thinking/effort controls, caching, Batch, conversation compaction, and
continuation behavior are all configured independently per route.

---

## File Structure

```
llm-trans/
├── config.yaml                 ← Master config: one section per provider + shared knobs
├── requirements.txt            ← Python dependencies (requirements-dev.txt adds pytest)
├── README.md                   ← This file
├── ARCHITECTURE.md             ← Maintainer-level architecture map
├── READ_ME.vi.md               ← Vietnamese setup and operator guide
├── mtl.bat / mtl.sh            ← CLI launcher (Windows / Linux+macOS)
├── mtl-ts.bat / mtl-ts.sh      ← TypeScript console launcher
├── .mcp.json                   ← Registers the MCP server for Claude Code
├── fonts/                      ← Literata + Inter (SIL OFL) for Vietnamese EPUBs
├── workflows/qc-gate.wf.mjs    ← Agent workflow: QC gate via the translation-evaluator skill
│
├── src/
│   ├── Deepseek/                ← Shared infrastructure + the DeepSeek route
│   │   ├── common/              ← config, language, token_telemetry, safety_fallback,
│   │   │                          chapter_signals, illustration_reinjection, ...
│   │   ├── mcp/                 ← MCP server (server.py) + per-phase tool servers
│   │   ├── prompt/              ← DeepSeek master prompts + prep prompt
│   │   └── translator/          ← DeepSeek route
│   ├── Qwen/                    ← Qwen route
│   ├── OpenAI/                  ← OpenAI Responses route (+ prep.py for OpenAI prep)
│   ├── Anthropic/               ← Anthropic Messages route (+ Proofreading Mode, Batch)
│   ├── Gemini/                  ← Gemini Interactions route
│   ├── CLI/                     ← Codex CLI / Claude Code route (subscription, no API key)
│   ├── prompts/vi/              ← Vietnamese master prompt
│   ├── utility/
│   │   ├── librarian/           ← Phase 1: EPUB extraction
│   │   ├── prep/                ← context.xml builder (+ metadata web-search chain)
│   │   ├── qc/                  ← English filesystem-only sanity gate
│   │   ├── vietnamese/          ← VI bible, analysis, QC, alignment, repair
│   │   └── webimport/           ← Syosetu / Kakuyomu import
│   └── builder/                 ← Phase 4: EPUB assembly, device profiles, XTC export
│
├── scripts/
│   ├── mtl.py                   ← CLI (see CLI Reference)
│   ├── reinject_illustrations.py← Re-insert illustration tags into a translated volume
│   ├── ocr_extract.py           ← Optional JP OCR for page scans (manga-ocr / PaddleOCR)
│   └── convert_agent_assets.py, validate_agent_assets.py  ← Agent/skill asset tooling
│
├── tests/                       ← unit/ + integration/ (offline by default)
└── ts/                          ← TypeScript Ink/React terminal console (optional)
```

`raw/`, `work/`, `output/` and `bibles/` are created at run time and are not
part of the repository. `workflows/qc-gate.wf.mjs` and the two
`*_agent_assets.py` scripts operate on local-only agent/skill assets
(`.github/`, `.agents/`), which `.gitignore` keeps out of the repository.

---

## CLI Reference

```bash
python scripts/mtl.py extract <epub_path> [--target vi] [--volume-id ID] [--force-rerun]
                                              # Phase 1: EPUB → JP chapters + barebone context.xml
python scripts/mtl.py import-url <url> [--target vi] [--range 1-50] [--volume-id ID] [--refresh] [--no-extract] [--run [--incremental --window N]]
                                              # Syosetu/Kakuyomu → JP workspace (same layout as extract)
python scripts/mtl.py prep <vol_id> [--series-id S] [--force-rerun] [--prep-provider P]
                                              # Fills context.xml (VI: also seeds bible_vi.json)
python scripts/mtl.py translate <vol_id> [--chapters C...] [--provider P] [--no-thinking-log] [--dry-run]
                                              # Phase 2 into EN/ or VI/; --dry-run assembles the payload, sends nothing
python scripts/mtl.py qc <vol_id> [--llm] [--dry-run] [--chapters C...] [--provider P]
                                              # EN: filesystem-only gate. VI: Vietnamese QC (+ semantic review)
python scripts/mtl.py repair <vol_id> ...     # VI: --mechanical | --severity S | --issue ID | --reset CHAPTER... | --reset all  [--chapters C...] [--dry-run] [--provider P]
python scripts/mtl.py bible-vi <vol_id> ...   # VI: --seed | --analyze [--force-analyze] [--chapters C...] [--dry-run] [--provider P] | --lock | --promote [--series-id S] | --show
python scripts/mtl.py build <vol_id> [--output NAME] [--profile PROFILE] [--emit-xtc xtc|xtch] [--dry-run]
                                              # Phase 4: translation → EPUB
python scripts/mtl.py run <epub_path|url> [--target vi] [--volume-id ID] [--series-id S] [--range R]
                      [--provider P] [--prep-provider P] [--incremental --window N] [--no-thinking-log] [--force-rerun]
                                              # extract/import → prep → translate → qc → build
                                              # same URL again: adds new episodes to the existing workspace
python scripts/mtl.py resume <vol_id> [--provider P] [--prep-provider P] [--series-id S] [--skip-prep] [--incremental --window N] [--no-thinking-log]
                                              # Continue an interrupted volume
python scripts/mtl.py list                    # List volumes in work/
python scripts/mtl.py status <vol_id>         # Pipeline state, target language, chapter completion
```

`--provider` accepts `deepseek`, `qwen`, `openai`, `anthropic`, `gemini`,
`codex`, `claude` (the `qc`, `repair` and `bible-vi` assistant accepts only
`openai`, `gemini`, `codex`, `claude`). `--prep-provider` accepts `deepseek`,
`openai`, `codex`, `claude`. `build --profile` takes `passthrough`,
`standard`, `xteink-x3` or `xteink-x4`; `--emit-xtc` needs an `xteink-*`
profile and a configured `builder.xtc` converter.

Without `--target`, every command behaves as before (English). If
Without `--target`, new workspaces use `project.target_language` (shipped as
`en`); existing workspaces retain their recorded language. If
`EPUBCHECK_JAR` points at an `epubcheck.jar` and Java is installed, `build`
also prints the epubcheck verdict.

`bible-vi` manages the Vietnamese bible. English bible writing uses MCP/IDE-agent
tools — see [Series Continuity](#series-continuity--the-bible-writer).

---

## Coding-agent harness

Development agents share [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md).
The harness records feature acceptance criteria, progress, file-based handoffs
and verification evidence tied to the current source snapshot.

```powershell
python scripts/harness.py start
python scripts/harness.py verify --profile python
# For console changes, after installing ts/ dependencies:
python scripts/harness.py verify --profile all
```

Use `harness/progress.md`, `harness/features.json` and `harness/handoff.md` to
continue work across sessions. Reports are saved under `.harness/runs/` and
ignored by Git. Feature completion rejects failed, incomplete or stale evidence.
The harness uses repository files; no database, index or external service is
required. Translation providers, prompts and Vietnamese bibles remain independent.

See [the harness guide](harness/README.md). The workflow follows
[Learn Harness Engineering](https://walkinglabs.github.io/learn-harness-engineering/en/).

## Testing

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest                       # offline: unit + integration, no API key, no network
$env:MTL_LIVE_NETWORK = "1"; python -m pytest tests/integration/test_live_sites.py   # optional live site check
```

`tests/unit` covers language and prompt selection (including byte-identical
English prompts against the previous release), the Vietnamese bible rules, QC
and repair, the site adapters, robots/politeness, and checkpointed import and
sync. `tests/integration` drives the real OpenAI and Gemini agents with a
scripted client (`generate` is the only mocked boundary) through extraction,
translation, interruption and resume, truncation recovery, QC and EPUB build,
for both Vietnamese and English. Nothing in the default run calls a paid API.

---

## MCP Tools

An MCP server exposes every phase (Librarian, Prep, Translator, QC, Bible
Writer, Builder) as callable tools for IDE-agent integration — the agent
calls one tool per phase instead of manually orchestrating sub-steps.

| Server (`src/Deepseek/mcp/servers/`) | Tools | Phase |
|--------|-------|-------|
| Librarian | `run_librarian`, `extract_epub`, `parse_opf_metadata`, `parse_toc`, `convert_xhtml_to_markdown`, `detect_publisher`, `catalog_images`, `split_content` | 1 |
| Prep | `prep_volume(volume_id, series_id=None)` | 1.P |
| Translator | `translate_chapter`, `run_translator` (both take `dry_run`) | 2 |
| QC | `qc_volume(volume_id)` | Gate |
| Bible | `write_bible(volume_id, series_id=None)` | Continuity |
| Builder | `run_builder`, `package_epub`, `markdown_to_xhtml`, `generate_opf`, `generate_nav`, `merge_translated_shards`, `optimize_image` | 4 |

Start the server: `python -m src.Deepseek.mcp.server` (Python MCP SDK 2.x).
`.mcp.json` already registers it for Claude Code as `llm-translator`; it
launches the bare `python` on PATH, so that interpreter needs
`requirements.txt` installed (a missing `mcp` package shows up as
"Connection closed").

---

## Requirements

- **Python** 3.10+ (3.11+ for `scripts/validate_agent_assets.py`, which uses `tomllib`)
- **Node.js** 20+ (TypeScript console only; 18+ for the optional XTC converter)
- An API key for whichever provider(s) you configure, set via `.env` — or a
  signed-in Codex CLI / Claude Code for `--provider codex|claude`
- Optional: Java + `EPUBCHECK_JAR` for epubcheck validation in `build`

Python packages: `anthropic>=0.100`, `openai>=2.0`, `google-genai>=2.3`,
`pyyaml`, `python-dotenv`, `lxml`, `beautifulsoup4`, `Pillow`, `tiktoken`,
`mcp>=2.0` (the 2.x server API; 1.x does not work)

---

## context.xml

Every volume gets a `context.xml` at `work/<volume_id>/context.xml` — the
translator's entire continuity/voice/terminology reference. `extract` writes
a barebone version; `prep` fills the generated blocks through a cached
sequential conversation. Root shape:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<mtls_project_context schema_version="1.0" volume_id="..." series_id="..." target_language="en">
  <opf_metadata format="json">{...raw OPF passthrough...}</opf_metadata>
  <validation_audit status="passed">...</validation_audit>
  <volume_identity>...</volume_identity>
  <chapter_titles_en>...</chapter_titles_en>
  <world_setting type="...">...</world_setting>
  <character_roster>...</character_roster>
  <name_map>...</name_map>
  <relationship_graph>...</relationship_graph>
  <verbatim_anchors>...</verbatim_anchors>
  <character_attribute_anchors status="pending"><pending/></character_attribute_anchors>
  <voice_fingerprints>...</voice_fingerprints>
  <cultural_glossary>...</cultural_glossary>
  <eps_arc_tracker>...</eps_arc_tracker>
  <scene_plans>...</scene_plans>
  <eps_signals>...</eps_signals>
  <chapter_signals>...</chapter_signals>
  <illustration_context>...</illustration_context>
  <translation_brief>...</translation_brief>
  <translation_inheritance status="pending"><pending/></translation_inheritance>
</mtls_project_context>
```

`character_attribute_anchors` and `translation_inheritance` stay `<pending/>`
until their owning feature runs — the former is out of scope for this client,
the latter only populates on a safety-refusal fallback. You can translate
without running prep at all (the master prompt carries enough inline
guidance), but the translator logs a loud warning when it does, since
character voice, locked names, and continuity anchors are all off in that
mode.
