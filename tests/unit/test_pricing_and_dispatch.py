"""DeepSeek peak/off-peak tariff selection and the single provider dispatch table."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.Deepseek.common.token_telemetry import (
    DEEPSEEK_PRICING_PER_MTOK,
    cost_breakdown_usd,
    deepseek_pricing_status,
)
from src.Deepseek.translator import provider as dispatch


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-11-16 is a Monday; 14 = Saturday, 15 = Sunday.
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("moment", "period"),
    [
        (_utc(16, 1), "peak"),          # Monday, first window opens
        (_utc(16, 3, 59), "peak"),
        (_utc(16, 4), "off_peak"),      # gap between the two windows
        (_utc(16, 6), "peak"),
        (_utc(16, 9, 59), "peak"),
        (_utc(16, 10), "off_peak"),
        (_utc(14, 2), "off_peak"),      # Saturday: windows do not apply
        (_utc(15, 7), "off_peak"),      # Sunday
    ],
)
def test_deepseek_peak_windows_are_weekday_only(moment, period):
    assert deepseek_pricing_status(moment)["period"] == period


def test_off_peak_is_half_of_peak_for_every_model():
    for model, periods in DEEPSEEK_PRICING_PER_MTOK.items():
        for column, peak_rate in periods["peak"].items():
            assert periods["off_peak"][column] == pytest.approx(peak_rate / 2), (model, column)


def test_current_and_legacy_flash_ids_bill_the_same():
    at = _utc(15, 12)
    kwargs = dict(input_tokens=1_000_000, output_tokens=1_000_000, pricing_at=at)
    current = cost_breakdown_usd(model_name="deepseek-flash", **kwargs)
    legacy = cost_breakdown_usd(model_name="deepseek-v4-flash", **kwargs)
    assert current == legacy


def test_dispatch_table_covers_every_provider():
    assert set(dispatch.PROVIDERS) == {"deepseek", "qwen", "openai", "anthropic", "gemini", "codex", "claude"}
    assert dispatch.configured_provider(" OpenAI ") == "openai"


def test_unknown_provider_is_refused_by_both_resolvers():
    with pytest.raises(ValueError, match="Unsupported translation provider"):
        dispatch.get_volume_translator("nope")
    with pytest.raises(ValueError, match="Unsupported translation provider"):
        dispatch.get_translator_class("nope")


def test_cli_providers_resolve_to_a_volume_translator_only():
    translate = dispatch.get_volume_translator("codex")
    assert translate.keywords == {"provider": "codex"}
    with pytest.raises(ValueError, match="subscription CLI"):
        dispatch.get_translator_class("claude")
