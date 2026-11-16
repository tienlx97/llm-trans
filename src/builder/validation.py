"""EPUB validation: the W3C epubcheck when it is installed, plus a built-in
structural check that needs nothing but the standard library and lxml.

epubcheck is looked up from ``EPUBCHECK_JAR`` (env) or ``builder.epubcheck_jar``
(config.yaml) and run with the ``java`` on PATH. Absent either, it is skipped
— the built-in check still runs in the test suite.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote

from lxml import etree

_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}


@dataclass
class ValidationResult:
    ok: bool
    summary: str
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    language: Optional[str] = None


def validate_epub_structure(epub_path: Path) -> ValidationResult:
    """OCF/OPF/nav/XHTML well-formedness checks (a subset of epubcheck)."""
    errors: List[str] = []
    warnings: List[str] = []
    language = None
    try:
        zf = zipfile.ZipFile(epub_path)
    except (OSError, zipfile.BadZipFile) as exc:
        return ValidationResult(False, f"not a zip: {exc}", [str(exc)])
    with zf:
        infos = zf.infolist()
        if not infos or infos[0].filename != "mimetype":
            errors.append("mimetype is not the first entry")
        elif infos[0].compress_type != zipfile.ZIP_STORED:
            errors.append("mimetype is compressed")
        elif zf.read("mimetype") != b"application/epub+zip":
            errors.append("mimetype content is wrong")
        names = set(zf.namelist())
        if "META-INF/container.xml" not in names:
            return ValidationResult(False, "missing META-INF/container.xml", errors + ["missing container.xml"])
        container = etree.fromstring(zf.read("META-INF/container.xml"))
        rootfile = container.find(".//c:rootfile", _NS)
        opf_path = rootfile.get("full-path") if rootfile is not None else None
        if not opf_path or opf_path not in names:
            return ValidationResult(False, "package document missing", errors + ["package document missing"])
        opf = etree.fromstring(zf.read(opf_path))
        base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        if opf.get("version", "").split(".")[0] != "3":
            warnings.append(f"package version {opf.get('version')!r} is not EPUB 3")
        lang_el = opf.find(".//dc:language", _NS)
        language = (lang_el.text or "").strip() if lang_el is not None and lang_el.text else None
        if not language:
            errors.append("dc:language missing")
        if opf.find(".//dc:title", _NS) is None or not (opf.find(".//dc:title", _NS).text or "").strip():
            errors.append("dc:title missing")
        if opf.find(".//dc:identifier", _NS) is None:
            errors.append("dc:identifier missing")
        items = opf.findall(".//opf:manifest/opf:item", _NS)
        ids = {item.get("id") for item in items}
        has_nav = False
        for item in items:
            href = unquote(item.get("href") or "")
            full = base + href
            if full not in names:
                errors.append(f"manifest item missing from container: {href}")
                continue
            if "nav" in (item.get("properties") or "").split():
                has_nav = True
            if item.get("media-type") == "application/xhtml+xml":
                try:
                    etree.fromstring(zf.read(full))
                except etree.XMLSyntaxError as exc:
                    errors.append(f"{href}: not well-formed XHTML ({exc})")
        if not has_nav:
            errors.append("no EPUB 3 navigation document (properties=nav)")
        for ref in opf.findall(".//opf:spine/opf:itemref", _NS):
            if ref.get("idref") not in ids:
                errors.append(f"spine itemref {ref.get('idref')!r} not in manifest")
    return ValidationResult(not errors, "structure ok" if not errors else f"{len(errors)} error(s)",
                            errors, warnings, language)


def _epubcheck_jar() -> Optional[Path]:
    raw = os.environ.get("EPUBCHECK_JAR", "").strip()
    if not raw:
        try:
            from src.Deepseek.common.config import get_config_section

            raw = str((get_config_section("builder") or {}).get("epubcheck_jar") or "").strip()
        except Exception:  # noqa: BLE001 - config is optional here
            raw = ""
    path = Path(raw) if raw else None
    return path if path is not None and path.is_file() else None


def run_epubcheck(epub_path: Path, *, timeout: int = 300) -> Optional[ValidationResult]:
    """``None`` when epubcheck/java is unavailable; otherwise its verdict."""
    jar = _epubcheck_jar()
    java = shutil.which("java")
    if jar is None or java is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "report.json"
        proc = subprocess.run([java, "-jar", str(jar), str(epub_path), "--json", str(report_path)],
                              capture_output=True, text=True, timeout=timeout)
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
            return ValidationResult(proc.returncode == 0, " | ".join(tail) or f"exit {proc.returncode}")
    errors = [f"{m.get('ID')}: {m.get('message')}" for m in report.get("messages", [])
              if m.get("severity") in ("FATAL", "ERROR")]
    warnings = [f"{m.get('ID')}: {m.get('message')}" for m in report.get("messages", []) if m.get("severity") == "WARNING"]
    return ValidationResult(not errors, f"{len(errors)} error(s), {len(warnings)} warning(s)", errors, warnings)
