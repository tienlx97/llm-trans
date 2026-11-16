"""Target-language resolution shared by every pipeline phase.

A volume workspace is bound to ONE target language at extraction time: the
Librarian records it in ``manifest.json`` (``runtime_config.target_language``)
and in ``context.xml``'s root. Every later phase reads it back from the
workspace instead of from the global config, so an English volume stays an
English volume even after ``project.target_language`` changes, and a
Vietnamese volume never writes into ``EN/``.

English keeps its historical layout byte-for-byte: ``EN/<id>_EN.md``, the
provider's own ``master_prompt`` and every English-only mechanism (verbatim
anchors, the DeepSeek safety fallback). Any other language must be declared
under ``project.languages.<code>`` in config.yaml with the providers that
support it and the prompt each of them uses; an undeclared combination fails
loudly before an API call is made rather than producing English into a
Vietnamese workspace.
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_LANGUAGE = "en"
TARGET_LANGUAGE_ENV = "MTL_TARGET_LANGUAGE"

# Non-standard spellings seen in the wild. "vn" is a country code, not a
# language code; it is accepted on input and normalized to ISO 639-1 "vi".
_ALIASES = {
    "english": "en",
    "en-us": "en",
    "en-gb": "en",
    "vn": "vi",
    "vie": "vi",
    "vietnamese": "vi",
    "vi-vn": "vi",
    "tiếng việt": "vi",
}


class UnsupportedTargetLanguage(ValueError):
    """A target language (or language/provider pair) that is not configured."""


def normalize_language(code: Optional[str]) -> str:
    value = str(code or "").strip().lower().replace("_", "-")
    if not value:
        return DEFAULT_LANGUAGE
    return _ALIASES.get(value, value)


def _languages_config() -> Dict[str, Any]:
    from src.Deepseek.common.config import load_config

    project = (load_config() or {}).get("project") or {}
    languages = project.get("languages") or {}
    return languages if isinstance(languages, dict) else {}


def language_config(code: Optional[str]) -> Dict[str, Any]:
    entry = _languages_config().get(normalize_language(code)) or {}
    return entry if isinstance(entry, dict) else {}


def supported_languages() -> List[str]:
    codes = {DEFAULT_LANGUAGE}
    codes.update(normalize_language(code) for code in _languages_config())
    return sorted(codes)


def validate_language(code: Optional[str]) -> str:
    lang = normalize_language(code)
    if lang != DEFAULT_LANGUAGE and lang not in _languages_config():
        raise UnsupportedTargetLanguage(
            f"Target language {lang!r} is not configured; add it under project.languages in config.yaml "
            f"(configured: {', '.join(supported_languages())})."
        )
    return lang


def default_target_language() -> str:
    """CLI override (env) > ``project.target_language`` > ``en``."""
    override = os.environ.get(TARGET_LANGUAGE_ENV, "").strip()
    if override:
        return normalize_language(override)
    from src.Deepseek.common.config import load_config

    project = (load_config() or {}).get("project") or {}
    return normalize_language(project.get("target_language") or DEFAULT_LANGUAGE)


def language_name(code: Optional[str]) -> str:
    lang = normalize_language(code)
    if lang == DEFAULT_LANGUAGE:
        return "English"
    return str(language_config(lang).get("language_name") or lang.upper())


def volume_target_language(work_dir: Path) -> str:
    """The language this workspace was extracted for.

    manifest ``runtime_config`` first (written by the Librarian), then the
    translator's pipeline_state stamp, then ``context.xml``'s root attribute.
    A workspace with none of them predates this field and is English.
    """
    work_dir = Path(work_dir)
    try:
        manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    if isinstance(manifest, dict):
        runtime = manifest.get("runtime_config") or {}
        if isinstance(runtime, dict) and runtime.get("target_language"):
            return normalize_language(runtime["target_language"])
        translator = ((manifest.get("pipeline_state") or {}).get("translator") or {})
        if isinstance(translator, dict) and translator.get("target_language"):
            return normalize_language(translator["target_language"])
    try:
        root = ET.parse(work_dir / "context.xml").getroot()
        if root.get("target_language"):
            return normalize_language(root.get("target_language"))
    except (OSError, ET.ParseError):
        pass
    return DEFAULT_LANGUAGE


def translated_dir_name(lang: Optional[str]) -> str:
    return normalize_language(lang).upper()


def translated_dir(work_dir: Path, lang: Optional[str] = None) -> Path:
    lang = volume_target_language(work_dir) if lang is None else lang
    return Path(work_dir) / translated_dir_name(lang)


def translated_suffix(lang: Optional[str]) -> str:
    return f"_{translated_dir_name(lang)}.md"


def translated_path(work_dir: Path, chapter_id: str, lang: Optional[str] = None) -> Path:
    lang = volume_target_language(work_dir) if lang is None else lang
    return translated_dir(work_dir, lang) / f"{chapter_id}{translated_suffix(lang)}"


def provider_supports(provider: str, lang: Optional[str]) -> bool:
    lang = normalize_language(lang)
    if lang == DEFAULT_LANGUAGE:
        return True
    providers = language_config(lang).get("providers") or {}
    return str(provider).strip().lower() in {str(p).strip().lower() for p in providers}


def require_provider_support(provider: str, lang: Optional[str]) -> None:
    lang = validate_language(lang)
    if not provider_supports(provider, lang):
        providers = sorted((language_config(lang).get("providers") or {}).keys())
        raise UnsupportedTargetLanguage(
            f"Provider {provider!r} has no {language_name(lang)} ({lang}) route. "
            f"Configured for {lang}: {', '.join(providers) or 'none'}. "
            "Pick one with --provider or translation.provider."
        )


def resolve_master_prompt(provider: str, english_prompt: Path, lang: Optional[str]) -> Path:
    """English returns the provider's own prompt unchanged; any other language
    returns ``project.languages.<lang>.providers.<provider>.master_prompt``."""
    lang = normalize_language(lang)
    if lang == DEFAULT_LANGUAGE:
        return Path(english_prompt)
    require_provider_support(provider, lang)
    from src.Deepseek.common.config import PIPELINE_ROOT

    entry = (language_config(lang).get("providers") or {}).get(str(provider).strip().lower()) or {}
    raw = entry.get("master_prompt") if isinstance(entry, dict) else None
    if not raw:
        raise UnsupportedTargetLanguage(
            f"project.languages.{lang}.providers.{provider}.master_prompt is not set in config.yaml."
        )
    path = Path(str(raw))
    path = path if path.is_absolute() else PIPELINE_ROOT / path
    if not path.is_file():
        raise UnsupportedTargetLanguage(f"{language_name(lang)} master prompt for {provider} not found: {path}")
    return path


def target_language_block(work_dir: Path, lang: Optional[str], chapter_id: str, source: str) -> str:
    """Per-chapter localization data for a non-English target ("" for English).

    Vietnamese: the chapter-gated slice of work/<vol>/.context/bible_vi.json
    (names, locked terms, pronoun matrix for the characters present).
    """
    lang = normalize_language(lang)
    if lang != "vi":
        return ""
    from src.utility.vietnamese.bible import load_volume_bible, render_localization_block

    cfg = language_config(lang)
    bible_cfg = cfg.get("bible") or {}
    return render_localization_block(
        load_volume_bible(work_dir),
        chapter_id=chapter_id,
        source=source,
        max_entries=int(bible_cfg.get("max_entries_per_chapter", 60) or 60),
        dialogue_style=cfg.get("dialogue_style"),
    )


def _vi_metadata(work_dir: Path) -> Dict[str, Any]:
    from src.utility.vietnamese.bible import load_volume_bible

    bible = load_volume_bible(work_dir) or {}
    meta = bible.get("metadata") or {}
    return meta if isinstance(meta, dict) else {}


def localized_volume_identity(work_dir: Path, volume_identity: Optional[ET.Element]) -> Dict[str, str]:
    """OPF overrides for a non-English book (``{}`` for English).

    context.xml's volume_identity holds English titles. A Vietnamese book uses
    the bible's ``metadata.title_vi`` / ``series_vi`` / ``author_vi`` when the
    operator has set them, otherwise the Japanese original title — never the
    English one. The romanized author and the publisher stay as prep wrote them.
    """
    lang = volume_target_language(work_dir)
    if lang == DEFAULT_LANGUAGE:
        return {}

    def field(name: str) -> str:
        node = volume_identity.find(name) if volume_identity is not None else None
        return (node.text or "").strip() if node is not None and node.text else ""

    meta = _vi_metadata(work_dir) if lang == "vi" else {}
    title = str(meta.get("title_vi") or field("title_jp") or "").strip()
    series = str(meta.get("series_vi") or field("series_jp") or title).strip()
    out = {"title_en": title, "series_en": series}
    if meta.get("author_vi"):
        out["author_en"] = str(meta["author_vi"]).strip()
    return {k: v for k, v in out.items() if v}


def localized_manifest_metadata(work_dir: Path, lang: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """The ``metadata_<lang>`` dict the Builder reads for display titles."""
    lang = normalize_language(lang)
    if lang == DEFAULT_LANGUAGE:
        return {}
    meta = _vi_metadata(work_dir) if lang == "vi" else {}
    jp = manifest.get("metadata") or {}
    title = str(meta.get("title_vi") or jp.get("title") or "").strip()
    if not title:
        try:
            root = ET.parse(Path(work_dir) / "context.xml").getroot()
            opf = json.loads((root.findtext("opf_metadata") or "{}"))
            title = str(opf.get("title") or "").strip()
        except (OSError, ET.ParseError, ValueError):
            title = ""
    author = str(meta.get("author_vi") or (manifest.get("metadata_en") or {}).get("author_en") or jp.get("author") or "")
    return {f"title_{lang}": title, "title": title, f"author_{lang}": author, "author": author} if title else {}


def localize_guidance(text: str, lang: Optional[str]) -> str:
    """Chapter guidance strings are authored in terms of English prose; for
    another target only the language noun changes, the craft intent does not."""
    lang = normalize_language(lang)
    if lang == DEFAULT_LANGUAGE or not text:
        return text
    return text.replace("English", language_name(lang))
