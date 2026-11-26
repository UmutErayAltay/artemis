"""Olay tabanlı tetikleyiciler (app_opened, calendar_before, system_resume) ve öneri zincirleri.

Tüm değerlendirmeler deterministik ve LLM'sizdir (ADR 0086). Bildirim metinleri yalnız
sabit şablonlardan (`fill`) üretilir, denetim ve gizlilik için temizlenir (`sanitize_name`),
ve her zaman yalnız yerel panele (`local_only=True`) teslim edilir; Telegram'a gitmez.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from artemis.config.triggers import SuggestionChainConfig, TriggerConfig
from artemis.proactive.triggers import fill, sanitize_name, suggest_uses_external
from artemis.tools.calendar_mail import (
    ICS_ENV,
    ICS_TARGET,
    Event,
    SourceError,
    fetch_ics,
    parse_events,
    resolve_secret,
)

if TYPE_CHECKING:
    from artemis.channels.runtime import Runtime
    from artemis.proactive.store import ProactiveStore

ActiveAppProvider = Callable[[], str | None]
CalendarEventsProvider = Callable[[datetime, datetime], Sequence[Event]]
ResumeProbe = Callable[[], bool]

# Takvim ICS yenileme aralığı: zamanlayıcı 30 sn'de bir dönse de sunucuya en az 5 dk'da bir gidilir.
CALENDAR_CACHE_SECONDS = 300.0
# Monotonik saat ile duvar saati arasındaki bu kadar fark sistem uykusundan dönüş sayılır.
SLEEP_GAP_SECONDS = 120.0
_UNSET = object()


def _unbiased_monotonic() -> float:
    """Uyku süresini dışlayan monotonik saniye sayacı (Windows QueryUnbiasedInterruptTime)."""
    if sys.platform == "win32":
        import ctypes

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            unbiased = ctypes.c_uint64(0)
            if kernel32.QueryUnbiasedInterruptTime(ctypes.byref(unbiased)):
                return float(unbiased.value) / 10_000_000.0
        except (OSError, AttributeError, ctypes.ArgumentError):
            pass
    return time.monotonic()


@dataclass(frozen=True)
class TriggerMatch:
    """Eşleşen olay tetikleyicisi bildirimi."""

    event_key: str
    title: str
    body: str
    suggest: str
    routine: str
    suggest_tainted: bool = False


@dataclass(frozen=True)
class ChainMatch:
    """Koşulları sağlanmış rutin öneri zinciri bildirimi."""

    chain_name: str
    event_key: str
    title: str
    body: str
    suggest: str
    routine: str
    suggest_tainted: bool = False


def normalize_app_name(value: str) -> str:
    """Uygulama ya da süreç adını karşılaştırma için sadeleştirir (`Code.exe` -> `code`)."""
    cleaned = value.strip().replace("/", "\\")
    base = PureWindowsPath(cleaned).name.lower() if cleaned else ""
    return base[:-4] if base.endswith(".exe") else base


def default_active_app() -> str | None:
    """Windows'ta ön plandaki uygulamanın dosya adını döndürür; diğer platformlarda None."""
    if sys.platform != "win32":
        return None
    from artemis.computer.windows import foreground_process

    try:
        exe, _pid = foreground_process()
    except (OSError, RuntimeError):
        return None
    if not exe:
        return None
    return PureWindowsPath(exe).name.lower()


def _runtime_settings(runtime: Any) -> Any:
    context = getattr(runtime, "context", None)
    if context is not None and hasattr(context, "settings"):
        return context.settings
    return runtime.settings


class CachedCalendarProvider:
    """ICS takvim olaylarını en az 300 sn bellek önbelleğiyle sağlayan bağdaştırıcı."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        ttl_seconds: float = CALENDAR_CACHE_SECONDS,
        secret_resolver: Callable[[str, str], str | None] | None = None,
        fetcher: Callable[[str], bytes] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._runtime = runtime
        self._ttl = max(CALENDAR_CACHE_SECONDS, ttl_seconds)
        self._resolve_secret = secret_resolver or resolve_secret
        self._fetch_ics = fetcher or fetch_ics
        self._mono = monotonic_clock or time.monotonic
        self._fetched_at: float | None = None
        self._cached_until: datetime | None = None
        self._cached_events: list[Event] = []

    def __call__(self, window_start: datetime, window_end: datetime) -> list[Event]:
        settings = _runtime_settings(self._runtime)
        if not settings.calendar.enabled:
            return []
        now_mono = self._mono()
        if (
            self._fetched_at is not None
            and (now_mono - self._fetched_at) < self._ttl
            and self._cached_until is not None
            and window_end <= self._cached_until
        ):
            return list(self._cached_events)
        url = self._resolve_secret(ICS_ENV, ICS_TARGET)
        if not url:
            self._fetched_at = now_mono
            self._cached_until = window_end
            self._cached_events = []
            return []
        fetch_end = max(window_end, window_start + timedelta(hours=24))
        try:
            data = self._fetch_ics(url)
            tz = ZoneInfo(settings.proactive.timezone)
            events, _warnings = parse_events(data, tz, window_start, fetch_end)
        except (SourceError, OSError, ValueError):
            # Çevrimdışı veya geçici ağ hatasında zamanlayıcı düşmez; mevcut önbellek korunur
            # ve ilk hatada bile TTL süresince yeniden ağ isteği atılmaz.
            self._fetched_at = now_mono
            self._cached_until = (
                max(self._cached_until, fetch_end)
                if self._cached_until is not None
                else fetch_end
            )
            return list(self._cached_events)
        self._fetched_at = now_mono
        self._cached_until = fetch_end
        self._cached_events = events
        return list(events)


class ResumeDetector:
    """Sistem uykudan uyanma sinyali (`WM_POWERBROADCAST`) ve saat boşluğu algılayıcısı."""

    def __init__(
        self,
        *,
        resume_probe: ResumeProbe | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        enable_gap_check: bool = True,
        sleep_gap_seconds: float = SLEEP_GAP_SECONDS,
    ) -> None:
        self._signal = threading.Event()
        self._resume_probe = resume_probe
        self._mono = monotonic_clock or _unbiased_monotonic
        self._enable_gap_check = enable_gap_check
        self._sleep_gap = sleep_gap_seconds
        self._last_wall: datetime | None = None
        self._last_mono: float | None = None

    def notify_resume(self) -> None:
        """İşletim sistemi uyanma olayını iş parçacığı güvenli biçimde işaretler."""
        self._signal.set()

    def poll(self, now: datetime) -> bool:
        """Bu taramada sistem uyanması olup olmadığını döndürür (tek seferlik tüketilir)."""
        resumed = False
        if self._signal.is_set():
            self._signal.clear()
            resumed = True
        if self._resume_probe is not None and self._resume_probe():
            resumed = True
        # TODO(windows-verify #48): Windows uykusu sonrası QueryUnbiasedInterruptTime
        # davranışını gerçek cihazda doğrula.
        current_mono = self._mono()
        if self._enable_gap_check and self._last_wall is not None and self._last_mono is not None:
            wall_delta = (now - self._last_wall).total_seconds()
            mono_delta = current_mono - self._last_mono
            if (wall_delta - mono_delta) >= self._sleep_gap:
                resumed = True
        self._last_wall = now
        self._last_mono = current_mono
        return resumed


class EventTriggerTracker:
    """Olay tabanlı tetikleyicilerin taban çizgisi, kenar ve bekleme durumu."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        active_app_getter: ActiveAppProvider | None = None,
        calendar_fetcher: CalendarEventsProvider | None = None,
        resume_probe: ResumeProbe | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        clock_injected: bool = False,
    ) -> None:
        self._runtime = runtime
        self._active_app_getter: ActiveAppProvider = (
            active_app_getter if active_app_getter is not None else default_active_app
        )
        self._calendar_fetcher: CalendarEventsProvider = (
            calendar_fetcher
            if calendar_fetcher is not None
            else CachedCalendarProvider(runtime, monotonic_clock=monotonic_clock)
        )
        # Testlerde yalnız sahte duvar saati ilerletildiğinde yanlış uyku sıçraması üretme;
        # monotonik saat de verilmişse veya gerçek saat kullanılıyorsa boşluk denetimi açıktır.
        enable_gap = (not clock_injected) or (monotonic_clock is not None)
        self._resume = ResumeDetector(
            resume_probe=resume_probe,
            monotonic_clock=monotonic_clock,
            enable_gap_check=enable_gap,
        )
        self._app_baseline: dict[str, bool] = {}
        self._app_edge_key: dict[str, str] = {}
        self._last_fired: dict[str, datetime] = {}
        self._calendar_seen: set[tuple[str, str]] = set()
        self._chain_last_fired: dict[str, datetime] = {}
        self._tick_resumed = False
        self._tick_active_app: str | None | object = _UNSET

    def notify_resume(self) -> None:
        """Harici sistem uyanma sinyalini (`WM_POWERBROADCAST`) kaydeder."""
        self._resume.notify_resume()

    def begin_tick(self, now: datetime) -> None:
        """Tarama başında tek seferlik uyanma yoklaması ve ön plan önbelleği sıfırlaması."""
        self._tick_resumed = self._resume.poll(now)
        self._tick_active_app = _UNSET

    def current_active_app(self) -> str | None:
        """Bu tarama içindeki ön plan uygulamasını döndürür (gerektiğinde bir kez sorgular)."""
        if self._tick_active_app is _UNSET:
            self._tick_active_app = self._active_app_getter()
        value = self._tick_active_app
        return value if isinstance(value, str) else None

    async def poll(
        self, trigger: TriggerConfig, now: datetime, store: ProactiveStore
    ) -> TriggerMatch | None:
        """Tetikleyici türüne göre eşleşme arar; yoksa None döner."""
        if trigger.kind == "app_opened":
            return self._poll_app_opened(trigger, now, store)
        if trigger.kind == "calendar_before":
            return await self._poll_calendar_before(trigger, now)
        if trigger.kind == "system_resume":
            return self._poll_system_resume(trigger, now, store)
        return None

    def mark_fired(self, trigger: TriggerConfig, match: TriggerMatch, now: datetime) -> None:
        """Başarılı teslimden sonra bekleme süresini ve yineleme kilidini günceller."""
        self._last_fired[trigger.name] = now
        if trigger.kind == "app_opened":
            self._app_baseline[trigger.name] = True
            self._app_edge_key.pop(trigger.name, None)
        elif trigger.kind == "calendar_before":
            self._calendar_seen.add((trigger.name, match.event_key))

    def _cooldown_active(
        self, trigger: TriggerConfig, now: datetime, store: ProactiveStore
    ) -> bool:
        last = self._last_fired.get(trigger.name)
        if last is None:
            last = store.last_delivered(f"trigger:{trigger.name}:")
        if last is None:
            return False
        return (now - last) < timedelta(minutes=trigger.cooldown_minutes)

    def _poll_app_opened(
        self, trigger: TriggerConfig, now: datetime, store: ProactiveStore
    ) -> TriggerMatch | None:
        active = self.current_active_app()
        target = normalize_app_name(trigger.app)
        matches = bool(active) and normalize_app_name(active or "") == target
        if trigger.name not in self._app_baseline:
            # Açılışta zaten açık olan uygulama için bildirim üretilmez.
            self._app_baseline[trigger.name] = matches
            return None
        prev = self._app_baseline[trigger.name]
        if not matches:
            self._app_baseline[trigger.name] = False
            self._app_edge_key.pop(trigger.name, None)
            return None
        if prev:
            return None
        if self._cooldown_active(trigger, now, store):
            self._app_baseline[trigger.name] = True
            self._app_edge_key.pop(trigger.name, None)
            return None
        app_display = (
            PureWindowsPath(active.strip().replace("/", "\\")).name
            if active
            else trigger.app
        )
        clean_app = sanitize_name(app_display, 120)
        body = sanitize_name(fill(trigger.message, name=trigger.name, app=clean_app), 600)
        suggest = (
            sanitize_name(fill(trigger.suggest, name=trigger.name, app=clean_app), 500)
            if trigger.suggest
            else ""
        )
        event_key = self._app_edge_key.setdefault(
            trigger.name,
            f"app:{target}:{now.astimezone(UTC).isoformat(timespec='seconds')}",
        )
        return TriggerMatch(
            event_key=event_key,
            title=f"Tetikleyici · {trigger.name}",
            body=body,
            suggest=suggest,
            routine=trigger.routine or "",
        )

    async def _poll_calendar_before(
        self, trigger: TriggerConfig, now: datetime
    ) -> TriggerMatch | None:
        window_end = now + timedelta(minutes=trigger.minutes_before)
        events = await asyncio.to_thread(self._calendar_fetcher, now, window_end)
        needle = trigger.title_filter.casefold()
        for event in events:
            if not isinstance(event.start, datetime):
                continue
            start_utc = event.start.astimezone(UTC)
            if not (now <= start_utc <= window_end):
                continue
            if needle and needle not in event.title.casefold():
                continue
            clean_title = sanitize_name(event.title, 120)
            event_key = f"cal:{start_utc.isoformat()}:{clean_title}"
            if (trigger.name, event_key) in self._calendar_seen:
                continue
            seconds_left = max(0, int((start_utc - now).total_seconds()))
            minutes_left = (seconds_left + 59) // 60 if seconds_left > 0 else 0
            clean_loc = sanitize_name(event.location, 120)
            body = sanitize_name(
                fill(
                    trigger.message,
                    name=trigger.name,
                    title=clean_title,
                    minutes=str(minutes_left),
                    location=clean_loc,
                ),
                600,
            )
            suggest = (
                sanitize_name(
                    fill(
                        trigger.suggest,
                        name=trigger.name,
                        title=clean_title,
                        minutes=str(minutes_left),
                        location=clean_loc,
                    ),
                    500,
                )
                if trigger.suggest
                else ""
            )
            return TriggerMatch(
                event_key=event_key,
                title=f"Tetikleyici · {trigger.name}",
                body=body,
                suggest=suggest,
                routine=trigger.routine or "",
                suggest_tainted=suggest_uses_external(trigger.suggest),
            )
        return None

    def _poll_system_resume(
        self, trigger: TriggerConfig, now: datetime, store: ProactiveStore
    ) -> TriggerMatch | None:
        if not self._tick_resumed:
            return None
        if self._cooldown_active(trigger, now, store):
            return None
        body = sanitize_name(fill(trigger.message, name=trigger.name), 600)
        suggest = (
            sanitize_name(fill(trigger.suggest, name=trigger.name), 500)
            if trigger.suggest
            else ""
        )
        return TriggerMatch(
            event_key=f"resume:{now.astimezone(UTC).isoformat(timespec='seconds')}",
            title=f"Tetikleyici · {trigger.name}",
            body=body,
            suggest=suggest,
            routine=trigger.routine or "",
        )

    def evaluate_chains(
        self,
        *,
        event_kind: Literal["trigger_fired", "routine_completed"],
        source_name: str,
        now: datetime,
        tz: ZoneInfo,
        store: ProactiveStore,
        routine_success: bool = True,
    ) -> list[ChainMatch]:
        """Tetikleyici veya rutin tamamlanması sonrası koşullu öneri zincirlerini değerlendirir."""
        settings = _runtime_settings(self._runtime)
        triggers = settings.triggers
        chains = getattr(triggers, "chains", ())
        if not triggers.enabled or not chains:
            return []
        if event_kind == "routine_completed" and not routine_success:
            return []
        # TODO(windows-verify #48): rutin zinciri tetiklendiğinde ön planda Artemis penceresi
        # olabileceğinden active_app eşleşmesini Windows'ta doğrula.
        active_app = self._active_app_getter() if self._tick_active_app is _UNSET else (
            self._tick_active_app if isinstance(self._tick_active_app, str) else None
        )
        return evaluate_chains(
            chains,
            event_kind=event_kind,
            source_name=source_name,
            now=now,
            tz=tz,
            work_profile=settings.work_profile,
            active_app=active_app,
            routines_enabled=settings.routines.enabled,
            last_fired=dict(self._chain_last_fired),
            store=store,
        )

    def mark_chain_fired(self, chain_name: str, now: datetime) -> None:
        """Başarılı teslimden sonra zincir bekleme süresini günceller."""
        self._chain_last_fired[chain_name] = now


def _in_time_range(local_time: dt_time, spec: str) -> bool:
    if not spec:
        return True
    start_s, _, end_s = spec.partition("-")
    start_t = dt_time.fromisoformat(start_s)
    end_t = dt_time.fromisoformat(end_s)
    if start_t <= end_t:
        return start_t <= local_time <= end_t
    return local_time >= start_t or local_time <= end_t


def evaluate_chains(
    chains: Sequence[SuggestionChainConfig],
    *,
    event_kind: Literal["trigger_fired", "routine_completed"],
    source_name: str,
    now: datetime,
    tz: ZoneInfo,
    work_profile: str,
    active_app: str | None,
    routines_enabled: bool,
    last_fired: dict[str, datetime],
    store: ProactiveStore | None = None,
) -> list[ChainMatch]:
    """Deterministik, LLM'siz koşullu öneri zincirlerini değerlendirir."""
    matches: list[ChainMatch] = []
    local_t = now.astimezone(tz).time().replace(second=0, microsecond=0)
    clean_source = sanitize_name(source_name, 80)
    clean_app = sanitize_name(active_app or "", 120)
    for chain in chains:
        if not chain.enabled:
            continue
        if event_kind == "routine_completed":
            if (
                not chain.after_routine
                or chain.after_trigger
                or chain.after_routine.casefold() != source_name.casefold()
            ):
                continue
        else:
            if (
                not chain.after_trigger
                or chain.after_routine
                or chain.after_trigger.casefold() != source_name.casefold()
            ):
                continue
        if chain.work_profiles and work_profile not in chain.work_profiles:
            continue
        if chain.active_app and (
            not active_app
            or normalize_app_name(active_app) != normalize_app_name(chain.active_app)
        ):
            continue
        if not _in_time_range(local_t, chain.time_range):
            continue
        last = last_fired.get(chain.name)
        if last is None and store is not None:
            last = store.last_delivered(f"chain:{chain.name}:")
        if last is not None and (now - last) < timedelta(minutes=chain.cooldown_minutes):
            continue
        routine = chain.routine if (chain.routine and routines_enabled) else ""
        suggest = (
            sanitize_name(
                fill(chain.suggest, name=chain.name, source=clean_source, app=clean_app), 500
            )
            if chain.suggest
            else ""
        )
        body = sanitize_name(
            fill(chain.message, name=chain.name, source=clean_source, app=clean_app), 600
        )
        last_fired[chain.name] = now
        matches.append(
            ChainMatch(
                chain_name=chain.name,
                event_key=f"{chain.name}:{now.astimezone(UTC).isoformat(timespec='seconds')}",
                title=f"Öneri · {chain.name}",
                body=body,
                suggest=suggest,
                routine=routine,
            )
        )
    return matches
