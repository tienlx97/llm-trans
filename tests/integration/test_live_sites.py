"""Live checks against the real sites. Skipped unless MTL_LIVE_NETWORK=1.

Each test makes at most three polite requests (robots.txt, the work page,
one episode) through the production fetcher, to catch site layout changes
the offline fixtures cannot. Override the targets with MTL_LIVE_SYOSETU_URL /
MTL_LIVE_KAKUYOMU_URL if a default work disappears.
"""

from __future__ import annotations

import os

import pytest

from src.utility.webimport import kakuyomu, syosetu
from src.utility.webimport.importer import make_fetcher

pytestmark = [pytest.mark.network, pytest.mark.integration]


def test_syosetu_live():
    url = os.environ.get("MTL_LIVE_SYOSETU_URL", "https://ncode.syosetu.com/n9669bk/")
    fetcher = make_fetcher()
    ncode, _ = syosetu.parse_url(url)
    title, author, _syn, refs, _next, _grp = syosetu.parse_toc_page(fetcher.fetch(syosetu.work_url(ncode)),
                                                                   syosetu.work_url(ncode), ncode, 0)
    assert title and author and refs
    episode = syosetu.parse_episode(fetcher.fetch(refs[0].url), refs[0])
    assert sum(1 for line in episode.body if line.strip()) > 5


def test_kakuyomu_live():
    url = os.environ.get("MTL_LIVE_KAKUYOMU_URL", "https://kakuyomu.jp/works/11054822663391611796")
    fetcher = make_fetcher()
    work = kakuyomu.fetch_work(url, fetcher.fetch)
    assert work.title and work.episodes
    episode = kakuyomu.parse_episode(fetcher.fetch(work.episodes[0].url), work.episodes[0])
    assert sum(1 for line in episode.body if line.strip()) > 5
