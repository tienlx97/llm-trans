"""Build a Japanese EPUB3 from downloaded web-novel episodes.

The result is a plain, valid EPUB3 (nav.xhtml plus an NCX for older
readers) with one XHTML document per episode, so the Librarian extracts it
through its normal TOC path. All scraped text is XML-escaped here.
"""

from __future__ import annotations

import re
import uuid
import zipfile
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import List, Sequence

from src.utility.webimport.models import Episode, WorkInfo

SCENE_BREAK = "＊　＊　＊"
_EMPHASIS_RE = re.compile(r"\*([^*\n]+)\*")


def _inline(text: str) -> str:
    """Escape, then turn the normalizer's *emphasis* back into <em>."""
    escaped = escape(text, quote=False)
    return _EMPHASIS_RE.sub(r"<em>\1</em>", escaped).replace("\n", "<br/>")


def _paragraphs(lines: Sequence[str]) -> List[str]:
    return [f"<p>{_inline(line)}</p>" for line in lines if line.strip()]


def episode_xhtml(episode: Episode, *, group_heading: str = "") -> str:
    # The in-body <h1> is required: with three or fewer TOC entries the
    # Librarian treats the TOC as minimal and detects chapters from body
    # headings in the spine.
    body: List[str] = [f"<h1>{_inline(episode.title)}</h1>"]
    if group_heading:
        body.append(f"<h2>{_inline(group_heading)}</h2>")
    if any(line.strip() for line in episode.preface):
        body.extend(_paragraphs(episode.preface))
        body.append(f"<p>{SCENE_BREAK}</p>")
    body.extend(_paragraphs(episode.body))
    if any(line.strip() for line in episode.afterword):
        body.append(f"<p>{SCENE_BREAK}</p>")
        body.extend(_paragraphs(episode.afterword))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja" lang="ja">\n'
        f"<head><meta charset=\"UTF-8\"/><title>{_inline(episode.title)}</title></head>\n"
        "<body>\n" + "\n".join(body) + "\n</body>\n</html>\n"
    )


def build_epub(work: WorkInfo, episodes: Sequence[Episode], output_path: Path, *, volume_label: str = "") -> Path:
    if not episodes:
        raise ValueError("No episodes to package")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, work.url + '#' + volume_label)}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = work.title + (f"　{volume_label}" if volume_label else "")

    docs = []
    previous_group = None
    for number, episode in enumerate(episodes, start=1):
        group = episode.ref.group_title if episode.ref.group_title != previous_group else ""
        previous_group = episode.ref.group_title
        docs.append((f"ep{number:04d}", f"text/ep{number:04d}.xhtml", episode, episode_xhtml(episode, group_heading=group)))

    manifest_items = "\n".join(
        f'    <item id="{doc_id}" href="{href}" media-type="application/xhtml+xml"/>' for doc_id, href, _e, _x in docs
    )
    spine_items = "\n".join(f'    <itemref idref="{doc_id}"/>' for doc_id, _h, _e, _x in docs)
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="ja" prefix="mtl: https://github.com/thang97-21/llm-LN-translator#">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{escape(book_id)}</dc:identifier>
    <dc:title>{escape(title)}</dc:title>
    <dc:creator>{escape(work.author or "不明")}</dc:creator>
    <dc:language>ja</dc:language>
    <dc:source>{escape(work.url)}</dc:source>
    <dc:description>{escape(work.synopsis[:2000])}</dc:description>
    <meta property="dcterms:modified">{modified}</meta>
    <meta property="mtl:toc-authoritative">true</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
{manifest_items}
  </manifest>
  <spine toc="ncx">
{spine_items}
  </spine>
</package>
"""
    nav_points = "\n".join(
        f'      <li><a href="{href}">{_inline(episode.title)}</a></li>' for _id, href, episode, _x in docs
    )
    nav = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja" lang="ja">
<head><meta charset="UTF-8"/><title>目次</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>目次</h1>
    <ol>
{nav_points}
    </ol>
  </nav>
</body>
</html>
"""
    ncx_points = "\n".join(
        f'    <navPoint id="np{n}" playOrder="{n}"><navLabel><text>{_inline(episode.title)}</text></navLabel>'
        f'<content src="{href}"/></navPoint>'
        for n, (_id, href, episode, _x) in enumerate(docs, start=1)
    )
    ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="ja">
  <head><meta name="dtb:uid" content="{escape(book_id)}"/></head>
  <docTitle><text>{escape(title)}</text></docTitle>
  <navMap>
{ncx_points}
  </navMap>
</ncx>
"""
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
    tmp = output_path.with_suffix(".epub.tmp")
    with zipfile.ZipFile(tmp, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        for _id, href, _episode, xhtml in docs:
            zf.writestr(f"OEBPS/{href}", xhtml, compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(output_path)
    return output_path
