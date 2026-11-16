"""小説家になろう (ncode.syosetu.com) adapter.

Only the general-audience host is supported. novel18.syosetu.com sits behind
an age confirmation and is refused rather than worked around.

Layout (2024+ site design): the work page lists episodes as
``.p-eplist__sublist > a.p-eplist__subtitle`` with arc headings in
``.p-eplist__chapter-title`` and is paginated (``a.c-pager__item--next``);
an episode page carries ``h1.p-novel__title`` and ``.p-novel__body`` with
``.p-novel__text`` blocks (``--preface`` / ``--afterword`` modifiers). A short
story (短編) has no episode list: its body sits on the work page itself.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from src.utility.webimport.models import AdapterError, Episode, EpisodeRef, WorkInfo
from src.utility.webimport.text import clean_text, node_text, paragraph_texts

SITE = "syosetu"
BASE = "https://ncode.syosetu.com"
_URL_RE = re.compile(r"^https?://ncode\.syosetu\.com/(n\d{4}[a-z]{1,3})/?(?:(\d+)/?)?(?:[?#].*)?$", re.IGNORECASE)
_MAX_TOC_PAGES = 200


def matches(url: str) -> bool:
    return bool(_URL_RE.match(url.strip()))


def parse_url(url: str) -> Tuple[str, Optional[int]]:
    """``(ncode, episode_number_or_None)``."""
    if "novel18.syosetu.com" in url:
        raise AdapterError("novel18.syosetu.com is age-gated; this importer does not bypass age confirmation.")
    match = _URL_RE.match(url.strip())
    if not match:
        raise AdapterError(f"Not a Syosetu work or episode URL: {url}")
    return match.group(1).lower(), int(match.group(2)) if match.group(2) else None


def work_url(ncode: str) -> str:
    return f"{BASE}/{ncode}/"


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def parse_toc_page(html: str, page_url: str, ncode: str, start_index: int, group_title: str = "") -> Tuple[str, str, str, List[EpisodeRef], Optional[str], str]:
    """One TOC page -> (title, author, synopsis, refs, next_page_url, last_group_title)."""
    soup = _soup(html)
    title_el = soup.select_one("h1.p-novel__title") or soup.select_one(".p-novel__title")
    title = clean_text(title_el.get_text()) if title_el else ""
    author_el = soup.select_one(".p-novel__author")
    author = clean_text(author_el.get_text(" ")) if author_el else ""
    author = re.sub(r"^\s*作者[：:]\s*", "", author)
    summary_el = soup.select_one("#novel_ex") or soup.select_one(".p-novel__summary")
    synopsis = clean_text(node_text(summary_el)) if summary_el else ""

    refs: List[EpisodeRef] = []
    index = start_index
    current_group = group_title
    for item in soup.select(".p-eplist > *"):
        classes = item.get("class") or []
        if "p-eplist__chapter-title" in classes:
            current_group = clean_text(item.get_text())
            continue
        if "p-eplist__sublist" not in classes:
            continue
        link = item.select_one("a.p-eplist__subtitle")
        if link is None or not link.get("href"):
            continue
        href = urljoin(page_url, link["href"])
        m = re.search(r"/%s/(\d+)/?" % re.escape(ncode), href, re.IGNORECASE)
        if not m:
            continue
        update_el = item.select_one(".p-eplist__update")
        revised = update_el.select_one("span[title]") if update_el else None
        hint = clean_text(revised.get("title")) if revised is not None else (clean_text(update_el.get_text(" ")) if update_el else "")
        index += 1
        refs.append(EpisodeRef(
            episode_id=m.group(1), url=f"{BASE}/{ncode}/{m.group(1)}/", title=clean_text(link.get_text()),
            index=index, group_title=current_group, updated_hint=hint,
        ))
    next_el = soup.select_one("a.c-pager__item--next")
    next_url = urljoin(page_url, next_el["href"]) if next_el is not None and next_el.get("href") else None
    return title, author, synopsis, refs, next_url, current_group


def fetch_work(url: str, fetch: Callable[[str], str]) -> WorkInfo:
    ncode, _episode = parse_url(url)
    page_url: Optional[str] = work_url(ncode)
    title = author = synopsis = ""
    refs: List[EpisodeRef] = []
    group = ""
    seen_pages = set()
    first_html = ""
    while page_url and page_url not in seen_pages and len(seen_pages) < _MAX_TOC_PAGES:
        seen_pages.add(page_url)
        html = fetch(page_url)
        if not first_html:
            first_html = html
        page_title, page_author, page_synopsis, page_refs, page_url, group = parse_toc_page(
            html, page_url, ncode, len(refs), group
        )
        title, author, synopsis = title or page_title, author or page_author, synopsis or page_synopsis
        refs.extend(page_refs)
    if not refs:
        # 短編: the work page is the only "episode".
        soup = _soup(first_html)
        if soup.select_one(".p-novel__body") is None:
            raise AdapterError(f"{work_url(ncode)}: no episode list and no novel body found")
        refs = [EpisodeRef(episode_id="1", url=work_url(ncode), title=title, index=1)]
    if not title:
        raise AdapterError(f"{work_url(ncode)}: work title not found (page layout changed?)")
    return WorkInfo(site=SITE, work_id=ncode, url=work_url(ncode), title=title, author=author,
                    synopsis=synopsis, episodes=refs)


def parse_episode(html: str, ref: EpisodeRef) -> Episode:
    soup = _soup(html)
    body_root = soup.select_one(".p-novel__body")
    if body_root is None:
        raise AdapterError(f"{ref.url}: novel body not found (page layout changed or access restricted)")
    title_el = soup.select_one("h1.p-novel__title")
    title = clean_text(title_el.get_text()) if title_el else ref.title
    preface: List[str] = []
    afterword: List[str] = []
    body: List[str] = []
    for block in body_root.select(".p-novel__text"):
        classes = block.get("class") or []
        lines = paragraph_texts(block)
        if "p-novel__text--preface" in classes:
            preface.extend(lines)
        elif "p-novel__text--afterword" in classes:
            afterword.extend(lines)
        else:
            body.extend(lines)
    if not any(line.strip() for line in body):
        raise AdapterError(f"{ref.url}: episode body is empty")
    return Episode(ref=ref, title=title or ref.title, body=body, preface=preface, afterword=afterword)


def site_host(url: str) -> str:
    return urlsplit(url).netloc
