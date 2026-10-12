"""Sağlayıcı bütçesi hata durumlarında ağ sınırını kapalı tutar."""

import sqlite3
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis.brain.errors import ProviderError
from jarvis.brain.models import Completion, Message
from jarvis.brain.router import ProviderRouter
from jarvis.config.settings import ModelConfig, Settings
from jarvis.safety.audit import AuditLog
from jarvis.tools.base import ToolDefinition


class RecordingProvider:
    def __init__(self, completion: Completion | None = None) -> None:
        self.completion = completion or Completion()
        self.calls = 0

    async def complete(
        self, messages: list[Message], tools: list[ToolDefinition], *, timeout: float
    ) -> Completion:
        self.calls += 1
        return self.completion


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        primary_model="cloud",
        allow_paid=True,
        models={
            "cloud": ModelConfig(
                provider="openrouter",
                model="fixture",
                input_per_million=Decimal("1"),
                output_per_million=Decimal("1"),
                context_window=1024,
            )
        },
    )


@pytest.fixture(autouse=True)
def test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", uuid4().hex)


def test_corrupt_budget_database_fails_safely_before_provider(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.state_dir.mkdir()
    (settings.state_dir / "budget.sqlite3").write_bytes(b"not a sqlite database")
    provider = RecordingProvider()

    with pytest.raises(ProviderError, match="Günlük bütçe kaydı") as failure:
        ProviderRouter(settings, AuditLog(settings.audit_path), providers={"cloud": provider})

    assert "not a sqlite database" not in str(failure.value)
    assert provider.calls == 0


async def test_budget_disk_error_blocks_provider_without_exposing_sqlite_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(tmp_path)
    provider = RecordingProvider()
    router = ProviderRouter(settings, AuditLog(settings.audit_path), providers={"cloud": provider})

    def locked_database(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked: secret-path")

    monkeypatch.setattr("jarvis.brain.budget.sqlite3.connect", locked_database)

    with pytest.raises(ProviderError, match="Günlük bütçe kaydı") as failure:
        await router.complete([], [], timeout=5)

    assert "secret-path" not in str(failure.value)
    assert provider.calls == 0


async def test_real_sqlite_write_lock_blocks_provider_quickly(tmp_path: Path) -> None:
    import time

    settings = settings_for(tmp_path)
    provider = RecordingProvider()
    router = ProviderRouter(settings, AuditLog(settings.audit_path), providers={"cloud": provider})
    locker = sqlite3.connect(settings.state_dir / "budget.sqlite3", isolation_level=None)
    try:
        locker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(ProviderError, match="Günlük bütçe kaydı"):
            await router.complete([], [], timeout=5)
        elapsed = time.monotonic() - started
    finally:
        locker.rollback()
        locker.close()

    assert elapsed < 1
    assert provider.calls == 0


@pytest.mark.parametrize("bad_amount", ["NaN", "-0.1", "not-a-number"])
async def test_corrupted_numeric_ledger_fails_closed_before_provider(
    tmp_path: Path, bad_amount: str
) -> None:
    settings = settings_for(tmp_path)
    provider = RecordingProvider()
    router = ProviderRouter(settings, AuditLog(settings.audit_path), providers={"cloud": provider})
    token = router.ledger.reserve(Decimal("0.5"))
    connection = sqlite3.connect(settings.state_dir / "budget.sqlite3")
    try:
        connection.execute(
            "UPDATE budget_reservations SET reserved = ? WHERE token = ?",
            (bad_amount, token),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ProviderError, match="Günlük bütçe kaydı"):
        await router.complete([], [], timeout=5)
    assert provider.calls == 0


async def test_audit_failure_after_completion_still_settles_cost(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    completion = Completion()
    provider = RecordingProvider(completion)
    audit = AuditLog(settings.audit_path)
    router = ProviderRouter(settings, audit, providers={"cloud": provider})
    original_emit = audit.emit

    def fail_success_record(event: str, **fields: object) -> None:
        if event == "provider_call" and fields.get("success") is True:
            raise OSError("disk full")
        original_emit(event, **fields)

    audit.emit = fail_success_record  # type: ignore[method-assign]

    with pytest.raises(ProviderError, match="denetim kaydı"):
        await router.complete([], [], timeout=5)

    assert provider.calls == 1
    assert router.ledger.reserved == 0
    assert router.ledger.spent == completion.usage.cost_usd
