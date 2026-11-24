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
        self, title: str, body: str, routine: str, suggest: str, day: str = ""
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


def test_bridge_forwards_system_resume_and_routine_completion_chains(tmp_path: Path) -> None:
    import time

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
        deadline = time.monotonic() + 5.0
        while bridge._proactive_scheduler is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert bridge._proactive_scheduler is not None

        bridge.notify_system_resume()
        time.sleep(0.05)

        RoutineStore(tmp_path / "state" / "routines.json").save(
            [Routine(name="selam", steps=[RoutineStep(tool="system_info", arguments={})])]
        )
        req = bridge.submit_tool("routine_run", RoutineNameArguments(name="selam"))
        assert req is not None
        seen_proactive: list[Proactive] = []
        end = time.monotonic() + 5.0
        while time.monotonic() < end:
            for ev in bridge.poll_events(64):
                if isinstance(ev, Proactive):
                    seen_proactive.append(ev)
            if seen_proactive:
                break
            time.sleep(0.02)
        assert len(seen_proactive) == 1
        assert seen_proactive[0].title == "Öneri · rutin-sonrasi"
        assert seen_proactive[0].body == "Rutin bitti: selam"
        assert seen_proactive[0].suggest == "Özetle"
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(timeout=20.0)
