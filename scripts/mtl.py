#!/usr/bin/env python
"""
LLM Translator — minimal CLI.

Run from the LLM Translator/ root (or invoke via mtl.bat, which cd's there
for you) so the `src.` import prefix resolves.

    extract <epub_path> [--target vi]
                             Phase 1: EPUB -> JP chapters (Librarian). The
                             workspace is bound to its target language here
                             (default: project.target_language, i.e. en).
    import-url <url> [--target vi] [--range 1-50] [--refresh] [--run]
                             Syosetu / Kakuyomu -> checkpointed episode cache
                             -> JP EPUB -> workspace (same layout as extract).
    prep <vol_id>            Fills context.xml (prep.provider). A Vietnamese
                             workspace also gets its bible_vi.json seeded.
    translate <vol_id> [--provider P] [--no-thinking-log] [--dry-run]
                             Phase 2 via translation.provider (or --provider):
                             deepseek | qwen | openai | anthropic | gemini,
                             or codex | claude: the signed-in Codex CLI /
                             Claude Code (subscription billing, no API key).
                             Output goes to the workspace's language folder
                             (EN/ or VI/). --dry-run writes each chapter's full
                             API payload to work/<vol_id>/DRY_RUN/ — zero API
                             cost, no output, no manifest changes.
    qc <vol_id> [--llm] [--dry-run]
                             EN: filesystem-only gate. VI: deterministic
                             Vietnamese QC; --llm adds the semantic review.
    repair <vol_id> [--mechanical | --severity S | --issue ID... | --reset CH...]
                             Vietnamese selective repair (see `repair -h`).
    bible-vi <vol_id> [--seed] [--analyze] [--lock] [--promote] [--show]
                             Vietnamese Series Bible maintenance.
    build <vol_id> [--dry-run] [--profile PROFILE]
                             Phase 4: translation -> EPUB (Builder).
    run <epub_path|url> [--target vi] [--provider P]
                             Full pipeline: extract/import -> prep -> translate
                             -> qc -> build.
    resume <vol_id> [--provider P] [--prep-provider P]
                             Continue an interrupted volume from its manifest:
                             only unfinished phases and pending chapters run.
    list                     List volumes in work/
    status <vol_id>          Pipeline state + chapter completion summary

Bible writing for the ENGLISH series bible (cross-volume continuity) stays
MCP-only (the `write_bible` tool).
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.Deepseek.common.config import WORK_DIR, ensure_utf8_console, get_config_section

# Every command here prints JP titles/names straight to stdout. On Windows,
# a piped/redirected stdout defaults to the console's ANSI codepage, which
# crashes on the first non-ASCII character — see ensure_utf8_console's
# docstring. Must run before any of this module's own print() calls.
ensure_utf8_console()

# Every phase module logs its own progress via logging.getLogger(__name__)
# (chapter-start/chapter-done, cache-hit ratios, conversation truncation
# warnings, ...) instead of print(). With no handler attached, the root
# logger drops all of it — a multi-minute translate call goes completely
# silent on stdout/stderr even though it's working. stream=stderr keeps it
# out of the way of the plain-text summaries cmd_* functions print on
# stdout, and out of anything that greps this CLI's stdout for output paths.
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")

from src.Deepseek.common.language import (
    DEFAULT_LANGUAGE,
    TARGET_LANGUAGE_ENV,
    UnsupportedTargetLanguage,
    default_target_language,
    language_config,
    require_provider_support,
    validate_language,
    volume_target_language,
)
from src.utility.librarian.agent import run_librarian
from src.builder.agent import run_builder
from src.builder.device_profiles import PROFILE_NAMES
from src.utility.prep.agent import PrepError, run_prep
from src.utility.qc.agent import run_qc

# The provider table lives in one place; codex / claude drive the signed-in
# Codex CLI / Claude Code (subscription billing, no API key); see src/CLI/.
from src.Deepseek.translator.provider import (
    CLI_PROVIDERS,
    PROVIDERS,
    configured_provider as _configured_provider,
    get_volume_translator as _resolve_translate_volume,
)

PREP_PROVIDERS = ("deepseek", "openai", "codex", "claude")


def _prep_provider(explicit: str | None, translation_provider: str | None) -> str | None:
    """--prep-provider wins; otherwise a CLI translation provider also does
    prep (someone translating through a subscription CLI usually has no API
    key for prep.provider); otherwise prep.provider from config.yaml."""
    if explicit:
        return explicit
    chosen = _configured_provider(translation_provider)
    return chosen if chosen in CLI_PROVIDERS else None


def _target(args: argparse.Namespace) -> str:
    """--target, validated; also exported so every module sees the same
    default for NEW workspaces created in this process."""
    lang = validate_language(getattr(args, "target", None) or default_target_language())
    os.environ[TARGET_LANGUAGE_ENV] = lang
    return lang


def _default_volume_id(epub_path: Path, lang: str, explicit: str | None) -> str | None:
    """English keeps the Librarian's own id scheme. Another language gets the
    same id plus a language suffix, so EN and VI workspaces of one EPUB never
    collide or overwrite each other."""
    if explicit or lang == DEFAULT_LANGUAGE:
        return explicit
    from src.utility.librarian.epub_extractor import EPUBExtractor

    return f"{EPUBExtractor(WORK_DIR)._generate_volume_id(Path(epub_path))}_{lang}"


def _check_provider_for_volume(volume_id: str, provider: str | None) -> str:
    chosen = _configured_provider(provider)
    require_provider_support(chosen, volume_target_language(WORK_DIR / volume_id))
    return chosen


def _manifest(volume_id: str) -> dict:
    path = WORK_DIR / volume_id / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _is_url(value: str | None) -> bool:
    return bool(value) and str(value).lower().startswith(("http://", "https://"))


# ── Vietnamese bible hook ───────────────────────────────────────────────────

def _series_id_for(volume_id: str) -> str:
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(WORK_DIR / volume_id / "context.xml").getroot()
        return str(root.get("series_id") or "")
    except (OSError, ET.ParseError):
        return ""


def _after_prep(volume_id: str, *, analyze: bool | None = None, dry_run: bool = False,
                assistant_provider: str | None = None) -> None:
    """Vietnamese workspaces: seed (and optionally analyze) bible_vi.json.
    English workspaces: nothing. With a subscription CLI as assistant the
    analysis runs by default: it costs plan usage, not money, and it is what
    covers characters beyond the opening that CLI prep reads."""
    work_dir = WORK_DIR / volume_id
    if volume_target_language(work_dir) != "vi":
        return
    from src.Deepseek.common.config import PIPELINE_ROOT
    from src.utility.vietnamese.bible import seed_volume_bible

    bible_cfg = language_config("vi").get("bible") or {}
    if bible_cfg.get("auto_seed", True):
        bible_root = PIPELINE_ROOT / str(get_config_section("prep").get("bible_dir") or "bibles/")
        bible, path = seed_volume_bible(work_dir, bible_root=bible_root, series_id=_series_id_for(volume_id))
        print(f"  Vietnamese bible: {len(bible['characters'])} character(s), "
              f"{len(bible['relationships'])} relationship(s) -> {path}")
    if analyze is None:
        analyze = bool(bible_cfg.get("auto_analyze", False)) or assistant_provider in CLI_PROVIDERS
    if analyze:
        from src.utility.vietnamese.analyze import analyze_volume
        from src.utility.vietnamese.assistant import make_assistant

        print(f"  Vietnamese bible analysis (characters, xưng hô, terms) via {assistant_provider or 'assistant.provider'}...")
        analyze_volume(work_dir, make_assistant(work_dir, dry_run=dry_run, provider=assistant_provider))
        print("  Vietnamese bible analysis complete" + (" (dry run)" if dry_run else ""))


def _after_sync(volume_id: str, synced: dict | None, provider: str | None) -> None:
    """New episodes added to an existing Vietnamese workspace: extend the
    bible with them (names/xưng hô of new characters) before translating."""
    if not synced or not synced.get("added"):
        return
    if volume_target_language(WORK_DIR / volume_id) != "vi":
        return
    chosen = _configured_provider(provider)
    auto = bool((language_config("vi").get("bible") or {}).get("auto_analyze", False))
    if chosen not in CLI_PROVIDERS and not auto:
        print(f"  New chapters: {', '.join(synced['added'])}. To add their characters to the bible: "
              f"python scripts/mtl.py bible-vi {volume_id} --analyze --chapters {' '.join(synced['added'])}")
        return
    from src.utility.vietnamese.analyze import analyze_volume
    from src.utility.vietnamese.assistant import make_assistant

    print(f"  Vietnamese bible: analysing {len(synced['added'])} new chapter(s)...")
    work_dir = WORK_DIR / volume_id
    analyze_volume(work_dir, make_assistant(work_dir, provider=chosen if chosen in CLI_PROVIDERS else None),
                   chapters=list(synced["added"]))


# ── commands ────────────────────────────────────────────────────────────────

def cmd_extract(args: argparse.Namespace) -> int:
    lang = _target(args)
    manifest = run_librarian(
        epub_path=Path(args.epub_path),
        volume_id=_default_volume_id(Path(args.epub_path), lang, args.volume_id),
        source_lang="ja",
        target_lang=lang,
        force_rerun=args.force_rerun,
    )
    print(f"\nManifest saved to: {manifest.volume_id}/manifest.json  (target: {lang})")
    return 0


def cmd_import_url(args: argparse.Namespace) -> int:
    from src.utility.webimport.http import AccessDenied
    from src.utility.webimport.importer import import_work, parse_range
    from src.utility.webimport.models import AdapterError

    lang = _target(args)
    try:
        result = import_work(
            args.url, target_language=lang, episode_range=parse_range(args.range),
            volume_id=args.volume_id, refresh=args.refresh, extract=not args.no_extract,
        )
    except AccessDenied as exc:
        print(f"\nImport stopped: {exc}")
        return 2
    except (AdapterError, ValueError) as exc:
        print(f"\nImport failed: {exc}")
        return 1
    work = result.work
    print(f"\n{work.title} — {work.author} ({work.site}, {len(work.episodes)} episodes listed)")
    print(f"  Selected: {len(result.selected)}  fetched: {len(result.fetched)}  "
          f"from cache: {len(result.reused)}  changed upstream: {len(result.changed)}")
    if result.failed:
        print(f"  FAILED: {len(result.failed)} episode(s) — re-run the same command to resume:")
        for episode_id, reason in sorted(result.failed.items()):
            print(f"    {episode_id}: {reason}")
        return 1
    print(f"  Source EPUB: {result.epub_path}")
    if args.no_extract:
        return 0
    print(f"  Volume: {result.volume_id}  (target: {lang})")
    if result.synced:
        print(f"  Synced into existing workspace: added {len(result.synced['added'])}, "
              f"changed {len(result.synced['changed'])} (set back to pending), unchanged {len(result.synced['unchanged'])}")
    if args.run:
        if not args.incremental:
            _after_sync(result.volume_id, result.synced, args.provider)
        return _continue_pipeline(result.volume_id, provider=args.provider, series_id=None,
                                  thinking_log_enabled=None, prep_provider=args.prep_provider,
                                  incremental=args.incremental, window=args.window)
    print(f"\nNext: python scripts/mtl.py resume {result.volume_id}")
    return 0


def cmd_prep(args: argparse.Namespace) -> int:
    try:
        receipt = run_prep(
            args.volume_id,
            series_id=args.series_id,
            force_rerun=getattr(args, "force_rerun", False),
            provider=args.prep_provider,
        )
    except PrepError as exc:
        print(f"\nPrep failed: {exc}")
        return 1
    print(f"\nPrep complete for {receipt['volume_id']}:")
    print(f"  Chapters:   {receipt['chapter_count']}")
    print(f"  Characters: {receipt['character_count']}")
    print(f"  Series:     {receipt['series_id'] or '(not detected)'}"
          + (" [sequel — bible loaded]" if receipt["is_sequel"] else ""))
    print(f"  Blocks populated: {len(receipt['blocks_populated'])}"
          f"/{len(receipt['blocks_populated']) + len(receipt['blocks_pending'])}")
    if receipt["blocks_pending"]:
        print(f"  Still pending:    {', '.join(receipt['blocks_pending'])}")
    _after_prep(args.volume_id, assistant_provider=args.prep_provider if args.prep_provider in CLI_PROVIDERS else None)
    return 0


def _print_qc(volume_id: str, report: dict) -> None:
    status = "PASSED" if report["passed"] else "FAILED"
    print(f"\nQC {status} — {volume_id}")
    completeness = report["completeness"]
    print(f"  Completeness: {completeness['en_chapter_count']}/{completeness['jp_chapter_count']} chapters")
    if report.get("counts"):
        print("  Issues: " + ", ".join(f"{k}={v}" for k, v in report["counts"].items())
              + f"  (report: work/{volume_id}/QC/qc_vi_report.md)")
    for rec in report["recommendations"]:
        print(f"  - {rec}")


def cmd_qc(args: argparse.Namespace) -> int:
    report = run_qc(args.volume_id, llm=args.llm, dry_run=args.dry_run, chapters=args.chapters,
                    assistant_provider=args.provider)
    _print_qc(args.volume_id, report)
    return 0 if report["passed"] else 1


def cmd_translate(args: argparse.Namespace) -> int:
    try:
        provider = _check_provider_for_volume(args.volume_id, args.provider)
    except UnsupportedTargetLanguage as exc:
        print(f"\n{exc}")
        return 1
    from src.CLI.runner import CliError

    try:
        results = _resolve_translate_volume(provider)(
            args.volume_id,
            chapters=args.chapters,
            thinking_log_enabled=False if args.no_thinking_log else None,
            dry_run=args.dry_run,
        )
    except CliError as exc:
        print(f"\n[translate] stopped: {exc}\nFinished chapters are kept; re-run to continue.")
        return 1
    if args.dry_run:
        print(f"\nDry run — {len(results)} chapter(s), no API calls made:")
        for chapter_id in sorted(results):
            print(f"  {chapter_id} -> work/{args.volume_id}/DRY_RUN/")
        return 0
    print(f"\nTranslated {len(results)} chapter(s):")
    for chapter_id, output_path in sorted(results.items()):
        print(f"  {chapter_id} -> {output_path}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    result = run_builder(
        volume_id=args.volume_id,
        output_filename=args.output,
        dry_run=args.dry_run,
        profile=args.profile,
        emit_xtc=args.emit_xtc,
    )
    if result.success and result.dry_run:
        print("\nDry run — EPUB structure assembled, no .epub packaged:")
        print(f"  {result.debug_dir}")
        return 0
    if result.success:
        print(f"\nEPUB built successfully: {result.output_path}")
        _maybe_epubcheck(result.output_path)
        return 0
    print(f"\nBuild failed: {result.error}")
    return 1


def _maybe_epubcheck(epub_path) -> None:
    """Run epubcheck when EPUBCHECK_JAR (or builder.epubcheck_jar) points at
    an epubcheck.jar and Java is on PATH. Report only; never fails the build."""
    from src.builder.validation import run_epubcheck

    result = run_epubcheck(Path(epub_path))
    if result is None:
        return
    print(f"  epubcheck: {'valid' if result.ok else 'ERRORS'} — {result.summary}")


def _translate_incrementally(volume_id: str, provider: str, window: int, thinking_log_enabled):
    """Commit analysis before translation, and stop before advancing past a failure."""
    from src.OpenAI.agent import _filter_completed_chapters
    from src.CLI.agent import reading_order
    from src.utility.vietnamese.analyze import analyze_volume
    from src.utility.vietnamese.assistant import make_assistant

    work_dir = WORK_DIR / volume_id
    pending = _filter_completed_chapters(
        work_dir, sorted((work_dir / "JP").glob("CHAPTER_*.md"), key=reading_order))
    translate = _resolve_translate_volume(provider)
    results = {}
    assistant = None
    for start in range(0, len(pending), window):
        batch = pending[start:start + window]
        ids = [p.stem for p in batch]
        print(f"[incremental] {start + 1}-{start + len(batch)}/{len(pending)} pending: {', '.join(ids)}")
        if assistant is None:
            assistant = make_assistant(work_dir, provider=provider if provider in CLI_PROVIDERS else None)
        analyze_volume(work_dir, assistant, chapters=ids)
        results.update(translate(volume_id, chapters=ids, thinking_log_enabled=thinking_log_enabled))
        if _filter_completed_chapters(work_dir, batch):
            raise RuntimeError("Incremental window has unfinished chapters; fix/retry it before continuing.")
    return results


def _continue_pipeline(volume_id: str, *, provider: str | None, series_id: str | None,
                       thinking_log_enabled, skip_prep: bool = False, prep_provider: str | None = None,
                       incremental: bool = False, window: int = 20) -> int:
    """Run whatever is unfinished for this volume, in order. Each phase is
    itself resumable: prep reuses finished turns, translate skips completed
    chapters, build is idempotent."""
    try:
        provider = _check_provider_for_volume(volume_id, provider)
    except UnsupportedTargetLanguage as exc:
        print(f"\n{exc}")
        return 1
    if incremental and (window < 1 or volume_target_language(WORK_DIR / volume_id) != "vi"):
        print("--incremental requires a Vietnamese workspace and --window >= 1")
        return 2
    state = (_manifest(volume_id).get("pipeline_state") or {})
    prep_state = state.get("prep") or {}
    if not skip_prep and prep_state.get("status") != "completed":
        chosen_prep = _prep_provider(prep_provider, provider)
        if chosen_prep:
            print(f"[prep] via {chosen_prep}")
        try:
            prep_receipt = run_prep(volume_id, series_id=series_id, provider=chosen_prep)
        except PrepError as exc:
            print(f"[prep] failed: {exc}")
            if "API_KEY" in str(exc):
                print("       No API key? Prep can also run through a signed-in CLI: "
                      "--prep-provider claude (Claude Code) or --prep-provider codex (Codex CLI).")
            return 1
        print(f"[prep] {len(prep_receipt['blocks_populated'])}"
              f"/{len(prep_receipt['blocks_populated']) + len(prep_receipt['blocks_pending'])} context.xml blocks populated")
        if incremental:
            _after_prep(volume_id, analyze=False)
        else:
            _after_prep(volume_id, assistant_provider=chosen_prep if chosen_prep in CLI_PROVIDERS else None)
    else:
        print("[prep] already completed" if not skip_prep else "[prep] skipped")
        if volume_target_language(WORK_DIR / volume_id) == "vi" and not (WORK_DIR / volume_id / ".context" / "bible_vi.json").exists():
            if incremental:
                _after_prep(volume_id, analyze=False)
            else:
                _after_prep(volume_id)

    from src.CLI.runner import CliError

    try:
        if incremental:
            results = _translate_incrementally(volume_id, provider, window, thinking_log_enabled)
        else:
            results = _resolve_translate_volume(provider)(volume_id, thinking_log_enabled=thinking_log_enabled)
    except (CliError, RuntimeError) as exc:
        print(f"[translate] stopped: {exc}")
        mode_flags = f" --incremental --window {window}" if incremental else ""
        print(f"            Finished chapters are kept. Continue later with: "
              f"python scripts/mtl.py resume {volume_id} --provider {provider}{mode_flags}")
        return 1
    chapters = _manifest(volume_id).get("chapters") or []
    done = sum(1 for c in chapters if isinstance(c, dict) and c.get("translation_status") == "completed")
    print(f"[translate] {len(results)} chapter(s) this run; {done}/{len(chapters)} completed")

    qc_report = run_qc(volume_id)
    print(f"[qc] {'passed' if qc_report['passed'] else 'FAILED (see recommendations below)'}")
    if not qc_report["passed"]:
        for rec in qc_report["recommendations"]:
            print(f"       - {rec}")

    result = run_builder(volume_id=volume_id)
    if not result.success:
        print(f"[build] failed: {result.error}")
        return 1
    print(f"[build] EPUB built successfully: {result.output_path}")
    _maybe_epubcheck(result.output_path)
    return 0 if qc_report["passed"] else 1


def cmd_run(args: argparse.Namespace) -> int:
    lang = _target(args)
    source = args.epub_path
    if not source:
        if not args.volume_id:
            print("run needs an EPUB path, a Syosetu/Kakuyomu URL, or --volume-id of an existing workspace")
            return 2
        return _continue_pipeline(args.volume_id, provider=args.provider, series_id=args.series_id,
                                  thinking_log_enabled=False if args.no_thinking_log else None,
                                  prep_provider=args.prep_provider, incremental=args.incremental, window=args.window)
    if _is_url(source):
        from src.utility.webimport.http import AccessDenied
        from src.utility.webimport.importer import import_work, parse_range
        from src.utility.webimport.models import AdapterError

        try:
            result = import_work(source, target_language=lang, episode_range=parse_range(args.range),
                                 volume_id=args.volume_id)
        except AccessDenied as exc:
            print(f"[1/5] Import stopped: {exc}")
            return 2
        except (AdapterError, ValueError) as exc:
            print(f"[1/5] Import failed: {exc}")
            return 1
        if not result.complete:
            print(f"[1/5] Import incomplete ({len(result.failed)} episode(s) failed) — re-run to resume")
            return 1
        volume_id = result.volume_id
        print(f"\n[1/5] Imported -> {volume_id}")
        if result.synced:
            print(f"      updated existing workspace: {len(result.synced['added'])} new, "
                  f"{len(result.synced['changed'])} changed (set back to pending), "
                  f"{len(result.synced['unchanged'])} unchanged")
            if not args.incremental:
                _after_sync(volume_id, result.synced, args.provider)
    else:
        try:
            require_provider_support(_configured_provider(args.provider), lang)
        except UnsupportedTargetLanguage as exc:
            print(f"\n{exc}")
            return 1
        manifest = run_librarian(
            epub_path=Path(source),
            volume_id=_default_volume_id(Path(source), lang, args.volume_id),
            source_lang="ja",
            target_lang=lang,
            force_rerun=args.force_rerun,
        )
        volume_id = manifest.volume_id
        print(f"\n[1/5] Extracted -> {volume_id}/manifest.json  (target: {lang})")
    return _continue_pipeline(volume_id, provider=args.provider, series_id=args.series_id,
                              thinking_log_enabled=False if args.no_thinking_log else None,
                              prep_provider=args.prep_provider, incremental=args.incremental, window=args.window)


def cmd_resume(args: argparse.Namespace) -> int:
    if not (WORK_DIR / args.volume_id / "manifest.json").exists():
        print(f"No manifest.json for volume {args.volume_id!r} in {WORK_DIR}")
        return 1
    return _continue_pipeline(args.volume_id, provider=args.provider, series_id=args.series_id,
                              thinking_log_enabled=False if args.no_thinking_log else None,
                              skip_prep=args.skip_prep, prep_provider=args.prep_provider,
                              incremental=args.incremental, window=args.window)


def _require_vi(volume_id: str) -> Path | None:
    work_dir = WORK_DIR / volume_id
    if not (work_dir / "manifest.json").exists():
        print(f"No manifest.json for volume {volume_id!r}")
        return None
    if volume_target_language(work_dir) != "vi":
        print(f"{volume_id} is a {volume_target_language(work_dir)} workspace; this command is for Vietnamese workspaces")
        return None
    return work_dir


def cmd_repair(args: argparse.Namespace) -> int:
    work_dir = _require_vi(args.volume_id)
    if work_dir is None:
        return 1
    from src.utility.vietnamese import repair

    if args.reset:
        targets = args.reset
        if [t.lower() for t in targets] == ["all"]:
            targets = [Path(str(c.get("source_file", ""))).stem for c in _manifest(args.volume_id).get("chapters") or []]
        reset = repair.reset_chapters(work_dir, targets)
        print(f"Reset for retranslation: {', '.join(reset) or 'none'} — run `resume {args.volume_id}`")
        return 0
    if args.mechanical:
        changed = repair.mechanical_fix_volume(work_dir, args.chapters)
        print(f"Mechanical fixes applied to {sum(changed.values())}/{len(changed)} chapter(s)")
        if not (args.issue or args.severity):
            return 0
    if not (args.issue or args.severity):
        print("Nothing selected: pass --mechanical, --severity <level>, --issue <ID>..., or --reset <CHAPTER>...")
        return 2
    if not (work_dir / "QC" / "qc_vi_report.json").exists():
        print(f"No QC report yet — run: python scripts/mtl.py qc {args.volume_id} [--llm]")
        return 1
    from src.utility.vietnamese.assistant import make_assistant

    summary = repair.repair_issues(work_dir, make_assistant(work_dir, dry_run=args.dry_run, provider=args.provider),
                                   issue_ids=args.issue, min_severity=args.severity, chapters=args.chapters)
    print(f"Repaired: {len(summary['repaired'])}  unchanged: {len(summary['unchanged'])}  "
          f"rejected: {len(summary['rejected'])}  skipped: {len(summary['skipped'])}")
    for issue_id, why in summary["rejected"].items():
        print(f"  rejected {issue_id}: {why}")
    if summary["needs_retranslation"]:
        print(f"  {len(summary['needs_retranslation'])} issue(s) need chapter retranslation "
              f"(missing passages/truncation): use --reset <CHAPTER>")
    print("Re-run `qc` to verify.")
    return 0


def cmd_bible_vi(args: argparse.Namespace) -> int:
    work_dir = _require_vi(args.volume_id)
    if work_dir is None:
        return 1
    from src.Deepseek.common.config import PIPELINE_ROOT
    from src.utility.vietnamese import bible as vb

    bible_root = PIPELINE_ROOT / str(get_config_section("prep").get("bible_dir") or "bibles/")
    series_id = args.series_id or _series_id_for(args.volume_id)
    did = False
    if args.seed:
        data, path = vb.seed_volume_bible(work_dir, bible_root=bible_root, series_id=series_id)
        print(f"Seeded {path}: {len(data['characters'])} characters, {len(data['relationships'])} relationships")
        did = True
    if args.analyze:
        from src.utility.vietnamese.analyze import analyze_volume
        from src.utility.vietnamese.assistant import make_assistant

        if vb.load_volume_bible(work_dir) is None:
            vb.seed_volume_bible(work_dir, bible_root=bible_root, series_id=series_id)
        analyze_volume(work_dir, make_assistant(work_dir, dry_run=args.dry_run, provider=args.provider),
                       chapters=args.chapters, force=args.force_analyze)
        print("Analysis " + ("dry run written to DRY_RUN/assistant/" if args.dry_run else "merged into bible_vi.json"))
        did = True
    if args.lock:
        from src.utility.vietnamese.analyze import lock_established

        data = vb.load_volume_bible(work_dir)
        if data is None:
            print("No bible_vi.json yet — run with --seed first")
            return 1
        count = lock_established(data)
        vb.save_bible(vb.volume_bible_path(work_dir), data)
        print(f"Locked {count} name/term entr{'y' if count == 1 else 'ies'}")
        did = True
    if args.promote:
        if not series_id:
            print("No series id: pass --series-id")
            return 1
        data = vb.load_volume_bible(work_dir)
        if data is None:
            print("No bible_vi.json to promote")
            return 1
        target = vb.series_bible_path(bible_root, series_id)
        merged = vb.promote_to_series(data, vb.load_bible(target), volume_id=args.volume_id)
        vb.save_bible(target, merged)
        print(f"Promoted into series bible {target} (English bible files untouched)")
        did = True
    if args.show or not did:
        data = vb.load_volume_bible(work_dir)
        if data is None:
            print("No bible_vi.json yet — run with --seed")
            return 1
        print(f"Bible: {vb.volume_bible_path(work_dir)}")
        for cid, char in data["characters"].items():
            g = char.get("gender") or {}
            print(f"  {cid}: {char.get('jp')} -> {char.get('vi')} "
                  f"[{g.get('value')}/{g.get('evidence')}]{' LOCKED' if char.get('locked') else ''}")
        for rel in data["relationships"]:
            pairs = "; ".join(f"{s.get('self')}/{s.get('other')} since {s.get('from_chapter')}" for s in rel.get("address") or [])
            print(f"  {rel['from']} -> {rel['to']} ({rel.get('relation')}): {pairs or 'no address pair yet'}")
        open_amb = [a for a in data.get("ambiguities") or [] if a.get("status") == "open"]
        print(f"  Open ambiguities: {len(open_amb)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    manifest_path = WORK_DIR / args.volume_id / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest.json for volume {args.volume_id!r} at {manifest_path}")
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chapters = manifest.get("chapters", []) or []
    completed = sum(1 for c in chapters if isinstance(c, dict) and c.get("translation_status") == "completed")

    print(f"Volume:    {args.volume_id}")
    print(f"Target:    {volume_target_language(WORK_DIR / args.volume_id)}")
    print(f"Chapters:  {completed}/{len(chapters)} translated")
    blocked = [c for c in chapters if isinstance(c, dict) and c.get("translation_status") == "blocked"]
    for entry in blocked:
        print(f"  blocked {Path(str(entry.get('source_file'))).stem}: {entry.get('last_failure')}")
    pipeline_state = manifest.get("pipeline_state", {}) or {}
    if pipeline_state:
        print("Pipeline state:")
        for phase, state in sorted(pipeline_state.items()):
            status = state.get("status", "unknown") if isinstance(state, dict) else state
            print(f"  {phase}: {status}")
    if manifest.get("web_import"):
        record = manifest["web_import"]
        print(f"Web source: {record.get('url')} (episodes {record.get('first_index')}-{record.get('last_index')})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if not WORK_DIR.exists():
        print("No volumes yet — work/ does not exist.")
        return 0
    volumes = sorted(p.name for p in WORK_DIR.iterdir() if p.is_dir())
    if not volumes:
        print("No volumes yet.")
        return 0
    for volume_id in volumes:
        print(volume_id)
    return 0


def _add_target(p: argparse.ArgumentParser) -> None:
    p.add_argument("--target", "-t", type=str, default=None,
                   help="Target language for a NEW workspace: en (default from config) or vi")


def _add_provider(p: argparse.ArgumentParser, *, prep: bool = True) -> None:
    p.add_argument("--provider", "-p", type=str, choices=PROVIDERS, default=None,
                   help="Override translation.provider for this run. codex / claude use the signed-in "
                        "Codex CLI / Claude Code (subscription, no API key)")
    if prep:
        _add_prep_provider(p)


def _add_prep_provider(p: argparse.ArgumentParser) -> None:
    p.add_argument("--prep-provider", type=str, choices=PREP_PROVIDERS, default=None,
                   help="Override prep.provider (default: the --provider CLI when it is codex/claude, "
                        "else config.yaml)")


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _add_incremental(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--incremental", action="store_true",
                        help="VI: analyze then translate each pending chapter window, with checkpoints")
    parser.add_argument("--window", type=_positive_int, default=20, metavar="N",
                        help="With --incremental: chapters per window (default: 20; 1 = chapter by chapter)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mtl",
        description="LLM Translator — Japanese light novels to English or Vietnamese "
                    "(deepseek | qwen | openai | anthropic | gemini | codex | claude).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_extract = subparsers.add_parser("extract", help="Phase 1: EPUB -> JP chapters")
    p_extract.add_argument("epub_path", type=str, help="Path to source EPUB file")
    p_extract.add_argument("--volume-id", "-v", type=str, default=None, help="Custom volume ID")
    _add_target(p_extract)
    p_extract.add_argument(
        "--force-rerun", action="store_true", default=False,
        help="Re-extracting over a workspace with translations proceeds automatically into a NEW "
             "derived volume directory (old output preserved) without prompting. Frontends that "
             "cannot answer prompts pass this.",
    )
    p_extract.set_defaults(func=cmd_extract)

    p_import = subparsers.add_parser("import-url", help="Syosetu/Kakuyomu URL -> JP workspace")
    p_import.add_argument("url", type=str, help="Work or episode URL (ncode.syosetu.com / kakuyomu.jp)")
    p_import.add_argument("--range", "-r", type=str, default=None,
                          help="Episode range by reading order: 1-50, 51-, or 7 (default: all, or the episode in the URL)")
    p_import.add_argument("--volume-id", "-v", type=str, default=None, help="Custom volume ID")
    _add_target(p_import)
    _add_provider(p_import)
    p_import.add_argument("--refresh", action="store_true",
                          help="Refetch cached episodes too (detects silent edits; changed chapters go back to pending)")
    p_import.add_argument("--no-extract", action="store_true", help="Only download and build the JP EPUB")
    p_import.add_argument("--run", action="store_true", help="Continue with prep -> translate -> qc -> build")
    p_import.set_defaults(func=cmd_import_url)
    _add_incremental(p_import)

    p_prep = subparsers.add_parser("prep", help="Fill context.xml (and seed bible_vi.json for VI)")
    p_prep.add_argument("volume_id", type=str, help="Volume ID (directory name in work/)")
    p_prep.add_argument(
        "--series-id", type=str, default=None,
        help="Explicit series bible to load (default: auto-detect from bibles/ by JP title match)",
    )
    p_prep.add_argument(
        "--force-rerun", action="store_true", default=False,
        help="Re-ask every multi-turn block instead of reusing the completed artifacts under "
             "work/<vol>/.context/multiturn/. Without this a repeat run costs only the turns "
             "that never finished; pass it to redo a block that parsed but reads badly.",
    )
    _add_prep_provider(p_prep)
    p_prep.set_defaults(func=cmd_prep)

    p_translate = subparsers.add_parser("translate", help="Phase 2: JP -> EN/VI via the configured provider")
    p_translate.add_argument("volume_id", type=str, help="Volume ID (directory name in work/)")
    p_translate.add_argument(
        "--chapters", "-c", type=str, nargs="+", default=None,
        help="Specific chapter IDs to translate (default: all pending)",
    )
    _add_provider(p_translate, prep=False)
    p_translate.add_argument(
        "--no-thinking-log", action="store_true",
        help="Skip saving the provider's per-chapter reasoning to work/<vol_id>/THINKING/ (on by default)",
    )
    p_translate.add_argument(
        "--dry-run", action="store_true",
        help="Developer flag: assemble each chapter's full API payload and write it to "
             "work/<vol_id>/DRY_RUN/ instead of sending it. Zero API cost, no output, "
             "no manifest changes. Bypasses the manifest completion filter — every "
             "requested chapter is assembled, already-completed or not.",
    )
    p_translate.set_defaults(func=cmd_translate)

    p_qc = subparsers.add_parser("qc", help="QC gate (VI: deterministic checks; --llm adds semantic review)")
    p_qc.add_argument("volume_id", type=str, help="Volume ID (directory name in work/)")
    p_qc.add_argument("--llm", action="store_true", help="VI only: add the LLM semantic review (paid API calls)")
    p_qc.add_argument("--dry-run", action="store_true", help="With --llm: write review payloads, send nothing")
    p_qc.add_argument("--chapters", "-c", nargs="+", default=None, help="VI only: restrict to these chapters")
    p_qc.add_argument("--provider", type=str, choices=("openai", "gemini", "codex", "claude"), default=None,
                      help="With --llm: override assistant.provider (codex/claude = subscription CLI)")
    p_qc.set_defaults(func=cmd_qc)

    p_repair = subparsers.add_parser("repair", help="VI: fix QC issues selectively")
    p_repair.add_argument("volume_id", type=str)
    p_repair.add_argument("--mechanical", action="store_true",
                          help="Deterministic fixes only (NFC, ellipsis, fullwidth punctuation, quotes); no API")
    p_repair.add_argument("--severity", choices=["critical", "major", "minor"], default=None,
                          help="LLM-repair every open, located issue at or above this severity")
    p_repair.add_argument("--issue", nargs="+", default=None, help="LLM-repair these issue IDs from the QC report")
    p_repair.add_argument("--chapters", "-c", nargs="+", default=None, help="Restrict to these chapters")
    p_repair.add_argument("--reset", nargs="+", default=None, metavar="CHAPTER",
                          help="Send chapters back to translation (old rendering kept in .context/stale/)")
    p_repair.add_argument("--dry-run", action="store_true", help="Write repair payloads, change nothing")
    p_repair.add_argument("--provider", type=str, choices=("openai", "gemini", "codex", "claude"), default=None,
                          help="LLM repair: override assistant.provider (codex/claude = subscription CLI)")
    p_repair.set_defaults(func=cmd_repair)

    p_bible = subparsers.add_parser("bible-vi", help="VI: Series Bible (characters, xưng hô, glossary)")
    p_bible.add_argument("volume_id", type=str)
    p_bible.add_argument("--seed", action="store_true", help="(Re)seed from context.xml; never overwrites locked/stronger data")
    p_bible.add_argument("--analyze", action="store_true", help="LLM pass: relationships, pronoun matrix, terms (paid)")
    p_bible.add_argument("--force-analyze", action="store_true", help="With --analyze: reanalyze even unchanged chapters")
    p_bible.add_argument("--lock", action="store_true", help="Approve: lock every name/term that has a Vietnamese form")
    p_bible.add_argument("--promote", action="store_true", help="Merge this volume into bibles/<series>/vi/bible_vi.json")
    p_bible.add_argument("--series-id", type=str, default=None)
    p_bible.add_argument("--show", action="store_true", help="Print the bible summary")
    p_bible.add_argument("--dry-run", action="store_true", help="With --analyze: write payloads, send nothing")
    p_bible.add_argument("--chapters", nargs="+", default=None, metavar="CHAPTER",
                         help="With --analyze: only these chapters (e.g. newly imported ones)")
    p_bible.add_argument("--provider", type=str, choices=("openai", "gemini", "codex", "claude"), default=None,
                         help="With --analyze: override assistant.provider (codex/claude = subscription CLI)")
    p_bible.set_defaults(func=cmd_bible_vi)

    p_build = subparsers.add_parser("build", help="Phase 4: translation -> EPUB")
    p_build.add_argument("volume_id", type=str, help="Volume ID (directory name in work/)")
    p_build.add_argument("--output", "-o", type=str, default=None, help="Output filename")
    p_build.add_argument(
        "--dry-run", action="store_true",
        help="Developer flag: assemble the EPUB structure and write it to "
             "work/<vol_id>/DRY_RUN/ instead of packaging a .epub. No manifest changes.",
    )
    p_build.add_argument(
        "--profile", type=str, choices=list(PROFILE_NAMES), default=None,
        help="Target device profile (default: builder.device_profile in config.yaml). "
             "xteink-x3 / xteink-x4 fit images to the panel, convert them to grayscale, "
             "force baseline JPEG and ship the e-ink stylesheet.",
    )
    p_build.add_argument(
        "--emit-xtc", type=str, choices=["xtc", "xtch"], default=None,
        help="Additionally render the CrossPoint native format. Requires an "
             "xteink-* profile and builder.xtc configured with a converter "
             "checkout and a font. Pre-rendered bitmap pages: ~10x the file "
             "size and no reader font/size control. Never fails the build.",
    )
    p_build.set_defaults(func=cmd_build)

    p_run = subparsers.add_parser("run", help="Full pipeline: extract/import -> prep -> translate -> qc -> build")
    p_run.add_argument("epub_path", type=str, nargs="?", default=None,
                       help="Source EPUB, or a Syosetu/Kakuyomu URL (omit with --volume-id to continue a workspace)")
    p_run.add_argument("--volume-id", "-v", type=str, default=None, help="Custom volume ID")
    p_run.add_argument("--series-id", type=str, default=None, help="Explicit series bible to load for prep")
    _add_target(p_run)
    _add_provider(p_run)
    p_run.add_argument("--range", "-r", type=str, default=None, help="URL input: episode range, e.g. 1-50")
    p_run.add_argument(
        "--no-thinking-log", action="store_true",
        help="Skip saving the provider's per-chapter reasoning to work/<vol_id>/THINKING/ (on by default)",
    )
    p_run.add_argument(
        "--force-rerun", action="store_true", default=False,
        help="Re-extracting over a workspace with translations proceeds automatically into a NEW "
             "derived volume directory (old output preserved) without prompting. Frontends that "
             "cannot answer prompts pass this.",
    )
    p_run.set_defaults(func=cmd_run)
    _add_incremental(p_run)

    p_resume = subparsers.add_parser("resume", help="Continue an interrupted volume (unfinished phases/chapters only)")
    p_resume.add_argument("volume_id", type=str)
    _add_provider(p_resume)
    p_resume.add_argument("--series-id", type=str, default=None)
    p_resume.add_argument("--skip-prep", action="store_true", help="Translate without context.xml prep")
    p_resume.add_argument("--no-thinking-log", action="store_true")
    p_resume.set_defaults(func=cmd_resume)
    _add_incremental(p_resume)

    p_list = subparsers.add_parser("list", help="List volumes in work/")
    p_list.set_defaults(func=cmd_list)

    p_status = subparsers.add_parser("status", help="Pipeline state + chapter completion summary")
    p_status.add_argument("volume_id", type=str, help="Volume ID (directory name in work/)")
    p_status.set_defaults(func=cmd_status)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
