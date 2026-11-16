"""Site adapters, robots/politeness, checkpointed download, resume, change
detection and sync — all against a scripted in-memory transport."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from fixtures import web as F
from src.builder.validation import validate_epub_structure
from src.utility.webimport import kakuyomu, syosetu
from src.utility.webimport.http import AccessDenied, FetchError, PoliteFetcher, RobotsRules
from src.utility.webimport.importer import download, import_work, parse_range, resolve_adapter
from src.utility.webimport.models import AdapterError

UA = "llm-LN-translator/1.0 (test)"


# ── adapters ─────────────────────────────────────────────────────────────

def test_url_routing_and_age_gate():
    assert resolve_adapter("https://ncode.syosetu.com/n1234ab/") is syosetu
    assert syosetu.parse_url("https://ncode.syosetu.com/N1234AB/7/") == ("n1234ab", 7)
    assert kakuyomu.parse_url("https://kakuyomu.jp/works/123/episodes/456") == ("123", "456")
    with pytest.raises(AdapterError, match="age"):
        syosetu.parse_url("https://novel18.syosetu.com/n1234ab/")
    with pytest.raises(AdapterError):
        resolve_adapter("https://example.com/novel")


def test_syosetu_toc_pagination_and_groups():
    pages = {"https://ncode.syosetu.com/n1234ab/": F.SYOSETU_TOC_P1,
             "https://ncode.syosetu.com/n1234ab/?p=2": F.SYOSETU_TOC_P2}
    work = syosetu.fetch_work("https://ncode.syosetu.com/n1234ab/", pages.__getitem__)
    assert work.title == "灯火の約束　〜試験用〜" and work.author == "試験 作者"
    assert [(e.index, e.episode_id, e.group_title) for e in work.episodes] == [
        (1, "1", "第１章　出会い"), (2, "2", "第１章　出会い"), (3, "3", "第２章　別れ")]
    assert work.episodes[0].updated_hint == "2024/02/01 12:00 改稿"


def test_syosetu_episode_ruby_preface_and_injection_safety():
    html = F.syosetu_episode(1, F.EPISODE_1_BODY, preface=["前書き"], afterword=["後書き"])
    ref = syosetu.fetch_work.__globals__["EpisodeRef"](episode_id="1", url="u", title="t", index=1)
    ep = syosetu.parse_episode(html, ref)
    assert ep.preface == ["前書き"] and ep.afterword == ["後書き"]
    body = "\n".join(ep.body)
    assert "本気《マジ》" in body                     # semantic ruby kept
    assert "*大事*" in body                            # emphasis dots -> emphasis
    assert "陽斗はると" in body      # furigana held for first-occurrence resolution
    assert "</source_text>" not in body and "＜/source_text＞" in body
    assert "alert" not in body                         # scripts never become text


def test_kakuyomu_work_uses_requested_id_and_chapters():
    work = kakuyomu.parse_work_page(F.kakuyomu_work(), "1177000000000000001")
    assert work.title == "灯火の約束" and work.author == "試験 作者"
    assert [(e.episode_id, e.group_title) for e in work.episodes] == [("11", ""), ("12", "第一部　夜"), ("13", "第一部　夜")]
    with pytest.raises(AdapterError):
        kakuyomu.parse_work_page(F.kakuyomu_work(), "424242")


def test_kakuyomu_episode():
    ref = kakuyomu.parse_work_page(F.kakuyomu_work(), "1177000000000000001").episodes[1]
    ep = kakuyomu.parse_episode(F.kakuyomu_episode("第1話　灯", ["", "　灯がともった。", "「行こう」"]), ref)
    assert ep.title == "第1話　灯" and ep.body == ["", "灯がともった。", "「行こう」"]


# ── robots + politeness ─────────────────────────────────────────────────

def test_robots_wildcards_and_groups():
    rules = RobotsRules(F.KAKUYOMU_ROBOTS)
    assert rules.can_fetch(UA, "https://kakuyomu.jp/works/1/episodes/2")
    assert not rules.can_fetch(UA, "https://kakuyomu.jp/works/1/episodes/2/read")
    assert rules.can_fetch(UA, "https://kakuyomu.jp/works/1/episodes/2/reader")   # $ anchors the match
    assert not rules.can_fetch(UA, "https://kakuyomu.jp/login?next=/")
    assert rules.crawl_delay("ClaudeBot/1.0") == 1.0
    assert not RobotsRules("User-agent: *\nDisallow: /").can_fetch(UA, "https://x/a")
    assert RobotsRules("User-agent: *\nDisallow: /\nAllow: /works/").can_fetch(UA, "https://x/works/1")


class FakeTransport:
    def __init__(self, routes):
        self.routes = routes   # url -> list of (status, text, headers) or a single tuple
        self.requests = []

    def __call__(self, url, headers, timeout):
        self.requests.append(url)
        assert headers["User-Agent"]
        route = self.routes.get(url)
        if route is None:
            return 404, url, "", {}
        if isinstance(route, list):
            status, text, hdrs = route.pop(0) if len(route) > 1 else route[0]
        else:
            status, text, hdrs = route
        if isinstance(status, Exception):
            raise status
        return status, url, text, hdrs


def make_fetcher(routes, sleeps=None):
    clock = {"t": 0.0}
    sleeps = sleeps if sleeps is not None else []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    return PoliteFetcher(user_agent=UA, min_delay=2.0, max_retries=2, transport=FakeTransport(routes),
                         sleep=sleep, clock=lambda: clock["t"]), sleeps


def test_fetcher_respects_robots_delay_and_access_controls():
    routes = {
        "https://site.test/robots.txt": (200, "User-agent: *\nDisallow: /private/\nCrawl-delay: 5", {}),
        "https://site.test/a": (200, "<p>ok</p>", {}),
        "https://site.test/b": (200, "<p>ok</p>", {}),
        "https://site.test/forbidden": (403, "", {}),
        "https://site.test/busy": [(429, "", {"Retry-After": "7"}), (200, "<p>late</p>", {})],
        "https://site.test/captcha": (200, '<div class="g-recaptcha"></div>', {}),
    }
    fetcher, sleeps = make_fetcher(routes)
    assert fetcher.fetch("https://site.test/a") == "<p>ok</p>"
    fetcher.fetch("https://site.test/b")
    assert 5.0 in [round(s, 3) for s in sleeps]                     # crawl-delay beats min_delay
    with pytest.raises(AccessDenied):
        fetcher.fetch("https://site.test/private/x")
    with pytest.raises(AccessDenied):
        fetcher.fetch("https://site.test/forbidden")
    with pytest.raises(AccessDenied):
        fetcher.fetch("https://site.test/captcha")
    assert fetcher.fetch("https://site.test/busy") == "<p>late</p>"
    assert 7.0 in sleeps                                            # Retry-After honoured
    assert "https://site.test/private/x" not in fetcher._transport.requests


def test_robots_unreachable_means_no_fetch():
    fetcher, _ = make_fetcher({"https://down.test/robots.txt": (OSError("boom"), "", {})})
    with pytest.raises(AccessDenied):
        fetcher.fetch("https://down.test/page")
    fetcher2, _ = make_fetcher({"https://none.test/page": (200, "x", {})})   # robots 404 -> allowed
    assert fetcher2.fetch("https://none.test/page") == "x"


# ── download / checkpoint / resume / change detection ──────────────────

def syosetu_routes(ep2_text="「約束だよ」"):
    base = "https://ncode.syosetu.com"
    return {
        f"{base}/robots.txt": (200, "User-agent: *\nCrawl-delay: 1", {}),
        f"{base}/n1234ab/": (200, F.SYOSETU_TOC_P1, {}),
        f"{base}/n1234ab/?p=2": (200, F.SYOSETU_TOC_P2, {}),
        f"{base}/n1234ab/1/": (200, F.syosetu_episode(1, F.EPISODE_1_BODY, title="プロローグ"), {}),
        f"{base}/n1234ab/2/": (200, F.syosetu_episode(2, ["陽斗は頷いた。", ep2_text], title="第一話「約束」"), {}),
        f"{base}/n1234ab/3/": (200, F.syosetu_episode(3, ["影が笑った。", "陽斗は黙った。"], title="第二話「影」"), {}),
    }


def test_parse_range():
    assert parse_range("1-50") == (1, 50)
    assert parse_range("7") == (7, 7)
    assert parse_range("51-")[0] == 51
    with pytest.raises(ValueError):
        parse_range("5-2")


def test_interrupted_download_resumes_without_refetching(tmp_path):
    routes = syosetu_routes()
    routes["https://ncode.syosetu.com/n1234ab/3/"] = (500, "", {})
    fetcher, _ = make_fetcher(routes)
    result, _eps = download("https://ncode.syosetu.com/n1234ab/", fetcher=fetcher, cache_root=tmp_path)
    assert result.fetched == ["1", "2"] and set(result.failed) == {"3"}
    state = json.loads((result.cache_dir / "state.json").read_text(encoding="utf-8"))
    assert state["episodes"]["3"]["status"] == "failed"

    routes = syosetu_routes()
    fetcher2, _ = make_fetcher(routes)
    result2, eps = download("https://ncode.syosetu.com/n1234ab/", fetcher=fetcher2, cache_root=tmp_path)
    assert result2.reused == ["1", "2"] and result2.fetched == ["3"] and not result2.failed
    episode_requests = [u for u in fetcher2._transport.requests if u.rstrip("/").split("/")[-1].isdigit()]
    assert episode_requests == ["https://ncode.syosetu.com/n1234ab/3/"]


def test_refresh_detects_changed_content(tmp_path):
    fetcher, _ = make_fetcher(syosetu_routes())
    download("https://ncode.syosetu.com/n1234ab/", fetcher=fetcher, cache_root=tmp_path)
    fetcher2, _ = make_fetcher(syosetu_routes(ep2_text="「約束を破るな」"))
    result, _ = download("https://ncode.syosetu.com/n1234ab/", refresh=True, fetcher=fetcher2, cache_root=tmp_path)
    assert result.changed == ["2"]


def test_import_builds_valid_epub_and_vi_workspace(tmp_path, work_dir):
    fetcher, _ = make_fetcher(syosetu_routes())
    result = import_work("https://ncode.syosetu.com/n1234ab/", target_language="vi", fetcher=fetcher,
                         cache_root=tmp_path / "cache", work_base=work_dir)
    assert result.complete
    check = validate_epub_structure(result.epub_path)
    assert check.ok, check.errors
    assert check.language == "ja"
    with zipfile.ZipFile(result.epub_path) as zf:
        ep1 = zf.read("OEBPS/text/ep0001.xhtml").decode("utf-8")
    assert "陽斗《はると》" in ep1 and "<em>大事</em>" in ep1 and "" not in ep1
    wd = work_dir / result.volume_id
    assert result.volume_id == "web_syosetu_n1234ab_0001-0003_vi"
    manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["runtime_config"]["target_language"] == "vi"
    assert manifest["web_import"]["episode_ids"] == ["1", "2", "3"]
    jp = sorted(p.name for p in (wd / "JP").glob("*.md"))
    assert len(jp) == 3


def test_reimport_syncs_changed_and_new_chapters(tmp_path, work_dir):
    cache = tmp_path / "cache"
    routes = syosetu_routes()
    fetcher, _ = make_fetcher(routes)
    first = import_work("https://ncode.syosetu.com/n1234ab/", target_language="vi", episode_range=(1, 2),
                        volume_id="vol_web", fetcher=fetcher, cache_root=cache, work_base=work_dir)
    wd = work_dir / "vol_web"
    manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
    for c in manifest["chapters"]:
        c["translation_status"] = "completed"
    (wd / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert (wd / "VI").is_dir() and not (wd / "EN").exists()     # bound to vi at extraction
    for c in manifest["chapters"]:
        (wd / "VI" / c["translated_file"]).write_text("# x\n\nbản dịch cũ.", encoding="utf-8")

    fetcher2, _ = make_fetcher(syosetu_routes(ep2_text="「約束を破るな」"))
    second = import_work("https://ncode.syosetu.com/n1234ab/", target_language="vi", episode_range=(1, 3),
                         volume_id="vol_web", refresh=True, fetcher=fetcher2, cache_root=cache, work_base=work_dir)
    assert second.synced["added"] == ["CHAPTER_03"]
    assert second.synced["changed"] == ["CHAPTER_02"] and second.synced["unchanged"] == ["CHAPTER_01"]
    manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
    status = {c["source_file"]: c["translation_status"] for c in manifest["chapters"]}
    assert status == {"CHAPTER_01.md": "completed", "CHAPTER_02.md": "pending", "CHAPTER_03.md": "pending"}
    assert list((wd / ".context" / "stale").glob("CHAPTER_02_VI.*.md"))
    with pytest.raises(ValueError, match="en workspace|vi workspace"):
        import_work("https://ncode.syosetu.com/n1234ab/", target_language="en", episode_range=(1, 3),
                    volume_id="vol_web", fetcher=make_fetcher(syosetu_routes())[0], cache_root=cache, work_base=work_dir)


def test_rerun_without_volume_id_grows_the_existing_workspace(tmp_path, work_dir):
    """Same command again after the author posted more episodes: the existing
    workspace (…_0001-0002_vi) gets the new chapter instead of a new
    …_0001-0003_vi workspace being created and translated from scratch."""
    cache = tmp_path / "cache"
    first = import_work("https://ncode.syosetu.com/n1234ab/", target_language="vi", episode_range=(1, 2),
                        fetcher=make_fetcher(syosetu_routes())[0], cache_root=cache, work_base=work_dir)
    second = import_work("https://ncode.syosetu.com/n1234ab/", target_language="vi",
                         fetcher=make_fetcher(syosetu_routes())[0], cache_root=cache, work_base=work_dir)
    assert second.volume_id == first.volume_id
    assert second.synced["added"] == ["CHAPTER_03"]
    assert sorted(p.name for p in work_dir.iterdir()) == [first.volume_id]
    # another language or another starting episode is a separate workspace
    other = import_work("https://ncode.syosetu.com/n1234ab/", target_language="en",
                        fetcher=make_fetcher(syosetu_routes())[0], cache_root=cache, work_base=work_dir)
    assert other.volume_id != first.volume_id and not other.synced


def test_builder_uses_the_site_as_publisher_for_web_novels(tmp_path):
    from src.builder.agent import _web_import_publisher

    (tmp_path / "manifest.json").write_text(json.dumps({"web_import": {"site": "syosetu"}}), encoding="utf-8")
    assert "syosetu" in _web_import_publisher(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({}), encoding="utf-8")
    assert _web_import_publisher(tmp_path) == ""


def test_vietnamese_stylesheet_embeds_the_configured_fonts():
    from src.Deepseek.common.config import PIPELINE_ROOT
    from src.builder.stylesheets import language_font_assets

    css, files = language_font_assets("vi", PIPELINE_ROOT)
    hrefs = [href for _, href, _ in files]
    assert "Fonts/Literata-Regular.ttf" in hrefs and "Fonts/Inter-Bold.ttf" in hrefs
    assert all(src.is_file() for src, _, _ in files)
    assert '@font-face' in css and 'url("../Fonts/Literata-Regular.ttf")' in css
    assert 'body:lang(vi) {\n  font-family: "Literata"' in css
    assert ':lang(vi) h1' in css and '"Inter"' in css
    assert language_font_assets("en", PIPELINE_ROOT) == ("", [])
