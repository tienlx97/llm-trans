"""Web novel import: TOC -> checkpointed episode cache -> JP EPUB -> workspace.

Cache layout (``web_import.cache_dir``, default raw/web/):

    <site>_<work_id>/state.json             work metadata + per-episode ledger
    <site>_<work_id>/episodes/<id>.json     parsed episode (text + hash)
    <site>_<work_id>/<first>-<last>.epub    synthesized source volume

Resume: every episode is written to the cache the moment it is parsed, and
the ledger right after, so an interrupted import refetches nothing it already
has. A cached episode is only refetched when the site's own update stamp for
it changed, or with ``refresh=True``.

Change detection: a refetched episode whose content hash differs from the
cached one is reported as changed. When the target workspace already exists,
the new extraction is synced into it chapter by chapter: new chapters are
added, changed chapters replace their JP source and go back to ``pending``
(the stale translation is kept under .context/stale/), unchanged chapters and
their translations are left alone.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.utility.webimport import kakuyomu, syosetu
from src.utility.webimport.epub_synth import build_epub
from src.utility.webimport.http import AccessDenied, FetchError, PoliteFetcher
from src.utility.webimport.models import AdapterError, Episode, EpisodeRef, WorkInfo
from src.utility.webimport.text import finalize_furigana

logger = logging.getLogger(__name__)
ADAPTERS = (syosetu, kakuyomu)


@dataclass
class ImportResult:
    work: WorkInfo
    cache_dir: Path
    selected: List[EpisodeRef]
    fetched: List[str] = field(default_factory=list)
    reused: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    epub_path: Optional[Path] = None
    volume_id: Optional[str] = None
    synced: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.failed and self.epub_path is not None


def resolve_adapter(url: str):
    for adapter in ADAPTERS:
        if adapter.matches(url):
            return adapter
    if "syosetu.com" in url:
        return syosetu  # parse_url raises a precise error (e.g. novel18)
    raise AdapterError(
        f"Unsupported URL: {url}. Supported: https://ncode.syosetu.com/<ncode>/ and https://kakuyomu.jp/works/<id>"
    )


def parse_range(value: Optional[str]) -> Optional[Tuple[int, int]]:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*(?:[-:]\s*(\d+)?)?\s*", value)
    if not match:
        raise ValueError(f"Invalid episode range {value!r}; use e.g. 1-50, 51-, or 7")
    first = int(match.group(1))
    if match.group(2):
        last = int(match.group(2))
    elif "-" in value or ":" in value:
        last = 10 ** 9
    else:
        last = first
    if first < 1 or last < first:
        raise ValueError(f"Invalid episode range {value!r}")
    return first, last


class WorkCache:
    def __init__(self, root: Path, site: str, work_id: str):
        self.dir = Path(root) / f"{site}_{work_id}"
        self.episodes_dir = self.dir / "episodes"
        self.state_path = self.dir / "state.json"
        self.state: Dict[str, Any] = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self) -> None:
        from src.Deepseek.common.atomic_io import atomic_write_json

        self.dir.mkdir(parents=True, exist_ok=True)
        self.state["updated_at"] = _now()
        atomic_write_json(self.state_path, self.state)

    def entry(self, episode_id: str) -> Dict[str, Any]:
        return self.state.setdefault("episodes", {}).setdefault(str(episode_id), {})

    def load_episode(self, episode_id: str) -> Optional[Episode]:
        try:
            return Episode.from_dict(json.loads((self.episodes_dir / f"{episode_id}.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return None

    def save_episode(self, episode: Episode) -> None:
        from src.Deepseek.common.atomic_io import atomic_write_json

        self.episodes_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.episodes_dir / f"{episode.ref.episode_id}.json", episode.to_dict())


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_volume_id(work: WorkInfo, first: int, last: int, target_language: str) -> str:
    safe_id = re.sub(r"[^\w]", "", work.work_id)
    return f"web_{work.site}_{safe_id}_{first:04d}-{last:04d}_{target_language}"


def _select(work: WorkInfo, url: str, adapter, episode_range: Optional[Tuple[int, int]]) -> List[EpisodeRef]:
    if episode_range:
        first, last = episode_range
        return [ref for ref in work.episodes if first <= ref.index <= last]
    _work_id, episode = adapter.parse_url(url)
    if episode is not None:
        wanted = str(episode)
        return [ref for ref in work.episodes if ref.episode_id == wanted]
    return list(work.episodes)


def make_fetcher() -> PoliteFetcher:
    from src.Deepseek.common.config import get_config_section

    cfg = get_config_section("web_import") or {}
    return PoliteFetcher(
        user_agent=str(cfg.get("user_agent") or "llm-LN-translator/1.0 (respects robots.txt)"),
        min_delay=float(cfg.get("min_delay_seconds", 2.0) or 2.0),
        timeout=float(cfg.get("timeout_seconds", 30) or 30),
        max_retries=int(cfg.get("max_retries", 3) or 3),
    )


def default_cache_root() -> Path:
    from src.Deepseek.common.config import PIPELINE_ROOT, get_config_section

    raw = Path(str((get_config_section("web_import") or {}).get("cache_dir") or "raw/web/"))
    return raw if raw.is_absolute() else PIPELINE_ROOT / raw


def download(
    url: str,
    *,
    episode_range: Optional[Tuple[int, int]] = None,
    refresh: bool = False,
    fetcher: Optional[PoliteFetcher] = None,
    cache_root: Optional[Path] = None,
) -> Tuple[ImportResult, List[Episode]]:
    """Fetch the TOC and every selected episode not already cached."""
    adapter = resolve_adapter(url)
    fetcher = fetcher or make_fetcher()
    work = adapter.fetch_work(url, fetcher.fetch)
    cache = WorkCache(cache_root or default_cache_root(), work.site, work.work_id)
    cache.state.update({
        "site": work.site, "work_id": work.work_id, "url": work.url, "title": work.title,
        "author": work.author, "synopsis": work.synopsis, "episode_count": len(work.episodes),
    })
    cache.save_state()
    selected = _select(work, url, adapter, episode_range)
    if not selected:
        raise AdapterError(f"No episodes selected from {work.url} (the work lists {len(work.episodes)})")
    result = ImportResult(work=work, cache_dir=cache.dir, selected=selected)
    episodes: List[Episode] = []
    for ref in selected:
        entry = cache.entry(ref.episode_id)
        cached = cache.load_episode(ref.episode_id)
        same_stamp = bool(ref.updated_hint) and entry.get("updated_hint") == ref.updated_hint
        if cached is not None and not refresh and entry.get("status") == "ok" and (same_stamp or not ref.updated_hint):
            cached.ref = ref
            episodes.append(cached)
            result.reused.append(ref.episode_id)
            continue
        try:
            episode = adapter.parse_episode(fetcher.fetch(ref.url), ref)
        except AccessDenied:
            entry.update({"status": "denied", "error": "access denied", "checked_at": _now()})
            cache.save_state()
            raise
        except (FetchError, AdapterError) as exc:
            entry.update({"status": "failed", "error": str(exc)[:500], "checked_at": _now()})
            cache.save_state()
            result.failed[ref.episode_id] = str(exc)
            logger.error("[WEB-IMPORT] episode %s failed: %s", ref.episode_id, exc)
            continue
        new_hash = episode.content_hash()
        if cached is not None and entry.get("hash") and entry.get("hash") != new_hash:
            result.changed.append(ref.episode_id)
        cache.save_episode(episode)
        entry.update({
            "status": "ok", "index": ref.index, "title": episode.title, "url": ref.url,
            "group_title": ref.group_title, "updated_hint": ref.updated_hint, "hash": new_hash,
            "fetched_at": _now(), "error": "",
        })
        cache.save_state()
        episodes.append(episode)
        result.fetched.append(ref.episode_id)
        logger.info("[WEB-IMPORT] %s/%s %s", ref.index, selected[-1].index, episode.title)
    return result, episodes


def finalize_episodes(episodes: List[Episode]) -> List[Episode]:
    """Resolve first-occurrence furigana across the volume, in reading order."""
    seen: set = set()
    out: List[Episode] = []
    for ep in episodes:
        fix = lambda lines: [finalize_furigana(line, seen) for line in lines]  # noqa: E731
        out.append(Episode(ref=ep.ref, title=finalize_furigana(ep.title, set()), preface=fix(ep.preface),
                           body=fix(ep.body), afterword=fix(ep.afterword)))
    return out


def import_work(
    url: str,
    *,
    target_language: str = "en",
    episode_range: Optional[Tuple[int, int]] = None,
    volume_id: Optional[str] = None,
    refresh: bool = False,
    extract: bool = True,
    fetcher: Optional[PoliteFetcher] = None,
    cache_root: Optional[Path] = None,
    work_base: Optional[Path] = None,
) -> ImportResult:
    result, episodes = download(url, episode_range=episode_range, refresh=refresh, fetcher=fetcher, cache_root=cache_root)
    if result.failed:
        logger.error(
            "[WEB-IMPORT] %d episode(s) failed; no volume built. Re-run the same command to resume — "
            "cached episodes are not fetched again.", len(result.failed),
        )
        return result
    episodes.sort(key=lambda ep: ep.ref.index)
    first, last = episodes[0].ref.index, episodes[-1].ref.index
    label = f"{first:04d}-{last:04d}"
    result.epub_path = build_epub(result.work, finalize_episodes(episodes), result.cache_dir / f"{label}.epub",
                                  volume_label=f"[{first}-{last}]")
    from src.Deepseek.common.config import WORK_DIR

    base = Path(work_base) if work_base else WORK_DIR
    if not volume_id and extract:
        # Re-running the same command after the author posted new episodes
        # must grow the existing workspace, not start a new one whose id
        # (…_0001-0110_vi) differs only because the range grew.
        volume_id = find_existing_workspace(base, result.work.site, result.work.work_id, first, target_language)
        if volume_id:
            logger.info("[WEB-IMPORT] updating existing workspace %s", volume_id)
    result.volume_id = volume_id or default_volume_id(result.work, first, last, target_language)
    if not extract:
        return result

    import_record = {
        "site": result.work.site, "work_id": result.work.work_id, "url": result.work.url,
        "first_index": first, "last_index": last, "episode_ids": [ep.ref.episode_id for ep in episodes],
        "hashes": {ep.ref.episode_id: ep.content_hash() for ep in episodes}, "imported_at": _now(),
    }
    workspace = base / result.volume_id
    if (workspace / "manifest.json").exists():
        result.synced = sync_into_existing(workspace, result.epub_path, target_language, import_record)
    else:
        from src.utility.librarian.agent import LibrarianAgent

        LibrarianAgent(work_base=base).process_epub(
            result.epub_path, result.volume_id, "ja", target_language, force_rerun=True
        )
        _stamp_import(workspace, import_record)
    return result


def find_existing_workspace(base: Path, site: str, work_id: str, first_index: int, target_language: str) -> Optional[str]:
    """The workspace already importing this work from the same first episode
    into the same language (the one covering the most episodes)."""
    from src.Deepseek.common.language import volume_target_language

    best: Optional[Tuple[int, str]] = None
    for manifest_path in Path(base).glob("*/manifest.json"):
        try:
            record = json.loads(manifest_path.read_text(encoding="utf-8")).get("web_import") or {}
        except (OSError, ValueError):
            continue
        if (record.get("site"), str(record.get("work_id"))) != (site, str(work_id)):
            continue
        if int(record.get("first_index", 0) or 0) != int(first_index):
            continue
        if volume_target_language(manifest_path.parent) != target_language:
            continue
        last = int(record.get("last_index", 0) or 0)
        if best is None or last > best[0]:
            best = (last, manifest_path.parent.name)
    return best[1] if best else None


def _stamp_import(workspace: Path, record: Dict[str, Any]) -> None:
    from src.Deepseek.common.atomic_io import atomic_write_json

    path = workspace / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["web_import"] = record
    atomic_write_json(path, manifest)


def sync_into_existing(workspace: Path, epub_path: Path, target_language: str, record: Dict[str, Any]) -> Dict[str, List[str]]:
    """Extract into a scratch workspace and merge chapter by chapter."""
    from src.Deepseek.common.atomic_io import atomic_write_json
    from src.Deepseek.common.language import translated_path, volume_target_language
    from src.utility.librarian.agent import LibrarianAgent

    existing_lang = volume_target_language(workspace)
    if existing_lang != target_language:
        raise ValueError(
            f"Workspace {workspace.name} is a {existing_lang} workspace; refusing to sync a {target_language} import "
            "into it. Use --volume-id to create a separate workspace."
        )
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    previous = manifest.get("web_import") or {}
    if previous and int(previous.get("first_index", 0)) != int(record["first_index"]):
        raise ValueError(
            f"{workspace.name} starts at episode {previous.get('first_index')}, this import at "
            f"{record['first_index']}; chapter numbering would shift. Use a new --volume-id."
        )

    report: Dict[str, List[str]] = {"added": [], "changed": [], "unchanged": []}
    with tempfile.TemporaryDirectory(prefix="mtl_import_", dir=str(workspace.parent)) as tmp:
        tmp_base = Path(tmp)
        LibrarianAgent(work_base=tmp_base).process_epub(epub_path, workspace.name, "ja", target_language, force_rerun=True)
        fresh_dir = tmp_base / workspace.name
        fresh_manifest = json.loads((fresh_dir / "manifest.json").read_text(encoding="utf-8"))
        existing_by_file = {c.get("source_file"): c for c in manifest.get("chapters", []) if isinstance(c, dict)}
        stale_dir = workspace / ".context" / "stale"
        for chapter in fresh_manifest.get("chapters", []):
            source_file = chapter.get("source_file")
            new_jp = fresh_dir / "JP" / source_file
            old_jp = workspace / "JP" / source_file
            if source_file not in existing_by_file:
                (workspace / "JP").mkdir(parents=True, exist_ok=True)
                shutil.copy2(new_jp, old_jp)
                manifest.setdefault("chapters", []).append(chapter)
                report["added"].append(Path(source_file).stem)
                continue
            if old_jp.exists() and old_jp.read_bytes() == new_jp.read_bytes():
                report["unchanged"].append(Path(source_file).stem)
                continue
            shutil.copy2(new_jp, old_jp)
            stem = Path(source_file).stem
            translation = translated_path(workspace, stem, target_language)
            if translation.exists():
                stale_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(translation, stale_dir / f"{translation.stem}.{datetime.now().strftime('%Y%m%d%H%M%S')}.md")
            entry = existing_by_file[source_file]
            entry["translation_status"] = "pending"
            entry["qc_status"] = "pending"
            entry["last_failure"] = "source changed upstream (web re-import); retranslation required"
            report["changed"].append(stem)
    manifest["web_import"] = record
    if report["added"] or report["changed"]:
        prep_state = (manifest.get("pipeline_state") or {}).get("prep")
        if isinstance(prep_state, dict):
            prep_state["stale_chapters"] = sorted(set(prep_state.get("stale_chapters", []) + report["added"] + report["changed"]))
    atomic_write_json(manifest_path, manifest)
    return report
