"""Hatırlatma geçmişi, canlı bildirim teslimi ve gecikmeli tekilleştirme kabul denetimleri."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from artemis.channels.desktop.app import DesktopWindow
from artemis.channels.desktop.approval_dialog import ApprovalDialog
from artemis.proactive.store import ProactiveStore, reminder_event_id
from scripts.kabul_masaustu_checks.common import (
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
    remaining_timeout,
    wait_until,
    wait_window_ready,
)

_CHECK_ERRORS = (
    AssertionError,
    OSError,
    ValueError,
    RuntimeError,
    TimeoutError,
    sqlite3.Error,
    yaml.YAMLError,
    ValidationError,
)


def _pump_events(
    app: QApplication,
    seconds: float,
    *,
    deadline: float | None = None,
    stage: str = "pump",
) -> None:
    """Belirtilen süre boyunca Qt olay döngüsünü işletir; küresel süreyi aşmaz."""
    limit = remaining_timeout(deadline, seconds, stage)
    end = time.monotonic() + limit
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    check_deadline(deadline, stage)


def _seed_reminder_occurrence(
    store: ProactiveStore,
    text: str,
    due_at: datetime,
    *,
    private: bool,
    state: str,
    detail: str,
    now: datetime,
) -> int:
    """Hatırlatma ekler, occurrence kaydını talep edip belirtilen bitiş durumuna taşır."""
    reminder = store.add_reminder(text, due_at, tainted=False, private=private, now=now)
    event_id = reminder_event_id(reminder.id)
    assert store.claim(event_id, reminder.due_at, "reminder", "test", now)
    store.finish(event_id, reminder.due_at, state, detail, now)
    return reminder.id


def _check_r153(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    """R:153 — Hatırlatma teslim filtresi, uyarı etiketi ve Esc ile kapanan retry onayı."""
    check_deadline(deadline, "R:153")
    sub = tmp_root / "r153"
    window: DesktopWindow | None = None
    try:
        cfg_path, _ws, state_dir = make_temp_config(
            sub,
            overrides={
                "proactive": {
                    "enabled": True,
                    "poll_seconds": 300.0,
                    "timezone": "Europe/Istanbul",
                }
            },
        )
        t0 = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
        store = ProactiveStore(state_dir / "proactive.sqlite3")
        fail_id = _seed_reminder_occurrence(
            store,
            "Başarısız ilaç hatırlatması",
            t0,
            private=False,
            state="failed",
            detail="test-fail",
            now=t0,
        )
        _seed_reminder_occurrence(
            store,
            "Belirsiz toplantı notu",
            t0 + timedelta(minutes=1),
            private=True,
            state="uncertain",
            detail="test-unc",
            now=t0,
        )
        _seed_reminder_occurrence(
            store,
            "Teslim edilen kayıt",
            t0 + timedelta(minutes=2),
            private=False,
            state="delivered",
            detail="test-ok",
            now=t0,
        )

        with fake_provider_bridge(RecordingProvider):
            window = open_real_window(app, cfg_path, deadline=deadline)
            window.tabs.setCurrentWidget(window.data_page)
            app.processEvents()
            dp = window.data_panel
            wait_until(
                app,
                lambda: (
                    window is not None
                    and dp._history_loaded
                    and not dp._memory_pending
                    and not window._busy
                ),
                desc="R:153 data_panel ready",
                deadline=deadline,
            )
            wait_window_ready(app, window, deadline=deadline)

            def _filter_reminders(state: str) -> None:
                idx = dp.filter.findData(state)
                assert idx >= 0, f"Hatırlatma filtresi bulunamadı: {state!r}"
                dp.filter.setCurrentIndex(idx)
                app.processEvents()
                QTest.mouseClick(dp.refresh, Qt.MouseButton.LeftButton)
                wait_until(
                    app,
                    lambda: window is not None and not window._busy,
                    desc=f"R:153 filter {state}",
                    deadline=deadline,
                )
                wait_window_ready(app, window, deadline=deadline)

            _filter_reminders("failed")
            assert dp.items.count() == 1
            item_fail = dp.items.item(0)
            assert item_fail is not None
            assert "Başarısız" in item_fail.text()
            assert "Başarısız ilaç hatırlatması" in item_fail.text()
            dp.items.setCurrentRow(0)
            app.processEvents()
            assert dp.retry.isEnabled()
            assert dp.retry_warning.isVisible()
            assert dp.retry_warning.text() == "Çift bildirim olabilir"

            _filter_reminders("uncertain")
            assert dp.items.count() == 1
            item_unc = dp.items.item(0)
            assert item_unc is not None
            assert "Teslim doğrulanamadı" in item_unc.text()
            assert "Belirsiz toplantı notu" in item_unc.text()
            dp.items.setCurrentRow(0)
            app.processEvents()
            assert dp.retry.isEnabled()
            assert dp.retry_warning.isVisible()

            _filter_reminders("delivered")
            assert dp.items.count() == 1
            dp.items.setCurrentRow(0)
            app.processEvents()
            assert not dp.retry.isEnabled()
            assert not dp.retry_warning.isVisible()

            _filter_reminders("failed")
            assert dp.items.count() == 1
            dp.items.setCurrentRow(0)
            app.processEvents()
            shot_list = grab_neutralized(app, window, out_dir, "R153_failed_uncertain_list.png")

            QTest.mouseClick(dp.retry, Qt.MouseButton.LeftButton)
            wait_until(
                app,
                lambda: window is not None and isinstance(window._approval_dialog, ApprovalDialog),
                desc="R:153 approval dialog",
                deadline=deadline,
            )
            dialog = window._approval_dialog
            assert isinstance(dialog, ApprovalDialog)
            assert dialog.item.tool_name == "reminder_retry"
            assert dialog.item.arguments == {"reminder_id": fail_id}
            assert dialog.no_button.isDefault()
            shot_dialog = grab_neutralized(app, dialog, out_dir, "R153_retry_approval.png")

            # Onay penceresi kesin kural gereği onaylanmaz; Esc ile reddedilir.
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
            wait_until(
                app,
                lambda: window is not None and window._approval_dialog is None,
                desc="R:153 approval closed",
                deadline=deadline,
            )
            wait_window_ready(app, window, deadline=deadline)
            assert len(store.list_reminders("all")) == 3

            return RowResult(
                "R:153",
                "SKIPPED",
                "failed/uncertain/delivered filtreleri, 'Çift bildirim olabilir' uyarısı, "
                "Yeniden dene etkin/pasif durumları ve reminder_retry ApprovalDialog (Esc ile "
                "reddedildi; yeni kayıt oluşmadı) doğrulandı; onaya basıp canlı yeniden teslim "
                "bildirimini gözlemek kesin kural gereği atlandı.",
                (shot_list.name, shot_dialog.name),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("R:153", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_f91(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    """F:91 — Enjekte saatle zamanı gelen hatırlatmanın sohbet paneline ve toast'a teslimi."""
    check_deadline(deadline, "F:91")
    sub = tmp_root / "f91"
    window: DesktopWindow | None = None
    try:
        cfg_path, _ws, state_dir = make_temp_config(
            sub,
            overrides={
                "proactive": {
                    "enabled": True,
                    "poll_seconds": 1.0,
                    "timezone": "Europe/Istanbul",
                }
            },
        )
        t0 = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
        clock = MutableClock(t0)
        rem = ProactiveStore(state_dir / "proactive.sqlite3").add_reminder(
            "Su içme zamanı F91",
            due_at=t0 + timedelta(minutes=2),
            tainted=False,
            private=False,
            now=t0,
        )

        with (
            fake_provider_bridge(RecordingProvider),
            injected_proactive_clock(clock),
            intercept_toasts("ok") as toasts,
        ):
            window = open_real_window(app, cfg_path, deadline=deadline)
            _pump_events(app, 1.2, deadline=deadline, stage="F:91 vade öncesi")
            assert "Su içme zamanı F91" not in window.chat.toPlainText()
            assert len(toasts) == 0

            clock.advance(minutes=2)
            wait_until(
                app,
                lambda: (
                    window is not None
                    and "Su içme zamanı F91" in window.chat.toPlainText()
                    and len(toasts) == 1
                ),
                timeout=15.0,
                desc="F:91 reminder delivered",
                deadline=deadline,
            )
            chat_text = window.chat.toPlainText()
            assert "Artemis (kendiliğinden)" in chat_text
            assert "Hatırlatma" in chat_text
            assert toasts[0].title == "Hatırlatma"
            assert toasts[0].body == "Su içme zamanı F91"

            event_id = reminder_event_id(rem.id)
            audit_path = state_dir / "audit.jsonl"

            def _delivery_persisted() -> bool:
                saved = ProactiveStore(state_dir / "proactive.sqlite3").get_reminder(rem.id)
                if saved is None or saved.state != "delivered":
                    return False
                events = [
                    ev
                    for ev in read_audit_events(audit_path, "proactive_delivery")
                    if ev.get("event_id") == event_id
                ]
                return len(events) == 1

            wait_until(
                app,
                _delivery_persisted,
                timeout=5.0,
                desc="F:91 store ve audit kaydı",
                deadline=deadline,
            )

            saved_rem = ProactiveStore(state_dir / "proactive.sqlite3").get_reminder(rem.id)
            assert saved_rem is not None and saved_rem.state == "delivered"
            events = [
                ev
                for ev in read_audit_events(audit_path, "proactive_delivery")
                if ev.get("event_id") == event_id
            ]
            assert len(events) == 1
            ev0 = events[0]
            assert ev0.get("delivered") is True
            targets = ev0.get("targets")
            assert isinstance(targets, dict)
            assert targets.get("panel") == "ok"
            assert targets.get("toast") == "ok"

            shot = grab_neutralized(app, window, out_dir, "F91_reminder_panel_toast.png")
            return RowResult(
                "F:91",
                "PASS",
                "Enjekte MutableClock (RecordingProvider) ile vade öncesi sessiz kaldı, vade "
                "saatinde sohbete 'Artemis (kendiliğinden)' + 'Hatırlatma' ve yakalanan toast "
                "yüküne (gerçek Action Center yerine) teslim edildi; SQLite ve audit doğrulandı.",
                (shot.name,),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("F:91", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_f92(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    """F:92 — Kapalıyken vadesi geçen hatırlatmanın açılışta gecikmeli ve tek kez teslimi."""
    check_deadline(deadline, "F:92")
    sub = tmp_root / "f92"
    try:
        cfg_path, _ws, state_dir = make_temp_config(
            sub,
            overrides={
                "proactive": {
                    "enabled": True,
                    "poll_seconds": 1.0,
                    "timezone": "Europe/Istanbul",
                    "reminder_grace_hours": 12.0,
                }
            },
        )
        t0 = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
        clock = MutableClock(t0)
        rem = ProactiveStore(state_dir / "proactive.sqlite3").add_reminder(
            "Kaçan hatırlatma F92",
            due_at=t0 + timedelta(minutes=2),
            tainted=False,
            private=False,
            now=t0,
        )

        # Pencere 1: vade öncesi açılışta teslim olmamalı.
        window1: DesktopWindow | None = None
        with (
            fake_provider_bridge(RecordingProvider),
            injected_proactive_clock(clock),
            intercept_toasts("ok") as toasts1,
        ):
            try:
                window1 = open_real_window(app, cfg_path, deadline=deadline)
                _pump_events(app, 1.2, deadline=deadline, stage="F:92 pencere 1")
                assert "Kaçan hatırlatma F92" not in window1.chat.toPlainText()
                assert len(toasts1) == 0
            finally:
                if window1 is not None:
                    close_window_cleanly(app, window1)
        assert_no_leaked_windows(app, "F:92 pencere 1 sonrası", max_visible=2, max_desktop=0)

        # Uygulama kapalıyken saat 10:30 Istanbul'a ilerler (28 dk gecikme, 12 saat pay içinde).
        clock.set(datetime(2026, 10, 15, 7, 30, tzinfo=UTC))

        # Pencere 2: yeniden açılışta gecikmeli etiketle bir kez teslim edilmeli.
        window2: DesktopWindow | None = None
        with (
            fake_provider_bridge(RecordingProvider),
            injected_proactive_clock(clock),
            intercept_toasts("ok") as toasts2,
        ):
            try:
                window2 = open_real_window(app, cfg_path, deadline=deadline)
                wait_until(
                    app,
                    lambda: (
                        window2 is not None
                        and "Kaçan hatırlatma F92" in window2.chat.toPlainText()
                        and len(toasts2) == 1
                    ),
                    timeout=15.0,
                    desc="F:92 missed reminder delivered",
                    deadline=deadline,
                )
                assert "Gecikmeli" in window2.chat.toPlainText()
                assert "Gecikmeli" in toasts2[0].body
                _pump_events(app, 2.2, deadline=deadline, stage="F:92 pencere 2 tekilleştirme")
                assert len(toasts2) == 1
                assert window2.chat.toPlainText().count("Kaçan hatırlatma F92") == 1
                shot = grab_neutralized(
                    app, window2, out_dir, "F92_missed_reminder_delivered_once.png"
                )
            finally:
                if window2 is not None:
                    close_window_cleanly(app, window2)
        assert_no_leaked_windows(app, "F:92 pencere 2 sonrası", max_visible=2, max_desktop=0)

        clock.advance(minutes=5)

        # Pencere 3: ikinci yeniden açılışta yinelenmemeli.
        window3: DesktopWindow | None = None
        with (
            fake_provider_bridge(RecordingProvider),
            injected_proactive_clock(clock),
            intercept_toasts("ok") as toasts3,
        ):
            try:
                window3 = open_real_window(app, cfg_path, deadline=deadline)
                _pump_events(app, 2.2, deadline=deadline, stage="F:92 pencere 3 tekrar yok")
                assert len(toasts3) == 0
                assert "Kaçan hatırlatma F92" not in window3.chat.toPlainText()
            finally:
                if window3 is not None:
                    close_window_cleanly(app, window3)
        assert_no_leaked_windows(app, "F:92 pencere 3 sonrası", max_visible=2, max_desktop=0)

        event_id = reminder_event_id(rem.id)
        events = [
            ev
            for ev in read_audit_events(state_dir / "audit.jsonl", "proactive_delivery")
            if ev.get("event_id") == event_id
        ]
        assert len(events) == 1

        return RowResult(
            "F:92",
            "PASS",
            "Kapat-aç döngüsünde enjekte saat (MutableClock), yakalanan toast ve "
            "RecordingProvider ile vade geçtikten sonraki açılışta 'Gecikmeli' tek kez "
            "teslim edildi; ikinci yeniden açılışta ve sonraki taramalarda tekrarlanmadı.",
            (shot.name,),
        )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("F:92", "FAIL", f"{type(error).__name__}: {error}")


def run_reminders_live_checks(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    """R:153, F:91 ve F:92 hatırlatma masaüstü kabul denetimlerini çalıştırır."""
    results: list[RowResult] = []
    for label, check_fn in (
        ("R:153", _check_r153),
        ("F:91", _check_f91),
        ("F:92", _check_f92),
    ):
        results.append(check_fn(app, tmp_root, out_dir, deadline=deadline))
        assert_no_leaked_windows(app, f"{label} sonrası", max_visible=2, max_desktop=0)
    return results
