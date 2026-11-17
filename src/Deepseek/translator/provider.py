"""Phase 2 provider selector — the single dispatch table.

Every entry point (scripts/mtl.py, the MCP translator tools) resolves a
provider name here, so adding a route means adding one row instead of
editing parallel if/elif chains that drift apart.
"""

from functools import partial
from importlib import import_module
from typing import Any, Optional

# provider -> (module that owns translate_volume, translator class name)
API_PROVIDERS = {
    "deepseek": ("src.Deepseek.translator.agent", "DeepSeekTranslator"),
    "qwen": ("src.Qwen.agent", "QwenTranslator"),
    "openai": ("src.OpenAI.agent", "OpenAITranslator"),
    "anthropic": ("src.Anthropic.agent", "AnthropicTranslator"),
    "gemini": ("src.Gemini.agent", "GeminiTranslator"),
}
# Signed-in Codex CLI / Claude Code (subscription billing, no API key); see src/CLI/.
CLI_PROVIDERS = ("codex", "claude")
PROVIDERS = (*API_PROVIDERS, *CLI_PROVIDERS)


def configured_provider(override: Optional[str] = None) -> str:
    """``override`` if given, else ``translation.provider`` from config.yaml."""
    if override:
        return str(override).strip().lower()
    from src.Deepseek.common.config import get_config_section

    return str(get_config_section("translation").get("provider") or "deepseek").strip().lower()


def _unsupported(selected: str) -> ValueError:
    return ValueError(
        f"Unsupported translation provider: {selected!r}; expected one of {', '.join(PROVIDERS)}"
    )


def get_translator_class(provider: Optional[str] = None) -> Any:
    """The ``<Provider>Translator`` class for single-chapter use.

    API routes only: the subscription CLIs have no shared-constructor
    translator class, so a caller that needs one chapter through codex/claude
    uses ``get_volume_translator()(volume_id, chapters=[...])`` instead.
    """
    selected = configured_provider(provider)
    if selected in API_PROVIDERS:
        module_name, class_name = API_PROVIDERS[selected]
        return getattr(import_module(module_name), class_name)
    if selected in CLI_PROVIDERS:
        raise ValueError(
            f"Provider {selected!r} is a subscription CLI route with no per-chapter translator class; "
            "call the volume translator with chapters=[...] instead."
        )
    raise _unsupported(selected)


def get_volume_translator(provider: Optional[str] = None) -> Any:
    """``translate_volume`` for the given (or configured) provider."""
    selected = configured_provider(provider)
    if selected in API_PROVIDERS:
        return import_module(API_PROVIDERS[selected][0]).translate_volume
    if selected in CLI_PROVIDERS:
        return partial(import_module("src.CLI.agent").translate_volume, provider=selected)
    raise _unsupported(selected)
