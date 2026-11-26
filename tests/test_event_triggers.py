"""M3B Olay tetikleyicileri ve koşullu rutin öneri zincirleri testleri (ADR 0086).

Gerçek OS penceresi, ağ veya LLM çağrısı yapılmaz: tüm sağlayıcılar enjekte edilir.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import ValidationError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from _wait import wait_until
from PySide6.QtWidgets import QApplication, QDialog

from artemis.brain.fake import ScriptedProvider
from artemis.channels.desktop import power_events
from artemis.channels.desktop.chain_dialog import ChainDialog, validate_chain
from artemis.channels.desktop.trigger_dialog import TriggerDialog, validate_trigger
from artemis.channels.desktop.triggers_section import TriggersSection
from artemis.channels.desktop.widgets import HelpTip
from artemis.channels.runtime import Runtime, create_runtime
from artemis.config import settings as settings_mod
from artemis.config.settings import (
    CalendarConfig,
    ProactiveConfig,
    RoutinesConfig,
    Settings,
    SuggestionChainConfig,
    TriggerConfig,
    TriggersConfig,
    load_settings,
)
from artemis.config.triggers import MAX_CHAINS
from artemis.proactive.delivery import Deliverer
from artemis.proactive.event_triggers import CachedCalendarProvider, evaluate_chains
from artemis.proactive.routines import Routine, RoutineStep, RoutineStore
from artemis.proactive.scheduler import ProactiveScheduler
from artemis.safety.gate import SafetyGate
from artemis.tools.calendar_mail import Event, SourceError
from artemis.tools.plugins.routines import RoutineNameArguments
from artemis.tools.registry import discover_tools

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeMono:
    def __init__(self, start: float = 100.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


class FakePanel:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, str]] = []

    def __call__(
        self, title: str, body: str, routine: str, suggest: str, day: str = "",
        _tainted: bool = False,
    ) -> bool:
        self.items.append((title, body, routine, suggest))
        return True


def _make_runtime(
    tmp_path: Path,
    *,
    items: tuple[TriggerConfig, ...] = (),
    chains: tuple[SuggestionChainConfig, ...] = (),
    triggers_enabled: bool = True,
    routines_enabled: bool = True,
    calendar_enabled: bool = False,
    work_profile: str = "work",
    max_events_per_day: int = 20,
    approve: Any = None,
) -> Runtime:
    workspace = tmp_path / "desktop"
    workspace.mkdir(parents=True, exist_ok=True)
    cfg = Settings(
        workspace=workspace,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        work_profile=work_profile,  # type: ignore[arg-type]
        proactive=ProactiveConfig(
            enabled=True, timezone="UTC", max_events_per_day=max_events_per_day
        ),
        triggers=TriggersConfig(enabled=triggers_enabled, items=items, chains=chains),
        routines=RoutinesConfig(enabled=routines_enabled),
        calendar=CalendarConfig(enabled=calendar_enabled),
    )
    return create_runtime(
        tmp_path / "unused.yaml",
        approve or (lambda _name, _args: False),
        provider=ScriptedProvider([]),
        settings=cfg,
    )


def _audit_records(runtime: Runtime) -> list[dict[str, Any]]:
    if not runtime.audit.path.exists():
        return []
    lines = runtime.audit.path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


# 1. Config & güvenlik değişmezleri -------------------------------------------------------------


def test_trigger_config_reexports_and_validation(tmp_path: Path) -> None:
    assert settings_mod.TriggerConfig is TriggerConfig
    assert settings_mod.TriggersConfig is TriggersConfig
    assert settings_mod.SuggestionChainConfig is SuggestionChainConfig

    # Klasik folder tetikleyici varsayılanı geriye dönük uyumlu kalır.
    folder_t = TriggerConfig(name="dl", folder=str(tmp_path))
    assert folder_t.kind == "folder"
    assert folder_t.message == "Yeni dosya geldi: {file}"

    app_t = TriggerConfig(name="ide", kind="app_opened", app="code.exe")
    assert app_t.message == "Uygulama açıldı: {app}"

    cal_t = TriggerConfig(name="meet", kind="calendar_before", minutes_before=15)
    assert cal_t.message == "Yaklaşan toplantı: {title} ({minutes} dk)"

    res_t = TriggerConfig(name="wake", kind="system_resume")
    assert res_t.message == "Sistem uykudan uyandı"

    with pytest.raises(ValidationError):
        TriggerConfig(name="bad_app", kind="app_opened", app="")
    with pytest.raises(ValidationError):
        TriggerConfig(name="bad_folder", kind="folder", folder="")
    with pytest.raises(ValidationError):
        TriggerConfig(name="two_btn", kind="system_resume", suggest="a", routine="r")
    with pytest.raises(ValidationError):
        TriggerConfig(name="empty_msg", kind="system_resume", message="")

    # Öneri zinciri doğrulamaları
    with pytest.raises(ValidationError):
        SuggestionChainConfig(name="no_src")
    with pytest.raises(ValidationError):
        SuggestionChainConfig(name="two_btn", after_routine="a", suggest="x", routine="y")
    with pytest.raises(ValidationError):
        SuggestionChainConfig(name="bad_time", after_routine="a", time_range="25:00-10:00")
    with pytest.raises(ValidationError):
        SuggestionChainConfig(name="self_loop", after_routine="sabah", routine="SABAH")
    with pytest.raises(ValidationError):
        TriggersConfig(
            chains=tuple(
                SuggestionChainConfig(name=f"c{i}", after_trigger="t")
                for i in range(MAX_CHAINS + 1)
            )
        )


def test_zero_llm_trigger_or_chain_tools_registered() -> None:
    registry = discover_tools()
    names = [defn.name for defn in registry.definitions()]
    assert not any("trigger" in name or "chain" in name for name in names), names


# 2. app_opened tetikleyicisi --------------------------------------------------------------------


async def test_app_opened_baselines_first_tick_and_is_edge_triggered_with_cooldown(
    tmp_path: Path,
) -> None:
    current_app: list[str | None] = ["Code.exe"]
    clock = FakeClock()
    panel = FakePanel()
    sent_tg: list[str] = []

    class SpyTg:
        def send_message(self, *_a: Any, **_kw: Any) -> None:
            sent_tg.append("sent")

    trig = TriggerConfig(
        name="kod-acildi",
        kind="app_opened",
        app="code.exe",
        cooldown_minutes=10,
        routine="kod-hazirla",
    )
    runtime = _make_runtime(tmp_path, items=(trig,))
    deliverer = Deliverer(
        runtime.settings, runtime.audit, routine_panel=panel, toast=None, telegram=SpyTg()  # type: ignore[arg-type]
    )
    scheduler = ProactiveScheduler(
        runtime,
        runtime.context.services["proactive_store"],
        deliverer,
        clock=clock,
        active_app_getter=lambda: current_app[0],
    )

    # Açılışta Code.exe zaten açıkken bildirim üretilmez.
    assert await scheduler.tick() == 0
    assert panel.items == []

    # Başka uygulamaya geçip tekrar Code.exe'ye gelince tek bildirim üretilir.
    clock.advance(seconds=30)
    current_app[0] = "chrome.exe"
    assert await scheduler.tick() == 0

    clock.advance(seconds=30)
    current_app[0] = "C:\\Apps\\Code.exe"
    assert await scheduler.tick() == 1
    assert len(panel.items) == 1
    assert panel.items[0][0] == "Tetikleyici · kod-acildi"
    assert panel.items[0][1] == "Uygulama açıldı: Code.exe"
    assert panel.items[0][2] == "kod-hazirla"
    assert panel.items[0][3] == ""

    # Açık kalmaya devam ederse tekrar ateşlenmez.
    clock.advance(seconds=30)
    assert await scheduler.tick() == 0

    # Bekleme süresi (10 dk) dolmadan kapatıp açınca da ateşlenmez.
    current_app[0] = "notepad.exe"
    clock.advance(minutes=2)
    await scheduler.tick()
    current_app[0] = "code.exe"
    clock.advance(minutes=2)
    assert await scheduler.tick() == 0

    # Bekleme süresi dolduktan sonra yeni açılış tekrar bildirir.
    current_app[0] = "notepad.exe"
    clock.advance(minutes=11)
    await scheduler.tick()
    current_app[0] = "code.exe"
    clock.advance(seconds=30)
    assert await scheduler.tick() == 1
    assert len(panel.items) == 2

    # Telegram'a hiçbir zaman gitmez.
    assert sent_tg == []
    deliveries = [r for r in _audit_records(runtime) if r["event"] == "proactive_delivery"]
    assert all("telegram" not in rec["targets"] for rec in deliveries)


# 3. calendar_before tetikleyicisi ve önbellek ---------------------------------------------------


async def test_calendar_before_filters_sanitizes_and_deduplicates(tmp_path: Path) -> None:
    clock = FakeClock()
    panel = FakePanel()
    events = [
        Event("Gün boyu", NOW.date(), NOW.date() + timedelta(days=1)),
        Event("Uzak Toplantı", NOW + timedelta(minutes=45), NOW + timedelta(minutes=75)),
        Event("Diğer İş", NOW + timedelta(minutes=5), NOW + timedelta(minutes=35)),
        Event(
            "Haftalık\x1b[31m Sync",
            NOW + timedelta(minutes=8),
            NOW + timedelta(minutes=38),
            location="Oda\u202e 1",
        ),
    ]
    trig = TriggerConfig(
        name="sync-uyari",
        kind="calendar_before",
        minutes_before=10,
        title_filter="sync",
        message="Yaklaşan: {title} ({minutes} dk) @ {location}",
        suggest="{title} notlarını hazırla",
    )
    runtime = _make_runtime(tmp_path, items=(trig,))
    tainted_flags: list[bool] = []

    def panel_ex(
        title: str, body: str, routine: str, suggest: str, day: str, tainted: bool
    ) -> bool:
        tainted_flags.append(tainted)
        return panel(title, body, routine, suggest, day)

    deliverer = Deliverer(
        runtime.settings,
        runtime.audit,
        panel=lambda title, body: panel(title, body, "", ""),
        routine_panel=panel_ex,
        toast=None,
    )
    scheduler = ProactiveScheduler(
        runtime,
        runtime.context.services["proactive_store"],
        deliverer,
        clock=clock,
        calendar_fetcher=lambda _s, _e: events,
    )

    assert await scheduler.tick() == 1
    assert len(panel.items) == 1
    title, body, routine, suggest = panel.items[0]
    assert title == "Tetikleyici · sync-uyari"
    assert "\x1b" not in body and "\u202e" not in body
    assert body == "Yaklaşan: Haftalık[31m Sync (8 dk) @ Oda 1"
    assert routine == ""
    assert suggest == "Haftalık[31m Sync notlarını hazırla"
    # {title} dış alandır: öneri leke bayrağıyla teslim edilir (#39).
    assert tainted_flags == [True]

    # Sonraki taramada veya yeniden başlatmada aynı toplantı tekrar bildirilmez.
    clock.advance(seconds=60)
    assert await scheduler.tick() == 0
    restarted = ProactiveScheduler(
        runtime,
        runtime.context.services["proactive_store"],
        deliverer,
        clock=clock,
        owner="b",
        calendar_fetcher=lambda _s, _e: events,
    )
    assert await restarted.tick() == 0
    assert len(panel.items) == 1


def test_cached_calendar_provider_respects_ttl_and_handles_offline(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, calendar_enabled=True)
    mono = FakeMono(100.0)
    fetches = 0
    ics_bytes = (
        b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u1\r\n"
        b"DTSTART:20261123T090500Z\r\nDTEND:20261123T093500Z\r\n"
        b"SUMMARY:Mimari\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    def fake_fetch(_url: str) -> bytes:
        nonlocal fetches
        fetches += 1
        if fetches == 2:
            raise SourceError("çevrimdışı")
        return ics_bytes

    provider = CachedCalendarProvider(
        runtime,
        secret_resolver=lambda _e, _t: "https://example.invalid/cal.ics",
        fetcher=fake_fetch,
        monotonic_clock=mono,
    )
    res1 = provider(NOW, NOW + timedelta(minutes=15))
    assert len(res1) == 1 and res1[0].title == "Mimari" and fetches == 1

    # 300 saniye dolmadan önbellekten döner; ağa gitmez.
    mono.value = 250.0
    res2 = provider(NOW, NOW + timedelta(minutes=15))
    assert len(res2) == 1 and fetches == 1

    # 300 saniye sonrası hata alsa da düşmez.
    mono.value = 450.0
    res3 = provider(NOW, NOW + timedelta(minutes=15))
    assert len(res3) == 1 and fetches == 2


# 4. system_resume tetikleyicisi ve Windows WM_POWERBROADCAST -------------------------------------


def test_power_events_detects_resume_messages() -> None:
    assert power_events.is_resume_message(power_events.WM_POWERBROADCAST, 0x0012)
    assert power_events.is_resume_message(power_events.WM_POWERBROADCAST, 0x0007)
    assert not power_events.is_resume_message(power_events.WM_POWERBROADCAST, 0x0004)
    assert not power_events.is_resume_message(0x0001, 0x0012)

    msg = wintypes.MSG()
    msg.message = power_events.WM_POWERBROADCAST
    msg.wParam = power_events.PBT_APMRESUMEAUTOMATIC
    expected = sys.platform == "win32"
    got = power_events.is_resume_native_event(b"windows_generic_MSG", ctypes.addressof(msg))
    assert got is expected
    assert power_events.is_resume_native_event(b"other", ctypes.addressof(msg)) is False


async def test_system_resume_signal_sleep_gap_and_cooldown(tmp_path: Path) -> None:
    clock = FakeClock()
    mono = FakeMono(100.0)
    panel = FakePanel()
    trig = TriggerConfig(
        name="uyanis",
        kind="system_resume",
        cooldown_minutes=30,
        suggest="Günlük brifingi hazırla",
    )
    runtime = _make_runtime(tmp_path, items=(trig,))
    deliverer = Deliverer(
        runtime.settings,
        runtime.audit,
        panel=lambda title, body: panel(title, body, "", ""),
        routine_panel=panel,
        toast=None,
    )
    scheduler = ProactiveScheduler(
        runtime,
        runtime.context.services["proactive_store"],
        deliverer,
        clock=clock,
        monotonic_clock=mono,
    )

    assert await scheduler.tick() == 0

    # WM_POWERBROADCAST uyanma sinyali bir kez bildirir, cooldown içinde ikinciyi engeller.
    scheduler.notify_system_resume()
    clock.advance(seconds=10)
    mono.value += 10.0
    assert await scheduler.tick() == 1
    assert len(panel.items) == 1 and panel.items[0][3] == "Günlük brifingi hazırla"

    scheduler.notify_system_resume()
    clock.advance(minutes=10)
    mono.value += 600.0
    assert await scheduler.tick() == 0

    # Cooldown sonrası duvar saati - monotonik saat boşluğu (>= 120s) uyanma olarak algılanır.
    clock.advance(minutes=35)
    mono.value += 5.0
    assert await scheduler.tick() == 1
    assert len(panel.items) == 2


# 5. Koşullu rutin öneri zincirleri ve tıklamayla SafetyGate akışı -------------------------------


async def test_conditional_chains_on_trigger_and_routine_completion(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 11, 23, 10, 0, tzinfo=UTC))
    panel = FakePanel()
    trig = TriggerConfig(name="uyanis", kind="system_resume", cooldown_minutes=5)
    c_trig = SuggestionChainConfig(
        name="sabah-zincir",
        after_trigger="uyanis",
        work_profiles=("work",),
        active_app="code.exe",
        time_range="09:00-18:00",
        cooldown_minutes=60,
        message="{source} sonrası rutin önerisi",
        routine="kod-ac",
    )
    c_routine = SuggestionChainConfig(
        name="sonraki-adim",
        after_routine="kod-ac",
        work_profiles=("work",),
        cooldown_minutes=30,
        message="{source} tamamlandı; notları aç?",
        suggest="Günlük notları özetle",
    )
    c_wrong_profile = SuggestionChainConfig(
        name="oyun-zincir",
        after_routine="kod-ac",
        work_profiles=("game",),
        message="Oyun",
        suggest="x",
    )
    runtime = _make_runtime(
        tmp_path, items=(trig,), chains=(c_trig, c_routine, c_wrong_profile)
    )
    deliverer = Deliverer(
        runtime.settings,
        runtime.audit,
        panel=lambda title, body: panel(title, body, "", ""),
        routine_panel=panel,
        toast=None,
    )
    scheduler = ProactiveScheduler(
        runtime,
        runtime.context.services["proactive_store"],
        deliverer,
        clock=clock,
        active_app_getter=lambda: "Code.exe",
    )

    scheduler.notify_system_resume()
    assert await scheduler.tick() == 2  # tetikleyici + c_trig önerisi
    assert panel.items[0][0] == "Tetikleyici · uyanis"
    assert panel.items[1][0] == "Öneri · sabah-zincir"
    assert panel.items[1][2] == "kod-ac" and panel.items[1][3] == ""

    # Başarısız rutin sonrası zincir önerisi üretilmez.
    assert await scheduler.notify_routine_completed("kod-ac", success=False) == 0

    # Başarılı rutin sonrası yalnız eşleşen koşuldaki öneri üretilir; oyun profili atlanır.
    assert await scheduler.notify_routine_completed("kod-ac", success=True) == 1
    assert panel.items[2][0] == "Öneri · sonraki-adim"
    assert panel.items[2][2] == "" and panel.items[2][3] == "Günlük notları özetle"

    # Cooldown içinde tekrar ateşlenmez; overnight saat aralığı da deterministiktir.
    assert await scheduler.notify_routine_completed("kod-ac", success=True) == 0
    overnight = SuggestionChainConfig(
        name="gece", after_routine="x", time_range="22:00-06:00", suggest="uyu"
    )
    fired: dict[str, datetime] = {}
    assert (
        evaluate_chains(
            [overnight],
            event_kind="routine_completed",
            source_name="x",
            now=datetime(2026, 11, 23, 23, 30, tzinfo=UTC),
            tz=ZoneInfo("UTC"),
            work_profile="work",
            active_app=None,
            routines_enabled=True,
            last_fired=fired,
        )
        != []
    )


async def test_routine_from_trigger_never_runs_without_click_and_uses_safety_gate(
    tmp_path: Path,
) -> None:
    approvals: list[tuple[str, dict[str, Any]]] = []
    runtime = _make_runtime(
        tmp_path,
        items=(TriggerConfig(name="wake", kind="system_resume", routine="selam"),),
        approve=lambda name, args: approvals.append((name, args)) or False,
    )
    RoutineStore(runtime.settings.state_dir / "routines.json").save(
        [Routine(name="selam", steps=[RoutineStep(tool="system_info", arguments={})])]
    )
    panel = FakePanel()
    deliverer = Deliverer(
        runtime.settings,
        runtime.audit,
        panel=lambda title, body: panel(title, body, "", ""),
        routine_panel=panel,
        toast=None,
    )
    scheduler = ProactiveScheduler(
        runtime, runtime.context.services["proactive_store"], deliverer, clock=FakeClock()
    )
    scheduler.notify_system_resume()
    assert await scheduler.tick() == 1
    assert panel.items[0][2] == "selam"

    # Tıklamadan hiçbir araç çalışmadı.
    assert [r for r in _audit_records(runtime) if r["event"] == "routine_run_start"] == []

    # Kullanıcı tıkladığında CommandRunner -> SafetyGate üzerinden yürür.
    dispatched: list[str] = []
    orig_dispatch = SafetyGate.dispatch

    async def spy_dispatch(
        self: SafetyGate, call: Any, context: Any, state: Any, approve: Any
    ) -> Any:
        dispatched.append(call.name)
        return await orig_dispatch(self, call, context, state, approve)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(SafetyGate, "dispatch", spy_dispatch)
    try:
        res = await runtime.runner.dispatch_tool("routine_run", RoutineNameArguments(name="selam"))
    finally:
        monkeypatch.undo()
    assert res.success
    assert dispatched == ["routine_run", "system_info"]


# 6. UI doğrulama ve YAML kaydetme turu ----------------------------------------------------------


def test_ui_dialogs_and_triggers_section_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "workspace": "desktop",
                "state_dir": "state",
                "audit_path": "audit.jsonl",
                "primary_model": "first",
                "models": {
                    "first": {
                        "provider": "openrouter",
                        "model": "first-model",
                        "input_per_million": "0",
                        "output_per_million": "0",
                    }
                },
                "routines": {"enabled": True},
                "proactive": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )
    RoutineStore(tmp_path / "state" / "routines.json").save(
        [Routine(name="ise-basla", steps=[RoutineStep(tool="system_info", arguments={})])]
    )

    # TriggerDialog: app_opened, calendar_before, system_resume validation & dialog submit
    t_app, errs = validate_trigger(name="kod", kind="app_opened", app="code.exe")
    assert t_app is not None and not errs
    t_cal, errs = validate_trigger(
        name="toplanti", kind="calendar_before", minutes_before=15, title_filter="Sync"
    )
    assert t_cal is not None and not errs
    t_res, errs = validate_trigger(name="uyanis", kind="system_resume", cooldown_minutes=45)
    assert t_res is not None and not errs

    dlg = TriggerDialog(None, [], ["ise-basla"], "")
    dlg.name.setText("uyanis")
    idx = dlg.trigger_kind.findData("system_resume")
    dlg.trigger_kind.setCurrentIndex(idx)
    dlg.cooldown_minutes.setValue(20)
    dlg.routine_radio.setChecked(True)
    dlg.submit()
    assert dlg.result_trigger == TriggerConfig(
        name="uyanis", kind="system_resume", cooldown_minutes=20, routine="ise-basla"
    )

    # ChainDialog: help bubble bounds + submit
    c_item, c_errs = validate_chain(
        name="zincir1", after_trigger="uyanis", work_profiles=("work",), suggest="Özet"
    )
    assert c_item is not None and not c_errs
    cdlg = ChainDialog(None, [], ["ise-basla"], ["uyanis"], "")
    tips = cdlg.findChildren(HelpTip)
    assert 1 <= len(tips) <= 3 and all(len(tip.toolTip()) <= 260 for tip in tips)
    cdlg.name.setText("zincir1")
    cdlg.after_trigger.setCurrentIndex(cdlg.after_trigger.findData("uyanis"))
    cdlg.suggest_radio.setChecked(True)
    cdlg.submit()
    assert cdlg.result_chain is not None and cdlg.result_chain.name == "zincir1"

    # TriggersSection: save trigger + chain -> reload YAML
    section = TriggersSection(cfg_path)

    def fake_trigger_exec(self: TriggerDialog) -> QDialog.DialogCode:
        self.name.setText("uyanis")
        self.trigger_kind.setCurrentIndex(self.trigger_kind.findData("system_resume"))
        self.submit()
        return QDialog.DialogCode(self.result_trigger is not None)

    def fake_chain_exec(self: ChainDialog) -> QDialog.DialogCode:
        self.name.setText("zincir1")
        self.after_trigger.setCurrentIndex(self.after_trigger.findData("uyanis"))
        self.suggest_radio.setChecked(True)
        self.submit()
        return QDialog.DialogCode(self.result_chain is not None)

    monkeypatch.setattr(TriggerDialog, "exec", fake_trigger_exec)
    monkeypatch.setattr(ChainDialog, "exec", fake_chain_exec)
    section.add_button.click()
    section.add_chain_button.click()

    loaded = load_settings(cfg_path)
    assert loaded.triggers.enabled is True
    assert len(loaded.triggers.items) == 1 and loaded.triggers.items[0].kind == "system_resume"
    assert len(loaded.triggers.chains) == 1 and loaded.triggers.chains[0].name == "zincir1"
    assert section.trigger_list.count() == 1
    assert section.chain_list.count() == 1

    # ChainDialog küçük ekrana sığar.
    from PySide6.QtCore import QRect

    cdlg_fit = ChainDialog(None, [], ["ise-basla"], ["uyanis"], "")
    cdlg_fit.show()
    cdlg_fit.fit_to_available(QRect(0, 0, 1366, 480))
    app.processEvents()
    assert cdlg_fit.height() <= 480
    for button in (cdlg_fit.cancel_button, cdlg_fit.ok_button):
        assert cdlg_fit.rect().contains(button.geometry())
    assert cdlg_fit.cancel_button.x() < cdlg_fit.ok_button.x()
    cdlg_fit.close()
    cdlg_fit.deleteLater()
    cdlg.deleteLater()
    dlg.deleteLater()

    # Zincir silme round-trip
    monkeypatch.setattr(section, "_confirm", lambda _text: True)
    section._delete_chain("zincir1")
    assert load_settings(cfg_path).triggers.chains == ()
    assert section.chain_list.count() == 0
    section.deleteLater()


def _wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    """Koşul sağlanana dek bekler; sabit uyku yok, koşul olunca hemen döner."""
    return wait_until(predicate, timeout)


def test_bridge_forwards_system_resume_and_routine_completion_chains(tmp_path: Path) -> None:

    from artemis.channels.desktop.bridge import DesktopBridge, Proactive

    chain = SuggestionChainConfig(
        name="rutin-sonrasi",
        after_routine="selam",
        message="Rutin bitti: {source}",
        suggest="Özetle",
    )
    trig = TriggerConfig(name="wake", kind="system_resume")

    def factory(_path: Path, approve: Any, on_record: Any) -> Runtime:
        workspace = tmp_path / "desktop"
        workspace.mkdir(parents=True, exist_ok=True)
        cfg = Settings(
            workspace=workspace,
            state_dir=tmp_path / "state",
            audit_path=tmp_path / "audit.jsonl",
            persist_memory=False,
            proactive=ProactiveConfig(enabled=True, timezone="UTC", poll_seconds=60.0),
            triggers=TriggersConfig(enabled=True, items=(trig,), chains=(chain,)),
            routines=RoutinesConfig(enabled=True),
        )
        return create_runtime(
            tmp_path / "unused.yaml",
            approve,
            on_record=on_record,
            provider=ScriptedProvider([]),
            settings=cfg,
        )

    bridge = DesktopBridge(tmp_path / "unused.yaml", runtime_factory=factory)
    try:
        assert bridge._ready.wait(timeout=5.0)
        assert _wait_until(lambda: bridge._proactive_scheduler is not None)

        # call_soon_threadsafe sırası korunur: sinyal, sonraki rutin tamamlanmasından önce işlenir.
        bridge.notify_system_resume()

        RoutineStore(tmp_path / "state" / "routines.json").save(
            [Routine(name="selam", steps=[RoutineStep(tool="system_info", arguments={})])]
        )
        req = bridge.submit_tool("routine_run", RoutineNameArguments(name="selam"))
        assert req is not None
        seen_proactive: list[Proactive] = []

        def _drain() -> bool:
            seen_proactive.extend(
                ev for ev in bridge.poll_events(64) if isinstance(ev, Proactive)
            )
            return bool(seen_proactive)

        assert _wait_until(_drain)
        assert len(seen_proactive) == 1
        assert seen_proactive[0].title == "Öneri · rutin-sonrasi"
        assert seen_proactive[0].body == "Rutin bitti: selam"
        assert seen_proactive[0].suggest == "Özetle"
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(timeout=20.0)


async def test_suggest_uses_external_and_tainted_delivery_for_calendar_and_folder(
    tmp_path: Path,
) -> None:
    from artemis.channels.desktop.trigger_dialog import DEFAULT_SUGGEST_BY_KIND
    from artemis.proactive.triggers import suggest_uses_external

    assert suggest_uses_external(None) is False
    assert suggest_uses_external("") is False
    assert suggest_uses_external("Hazırlık notlarını getir ({minutes} dk)") is False
    assert suggest_uses_external("{app} açıldı") is True  # exe adı saldırgan seçimi olabilir
    assert suggest_uses_external("{title} notlarını getir") is True
    assert suggest_uses_external("{location} yol tarifini aç") is True
    assert suggest_uses_external("Özetle: {file}") is True
    assert suggest_uses_external("Özetle: {path}") is True

    default_cal = DEFAULT_SUGGEST_BY_KIND["calendar_before"]
    assert "{title}" not in default_cal and "{location}" not in default_cal
    assert suggest_uses_external(default_cal) is False

    seen: list[tuple[str, str, str, str, str, bool]] = []

    def panel_ex(
        title: str,
        body: str,
        routine: str,
        suggest: str,
        day: str,
        suggest_tainted: bool,
    ) -> bool:
        seen.append((title, body, routine, suggest, day, suggest_tainted))
        return True

    clock = FakeClock()
    dl = tmp_path / "downloads"
    dl.mkdir()
    triggers = (
        TriggerConfig(
            name="cal-tainted-title",
            kind="calendar_before",
            minutes_before=15,
            suggest="{title} notlarını hazırla",
        ),
        TriggerConfig(
            name="cal-tainted-loc",
            kind="calendar_before",
            minutes_before=15,
            suggest="{location} adresini aç",
        ),
        TriggerConfig(
            name="cal-clean",
            kind="calendar_before",
            minutes_before=15,
            suggest=default_cal,
        ),
        TriggerConfig(
            name="folder-tainted-file",
            kind="folder",
            folder=str(dl),
            suggest="Özetle: {file}",
        ),
        TriggerConfig(
            name="folder-tainted-path",
            kind="folder",
            folder=str(dl),
            suggest="Özetle: {path}",
        ),
        TriggerConfig(
            name="folder-clean",
            kind="folder",
            folder=str(dl),
            suggest="Klasörü kontrol et",
        ),
    )
    events = [
        Event("Sync", NOW + timedelta(minutes=5), NOW + timedelta(minutes=35), location="Oda 2")
    ]
    runtime = _make_runtime(tmp_path, items=triggers)
    deliverer = Deliverer(
        runtime.settings,
        runtime.audit,
        routine_panel=panel_ex,
        toast=None,
    )
    scheduler = ProactiveScheduler(
        runtime,
        runtime.context.services["proactive_store"],
        deliverer,
        clock=clock,
        calendar_fetcher=lambda _s, _e: events,
    )

    # İlk tarama klasörler için taban çizgisi kurar, takvimleri bildirir.
    assert await scheduler.tick() == 3
    by_title = {item[0]: item for item in seen}
    assert by_title["Tetikleyici · cal-tainted-title"][5] is True
    assert by_title["Tetikleyici · cal-tainted-loc"][5] is True
    assert by_title["Tetikleyici · cal-clean"][5] is False

    seen.clear()
    (dl / "rapor.pdf").write_bytes(b"pdf")
    clock.advance(seconds=30)
    assert await scheduler.tick() == 0
    clock.advance(seconds=30)
    assert await scheduler.tick() == 3
    by_title = {item[0]: item for item in seen}
    assert by_title["Tetikleyici · folder-tainted-file"][5] is True
    assert by_title["Tetikleyici · folder-tainted-path"][5] is True
    assert by_title["Tetikleyici · folder-clean"][5] is False


async def test_failed_trigger_and_chain_delivery_retries_are_bounded_with_backoff(
    tmp_path: Path,
) -> None:
    import sqlite3

    from artemis.proactive.event_triggers import ChainMatch
    from artemis.proactive.schedule import local_day_bounds
    from artemis.proactive.scheduler import (
        TRIGGER_MAX_ATTEMPTS,
        TRIGGER_RETRY_BACKOFF_SECONDS,
    )

    assert TRIGGER_MAX_ATTEMPTS == 3
    assert TRIGGER_RETRY_BACKOFF_SECONDS == (60.0, 300.0)

    clock = FakeClock()
    dl = tmp_path / "dl"
    dl.mkdir()
    active_app = ["other.exe"]
    triggers = (
        TriggerConfig(name="dl", kind="folder", folder=str(dl), suggest="Özetle"),
        TriggerConfig(
            name="cal",
            kind="calendar_before",
            minutes_before=15,
            suggest="Hazırlan",
        ),
        TriggerConfig(name="app", kind="app_opened", app="code.exe", suggest="Başla"),
    )
    events = [Event("Sync", NOW + timedelta(minutes=15), NOW + timedelta(minutes=45))]
    runtime = _make_runtime(tmp_path, items=triggers)
    store = runtime.context.services["proactive_store"]

    panel_should_fail = True
    deliveries: list[str] = []

    def maybe_panel(
        title: str, _body: str, _routine: str, _suggest: str, _day: str = "",
        _tainted: bool = False,
    ) -> bool:
        deliveries.append(title)
        return not panel_should_fail

    deliverer = Deliverer(
        runtime.settings,
        runtime.audit,
        routine_panel=maybe_panel,
        toast=None,
    )
    scheduler = ProactiveScheduler(
        runtime,
        store,
        deliverer,
        clock=clock,
        active_app_getter=lambda: active_app[0],
        calendar_fetcher=lambda _s, _e: events,
    )

    # İlk tarama: klasör & app baseline kurulur; yalnız calendar dener (deneme 1 -> failed).
    assert await scheduler.tick() == 0
    (dl / "yeni.pdf").write_bytes(b"pdf")
    active_app[0] = "code.exe"

    # 30 sn sonra: klasör stabil değil, cal backoff (60 sn) içinde, app deneme 1 -> failed.
    clock.advance(seconds=30)
    assert await scheduler.tick() == 0

    # +30 sn (toplam 60 sn): klasör hazır (deneme 1 -> failed), cal deneme 2 -> failed,
    # app henüz 30 sn geçtiği için backoff içinde.
    clock.advance(seconds=30)
    assert await scheduler.tick() == 0

    def _occ_rows() -> list[tuple[str, str]]:
        with sqlite3.connect(store.path) as db:
            return db.execute(
                "SELECT event_id, state FROM occurrences ORDER BY event_id"
            ).fetchall()

    rows_after_60s = _occ_rows()
    cal_rows = [r for r in rows_after_60s if r[0].startswith("trigger:cal:")]
    assert len(cal_rows) == 2

    # 20 ek tarama ile tüm tetikleyiciler backoff'ları aşsa bile en fazla 3 satır oluşmalı.
    for _ in range(20):
        clock.advance(seconds=120)
        await scheduler.tick()

    rows = _occ_rows()
    for prefix in ("trigger:dl:", "trigger:cal:", "trigger:app:"):
        matching = [r for r in rows if r[0].startswith(prefix)]
        assert len(matching) == TRIGGER_MAX_ATTEMPTS, (prefix, matching)
        assert all(state == "failed" for _, state in matching)

    # Zincir teslimi de başarısız olunca aynı şekilde en çok 3 satırla sınırlanır.
    chain_match = ChainMatch(
        chain_name="zincir",
        event_key="chain:zincir:src:2026-11-23T09:00",
        title="Öneri · zincir",
        body="Gövde",
        suggest="Devam",
        routine="",
    )
    day_start, _ = local_day_bounds(clock(), scheduler.tz)
    for _ in range(10):
        clock.advance(seconds=400)
        assert await scheduler._fire_chain(chain_match, day_start, clock()) == 0
    chain_rows = [r for r in _occ_rows() if r[0].startswith("chain:zincir:")]
    assert len(chain_rows) == TRIGGER_MAX_ATTEMPTS
    assert all(state == "failed" for _, state in chain_rows)

    # Kurtarma: ikinci denemede backoff dolduktan sonra teslim başarılı olursa işaretlenir.
    rec_clock = FakeClock()
    rec_runtime = _make_runtime(
        tmp_path / "rec",
        items=(TriggerConfig(name="cal2", kind="calendar_before", minutes_before=15, suggest="S"),),
    )
    rec_store = rec_runtime.context.services["proactive_store"]
    rec_fail = [True]
    rec_deliverer = Deliverer(
        rec_runtime.settings,
        rec_runtime.audit,
        routine_panel=lambda *_args: not rec_fail[0],
        toast=None,
    )
    rec_sched = ProactiveScheduler(
        rec_runtime,
        rec_store,
        rec_deliverer,
        clock=rec_clock,
        calendar_fetcher=lambda _s, _e: [
            Event("Sync", NOW + timedelta(minutes=5), NOW + timedelta(minutes=35))
        ],
    )
    assert await rec_sched.tick() == 0
    rec_fail[0] = False
    rec_clock.advance(seconds=30)  # backoff 60 sn dolmadı -> yeniden denemez
    assert await rec_sched.tick() == 0
    rec_clock.advance(seconds=35)  # toplam 65 sn -> deneme 2 başarılı!
    assert await rec_sched.tick() == 1
    assert await rec_sched.tick() == 0  # mark_fired çalıştı, tekrarlamaz


def test_cached_calendar_provider_honors_ttl_on_initial_fetch_error(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, calendar_enabled=True)
    mono = FakeMono(start=100.0)
    calls: list[str] = []

    def failing_fetch(url: str) -> bytes:
        calls.append(url)
        raise SourceError("çevrimdışı")

    provider = CachedCalendarProvider(
        runtime,
        secret_resolver=lambda _env, _target: "https://cal.example/feed.ics",
        fetcher=failing_fetch,
        monotonic_clock=mono,
    )

    # Soğuk önbellekte ilk hata: 1 ağ isteği atılır ve TTL boyunca tekrarlanmaz.
    assert provider(NOW, NOW + timedelta(minutes=15)) == []
    assert len(calls) == 1

    # Zaman penceresi 30 sn ilerlese bile TTL içinde ikinci istek atılmamalı.
    mono.value += 30.0
    assert provider(NOW + timedelta(seconds=30), NOW + timedelta(minutes=15, seconds=30)) == []
    mono.value += 200.0
    assert provider(NOW + timedelta(minutes=4), NOW + timedelta(minutes=19)) == []
    assert len(calls) == 1

    # TTL dolduktan sonra yeni deneme yapılır.
    mono.value += 100.0  # toplam +330s >= 300s
    assert provider(NOW + timedelta(minutes=5), NOW + timedelta(minutes=20)) == []
    assert len(calls) == 2


def test_resume_detector_uses_unbiased_clock_and_detects_sleep_gap() -> None:
    from artemis.proactive.event_triggers import ResumeDetector

    fake_unbiased = [100.0]
    detector = ResumeDetector(monotonic_clock=lambda: fake_unbiased[0], sleep_gap_seconds=120.0)
    assert detector.poll(NOW) is False

    # Normal çalışma: duvar saati +30s, kesintisiz sayaç +30s -> uyku yok.
    fake_unbiased[0] += 30.0
    assert detector.poll(NOW + timedelta(seconds=30)) is False

    # Uyku: duvar saati +600s, uyku hariç sayaç +2s -> 598s fark >= 120s -> uyandı.
    fake_unbiased[0] += 2.0
    assert detector.poll(NOW + timedelta(seconds=630)) is True



async def test_routine_panel_carries_suggest_tainted_flag(tmp_path: Path) -> None:
    """Tek panel sink'i dış alanlı önerinin leke bayrağını paneline aynen taşır (#39)."""
    seen: list[tuple[str, bool]] = []

    def sink(
        _title: str, _body: str, _routine: str, suggest: str, _day: str, tainted: bool
    ) -> bool:
        seen.append((suggest, tainted))
        return True

    runtime = _make_runtime(tmp_path)
    deliverer = Deliverer(runtime.settings, runtime.audit, routine_panel=sink, toast=None)
    common = {
        "event_id": "e",
        "scheduled_for": datetime(2026, 11, 26, 9, 0, tzinfo=UTC),
        "kind": "trigger",
        "title": "Toplantı",
        "body": "10 dk kaldı",
        "private": True,
        "local_only": True,
    }
    await deliverer.deliver(**common, suggest="X için hazırlık", suggest_tainted=True)
    await deliverer.deliver(**common, suggest="temiz öneri", suggest_tainted=False)
    assert seen == [("X için hazırlık", True), ("temiz öneri", False)]


def test_app_placeholder_taints_app_opened_and_chain_suggestions() -> None:
    # Karne 7 Y1: {app} (ön plandaki exe adı) içeren öneri lekeli sayılır; {source} temizdir.
    def chain(name: str, suggest: str) -> SuggestionChainConfig:
        return SuggestionChainConfig(
            name=name, after_routine="kod-ac", message="m", suggest=suggest
        )

    matches = evaluate_chains(
        [chain("app-li", "{app} için özetle"), chain("kaynakli", "{source} sonrası özetle")],
        event_kind="routine_completed",
        source_name="kod-ac",
        now=datetime(2026, 11, 23, 10, 0, tzinfo=UTC),
        tz=ZoneInfo("UTC"),
        work_profile="work",
        active_app="evil.exe",
        routines_enabled=True,
        last_fired={},
    )
    tainted = {m.chain_name: m.suggest_tainted for m in matches}
    assert tainted == {"app-li": True, "kaynakli": False}


async def test_app_opened_suggest_with_app_placeholder_is_tainted(tmp_path: Path) -> None:
    current_app: list[str | None] = ["chrome.exe"]
    clock = FakeClock()
    seen: list[bool] = []

    def panel(
        _t: str, _b: str, _r: str, _s: str, _d: str = "", tainted: bool = False
    ) -> bool:
        seen.append(tainted)
        return True

    trig = TriggerConfig(
        name="kod-acildi", kind="app_opened", app="code.exe", suggest="{app} için özetle"
    )
    runtime = _make_runtime(tmp_path, items=(trig,))
    deliverer = Deliverer(runtime.settings, runtime.audit, routine_panel=panel, toast=None)
    scheduler = ProactiveScheduler(
        runtime,
        runtime.context.services["proactive_store"],
        deliverer,
        clock=clock,
        active_app_getter=lambda: current_app[0],
    )
    assert await scheduler.tick() == 0
    clock.advance(seconds=30)
    current_app[0] = "code.exe"
    assert await scheduler.tick() == 1
    assert seen == [True]
