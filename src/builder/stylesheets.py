"""
Stylesheets for EPUB output, selected per device profile.

DEFAULT_CSS lives in agent.py and targets ordinary 300 PPI readers. This module
holds EINK_CSS, which targets CrossPoint firmware on the XTEINK X3/X4 and looks
suspiciously short. It is short on purpose.

CrossPoint's CSS engine implements exactly nine properties:

    block   margin, padding, text-indent, text-align, direction
    inline  font-weight, font-style, text-decoration, vertical-align

Everything else is parsed and silently discarded. Selector support is a four
level priority list -- inline style attribute, then class, then tag, then
built-in default -- which is not a cascade. There are no descendant, child,
attribute or namespaced selectors, and no !important to lean on.

So the rules DEFAULT_CSS relies on to do its job do not survive the trip:

    section > div > p:first-of-type    child + pseudo-class, never matches
    p[class="section-break"]           attribute selector, never matches
    section[epub|type~="toc"] ...      namespaced attribute, never matches

That last pair is why scene breaks are unstyled on this hardware today -- the
rule is guarded by an attribute selector and !important, and the device
understands neither. Here it is a bare class, which is a selector form the
engine actually implements.

Two deliberate omissions, both because the firmware treats them as USER
settings that key its layout cache -- overriding them from CSS fights the
reader's own choice and forces every cached section to be laid out again:

  - no font-family / font-size / line-height
  - no text-align on body or p (alignment is a device setting; we only set it
    on elements where centring is semantic, like headings and scene breaks)

And no geometry at all -- max-width, height, object-fit, page-break-*, @media.
Images are sized by the engine as scale = min(maxW/w, maxH/h, 1.0), so CSS has
no say and every byte spent telling it otherwise is parse time on a 160 MHz
core. Complex stylesheets are a documented OOM source on this firmware; the
cheapest rule is the one we never ship.
"""

import re
from pathlib import Path
from typing import List

EINK_CSS = '''/* CrossPoint / XTEINK stylesheet - nine supported properties only */
@charset "UTF-8";

/* Body text. No text-align: the device owns alignment. */
p {
  text-indent: 1em;
  margin: 0;
}

.noindent {
  text-indent: 0;
}

.centerp {
  text-align: center;
  text-indent: 0;
}

/* Bare class selector, not p[class="..."] - this is the form that matches. */
.section-break {
  text-align: center;
  text-indent: 0;
  margin: 1em 0;
}

.blockquote {
  text-indent: 0;
  margin: 0.35em 1em;
}

/* Verse. Flat class selectors only (a descendant combinator never matches
   here) and shorthand padding, since the audit tokenises property names
   literally. No break-inside: the engine does not implement it, so declaring
   it would only put a lie in the sheet. */
.poem {
  text-indent: 0;
  margin: 1em 0 1em 1.5em;
}

.lyric {
  text-indent: -1em;
  padding: 0 0 0 1em;
  text-align: left;
  margin: 0.05em 0;
}

.lyric-break {
  text-indent: 0;
  margin: 0.35em 0;
}

/* Structured blocks. Same flat-selector and whitelisted-property discipline
   as the verse rules above. */
.note {
  text-indent: 0;
  margin: 0.5em 0;
}

.doc-list {
  text-indent: 0;
  margin: 0.6em 0 0.6em 1.5em;
  padding: 0;
}

li {
  text-indent: 0;
  margin: 0.15em 0;
}

/* Headings. Centring is semantic here, not a typographic preference. */
h1 {
  text-align: center;
  text-indent: 0;
  margin: 1.2em 0 0.8em 0;
}

h2 {
  text-align: center;
  text-indent: 0;
  margin: 1em 0 0.5em 0;
}

/* Image wrappers. Sizing is the engine's job; we only centre the box. */
.cover-image {
  text-align: center;
  text-indent: 0;
  margin: 0;
}

.illustration {
  text-align: center;
  text-indent: 0;
  margin: 0.5em 0;
}

.kuchie-image {
  text-align: center;
  text-indent: 0;
  margin: 0;
}

.image_full {
  text-align: center;
  text-indent: 0;
  margin: 0;
}

.horizontal-kuchie {
  text-align: center;
  text-indent: 0;
  margin: 0;
}

/* Emphasis */
em {
  font-style: italic;
}

strong {
  font-weight: bold;
}

/* Footnotes. The firmware picks up internal links as footnotes on its own;
   these rules only keep the markers from looking like body text. */
a {
  text-decoration: none;
}

.noteref {
  vertical-align: super;
  text-decoration: none;
}

.footnote-backref {
  text-decoration: none;
}

.footnotes {
  margin: 1.2em 0 0 0;
}

/* Table of contents */
.toc-list {
  text-indent: 0;
  margin: 0;
}
'''


# Properties CrossPoint's CSS engine actually implements. device_budgets.py
# uses these to audit whatever stylesheet we ship, so a future well-meaning
# addition gets caught at build time instead of on the device.
EINK_SUPPORTED_BLOCK_PROPERTIES = frozenset({
    "margin",
    "padding",
    "text-indent",
    "text-align",
    "direction",
})

EINK_SUPPORTED_INLINE_PROPERTIES = frozenset({
    "font-weight",
    "font-style",
    "text-decoration",
    "vertical-align",
})

EINK_SUPPORTED_PROPERTIES = (
    EINK_SUPPORTED_BLOCK_PROPERTIES | EINK_SUPPORTED_INLINE_PROPERTIES
)


# Appended to the default stylesheet for a non-English book. Vietnamese
# stacks tone marks above vowel diacritics (ể, ỗ, ặ), so it needs a little
# more line height than Latin-only text, a font stack with full Vietnamese
# coverage ahead of the generic serif, and no automatic hyphenation (reader
# hyphenation dictionaries split Vietnamese syllables wrongly).
VI_CSS = """

/* Vietnamese */
:root:lang(vi), body:lang(vi) {
  font-family: "Noto Serif", "Literata", "Bookerly", "Charis SIL", Georgia, "Times New Roman", serif;
  line-height: 1.7;
  -webkit-hyphens: none;
  hyphens: none;
  word-break: normal;
  overflow-wrap: break-word;
}

/* Dialogue paragraphs (class added by the builder) keep the prose indent but
   avoid splitting a short exchange across pages where the reader allows it. */
p.dialogue {
  orphans: 2;
  widows: 2;
}
"""

LANGUAGE_CSS = {"vi": VI_CSS}

_DIALOGUE_P_RE = re.compile(r"<p>(?=\s*(?:<(?:em|i|strong|b)>)?\s*[“\"‘「『—–])")


def mark_dialogue_paragraphs(xhtml: str) -> str:
    """Give dialogue paragraphs (opening quote or dialogue dash) a class so
    the stylesheet can treat them distinctly from narration."""
    return _DIALOGUE_P_RE.sub('<p class="dialogue">', xhtml)


# ── Embedded fonts (per target language) ────────────────────────────────────
# project.languages.<lang>.epub_fonts in config.yaml names a body (reading)
# font and a heading font, each with up to four faces. The files are copied
# into OEBPS/Fonts/ and declared with @font-face, so the book looks the same
# on readers that do not ship the font (Kindle shows them with "Publisher
# Font" on). A missing file is skipped with a warning; the CSS still names the
# family first, so a reader that has it installed uses it.

_FACE_STYLES = {
    "regular": ("normal", "normal"),
    "bold": ("bold", "normal"),
    "italic": ("normal", "italic"),
    "bold_italic": ("bold", "italic"),
}

_ROLE_SELECTORS = {
    "body": ":root:lang(vi), body:lang(vi)",
    "heading": ":lang(vi) h1, :lang(vi) h2, :lang(vi) h3, :lang(vi) h4",
}


def language_font_assets(lang: str, project_root: Path):
    """``(css, files)`` for the target language's embedded fonts, where
    ``files`` is ``[(source_path, href_inside_OEBPS, item_id)]``.
    ``("", [])`` when the language configures no fonts."""
    from src.Deepseek.common.language import language_config

    lang = str(lang or "en").lower()
    cfg = (language_config(lang) or {}).get("epub_fonts") if lang != "en" else None
    if not cfg:
        return "", []
    embed = bool(cfg.get("embed", True))
    selector_lang = f":lang({lang})"
    faces_css: List[str] = []
    rules: List[str] = []
    files = []
    for role in ("body", "heading"):
        spec = cfg.get(role) or {}
        family = str(spec.get("family") or "").strip()
        if not family:
            continue
        for face, rel in (spec.get("files") or {}).items() if embed else ():
            if face not in _FACE_STYLES or not rel:
                continue
            src = (project_root / str(rel)).resolve()
            if not src.is_file():
                print(f"     [WARNING] font file not found, not embedded: {src}")
                continue
            weight, style = _FACE_STYLES[face]
            href = f"Fonts/{src.name}"
            item_id = "font-" + re.sub(r"[^a-z0-9]+", "-", src.stem.lower()).strip("-")
            if all(h != href for _, h, _ in files):
                files.append((src, href, item_id))
            faces_css.append(
                f'@font-face {{\n  font-family: "{family}";\n  src: url("../{href}") format("truetype");\n'
                f"  font-weight: {weight};\n  font-style: {style};\n}}"
            )
        fallback = str(spec.get("fallback") or "serif").strip()
        selector = _ROLE_SELECTORS[role].replace(":lang(vi)", selector_lang)
        rules.append(f'{selector} {{\n  font-family: "{family}", {fallback};\n}}')
    if not rules:
        return "", []
    css = "\n\n/* Embedded fonts */\n" + "\n".join(faces_css + rules) + "\n"
    return css, files
