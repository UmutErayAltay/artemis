"""Hafif model kipi (ADR 0092): otomatik kural ve yerel uç nokta tanıma."""

from __future__ import annotations

import pytest

from artemis.brain.light_mode import is_local_endpoint, light_mode_on
from artemis.config.settings import EndpointConfig, ModelConfig

ENDPOINTS = {
    "yerel": EndpointConfig(base_url="http://127.0.0.1:1234/v1", api_key=False),
    "bulut": EndpointConfig(base_url="https://api.example.com/v1"),
}


def _model(provider: str = "openrouter", name: str = "x/y", **extra: object) -> ModelConfig:
    if provider == "openai_compatible":
        extra.setdefault("endpoint", "bulut")
    return ModelConfig.model_validate(
        {"provider": provider, "model": name, "input_per_million": 0, "output_per_million": 0,
         **extra}
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (_model("ollama", "qwen3:30b"), True),
        (_model("openai_compatible", "m", endpoint="yerel"), True),
        (_model("openai_compatible", "m", endpoint="bulut"), False),
        (_model("openrouter", "meta/llama:free"), True),
        (_model("openrouter", "anthropic/claude-sonnet"), False),
        (_model("anthropic", "claude-x"), False),
        (_model("claude_cli", "sonnet"), False),
        # Açık değer otomatiği ezer: iki yönde.
        (_model("ollama", "q", light_mode=False), False),
        (_model("anthropic", "claude-x", light_mode=True), True),
    ],
)
def test_auto_rule_matrix(model: ModelConfig, expected: bool) -> None:
    assert light_mode_on(model, ENDPOINTS) is expected


def test_missing_model_is_off() -> None:
    assert light_mode_on(None, ENDPOINTS) is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:11434/v1", True),
        ("http://localhost:1234", True),
        ("http://[::1]:8000/v1", True),
        ("http://0.0.0.0:8000", True),
        ("http://LOCALHOST/v1", True),
        ("https://api.openai.com/v1", False),
        ("http://192.168.1.5:11434", False),
        ("https://localhost.evil.com/v1", False),
        ("not a url", False),
    ],
)
def test_is_local_endpoint(url: str, expected: bool) -> None:
    assert is_local_endpoint(url) is expected
