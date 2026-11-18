"""Zamanlayıcı tetikleyici teslimi ve Telegram gizlilik turu izolasyonu testleri."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from artemis.channels.telegram.channel import TelegramChannel
from artemis.config.settings import ProactiveConfig, Settings
from artemis.proactive.delivery import DeliveryOutcome
from artemis.proactive.scheduler import ProactiveScheduler
from artemis.proactive.store import ProactiveStore
from artemis.safety.audit import AuditLog

NOW = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeDeliverer:
    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def deliver(self, **kwargs: Any) -> DeliveryOutcome:
        self.calls.append(kwargs)
        ok = self.outcomes.pop(0) if self.outcomes else True
        return DeliveryOutcome(ok, {"panel": "ok"} if ok else {})


def _trigger_scheduler(
    tmp_path: Path, watch_dir: Path, deliverer: FakeDeliverer, clock: FakeClock
) -> tuple[ProactiveScheduler, Any, ProactiveStore]:
    trigger = SimpleNamespace(
        name="gelen",
        folder=watch_dir,
        pattern="*.txt",
        message="Yeni dosya geldi.",
        suggest="",
        routine=None,
    )
    settings = Settings(
        workspace=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        proactive=ProactiveConfig(enabled=True),
    )
    settings = settings.model_copy(
        update={"triggers": SimpleNamespace(enabled=True, items=(trigger,))}
    )
    audit = AuditLog(tmp_path / "audit.jsonl")
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    runtime = SimpleNamespace(settings=settings, audit=audit)
    scheduler = ProactiveScheduler(runtime, store, deliverer, clock=clock, owner="a")
    return scheduler, trigger, store


async def test_trigger_failed_delivery_keeps_file_ready_for_next_poll(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watched"
    watch_dir.mkdir()
    clock = FakeClock()
    deliverer = FakeDeliverer([False, True])
    scheduler, trigger, store = _trigger_scheduler(tmp_path, watch_dir, deliverer, clock)

    # İlk tarama yalnız taban çizgisidir.
    assert await scheduler.tick() == 0
    (watch_dir / "rapor.txt").write_text("gizli icerik", encoding="utf-8")

    # İkinci taramada dosya yeni görülür, kararlılık için bir tarama daha beklenir.
    clock.now += timedelta(seconds=60)
    assert await scheduler.tick() == 0
    assert deliverer.calls == []

    # Üçüncü taramada teslim başarısız olur: dosya beklemede kalmalı.
    clock.now += timedelta(seconds=60)
    assert await scheduler.tick() == 0
    assert len(deliverer.calls) == 1
    assert store.trigger_files("gelen")["rapor.txt"][1] is False

    # Sonraki yoklama aynı dosyayı hâlâ hazır bildirmeli ve yeniden teslim edebilmeli.
    clock.now += timedelta(seconds=60)
    assert await scheduler._poll_trigger(trigger, clock()) == ["rapor.txt"]
    assert await scheduler.tick() == 1
    assert len(deliverer.calls) == 2


async def test_trigger_successful_delivery_is_not_reported_again(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watched"
    watch_dir.mkdir()
    clock = FakeClock()
    deliverer = FakeDeliverer([True, True])
    scheduler, trigger, store = _trigger_scheduler(tmp_path, watch_dir, deliverer, clock)

    assert await scheduler.tick() == 0
    (watch_dir / "fatura.txt").write_text("tutar", encoding="utf-8")

    clock.now += timedelta(seconds=60)
    assert await scheduler.tick() == 0

    clock.now += timedelta(seconds=60)
    assert await scheduler.tick() == 1
    assert len(deliverer.calls) == 1
    assert store.trigger_files("gelen")["fatura.txt"][1] is True

    # Başarılı teslimden sonra aynı dosya bir daha hazır sayılmaz ve yeniden teslim edilmez.
    clock.now += timedelta(seconds=60)
    assert await scheduler._poll_trigger(trigger, clock()) == []
    assert await scheduler.tick() == 0
    assert len(deliverer.calls) == 1


def _runtime_for(turn_id: str) -> Any:
    return SimpleNamespace(
        context=SimpleNamespace(turn_id=turn_id),
        runner=SimpleNamespace(
            conversation=SimpleNamespace(render=lambda: SimpleNamespace(messages=[]))
        ),
    )


def _channel(tmp_path: Path) -> TelegramChannel:
    return TelegramChannel(
        object(),  # type: ignore[arg-type]
        1,
        audit=AuditLog(tmp_path / "a.jsonl"),
        approval_timeout=5,
        max_message_age=60,
        poll_timeout=0,
    )


def test_non_unattended_turn_start_does_not_clear_other_private_turn(tmp_path: Path) -> None:
    channel = _channel(tmp_path)
    channel.observe({"event": "turn_start", "turn_id": "turn-a", "unattended": True})
    channel.observe({"event": "privacy_decision", "turn_id": "turn-a", "cloud_allowed": False})
    channel.observe({"event": "privacy_decision", "turn_id": "turn-c", "cloud_allowed": False})
    channel.observe({"event": "turn_start", "turn_id": "turn-b", "unattended": False})

    assert channel._private_reason(_runtime_for("turn-a")) == "private_turn"
    assert channel._private_reason(_runtime_for("turn-c")) == "private_turn"
    assert channel._private_reason(_runtime_for("turn-b")) is None


def test_turn_own_privacy_mark_and_turn_end_cleanup(tmp_path: Path) -> None:
    channel = _channel(tmp_path)
    channel.observe({"event": "turn_start", "turn_id": "turn-a", "unattended": True})
    channel.observe({"event": "privacy_decision", "turn_id": "turn-a", "cloud_allowed": False})
    channel.observe({"event": "turn_start", "turn_id": "turn-b", "unattended": False})
    channel.observe({"event": "privacy_decision", "turn_id": "turn-b", "cloud_allowed": False})

    assert channel._private_reason(_runtime_for("turn-a")) == "private_turn"
    assert channel._private_reason(_runtime_for("turn-b")) == "private_turn"

    channel.observe({"event": "turn_end", "turn_id": "turn-b"})
    assert channel._private_reason(_runtime_for("turn-b")) is None
    assert channel._private_reason(_runtime_for("turn-a")) == "private_turn"

    channel.observe({"event": "turn_end", "turn_id": "turn-a"})
    assert channel._private_reason(_runtime_for("turn-a")) is None


async def test_trigger_respects_daily_cap_and_keeps_file_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Günlük sınır dolunca tetikleyici teslim etmez; dosya kaybolmaz, sınır açılınca bildirilir.
    watch_dir = tmp_path / "watched"
    watch_dir.mkdir()
    clock = FakeClock()
    deliverer = FakeDeliverer([True])
    scheduler, _trigger, store = _trigger_scheduler(tmp_path, watch_dir, deliverer, clock)
    assert await scheduler.tick() == 0
    (watch_dir / "rapor.txt").write_text("icerik", encoding="utf-8")
    real_count = store.count_finished_between
    monkeypatch.setattr(store, "count_finished_between", lambda *_a: 10**6)
    for _ in range(3):
        clock.now += timedelta(seconds=60)
        assert await scheduler.tick() == 0
    assert deliverer.calls == []
    assert store.trigger_files("gelen")["rapor.txt"][1] is False
    monkeypatch.setattr(store, "count_finished_between", real_count)
    clock.now += timedelta(seconds=60)
    assert await scheduler.tick() == 1
