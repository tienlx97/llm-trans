"""Shared fixtures. No test here makes a network call or a paid API call:

* API keys are fake placeholders, and every provider client is replaced at
  its ``generate`` boundary by a scripted fake.
* ``WORK_DIR``/``OUTPUT_DIR`` are redirected to a temp dir in every loaded
  ``src.*`` module (they are imported by value), so nothing is written into
  the repository's work/, output/ or LOG/.
* Tests marked ``network`` only run with MTL_LIVE_NETWORK=1.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load every module that captures WORK_DIR/OUTPUT_DIR at import time, so the
# autouse fixture below can redirect all of them.
import src.Deepseek.common.config  # noqa: E402,F401
import src.Deepseek.common.token_telemetry  # noqa: E402,F401
import src.Deepseek.translator.dry_run  # noqa: E402,F401
import src.OpenAI.agent  # noqa: E402,F401
import src.Gemini.agent  # noqa: E402,F401
import src.Deepseek.translator.agent  # noqa: E402,F401
import src.Qwen.agent  # noqa: E402,F401
import src.Anthropic.agent  # noqa: E402,F401
import src.builder.agent  # noqa: E402,F401
import src.utility.qc.agent  # noqa: E402,F401
import src.utility.librarian.agent  # noqa: E402,F401
import src.utility.webimport.importer  # noqa: E402,F401
import src.utility.prep.agent  # noqa: E402,F401
import src.utility.prep.multiturn_agent  # noqa: E402,F401
import src.utility.prep.parallel_agent  # noqa: E402,F401
import src.OpenAI.prep  # noqa: E402,F401
import src.CLI.agent  # noqa: E402,F401


def pytest_collection_modifyitems(config, items):
    if os.environ.get("MTL_LIVE_NETWORK") == "1":
        return
    skip = pytest.mark.skip(reason="live network test; set MTL_LIVE_NETWORK=1 to run")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Point every module's WORK_DIR / OUTPUT_DIR at a per-test temp dir."""
    import src.Deepseek.common.config  # noqa: F401 - ensure the base module is loaded

    work = tmp_path / "work"
    out = tmp_path / "output"
    work.mkdir()
    out.mkdir()
    for name, module in list(sys.modules.items()):
        if not name.startswith(("src.", "scripts")) or module is None:
            continue
        if hasattr(module, "WORK_DIR"):
            monkeypatch.setattr(module, "WORK_DIR", work, raising=False)
        if hasattr(module, "OUTPUT_DIR"):
            monkeypatch.setattr(module, "OUTPUT_DIR", out, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder-not-a-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-placeholder-not-a-key")
    monkeypatch.delenv("MTL_TARGET_LANGUAGE", raising=False)
    return work


@pytest.fixture
def work_dir(isolated_dirs):
    return isolated_dirs


def repatch_dirs(monkeypatch, work: Path):
    """Call after importing a module that was not loaded when the fixture ran."""
    for name, module in list(sys.modules.items()):
        if name.startswith("src.") and module is not None and hasattr(module, "WORK_DIR"):
            monkeypatch.setattr(module, "WORK_DIR", work, raising=False)
