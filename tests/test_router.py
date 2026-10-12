"""Yedek ve maliyet kararları sahte yanıtlarla, gerçek SQLite üzerinde ölçülür."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis.brain.errors import BudgetExceeded, ProviderError, RetryableProviderError
from jarvis.brain.models import Completion, Message, Usage
from jarvis.brain.providers import build_provider
from jarvis.brain.router import ProviderRouter
from jarvis.config.settings import ModelConfig, Settings
from jarvis.safety.audit import AuditLog
from jarvis.tools.base import ToolDefinition


class Responses:
    def __init__(self, *responses: Completion | ProviderError) -> None:
        self.responses = list(responses)
        self.timeouts: list[float] = []

    async def complete(
        self, messages: list[Message], tools: list[ToolDefinition], *, timeout: float
    ) -> Completion:
        self.timeouts.append(timeout)
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        return response


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Test anahtarı kaynakta sabit tutulmaz ve hiçbir dış sunucuya gönderilmez.
    monkeypatch.setenv("OPENROUTER_API_KEY", uuid4().hex)


def settings_for(tmp_path: Path, *, paid: bool = False, budget: str = "1") -> Settings:
    return Settings(
        workspace=tmp_path / "desktop",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        primary_model="cloud",
        fallback_model="local",
        daily_budget_usd=Decimal(budget),
        allow_paid=paid,
        max_output_tokens=100,
        models={
            "cloud": ModelConfig(
                provider="openrouter",
                model="fixture-cloud",
                input_per_million=Decimal("1" if paid else "0"),
                output_per_million=Decimal("2" if paid else "0"),
                context_window=1024,
            ),
            "local": ModelConfig(
                provider="ollama",
                model="fixture-local",
                input_per_million=Decimal("0"),
                output_per_million=Decimal("0"),
            ),
        },
    )


async def test_fallback_cooldown_then_cloud_retry(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    cloud = Responses(RetryableProviderError("Bağlantı yok."), Completion(content="Bulut"))
    local = Responses(Completion(content="Yerel 1"), Completion(content="Yerel 2"))
    now = [100.0]
    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"cloud": cloud, "local": local},
        clock=lambda: now[0],
    )
    first = await router.complete([], [], timeout=10)
    assert first.local_fallback and first.content == "Yerel 1"
    now[0] = 130.0
    second = await router.complete([], [], timeout=10)
    assert second.local_fallback and second.content == "Yerel 2"
    now[0] = 161.0
    third = await router.complete([], [], timeout=10)
    assert not third.local_fallback and third.content == "Bulut"
    assert len(cloud.timeouts) == 2
    assert len(local.timeouts) == 2
    records = [json.loads(line) for line in settings.audit_path.read_text().splitlines()]
    assert sum(record["event"] == "provider_fallback" for record in records) == 1


@pytest.mark.parametrize("failure", [ProviderError("Kimlik hatası"), BudgetExceeded("Bütçe")])
async def test_auth_and_budget_do_not_trigger_fallback(
    tmp_path: Path, failure: ProviderError
) -> None:
    settings = settings_for(tmp_path)
    local = Responses(Completion())
    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"cloud": Responses(failure), "local": local},
    )
    with pytest.raises(type(failure)):
        await router.complete([], [], timeout=10)
    assert local.timeouts == []


async def test_budget_reserves_before_network_and_persists_cost(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, paid=True, budget="0.0015")
    # Bir sonraki çağrı için gereken .001224, ilk .0004 harcamayla tavana sığmaz.
    cloud = Responses(
        Completion(usage=Usage(input_tokens=200, output_tokens=100, cost_usd=Decimal("0.0004")))
    )
    router = ProviderRouter(settings, AuditLog(settings.audit_path), providers={"cloud": cloud})
    await router.complete([], [], timeout=10)
    assert router.ledger.totals() == (Decimal("0.0004"), Decimal("0"))
    reopened = ProviderRouter(settings, AuditLog(settings.audit_path), providers={"cloud": cloud})
    with pytest.raises(BudgetExceeded):
        await reopened.complete([], [], timeout=10)
    assert len(cloud.timeouts) == 1
    entries = [json.loads(line) for line in settings.audit_path.read_text().splitlines()]
    billed = next(record for record in entries if record["event"] == "provider_call")
    assert billed["input_tokens"] == 200 and billed["output_tokens"] == 100
    assert billed["cost_usd"] == "0.0004"


async def test_uncertain_failure_retains_reservation(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, paid=True)
    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={
            "cloud": Responses(RetryableProviderError("Sunucu durdu")),
            "local": Responses(Completion(content="Yerel")),
        },
    )
    await router.complete([], [], timeout=10)
    assert router.ledger.reserved == Decimal("0.001224")


async def test_paid_permission_is_checked_before_provider(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, paid=True).model_copy(update={"allow_paid": False})
    cloud = Responses(Completion())
    router = ProviderRouter(settings, AuditLog(settings.audit_path), providers={"cloud": cloud})
    with pytest.raises(BudgetExceeded):
        await router.complete([], [], timeout=10)
    assert cloud.timeouts == []
    assert router.ledger.totals() == (0, 0)


async def test_missing_key_does_not_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    settings = settings_for(tmp_path, paid=True)
    cloud = Responses(Completion())
    router = ProviderRouter(settings, AuditLog(settings.audit_path), providers={"cloud": cloud})
    with pytest.raises(ProviderError, match="ortam değişkeni"):
        await router.complete([], [], timeout=10)
    assert cloud.timeouts == []
    assert router.ledger.totals() == (0, 0)


async def test_fallback_receives_remaining_time(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    now = [100.0]

    class SlowFailure:
        async def complete(
            self, messages: list[Message], tools: list[ToolDefinition], *, timeout: float
        ) -> Completion:
            now[0] += 4
            raise RetryableProviderError("Süre tüketildi")

    local = Responses(Completion())
    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"cloud": SlowFailure(), "local": local},
        clock=lambda: now[0],
    )
    await router.complete([], [], timeout=10)
    assert local.timeouts == [6]


async def test_cloud_privacy_decision_is_audited(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    router = ProviderRouter(
        settings, AuditLog(settings.audit_path), providers={"cloud": Responses(Completion())}
    )
    await router.complete([Message(role="tool", content="Özel not", private=True)], [], timeout=10)
    records = [json.loads(line) for line in settings.audit_path.read_text().splitlines()]
    transfer = next(record for record in records if record["event"] == "cloud_transfer")
    assert transfer["masked_messages"] == 1
    assert transfer["allow_cloud_file_content"] is False
    assert "Özel not" not in settings.audit_path.read_text()


async def test_cancellation_retains_budget_and_audits(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, paid=True)

    class CancelledProvider:
        async def complete(
            self, messages: list[Message], tools: list[ToolDefinition], *, timeout: float
        ) -> Completion:
            raise asyncio.CancelledError

    router = ProviderRouter(
        settings, AuditLog(settings.audit_path), providers={"cloud": CancelledProvider()}
    )
    with pytest.raises(asyncio.CancelledError):
        await router.complete([], [], timeout=10)
    assert router.ledger.reserved == Decimal("0.001224")
    assert "CancelledError" in settings.audit_path.read_text()


def test_build_provider_requires_explicit_model(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="Birincil model"):
        build_provider(Settings(), AuditLog(tmp_path / "audit.jsonl"))
    settings = settings_for(tmp_path)
    assert isinstance(build_provider(settings, AuditLog(settings.audit_path)), ProviderRouter)
