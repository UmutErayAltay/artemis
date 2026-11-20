"""Canlı zamanlanmış rutin ve tetikleyici masaüstü kabul denetimleri (Y:67-69, Y:109-110, Y:115)."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from artemis.channels.desktop.approval_dialog import ApprovalDialog
from artemis.proactive.routines import (
    Routine,
    RoutineSchedule,
    RoutinesError,
    RoutineStep,
    RoutineStore,
)
from artemis.proactive.store import ProactiveStore, ProactiveStoreError
from scripts.kabul_masaustu_checks.common import (
    DEMO_DOWNLOADS_DIR,
    GlobalTimeoutError,
    MutableClock,
    RecordingProvider,
    RowResult,
    assert_no_leaked_windows,
    check_deadline,
    close_window_cleanly,
    fake_provider_bridge,
    grab_neutralized,
    injected_proactive_clock,
    intercept_toasts,
    make_temp_config,
    open_real_window,
    read_audit_events,
    select_settings_section,
    wait_until,
    wait_window_ready,
)

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow

_CHECK_ERRORS = (
    AssertionError,
    OSError,
    ValueError,
    RuntimeError,
    TimeoutError,
    sqlite3.Error,
    ProactiveStoreError,
    RoutinesError,
    yaml.YAMLError,
    ValidationError,
)


def _pump_events(
    app: QApplication,
    seconds: float,
    *,
    deadline: float | None = None,
    stage: str = "",
) -> None:
    """Verilen süre boyunca Qt olay döngüsünü işletir; küresel zaman aşımını denetler."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_deadline(deadline, stage)
        app.processEvents()
        time.sleep(0.05)
    app.processEvents()


def _dismiss_approval_with_esc(
    app: QApplication,
    window: DesktopWindow,
    *,
    desc: str,
    deadline: float | None = None,
) -> None:
    """Onay penceresini asla onaylamadan Esc ile kapatır ve pencerenin hazır olmasını bekler."""
    dialog = window._approval_dialog
    assert isinstance(dialog, ApprovalDialog)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    wait_until(
        app,
        lambda: window._approval_dialog is None,
        desc=desc,
        deadline=deadline,
    )
    wait_window_ready(app, window, deadline=deadline)


def _check_y67_y68(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    """Y:67 ve Y:68 — Rutin zamanlama bildirimi, Kapat ile iptal ve Çalıştır ile SafetyGate."""
    check_deadline(deadline, "Y:67/Y:68")
    sub = tmp_root / "y67_y68"
    # 2026-10-15 07:00 UTC = 10:00 Europe/Istanbul (Perşembe, weekday == 3).
    t0 = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
    clock = MutableClock(t0)

    cfg_path, workspace, state_dir = make_temp_config(
        sub,
        overrides={
            "routines": {"enabled": True},
            "proactive": {
                "enabled": True,
                "poll_seconds": 1.0,
                "timezone": "Europe/Istanbul",
                "briefing_grace_hours": 2.0,
            },
        },
    )
    target_file = workspace / "gecici.txt"
    target_file.write_text("silme denemesi", encoding="utf-8")

    routine_store = RoutineStore(state_dir / "routines.json")
    routine_store.save(
        [
            Routine(
                name="işe başla",
                steps=[
                    RoutineStep(tool="current_time", arguments={}),
                    RoutineStep(tool="delete_file", arguments={"path": "gecici.txt"}),
                ],
            )
        ]
    )

    audit_path = state_dir / "audit.jsonl"
    window: DesktopWindow | None = None
    y67_result: RowResult | None = None
    y68_result: RowResult | None = None

    try:
        with (
            fake_provider_bridge(RecordingProvider),
            injected_proactive_clock(clock),
            intercept_toasts("ok") as toasts,
        ):
            window = open_real_window(app, cfg_path, deadline=deadline)
            try:
                select_settings_section(app, window, "Rutinler")
                sec = window.settings_panel.routines
                assert sec.routine_list.count() == 1
                sec.routine_list.setCurrentRow(0)
                app.processEvents()

                sec.time_edit.setText("10:02")
                for idx in range(7):
                    sec.day_boxes[idx].setChecked(idx == 3)
                QTest.mouseClick(sec.save_time_button, Qt.MouseButton.LeftButton)
                app.processEvents()

                assert sec.notice.text() == "Zaman kaydedildi.", (
                    f"Beklenmeyen bildirim: {sec.notice.text()!r}"
                )
                saved = routine_store.find("işe başla")
                assert saved is not None and saved.schedule is not None
                assert saved.schedule.at == "10:02"
                assert saved.schedule.days == (3,)

                window.tabs.setCurrentWidget(window.chat_page)
                app.processEvents()
                _pump_events(app, 0.4, deadline=deadline, stage="Y:67 ilk bekleme")
                assert not window.routine_bar.isVisible()
                assert len(toasts) == 0

                clock.advance(minutes=2)
                wait_until(
                    app,
                    lambda: (
                        window.routine_bar.isVisible()
                        and "işe başla rutininin zamanı. Çalıştırılsın mı?"
                        in window.chat.toPlainText()
                        and len(toasts) == 1
                    ),
                    timeout=15.0,
                    desc="Y:67 rutin zamanı bildirimi",
                    deadline=deadline,
                )

                assert window.routine_run_button.text() == "Çalıştır"
                assert window.routine_dismiss_button.text() == "Kapat"
                assert toasts[0].title == "Rutin zamanı"
                assert "işe başla" in toasts[0].body
                assert len(read_audit_events(audit_path, "routine_run_start")) == 0
                assert window._approval_dialog is None
                assert target_file.is_file()

                shot_y67 = grab_neutralized(
                    app, window, out_dir, "Y67_routine_schedule_notice.png"
                )
                y67_result = RowResult(
                    row_id="Y:67",
                    status="PASS",
                    observation=(
                        "Rutin 10:02 Perşembe olarak kaydedildi; enjekte MutableClock iki dakika "
                        "ilerletilince çalıştırmadan yalnız bildirim çubuğu ve yakalanan toast "
                        "göründü (RecordingProvider, routine_run_start=0)."
                    ),
                    screenshots=(shot_y67.name,),
                )

                # Y:68 — Kapat çalıştırmaz; bir hafta sonra Çalıştır SafetyGate onayı açar.
                check_deadline(deadline, "Y:68")
                QTest.mouseClick(window.routine_dismiss_button, Qt.MouseButton.LeftButton)
                app.processEvents()
                assert not window.routine_bar.isVisible()
                _pump_events(app, 1.2, deadline=deadline, stage="Y:68 kapat sonrası")
                assert len(read_audit_events(audit_path, "routine_run_start")) == 0
                assert target_file.is_file()

                clock.advance(days=7)
                wait_until(
                    app,
                    lambda: window.routine_bar.isVisible() and len(toasts) == 2,
                    timeout=15.0,
                    desc="Y:68 sonraki hafta rutin bildirimi",
                    deadline=deadline,
                )

                QTest.mouseClick(window.routine_run_button, Qt.MouseButton.LeftButton)
                app.processEvents()
                assert not window.routine_bar.isVisible()

                wait_until(
                    app,
                    lambda: isinstance(window._approval_dialog, ApprovalDialog),
                    timeout=15.0,
                    desc="Y:68 delete_file onay diyaloğu",
                    deadline=deadline,
                )
                dialog = window._approval_dialog
                assert isinstance(dialog, ApprovalDialog)
                assert dialog.item.tool_name == "delete_file"
                assert dialog.item.arguments == {"path": "gecici.txt"}
                assert dialog.no_button.isDefault()
                assert len(read_audit_events(audit_path, "routine_run_start")) == 1

                shot_y68 = grab_neutralized(
                    app, dialog, out_dir, "Y68_routine_run_approval.png"
                )
                _dismiss_approval_with_esc(
                    app, window, desc="Y:68 onay diyaloğu kapanışı", deadline=deadline
                )
                assert target_file.is_file()
                assert "onaylanmadı" in window.chat.toPlainText()

                y68_result = RowResult(
                    row_id="Y:68",
                    status="PASS",
                    observation=(
                        "'Kapat' bildirimi çalıştırmadan kaldırdı; sonraki Perşembe 'Çalıştır' "
                        "SafetyGate rutin yolunu (routine_run_start=1) 1. adımdan geçip 2. adımda "
                        "delete_file ApprovalDialog'una taşıdı ve kural gereği Esc ile kapatıldı "
                        "(saat, toast ve LLM sahtelenmiş)."
                    ),
                    screenshots=(shot_y68.name,),
                )
            finally:
                close_window_cleanly(app, window)
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        fail_msg = f"{type(error).__name__}: {error}"
        if y67_result is None:
            y67_result = RowResult("Y:67", "FAIL", fail_msg)
        if y68_result is None:
            y68_result = RowResult("Y:68", "FAIL", fail_msg)
    assert_no_leaked_windows(app, "Y:67/Y:68 sonrası", max_visible=2, max_desktop=0)
    assert y67_result is not None and y68_result is not None
    return [y67_result, y68_result]


def _check_y69(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    """Y:69 — briefing_grace_hours süresi içinde geç açılışta bildirim, dışında sessizlik."""
    check_deadline(deadline, "Y:69")
    routine_10 = Routine(
        name="işe başla",
        steps=[RoutineStep(tool="current_time", arguments={})],
        schedule=RoutineSchedule(at="10:00", days=(3,)),
    )
    overrides = {
        "routines": {"enabled": True},
        "proactive": {
            "enabled": True,
            "poll_seconds": 1.0,
            "timezone": "Europe/Istanbul",
            "briefing_grace_hours": 2.0,
        },
    }

    try:
        # Durum A: Süre payı içinde (11:30 İstanbul -> 10:00'dan 1s30d sonra <= 2s).
        cfg_in, _ws_in, state_in = make_temp_config(tmp_root / "y69_in", overrides=overrides)
        RoutineStore(state_in / "routines.json").save([routine_10])
        clock_in = MutableClock(datetime(2026, 10, 15, 8, 30, tzinfo=UTC))
        win_in: DesktopWindow | None = None
        try:
            with (
                fake_provider_bridge(RecordingProvider),
                injected_proactive_clock(clock_in),
                intercept_toasts("ok") as toasts_in,
            ):
                win_in = open_real_window(app, cfg_in, deadline=deadline)
                wait_until(
                    app,
                    lambda: (
                        win_in.routine_bar.isVisible()
                        and "işe başla rutininin zamanı" in win_in.chat.toPlainText()
                        and len(toasts_in) == 1
                    ),
                    timeout=15.0,
                    desc="Y:69 süre payı içinde bildirim",
                    deadline=deadline,
                )
                assert len(read_audit_events(state_in / "audit.jsonl", "routine_run_start")) == 0
                shot_in = grab_neutralized(
                    app, win_in, out_dir, "Y69_routine_grace_within.png"
                )
        finally:
            if win_in is not None:
                close_window_cleanly(app, win_in)
        assert_no_leaked_windows(app, "Y:69 Case A sonrası", max_visible=2, max_desktop=0)

        # Durum B: Süre payı dışında (12:30 İstanbul -> 10:00'dan 2s30d sonra > 2s).
        check_deadline(deadline, "Y:69 Case B")
        cfg_out, _ws_out, state_out = make_temp_config(tmp_root / "y69_out", overrides=overrides)
        RoutineStore(state_out / "routines.json").save([routine_10])
        clock_out = MutableClock(datetime(2026, 10, 15, 9, 30, tzinfo=UTC))
        win_out: DesktopWindow | None = None
        try:
            with (
                fake_provider_bridge(RecordingProvider),
                injected_proactive_clock(clock_out),
                intercept_toasts("ok") as toasts_out,
            ):
                win_out = open_real_window(app, cfg_out, deadline=deadline)
                _pump_events(app, 2.3, deadline=deadline, stage="Y:69 süre payı dışı")
                assert not win_out.routine_bar.isVisible()
                assert "işe başla rutininin zamanı" not in win_out.chat.toPlainText()
                assert len(toasts_out) == 0
                assert (
                    len(read_audit_events(state_out / "audit.jsonl", "routine_run_start")) == 0
                )
        finally:
            if win_out is not None:
                close_window_cleanly(app, win_out)
        assert_no_leaked_windows(app, "Y:69 Case B sonrası", max_visible=2, max_desktop=0)

        return RowResult(
            row_id="Y:69",
            status="PASS",
            observation=(
                "2 saatlik briefing_grace_hours içinde (11:30) geç açılışta rutin bildirimi "
                "geldi, süre payı dışında (12:30) bildirim ve çalıştırma oluşmadı "
                "(enjekte MutableClock, yakalanan toast, sahte LLM)."
            ),
            screenshots=(shot_in.name,),
        )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("Y:69", "FAIL", f"{type(error).__name__}: {error}")


def _check_y109_y110(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    """Y:109 ve Y:110 — Tetikleyici yeni PDF bildirimi (tek kez) ve Yaz ile istem doldurma."""
    check_deadline(deadline, "Y:109/Y:110")
    sub = tmp_root / "y109_110"
    watched_dir = sub / "Indirilenler"
    watched_dir.mkdir(parents=True, exist_ok=True)
    (watched_dir / "eski.pdf").write_bytes(b"%PDF-1.4\neski\n")

    cfg_path, _ws, state_dir = make_temp_config(
        sub,
        overrides={
            "proactive": {
                "enabled": True,
                "poll_seconds": 1.0,
                "timezone": "Europe/Istanbul",
            },
            "triggers": {
                "enabled": True,
                "items": [
                    {
                        "name": "indirilenler",
                        "folder": str(watched_dir),
                        "pattern": "*.pdf",
                        "suggest": "Şu dosyayı özetle: {path}",
                    }
                ],
            },
        },
    )

    provider = RecordingProvider()
    window: DesktopWindow | None = None
    y109_result: RowResult | None = None
    y110_result: RowResult | None = None

    try:
        with (
            fake_provider_bridge(lambda: provider),
            intercept_toasts("ok") as toasts,
        ):
            window = open_real_window(app, cfg_path, deadline=deadline)
            try:
                store = ProactiveStore(state_dir / "proactive.sqlite3")
                wait_until(
                    app,
                    lambda: store.trigger_files("indirilenler") is not None,
                    timeout=15.0,
                    desc="Y:109 tetikleyici taban çizgisi",
                    deadline=deadline,
                )
                assert len(toasts) == 0
                assert not window.routine_bar.isVisible()

                fatura = watched_dir / "fatura.pdf"
                fatura.write_bytes(b"%PDF-1.4\ndemo\n")

                wait_until(
                    app,
                    lambda: window.routine_bar.isVisible() and len(toasts) == 1,
                    timeout=15.0,
                    desc="Y:109 fatura.pdf bildirimi",
                    deadline=deadline,
                )

                chat_text = window.chat.toPlainText()
                assert "Tetikleyici · indirilenler" in chat_text
                assert "Yeni dosya geldi: fatura.pdf" in chat_text
                assert window.routine_run_button.text() == "Yaz"
                assert "fatura.pdf" in window.routine_label.text()
                assert toasts[0].title == "Tetikleyici · indirilenler"
                assert "fatura.pdf" in toasts[0].body

                _pump_events(app, 1.3, deadline=deadline, stage="Y:109 tek bildirim kontrolü")
                assert len(toasts) == 1
                assert window.chat.toPlainText().count("Tetikleyici · indirilenler") == 1

                shot_y109 = grab_neutralized(
                    app,
                    window,
                    out_dir,
                    "Y109_trigger_pdf_notice.png",
                    real_roots={watched_dir: DEMO_DOWNLOADS_DIR},
                )
                y109_result = RowResult(
                    row_id="Y:109",
                    status="PASS",
                    observation=(
                        "Geçici klasöre bırakılan fatura.pdf iki poll_seconds=1.0 taraması "
                        "sonrası bir kez 'Tetikleyici · indirilenler' sohbet mesajı, 'Yaz' "
                        "çubuğu ve yakalanan toast üretti (tarayıcı indirmesi yerine dosya "
                        "bırakıldı; RecordingProvider)."
                    ),
                    screenshots=(shot_y109.name,),
                )

                # Y:110 — "Yaz" tıklaması öneriyi göndermeden istem kutusuna yazar.
                check_deadline(deadline, "Y:110")
                assert window.prompt.toPlainText() == ""
                assert len(provider.requests) == 0

                QTest.mouseClick(window.routine_run_button, Qt.MouseButton.LeftButton)
                app.processEvents()

                assert not window.routine_bar.isVisible()
                assert window.prompt.toPlainText() == f"Şu dosyayı özetle: {fatura}"
                assert not window._busy
                _pump_events(app, 0.5, deadline=deadline, stage="Y:110 gönderilmeme kontrolü")
                assert len(provider.requests) == 0

                shot_y110 = grab_neutralized(
                    app,
                    window,
                    out_dir,
                    "Y110_trigger_suggest_filled.png",
                    real_roots={watched_dir: DEMO_DOWNLOADS_DIR},
                )
                y110_result = RowResult(
                    row_id="Y:110",
                    status="PASS",
                    observation=(
                        "'Yaz' düğmesi önerilen istemi otomatik göndermeden sohbet kutusuna "
                        "doldurdu (RecordingProvider.requests=0, poll_seconds=1.0, yakalanan "
                        "toast, geçici klasör PDF bırakma)."
                    ),
                    screenshots=(shot_y110.name,),
                )
            finally:
                close_window_cleanly(app, window)
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        fail_msg = f"{type(error).__name__}: {error}"
        if y109_result is None:
            y109_result = RowResult("Y:109", "FAIL", fail_msg)
        if y110_result is None:
            y110_result = RowResult("Y:110", "FAIL", fail_msg)
    assert_no_leaked_windows(app, "Y:109/Y:110 sonrası", max_visible=2, max_desktop=0)
    assert y109_result is not None and y110_result is not None
    return [y109_result, y110_result]


def _check_y115(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    """Y:115 — Rutin bağlı tetikleyicide Çalıştır ile SafetyGate rutin onayı."""
    check_deadline(deadline, "Y:115")
    sub = tmp_root / "y115"
    watched_dir = sub / "Indirilenler"
    watched_dir.mkdir(parents=True, exist_ok=True)

    cfg_path, workspace, state_dir = make_temp_config(
        sub,
        overrides={
            "proactive": {"enabled": True, "poll_seconds": 1.0},
            "routines": {"enabled": True},
            "triggers": {
                "enabled": True,
                "items": [
                    {
                        "name": "belge_rutini",
                        "folder": str(watched_dir),
                        "pattern": "*.pdf",
                        "routine": "pdf_kontrol",
                    }
                ],
            },
        },
    )
    target_file = workspace / "hedef.txt"
    target_file.write_text("korunan hedef", encoding="utf-8")

    RoutineStore(state_dir / "routines.json").save(
        [
            Routine(
                name="pdf_kontrol",
                steps=[
                    RoutineStep(tool="current_time", arguments={}),
                    RoutineStep(tool="delete_file", arguments={"path": "hedef.txt"}),
                ],
            )
        ]
    )

    audit_path = state_dir / "audit.jsonl"
    window: DesktopWindow | None = None
    try:
        with (
            fake_provider_bridge(RecordingProvider),
            intercept_toasts("ok") as toasts,
        ):
            window = open_real_window(app, cfg_path, deadline=deadline)
            try:
                store = ProactiveStore(state_dir / "proactive.sqlite3")
                wait_until(
                    app,
                    lambda: store.trigger_files("belge_rutini") is not None,
                    timeout=15.0,
                    desc="Y:115 tetikleyici taban çizgisi",
                    deadline=deadline,
                )

                (watched_dir / "gelen.pdf").write_bytes(b"%PDF-1.4\ngelen\n")
                wait_until(
                    app,
                    lambda: window.routine_bar.isVisible() and len(toasts) == 1,
                    timeout=15.0,
                    desc="Y:115 gelen.pdf bildirimi",
                    deadline=deadline,
                )

                assert window.routine_run_button.text() == "Çalıştır"
                assert "gelen.pdf" in window.routine_label.text()

                QTest.mouseClick(window.routine_run_button, Qt.MouseButton.LeftButton)
                app.processEvents()

                wait_until(
                    app,
                    lambda: isinstance(window._approval_dialog, ApprovalDialog),
                    timeout=15.0,
                    desc="Y:115 delete_file onay diyaloğu",
                    deadline=deadline,
                )
                dialog = window._approval_dialog
                assert isinstance(dialog, ApprovalDialog)
                assert dialog.item.tool_name == "delete_file"
                assert dialog.item.arguments == {"path": "hedef.txt"}
                assert dialog.no_button.isDefault()
                assert len(read_audit_events(audit_path, "routine_run_start")) == 1

                shot = grab_neutralized(
                    app,
                    dialog,
                    out_dir,
                    "Y115_trigger_routine_approval.png",
                    real_roots={watched_dir: DEMO_DOWNLOADS_DIR},
                )
                _dismiss_approval_with_esc(
                    app, window, desc="Y:115 onay diyaloğu kapanışı", deadline=deadline
                )
                assert target_file.is_file()
                assert "onaylanmadı" in window.chat.toPlainText()

                result = RowResult(
                    row_id="Y:115",
                    status="PASS",
                    observation=(
                        "Geçici klasöre gelen.pdf bırakılınca (poll_seconds=1.0, yakalanan "
                        "toast) 'Çalıştır' çubuğu çıktı; tıklanınca pdf_kontrol rutini "
                        "routine_run_start=1 ile delete_file ApprovalDialog'una ulaştı ve "
                        "kural gereği Esc ile reddedildi (RecordingProvider)."
                    ),
                    screenshots=(shot.name,),
                )
            finally:
                close_window_cleanly(app, window)
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        result = RowResult("Y:115", "FAIL", f"{type(error).__name__}: {error}")
    assert_no_leaked_windows(app, "Y:115 sonrası", max_visible=2, max_desktop=0)
    return result


def run_routines_triggers_live_checks(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    """Y:67-69, Y:109, Y:110 ve Y:115 canlı rutin/tetikleyici denetimlerini çalıştırır."""
    results: list[RowResult] = []
    results.extend(_check_y67_y68(app, tmp_root, out_dir, deadline=deadline))
    results.append(_check_y69(app, tmp_root, out_dir, deadline=deadline))
    results.extend(_check_y109_y110(app, tmp_root, out_dir, deadline=deadline))
    results.append(_check_y115(app, tmp_root, out_dir, deadline=deadline))
    return results
