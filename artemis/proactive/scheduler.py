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
import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from artemis.brain.agent_loop import AgentLoop
from artemis.brain.conversation import ConversationContext
from artemis.channels.runner import CommandRunner
from artemis.channels.runtime import Runtime
from artemis.config.settings import BriefingConfig, TriggerConfig
from artemis.proactive.daily_summary import build_summary, summary_path, write_summary
from artemis.proactive.delivery import Deliverer, DeliveryOutcome
from artemis.proactive.routines import RoutinesError, RoutineStore
from artemis.proactive.schedule import (
    briefing_occurrence,
    classify_reminder,
    format_local,
    local_day_bounds,
)
from artemis.proactive.store import (
    ProactiveStore,
    Reminder,
    reminder_event_id,
)
from artemis.proactive.triggers import build_notice, scan_folder
from artemis.safety.gate import SafetyGate
from artemis.tools.base import ToolContext

# Talep eden süreç bu süreden uzun sessiz kalırsa öldüğü varsayılır. Brifing turu en çok
# `max_seconds` sürer; teslim için pay bırakılır.
STALE_MARGIN = timedelta(minutes=5)
# Özet saatinden bu kadar sonra üretilirse "gecikmeli" diye işaretlenir (uygulama kapalıydı).
SUMMARY_LATE_AFTER = timedelta(minutes=5)
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
        self._routines_error_reported = False
        self._trigger_errors: set[str] = set()

    async def serve(self, stop: asyncio.Event) -> None:
        """Sınırlı aralıklarla tarar; durdurma sinyali beklemeyi hemen keser."""
        audit = self.runtime.audit
        audit.emit("proactive_started", owner=self.owner, timezone=self.config.timezone)
        while not stop.is_set():
            try:
                await self.tick()
            except (OSError, ValueError, RuntimeError) as error:
                # Depo (ProactiveStoreError bir RuntimeError'dır), saat dilimi veya teslim
                # arızası zamanlayıcıyı sessizce öldürmemeli: denetime yazılır, sonraki
                # taramada yeniden denenir. Talep edilmiş oluşum varsa belirsiz kalır.
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
            # Özet sınırdan bağımsız gönderilir: kaçırılanlar zaten "missed" kapandı ve
            # başka iz bırakmazdı; tek mesaj olduğu için yine de sınıra sayılır.
            delivered = await self._deliver_missed(missed, now)
            handled += delivered
            left -= delivered
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
        for name, scheduled in self._due_routines(now):
            if left <= 0:
                capped = True
                continue
            event_id = f"routine:{name}"
            if not self.store.claim(event_id, scheduled, "routine", self.owner, now):
                continue
            # Çalıştırılmaz: yalnız sorulur. Çalıştırma kullanıcının panel tıklamasıyla, onaylı
            # normal turda olur; gözetimsiz kural kapıda olduğu gibi kalır (ADR 0058).
            outcome = await self.deliverer.deliver(
                event_id=event_id,
                scheduled_for=scheduled,
                kind="routine",
                title="Rutin zamanı",
                body=f"{name} rutininin zamanı. Çalıştırılsın mı?",
                private=False,
                routine=name,
            )
            self._finish(event_id, scheduled, outcome, "completed")
            left -= int(outcome.delivered)
            handled += 1
        if self.runtime.settings.daily_summary.enabled:
            if left <= 0:
                capped = True
            else:
                delivered = await self._daily_summary(now)
                left -= delivered
                handled += delivered
        triggers = self.runtime.settings.triggers
        for trigger in triggers.items if triggers.enabled else ():
            try:
                ready = await self._poll_trigger(trigger, now)
                if not ready:
                    continue
                if left <= 0:
                    capped = True
                    continue
                delivered = await self._fire_trigger(trigger, ready, start, now)
                if delivered:
                    self._mark_trigger_seen(trigger.name, ready)
            except (OSError, ValueError, RuntimeError) as error:
                # Bozuk ya da erişilemeyen klasör yalnız o tetikleyiciyi etkiler; hata her taramada
                # değil, bir kez denetime yazılır. Diğer tetikleyiciler çalışmaya devam eder.
                if trigger.name not in self._trigger_errors:
                    self._trigger_errors.add(trigger.name)
                    self.runtime.audit.emit(
                        "trigger_unavailable", trigger=trigger.name, error_type=type(error).__name__
                    )
                continue
            self._trigger_errors.discard(trigger.name)
            left -= delivered
            handled += delivered
        if capped:
            today = now.astimezone(self.tz).date()
            if self._capped_day != today:
                # Denetim her taramada değil, yerel gün başına bir kez yazılır.
                self._capped_day = today
                self.runtime.audit.emit(
                    "proactive_capped", max_events_per_day=self.config.max_events_per_day
                )
        return handled

    async def _daily_summary(self, now: datetime) -> int:
        """Gün sonu özeti (ADR 0066): LLM'siz, yerel dosya + "hazır" bildirimi. Boş gün yazmaz.

        Özet deterministik olduğundan kapalıyken kaçan gün, süre payı içinde sonraki açılışta
        yine üretilir ("gecikmeli" etiketiyle). Hiçbir hata taramayı düşürmez.
        """
        settings = self.runtime.settings
        grace = timedelta(hours=self.config.briefing_grace_hours)
        scheduled = briefing_occurrence(
            (0, 1, 2, 3, 4, 5, 6),
            time.fromisoformat(settings.daily_summary.at),
            now,
            self.tz,
            grace,
        )
        if scheduled is None:
            return 0
        event_id = "daily_summary"
        if not self.store.claim(event_id, scheduled, "daily_summary", self.owner, now):
            return 0
        day = scheduled.astimezone(self.tz).date()
        start, end = local_day_bounds(scheduled, self.tz)
        try:
            counts = self.store.finished_counts_between(start, end, "reminder")
            text = await asyncio.to_thread(
                build_summary,
                settings.audit_path,
                day,
                start,
                end,
                reminders=counts,
                late_at=format_local(now, self.tz) if now - scheduled > SUMMARY_LATE_AFTER else "",
            )
            if text is not None:
                await asyncio.to_thread(
                    write_summary, summary_path(settings.state_dir, day), text
                )
        except (OSError, ValueError, RuntimeError, ArithmeticError) as error:
            self.runtime.audit.emit("daily_summary_error", error_type=type(error).__name__)
            self._finish(event_id, scheduled, DeliveryOutcome(False, {}), "completed")
            return 0
        if text is None:
            # Boş gün: dosya da bildirim de yok. "missed" yalnız yeniden talep edilmesin diye
            # kapanış durumudur; günlük sınıra sayılmaz.
            self.store.finish(
                event_id, scheduled, "missed", "Boş gün; özet yazılmadı.", self.clock()
            )
            return 0
        outcome = await self.deliverer.deliver(
            event_id=event_id,
            scheduled_for=scheduled,
            kind="daily_summary",
            title="Günün özeti hazır",
            body=f"{day.isoformat()} özeti state_dir/gunluk klasörüne yazıldı.",
            private=True,
            summary_day=day.isoformat(),
            local_only=True,  # kullanıcının etkinliği: Telegram'a hiç gitmez
        )
        self._finish(event_id, scheduled, outcome, "completed")
        return int(outcome.delivered)

    async def _poll_trigger(
        self, trigger: TriggerConfig, now: datetime
    ) -> list[str]:
        """Klasörü tarar, durumu yazar; bildirilmeye hazır (boyutu iki yoklamada aynı) dosyaları
        döndürür. İlk tarama yalnız taban çizgisidir. Hazır dosyalar teslim başarılı olana dek
        beklemede kalır; teslim başarısız olursa veya günlük sınır doluysa sonraki taramada
        yeniden bildirilir."""
        found, truncated = await asyncio.to_thread(scan_folder, trigger.folder, trigger.pattern)
        known = self.store.trigger_files(trigger.name)
        if known is None:  # ilk tarama: var olanlar bildirilmez
            baseline = {name: (size, True) for name, size in found.items()}
            self.store.save_trigger_files(trigger.name, baseline, keep_missing=False)
            return []
        rows: dict[str, tuple[int, bool]] = {}
        ready: list[str] = []
        for name, size in found.items():
            before = known.get(name)
            if before is None or (not before[1] and before[0] != size):
                rows[name] = (size, False)  # yeni ya da hâlâ büyüyor
            elif before[1]:
                rows[name] = before
            else:
                ready.append(name)
                # Teslim başarısız olursa kaybolmasın diye görüldü işareti teslim sonrası yazılır.
                rows[name] = (size, False)
        self.store.save_trigger_files(trigger.name, rows, keep_missing=truncated)
        return ready

    def _mark_trigger_seen(self, trigger_name: str, ready: list[str]) -> None:
        # Yalnız başarılı teslimden sonra tetikleyici dosyaları görüldü işaretlenir.
        known = self.store.trigger_files(trigger_name) or {}
        updates = {name: (known[name][0], True) for name in ready if name in known}
        if updates:
            self.store.save_trigger_files(trigger_name, updates, keep_missing=True)

    async def _fire_trigger(
        self, trigger: TriggerConfig, ready: list[str], day_start: datetime, now: datetime
    ) -> int:
        digest = hashlib.sha1("|".join(sorted(ready)).encode("utf-8")).hexdigest()[:12]
        # Aynı dosya kümesi aynı yerel günde bir kez talep edilir: iki süreç çift bildirmez.
        # Önceki deneme `failed` bittiyse yeniden denenebilsin diye sıradaki deneme kimliği
        # açılır; `delivered`/`claimed`/`uncertain` durumunda talep yine reddedilir.
        base_id = f"trigger:{trigger.name}:{digest}"
        event_id = base_id
        attempt = 1
        while not self.store.claim(event_id, day_start, "trigger", self.owner, now):
            if self.store.occurrence_state(event_id, day_start) != "failed":
                return 0
            attempt += 1
            event_id = f"{base_id}:{attempt}"
        routine = trigger.routine if self.runtime.settings.routines.enabled else None
        title, body, suggest = build_notice(
            trigger.name, trigger.folder, trigger.message, trigger.suggest, ready
        )
        # Yalnız sorulur; çalıştırma/yazma kullanıcının tıklamasıyla, normal turda olur.
        # Dosya adı kişisel olabilir: Telegram'a gitmez.
        outcome = await self.deliverer.deliver(
            event_id=event_id,
            scheduled_for=day_start,
            kind="trigger",
            title=title,
            body=body,
            private=True,
            routine=routine or "",
            suggest=suggest,
            local_only=True,
        )
        self._finish(event_id, day_start, outcome, "completed")
        return int(outcome.delivered)

    def _due_routines(self, now: datetime) -> list[tuple[str, datetime]]:
        """Zamanı gelen zamanlı rutinler; bozuk dosya zamanlayıcıyı durdurmaz."""
        settings = self.runtime.settings
        if not settings.routines.enabled:
            return []
        try:
            routines = RoutineStore(settings.state_dir / "routines.json").load()
        except RoutinesError:
            if not self._routines_error_reported:
                self._routines_error_reported = True
                self.runtime.audit.emit("routines_unavailable", reason="corrupt_or_unreadable")
            return []
        self._routines_error_reported = False
        grace = timedelta(hours=self.config.briefing_grace_hours)
        due: list[tuple[str, datetime]] = []
        for routine in routines:
            if routine.schedule is None:
                continue
            scheduled = briefing_occurrence(
                routine.schedule.days,
                time.fromisoformat(routine.schedule.at),
                now,
                self.tz,
                grace,
            )
            if scheduled is not None:
                due.append((routine.name, scheduled))
        return due

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
            body="Artemis kapalıyken zamanı geçti:\n" + "\n".join(lines),
            private=any(item.created_private for item in missed),
        )
        self._finish(event_id, now, outcome, "completed")
        return int(outcome.delivered)

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
