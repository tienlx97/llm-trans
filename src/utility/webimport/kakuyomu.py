"""カクヨム (kakuyomu.jp) adapter.

The work page embeds its table of contents as Next.js data
(``script#__NEXT_DATA__`` -> ``props.pageProps.__APOLLO_STATE__``): the
``Work:<id>`` record's ``tableOfContentsV2`` lists ``TableOfContentsChapter``
records, each with an optional ``Chapter`` heading and ``episodeUnions`` refs
to ``Episode`` records. The page also embeds OTHER works (recommendations),
so the work record is looked up by the requested id, never "the first Work".

An episode page carries ``.widget-episodeTitle`` and ``.widget-episodeBody``
with one ``<p>`` per line. robots.txt disallows ``/works/*/episodes/*/read``;
this adapter never requests that path, and the fetcher enforces robots.txt
regardless.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from src.utility.webimport.models import AdapterError, Episode, EpisodeRef, WorkInfo
from src.utility.webimport.text import clean_text, paragraph_texts

SITE = "kakuyomu"
BASE = "https://kakuyomu.jp"
_URL_RE = re.compile(r"^https?://kakuyomu\.jp/works/(\d+)(?:/episodes/(\d+))?/?(?:[?#].*)?$")


def matches(url: str) -> bool:
    return bool(_URL_RE.match(url.strip()))


def parse_url(url: str) -> Tuple[str, Optional[str]]:
    match = _URL_RE.match(url.strip())
    if not match:
        raise AdapterError(f"Not a Kakuyomu work or episode URL: {url}")
    return match.group(1), match.group(2)


def work_url(work_id: str) -> str:
    return f"{BASE}/works/{work_id}"


def episode_url(work_id: str, episode_id: str) -> str:
    return f"{BASE}/works/{work_id}/episodes/{episode_id}"


def _apollo_state(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("script#__NEXT_DATA__")
    if node is None or not node.string:
        raise AdapterError("Kakuyomu work page has no __NEXT_DATA__ (layout changed or access restricted)")
    try:
        data = json.loads(node.string)
        state = data["props"]["pageProps"]["__APOLLO_STATE__"]
    except (ValueError, KeyError, TypeError) as exc:
        raise AdapterError(f"Kakuyomu work data not parseable: {exc}") from exc
    if not isinstance(state, dict):
        raise AdapterError("Kakuyomu work data has an unexpected shape")
    return state


def _deref(state: Dict[str, Any], value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict) and "__ref" in value:
        resolved = state.get(value["__ref"])
        return resolved if isinstance(resolved, dict) else None
    return value if isinstance(value, dict) else None


def parse_work_page(html: str, work_id: str) -> WorkInfo:
    state = _apollo_state(html)
    work = state.get(f"Work:{work_id}")
    if not isinstance(work, dict):
        raise AdapterError(f"Kakuyomu work {work_id} not found in page data")
    author_rec = _deref(state, work.get("author")) or {}
    author = clean_text(str(work.get("alternateAuthorName") or author_rec.get("activityName") or author_rec.get("name") or ""))
    toc = work.get("tableOfContentsV2")
    if toc is None:
        toc = work.get("tableOfContents")
    refs: List[EpisodeRef] = []
    for entry in toc or []:
        section = _deref(state, entry) or {}
        chapter = _deref(state, section.get("chapter")) or {}
        group = clean_text(str(chapter.get("title") or ""))
        for ep_ref in section.get("episodeUnions") or section.get("episodes") or []:
            episode = _deref(state, ep_ref) or {}
            if episode.get("__typename") not in (None, "Episode"):
                continue
            ep_id = str(episode.get("id") or "")
            if not ep_id:
                continue
            refs.append(EpisodeRef(
                episode_id=ep_id, url=episode_url(work_id, ep_id), title=clean_text(str(episode.get("title") or "")),
                index=len(refs) + 1, group_title=group, updated_hint=str(episode.get("publishedAt") or ""),
            ))
    title = clean_text(str(work.get("title") or ""))
    if not title:
        raise AdapterError(f"Kakuyomu work {work_id}: title missing from page data")
    return WorkInfo(site=SITE, work_id=work_id, url=work_url(work_id), title=title, author=author,
                    synopsis=clean_text(str(work.get("introduction") or "")), episodes=refs)


def fetch_work(url: str, fetch: Callable[[str], str]) -> WorkInfo:
    work_id, _episode = parse_url(url)
    return parse_work_page(fetch(work_url(work_id)), work_id)


def parse_episode(html: str, ref: EpisodeRef) -> Episode:
    soup = BeautifulSoup(html, "lxml")
    body_el = soup.select_one(".widget-episodeBody")
    if body_el is None:
        raise AdapterError(f"{ref.url}: episode body not found (layout changed or access restricted)")
    title_el = soup.select_one(".widget-episodeTitle")
    body = paragraph_texts(body_el)
    if not any(line.strip() for line in body):
        raise AdapterError(f"{ref.url}: episode body is empty")
    return Episode(ref=ref, title=clean_text(title_el.get_text()) if title_el else ref.title, body=body)
