"""Yedek ve maliyet kararları sahte yanıtlarla, gerçek SQLite üzerinde ölçülür."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from artemis.brain.errors import (
    BudgetExceeded,
    MediaRejectedError,
    ProviderError,
    RateLimitedError,
    RetryableProviderError,
)
from artemis.brain.models import Completion, Message, Usage
from artemis.brain.providers import _check_status, _post_lines, build_provider
from artemis.brain.router import ProviderRouter
from artemis.config.settings import ModelConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.tools.base import ToolDefinition


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
    cloud = Responses(RateLimitedError("Hız sınırı."), Completion(content="Bulut"))
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


async def test_provider_timeout_uses_remaining_time_for_fallback(tmp_path: Path) -> None:
    settings = settings_for(tmp_path).model_copy(update={"provider_timeout_seconds": 0.02})

    class SlowCloud:
        async def complete(
            self, messages: list[Message], tools: list[ToolDefinition], *, timeout: float
        ) -> Completion:
            await asyncio.sleep(1)
            return Completion(content="Gecikmiş yanıt")

    local = Responses(Completion(content="Yedek"))
    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"cloud": SlowCloud(), "local": local},
    )

    result = await router.complete([], [], timeout=2)

    assert result.local_fallback and result.content == "Yedek"
    # ADR 0092: yerel yedekte istek sınırı yok; kalan tur bütçesi (2 sn - 0.02 sn) verilir.
    assert 1.5 < local.timeouts[0] <= 2
    assert router._fallback_until == 0


async def test_external_cancellation_does_not_start_fallback(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    class WaitingCloud:
        async def complete(
            self, messages: list[Message], tools: list[ToolDefinition], *, timeout: float
        ) -> Completion:
            await asyncio.Event().wait()
            return Completion()

    local = Responses(Completion())
    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"cloud": WaitingCloud(), "local": local},
    )
    task = asyncio.create_task(router.complete([], [], timeout=10))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert local.timeouts == []


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


async def test_cloud_transfer_audit_failure_releases_unsent_reservation(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path, paid=True)
    provider = Responses(Completion())
    audit = AuditLog(settings.audit_path)
    router = ProviderRouter(settings, audit, providers={"cloud": provider})

    def fail_transfer(event: str, **fields: object) -> None:
        if event == "cloud_transfer":
            raise OSError("synthetic disk error")

    audit.emit = fail_transfer  # type: ignore[method-assign]
    with pytest.raises(ProviderError, match="denetim kaydı"):
        await router.complete([], [], timeout=5)

    assert provider.timeouts == []
    assert router.ledger.totals() == (Decimal(0), Decimal(0))


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


async def test_connection_error_does_not_pin_fallback(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    cloud = Responses(RetryableProviderError("Bağlantı yok."), Completion(content="Bulut"))
    local = Responses(Completion(content="Yerel"), Completion(content="Yerel 2"))
    now = [100.0]
    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"cloud": cloud, "local": local},
        clock=lambda: now[0],
    )
    first = await router.complete([], [], timeout=10)
    assert first.local_fallback and first.content == "Yerel"
    now[0] = 110.0  # fallback_seconds içinde kalsa da bağlantı hatası birincili kilitlemez
    second = await router.complete([], [], timeout=10)
    assert not second.local_fallback and second.content == "Bulut"
    assert len(cloud.timeouts) == 2
    assert len(local.timeouts) == 1


async def test_rate_limit_pins_fallback(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    cloud = Responses(RateLimitedError("429 hız sınırı"), Completion(content="Bulut"))
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
    now[0] = 110.0
    second = await router.complete([], [], timeout=10)
    assert second.local_fallback and second.content == "Yerel 2"
    assert len(cloud.timeouts) == 1
    assert len(local.timeouts) == 2


async def test_private_messages_on_local_model_do_not_tier_fallback_to_cloud(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    cloud = Responses(Completion(content="Bulut"))
    local = Responses(ProviderError("Yerel Ollama kapalı"))
    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"cloud": cloud, "local": local},
    )
    messages = [
        Message(role="system", content="Sistem"),
        Message(role="user", content="Gizli dikte metni", private=True),
    ]
    with pytest.raises(ProviderError, match="Ollama"):
        await router.complete(messages, [], timeout=10, model="local")
    assert cloud.timeouts == []
    assert len(local.timeouts) == 1
    events = [json.loads(line)["event"] for line in settings.audit_path.read_text().splitlines()]
    assert "model_tier_fallback" not in events
    assert "cloud_transfer" not in events


class _CaptureMediaProvider:
    def __init__(self, *responses: Completion | ProviderError) -> None:
        self.responses = list(responses)
        self.seen_messages: list[list[Message]] = []

    async def complete(
        self, messages: list[Message], tools: list[ToolDefinition], *, timeout: float
    ) -> Completion:
        self.seen_messages.append(list(messages))
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        return response


async def test_non_media_400_context_length_does_not_strip_images_or_fallback(
    tmp_path: Path,
) -> None:
    response = httpx.Response(
        400,
        json={
            "error": {
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
                "message": "This model's maximum context length is 8192 tokens.",
            }
        },
    )
    with pytest.raises(ProviderError) as exc_info:
        _check_status(response)
    assert not isinstance(exc_info.value, MediaRejectedError)

    settings = settings_for(tmp_path)
    cloud = _CaptureMediaProvider(exc_info.value, Completion(content="olmamalı"))
    local = _CaptureMediaProvider(Completion(content="yerel olmamalı"))
    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"cloud": cloud, "local": local},
    )
    msg = Message(role="user", content="ekrana bak", image_png=b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ProviderError) as router_exc:
        await router.complete([msg], [], timeout=10)
    assert not isinstance(router_exc.value, MediaRejectedError)
    assert len(cloud.seen_messages) == 1
    assert cloud.seen_messages[0][0].image_png is not None
    assert local.seen_messages == []
    events = [json.loads(line)["event"] for line in settings.audit_path.read_text().splitlines()]
    assert "image_input_fallback" not in events
    assert "audio_input_fallback" not in events


async def test_media_400_image_rejection_classifies_hides_body_and_respects_flag(
    tmp_path: Path,
) -> None:
    secret_body = "model does not support image_url secret-token-xyz"
    response = httpx.Response(
        400,
        json={"error": {"message": secret_body, "type": "invalid_request_error"}},
    )
    with pytest.raises(MediaRejectedError) as exc_info:
        _check_status(response)
    assert exc_info.value.media_kind == "image"
    assert "secret-token-xyz" not in str(exc_info.value)
    assert "image_url" not in str(exc_info.value)

    msg = Message(role="user", content="ekranda ne var", image_png=b"\x89PNG\r\n\x1a\n")

    # allow_image_fallback=True (varsayılan): görüntüyü düşürüp bir kez yeniden dener.
    settings = settings_for(tmp_path / "allow")
    cloud_allow = _CaptureMediaProvider(exc_info.value, Completion(content="metinle devam"))
    router_allow = ProviderRouter(
        settings, AuditLog(settings.audit_path), providers={"cloud": cloud_allow}
    )
    comp = await router_allow.complete([msg], [], timeout=10)
    assert comp.content == "metinle devam"
    assert len(cloud_allow.seen_messages) == 2
    assert cloud_allow.seen_messages[0][0].image_png is not None
    assert cloud_allow.seen_messages[1][0].image_png is None
    events = [json.loads(line)["event"] for line in settings.audit_path.read_text().splitlines()]
    assert "image_input_fallback" in events

    # allow_image_fallback=False: görüntüyü düşürmeden başarısız olur.
    deny_settings = settings_for(tmp_path / "deny")
    cloud_deny = _CaptureMediaProvider(exc_info.value, Completion(content="olmamalı"))
    router_deny = ProviderRouter(
        deny_settings,
        AuditLog(deny_settings.audit_path),
        providers={"cloud": cloud_deny},
        allow_image_fallback=False,
    )
    with pytest.raises(MediaRejectedError) as deny_exc:
        await router_deny.complete([msg], [], timeout=10)
    assert deny_exc.value.media_kind == "image"
    assert len(cloud_deny.seen_messages) == 1
    deny_events = [
        json.loads(line)["event"] for line in deny_settings.audit_path.read_text().splitlines()
    ]
    assert "image_input_fallback" not in deny_events


async def test_streaming_post_lines_reads_400_body_to_classify_media_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"detail": "This model does not support input_audio payloads."},
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("artemis.brain.providers.httpx.AsyncClient", client_factory)
    with pytest.raises(MediaRejectedError) as exc_info:
        await _post_lines("https://example.invalid/v1/chat/completions", {}, {}, timeout=5,
                          on_line=lambda _line: None)
    assert exc_info.value.media_kind == "audio"
    assert "input_audio" not in str(exc_info.value)


async def test_disallow_image_fallback_rejects_claude_cli_before_call(tmp_path: Path) -> None:
    settings = Settings(
        workspace=tmp_path / "desktop",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        primary_model="cli",
        models={
            "cli": ModelConfig(
                provider="claude_cli",
                model="sonnet",
                input_per_million=Decimal("0"),
                output_per_million=Decimal("0"),
            )
        },
    )
    cli_provider = _CaptureMediaProvider(Completion(content="olmamalı"))
    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"cli": cli_provider},
        allow_image_fallback=False,
    )
    msg = Message(role="user", content="ekran", image_png=b"\x89PNG\r\n\x1a\n")
    with pytest.raises(MediaRejectedError) as exc_info:
        await router.complete([msg], [], timeout=10)
    assert exc_info.value.media_kind == "image"
    assert cli_provider.seen_messages == []
