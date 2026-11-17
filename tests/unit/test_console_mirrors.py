"""The TypeScript console hand-mirrors several Python facts. The console has no
test runner of its own, so the drift checks live here: each one parses the
TypeScript source as text and compares it with the Python source of truth."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "ts" / "src" / "core"


def _ts(name: str) -> str:
    return (CORE / name).read_text(encoding="utf-8")


def test_advisor_compatibility_table_matches_python():
    from src.Anthropic.client import ADVISOR_COMPATIBILITY

    source = _ts("proofreading.ts")
    block = source[source.index("export const ADVISOR_COMPATIBILITY"):]
    block = block[: block.index("};")]
    mirrored = {
        executor: set(re.findall(r"'([^']+)'", advisors))
        for executor, advisors in re.findall(r"'([^']+)':\s*\[([^\]]*)\]", block)
    }
    assert mirrored == {executor: set(advisors) for executor, advisors in ADVISOR_COMPATIBILITY.items()}


def test_mcp_overlay_lists_exactly_the_server_tools():
    from src.Deepseek.mcp.server import create_mcp_server

    served = {tool.name for tool in asyncio.run(create_mcp_server().list_tools())}
    overlay = set(re.findall(r"route: \{ transport: 'mcp', tool: '([^']+)' \}", _ts("capabilities.ts")))
    assert overlay == served


def test_every_config_schema_path_exists_in_config_yaml():
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    missing = []
    for path in re.findall(r"\{ path: '([^']+)'", _ts("configSchema.ts")):
        node = config
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                missing.append(path)
                break
            node = node[part]
    assert not missing, f"configSchema.ts paths with no key in config.yaml: {missing}"


def test_console_knows_every_translation_provider():
    from src.Deepseek.translator.provider import PROVIDERS

    schema = _ts("configSchema.ts")
    choices = re.search(r"const PROVIDER_CHOICES = \[([^\]]*)\]", schema).group(1)
    assert set(re.findall(r"'([^']+)'", choices)) == set(PROVIDERS)
    for name in ("preflight.ts", "mtls.ts"):
        pattern = re.search(r"provider:\\s\*\(([a-z|]+)\)", _ts(name))
        assert pattern and set(pattern.group(1).split("|")) == set(PROVIDERS), name


def test_device_profile_choices_match_registry():
    from src.builder.device_profiles import PROFILE_NAMES

    capabilities = re.search(r"const PROFILE_CHOICES = \[([^\]]*)\]", _ts("capabilities.ts")).group(1)
    # The leading '' choice means "use builder.device_profile from config.yaml".
    assert set(re.findall(r"'([^']*)'", capabilities)) - {""} == set(PROFILE_NAMES)
    schema = re.search(r"const DEVICE_PROFILE_CHOICES = \[([^\]]*)\]", _ts("configSchema.ts")).group(1)
    assert set(re.findall(r"'([^']+)'", schema)) == set(PROFILE_NAMES)
