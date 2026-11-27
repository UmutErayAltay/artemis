"""Yerel uç noktada istek başına süre sınırı yok, bulutta var (ADR 0092 madde 6)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from artemis.brain.errors import RetryableProviderError
from artemis.brain.models import Completion, Message
from artemis.brain.router import ProviderRouter
from artemis.config.settings import EndpointConfig, ModelConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.tools.base import ToolDefinition


class Slow:
    """Verilen süre kadar bekleyip yanıt veren sahte sağlayıcı; aldığı sınırı da saklar."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.timeouts: list[float] = []

    async def complete(
        self, messages: list[Message], tools: list[ToolDefinition], *, timeout: float,
        stream: object = None,
    ) -> Completion:
        self.timeouts.append(timeout)
        await asyncio.sleep(self.delay)
        return Completion(content="bitti")


def _model(provider: str, **extra: object) -> ModelConfig:
    return ModelConfig.model_validate({
        "provider": provider, "model": f"fx-{uuid4().hex[:4]}", "input_per_million": Decimal(0),
        "output_per_million": Decimal(0), **extra,
    })


def _settings(tmp_path: Path, primary: str) -> Settings:
    return Settings(
        workspace=tmp_path, state_dir=tmp_path / "state", audit_path=tmp_path / "audit.jsonl",
        provider_timeout_seconds=0.05, max_output_tokens=100, primary_model=primary,
        endpoints={
            "yerel": EndpointConfig(base_url="http://127.0.0.1:1234/v1", api_key=False),
            "uzak": EndpointConfig(base_url="https://api.example.com/v1"),
        },
        models={
            "bulut": _model("openrouter"),
            "ollama": _model("ollama"),
            "lmstudio": _model("openai_compatible", endpoint="yerel"),
            "uzakuc": _model("openai_compatible", endpoint="uzak"),
        },
    )


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", uuid4().hex)  # hiçbir sunucuya gitmez
    monkeypatch.setenv("ARTEMIS_UZAK_API_KEY", uuid4().hex)


@pytest.mark.parametrize("alias", ["ollama", "lmstudio"])
async def test_local_model_is_not_cut_at_provider_timeout(tmp_path: Path, alias: str) -> None:
    settings = _settings(tmp_path, alias)
    slow = Slow(0.3)  # 0.05 sn sınırın 6 katı
    router = ProviderRouter(settings, AuditLog(settings.audit_path), providers={alias: slow})
    result = await router.complete([], [], timeout=5)
    assert result.content == "bitti"
    # Sınır kalan tur bütçesidir (5 sn), provider_timeout_seconds (0.05) değil.
    assert slow.timeouts == [pytest.approx(5, abs=0.5)]


@pytest.mark.parametrize("alias", ["bulut", "uzakuc"])
async def test_cloud_model_is_still_cut(tmp_path: Path, alias: str) -> None:
    settings = _settings(tmp_path, alias)
    slow = Slow(0.3)
    router = ProviderRouter(settings, AuditLog(settings.audit_path), providers={alias: slow})
    with pytest.raises(RetryableProviderError):
        await router.complete([], [], timeout=5)
    assert slow.timeouts == [pytest.approx(0.05, abs=0.03)]
    assert slow.timeouts[0] < 0.06  # bütçe (5 sn) değil, sağlayıcı sınırı
