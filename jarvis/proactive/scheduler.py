"""Hatırlatma ve brifing zamanlayıcısı (ADR 0027).

Yalnız uzun yaşayan süreçte (masaüstü, Telegram kanalı) çalışır. Her oluşum SQLite'ta önce
talep edilir, sonra işlenir: iki süreç aynı anda çalışsa da bir oluşum en fazla bir kez
teslim edilir. Talep edip ölen sürecin oluşumu otomatik tekrar çalıştırılmaz; belirsiz
olarak raporlanır (ADR 0026 ile aynı ilke).

Brifing kendi AgentLoop'unda, boş geçmişle ve `unattended=True` ile çalışır: kullanıcının
yürüyen turunu kilitlemez, panel geçmişine eklenmez, oradaki dış içerikli turları devralmaz.
Yan etki kararı kapıdadır; bu modül onay politikası taşımaz.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from jarvis.brain.agent_loop import AgentLoop
from jarvis.brain.conversation import ConversationContext
from jarvis.channels.runner import CommandRunner
from jarvis.channels.runtime import Runtime
from jarvis.config.settings import BriefingConfig
from jarvis.proactive.delivery import Deliverer, DeliveryOutcome
from jarvis.proactive.schedule import (
    briefing_occurrence,
    classify_reminder,
    format_local,
    local_day_bounds,
)
from jarvis.proactive.store import (
    ProactiveStore,
    ProactiveStoreError,
    Reminder,
    reminder_event_id,
)
from jarvis.safety.gate import SafetyGate
from jarvis.tools.base import ToolContext

# Talep eden süreç bu süreden uzun sessiz kalırsa öldüğü varsayılır. Brifing turu en çok
# `max_seconds` sürer; teslim için pay bırakılır.
STALE_MARGIN = timedelta(minutes=5)
MISSED_LIST_LIMIT = 20
BRIEFING_PREFIX = (
    "[Gözetimsiz proaktif tur: kullanıcı şu an ekranda olmayabilir ve onay veremez. "
    "Yalnız okuma araçlarını kullan; yan etkili işlemler reddedilir. Kısa, Türkçe bir "
    "özet yaz.]\n"
)


def _refuse(tool_name: str, arguments: dict[str, Any]) -> bool:
    """Gözetimsiz turda kapı onay sormaz; yine de bir çağrı gelirse cevap hayırdır."""
    return False


class ProactiveScheduler:
    def __init__(
        self,
        runtime: Runtime,
        store: ProactiveStore,
        deliverer: Deliverer,
        *,
        clock: Callable[[], datetime] | None = None,
        owner: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.store = store
        self.deliverer = deliverer
        self.clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self.owner = owner if owner is not None else uuid4().hex
        self.config = runtime.settings.proactive
        self.tz = ZoneInfo(self.config.timezone)
        self._capped_day: date | None = None

    async def serve(self, stop: asyncio.Event) -> None:
        """Sınırlı aralıklarla tarar; durdurma sinyali beklemeyi hemen keser."""
        audit = self.runtime.audit
        audit.emit("proactive_started", owner=self.owner, timezone=self.config.timezone)
        while not stop.is_set():
            try:
                await self.tick()
            except ProactiveStoreError as error:
                # Depo arızası kanalı düşürmez; bir sonraki taramada yeniden denenir.
                audit.emit("proactive_error", error_type=type(error).__name__)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.config.poll_seconds)

    async def tick(self) -> int:
        """Bir tarama; teslim edilen veya kapatılan oluşum sayısını döndürür."""
        now = self.clock()
        settings = self.runtime.settings
        stale_before = now - timedelta(seconds=settings.max_seconds) - STALE_MARGIN
        uncertain = self.store.mark_stale_claims(stale_before, now)
        if uncertain:
            self.runtime.audit.emit("proactive_uncertain", count=uncertain)
        start, end = local_day_bounds(now, self.tz)
        left = self.config.max_events_per_day - self.store.count_finished_between(start, end)
        handled = 0
        capped = False
        missed: list[Reminder] = []
        grace = timedelta(hours=self.config.reminder_grace_hours)
        for reminder in self.store.due_reminders(now):
            decision = classify_reminder(reminder.due_at, now, grace)
            event_id = reminder_event_id(reminder.id)
            if decision == "missed":
                if self.store.claim(event_id, reminder.due_at, "reminder", self.owner, now):
                    self.store.finish(
                        event_id, reminder.due_at, "missed", "Gecikme sınırı aşıldı.", now
                    )
                    missed.append(reminder)
                    handled += 1
                continue
            if decision == "wait":
                continue
            if left <= 0:
                capped = True
                continue
            if not self.store.claim(event_id, reminder.due_at, "reminder", self.owner, now):
                continue
            outcome = await self._deliver_reminder(reminder, late=decision == "late")
            self._finish(event_id, reminder.due_at, outcome, "completed")
            left -= int(outcome.delivered)
            handled += 1
        if missed:
            handled += await self._deliver_missed(missed, now)
        for briefing in self.config.briefings:
            scheduled = briefing_occurrence(
                briefing.days,
                briefing.at,
                now,
                self.tz,
                timedelta(hours=self.config.briefing_grace_hours),
            )
            if scheduled is None:
                continue
            if left <= 0:
                capped = True
                continue
            event_id = f"briefing:{briefing.name}"
            if not self.store.claim(event_id, scheduled, "briefing", self.owner, now):
                continue
            outcome, stop_reason = await self._run_briefing(briefing, scheduled)
            self._finish(event_id, scheduled, outcome, stop_reason)
            left -= int(outcome.delivered)
            handled += 1
        if capped:
            today = now.astimezone(self.tz).date()
            if self._capped_day != today:
                # Denetim her taramada değil, yerel gün başına bir kez yazılır.
                self._capped_day = today
                self.runtime.audit.emit(
                    "proactive_capped", max_events_per_day=self.config.max_events_per_day
                )
        return handled

    def _finish(
        self, event_id: str, scheduled: datetime, outcome: DeliveryOutcome, stop_reason: str
    ) -> None:
        ok = outcome.delivered and stop_reason == "completed"
        detail = json.dumps(
            {"targets": outcome.targets, "stop_reason": stop_reason}, ensure_ascii=False
        )
        self.store.finish(
            event_id, scheduled, "delivered" if ok else "failed", detail, self.clock()
        )

    async def _deliver_reminder(self, reminder: Reminder, *, late: bool) -> DeliveryOutcome:
        body = reminder.text
        if late:
            body += f"\n(Gecikmeli: {format_local(reminder.due_at, self.tz)} için kurulmuştu.)"
        if reminder.created_tainted:
            body += "\n(Bu hatırlatma dış bir içerik okunduktan sonra kuruldu.)"
        return await self.deliverer.deliver(
            event_id=reminder_event_id(reminder.id),
            scheduled_for=reminder.due_at,
            kind="reminder",
            title="Hatırlatma",
            body=body,
            private=reminder.created_private,
        )

    async def _deliver_missed(self, missed: list[Reminder], now: datetime) -> int:
        """Kapalıyken kaçırılanlar tek özet olarak, yine talep edilerek bildirilir."""
        event_id = "missed-summary"
        if not self.store.claim(event_id, now, "missed_summary", self.owner, now):
            return 0
        lines = [
            f"- {format_local(item.due_at, self.tz)}: {item.text}"
            for item in missed[:MISSED_LIST_LIMIT]
        ]
        if len(missed) > MISSED_LIST_LIMIT:
            lines.append(f"... ve {len(missed) - MISSED_LIST_LIMIT} hatırlatma daha.")
        outcome = await self.deliverer.deliver(
            event_id=event_id,
            scheduled_for=now,
            kind="missed_summary",
            title=f"Kaçırılan {len(missed)} hatırlatma",
            body="Jarvis kapalıyken zamanı geçti:\n" + "\n".join(lines),
            private=any(item.created_private for item in missed),
        )
        self._finish(event_id, now, outcome, "completed")
        return 1

    async def _run_briefing(
        self, briefing: BriefingConfig, scheduled: datetime
    ) -> tuple[DeliveryOutcome, str]:
        runtime = self.runtime
        # Ayrı bağlam: iptal sinyali, tur kimliği ve süre kullanıcının turuyla paylaşılmaz.
        context = ToolContext(
            workspace=runtime.context.workspace,
            settings=runtime.settings,
            audit=runtime.audit,
            services=runtime.context.services,
        )
        agent = AgentLoop(runtime.provider, SafetyGate(runtime.registry), context, _refuse)
        runner = CommandRunner(
            agent, ConversationContext(0, runtime.settings.context_token_budget)
        )
        local = format_local(scheduled, self.tz)
        text = BRIEFING_PREFIX + f"Brifing «{briefing.name}» ({local}): {briefing.prompt}"
        result = await runner.run(text, unattended=True)
        record = result.record
        if record is None:
            body = f"Brifing tamamlanmadı ({result.stop_reason})."
            private = False
        else:
            body = result.text or "(boş yanıt)"
            private = record.answer_private
        outcome = await self.deliverer.deliver(
            event_id=f"briefing:{briefing.name}",
            scheduled_for=scheduled,
            kind="briefing",
            title=f"Brifing · {briefing.name}",
            body=body,
            private=private,
        )
        return outcome, result.stop_reason
