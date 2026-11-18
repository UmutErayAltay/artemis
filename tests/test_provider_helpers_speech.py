"""Ses modeli maliyet tahmini yardımcıları."""

from __future__ import annotations

from decimal import Decimal

from artemis.channels.desktop.provider_helpers import (
    SPEECH_REPLY_INPUT_TOKENS,
    SPEECH_REPLY_OUTPUT_TOKENS,
    speech_cost_text,
    speech_cost_text_from_fields,
    speech_reply_cost,
)


def test_speech_reply_cost() -> None:
    expected = (
        Decimal(SPEECH_REPLY_INPUT_TOKENS) * Decimal("0.5")
        + Decimal(SPEECH_REPLY_OUTPUT_TOKENS) * Decimal("10")
    ) / Decimal(1_000_000)
    assert speech_reply_cost(Decimal("0.5"), Decimal("10")) == expected


def test_speech_cost_text_zero_returns_empty() -> None:
    assert speech_cost_text(Decimal(0), Decimal(0)) == ""
    assert speech_cost_text(Decimal("0.0"), Decimal("0.00")) == ""


def test_speech_cost_text_nonzero_format() -> None:
    text = speech_cost_text(Decimal("0.5"), Decimal("10"))
    assert text.startswith("Tahmin: bir sesli yanıt")
    assert "USD; 100 yanıt ≈ " in text
    assert text == "Tahmin: bir sesli yanıt (~15 sn) ≈ 0.0038 USD; 100 yanıt ≈ 0.38 USD."


def test_speech_cost_text_from_fields_invalid_returns_empty() -> None:
    assert speech_cost_text_from_fields("abc", "10") == ""
    assert speech_cost_text_from_fields("0.5", "NaN") == ""
    assert speech_cost_text_from_fields("-1", "10") == ""
    assert speech_cost_text_from_fields("", "") == ""
    assert speech_cost_text_from_fields("0.5", "10").startswith("Tahmin: bir sesli yanıt")
