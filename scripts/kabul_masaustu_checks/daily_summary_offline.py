"""Gün sonu özeti masaüstü kabul denetimleri (çevrimdışı: Y:195, Y:196, Y:198, Y:200)."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import artemis.channels.desktop.bridge as bridge_mod
from artemis.channels.desktop.text_viewer import TextViewerDialog
from artemis.proactive.store import ProactiveStore
from scripts.kabul_masaustu_checks.common import (
    DEMO_VAULT_DIR,
    GlobalTimeoutError,
    MutableClock,
    RecordingProvider,
    RowResult,
    assert_no_leaked_windows,
    check_deadline,
    close_window_cleanly,
    drive_modal,
    fake_provider_bridge,
    grab_neutralized,
    injected_proactive_clock,
    intercept_toasts,
    make_temp_config,
    open_real_window,
    read_audit_events,
    remaining_timeout,
    wait_until,
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
    yaml.YAMLError,
    ValidationError,
)

_SYNTHETIC_DAY = "2026-10-15"
_SECRET_MARKERS = (
    "GIZLI-SOHBET-METNI-DS",
    "GIZLI-DOSYA-ICERIGI-DS",
    "GIZLI-SONUC-DS",
    "GIZLI-SILME-DS",
)


class _RecordingTelegramClient:
    """Ağ çağrısı olmadan Telegram gönderim girişimlerini kaydeden sahte istemci."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, str]] = []

    def send_message(self, chat_id: int, text: str, *, kind: str = "proactive") -> None:
        self.sent.append((chat_id, text, kind))

    def close(self) -> None:
        return None


@contextlib.contextmanager
def _fake_telegram_client() -> Iterator[_RecordingTelegramClient]:
    """Masaüstü köprüsünde `build_telegram_client` çağrısını sahte istemciyle değiştirir."""
    client = _RecordingTelegramClient()
    original = bridge_mod.build_telegram_client

    def _patched_build(*_args: Any, **_kwargs: Any) -> _RecordingTelegramClient:
        return client

    bridge_mod.build_telegram_client = _patched_build  # type: ignore[assignment]
    try:
        yield client
    finally:
        bridge_mod.build_telegram_client = original


def _write_synthetic_audit(audit_path: Path, state_dir: Path) -> None:
    """Sabit 2026-10-15 günü için sentetik denetim satırları ve teslim edilmiş hatırlatma yazar."""
    rows = [
        {
            "event": "turn_start",
            "timestamp": "2026-10-15T10:00:00+00:00",
            "turn_id": "t1",
            "input": "GIZLI-SOHBET-METNI-DS",
        },
        {
            "event": "tool_requested",
            "timestamp": "2026-10-15T10:01:00+00:00",
            "turn_id": "t1",
            "tool": "create_file",
            "arguments": {
                "path": "notlar/gunluk_deneme.txt",
                "content": "GIZLI-DOSYA-ICERIGI-DS",
            },
        },
        {
            "event": "tool_result",
            "timestamp": "2026-10-15T10:02:00+00:00",
            "turn_id": "t1",
            "tool": "create_file",
            "result": {"success": True, "status": "ok", "message": "GIZLI-SONUC-DS"},
        },
        {
            "event": "tool_requested",
            "timestamp": "2026-10-15T10:03:00+00:00",
            "turn_id": "t1",
            "tool": "delete_file",
            "arguments": {"path": "notlar/eski.txt"},
        },
        {
            "event": "tool_result",
            "timestamp": "2026-10-15T10:04:00+00:00",
            "turn_id": "t1",
            "tool": "delete_file",
            "result": {"success": True, "status": "ok", "message": "GIZLI-SILME-DS"},
        },
        {
            "event": "turn_end",
            "timestamp": "2026-10-15T10:05:00+00:00",
            "turn_id": "t1",
            "stop_reason": "completed",
            "usage": {"cost_usd": "0.00"},
        },
    ]
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    due_at = datetime(2026, 10, 15, 11, 0, tzinfo=UTC)
    store = ProactiveStore(state_dir / "proactive.sqlite3")
    if not store.claim("reminder:1", due_at, "reminder", "kabul-seed", due_at):
        raise RuntimeError("Sentetik hatırlatma oluşumu talep edilemedi.")
    store.finish("reminder:1", due_at, "delivered", "ok", due_at)


def _daily_summary_audit_row(audit_path: Path) -> dict[str, Any] | None:
    for row in read_audit_events(audit_path, "proactive_delivery"):
        if row.get("event_id") == "daily_summary":
            return row
    return None


def _check_y195_y196(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    check_deadline(deadline, "Y:195/Y:196")
    sub = tmp_root / "y195_196"
    vault_dir = sub / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    window: DesktopWindow | None = None
    results: list[RowResult] = []
    try:
        cfg, _ws, state_dir = make_temp_config(
            sub,
            overrides={
                "proactive": {
                    "enabled": True,
                    "poll_seconds": 1.0,
                    "timezone": "Europe/Istanbul",
                    "telegram": True,
                },
                "daily_summary": {"enabled": True, "at": "22:30"},
                "memory": {"vault_path": str(vault_dir), "encrypt_at_rest": False},
                "telegram": {"chat_id": 123456, "run_in_desktop": False},
            },
        )
        audit_path = state_dir / "audit.jsonl"
        _write_synthetic_audit(audit_path, state_dir)
        clock = MutableClock(datetime(2026, 10, 15, 19, 31, tzinfo=UTC))

        with (
            _fake_telegram_client() as fake_telegram,
            fake_provider_bridge(RecordingProvider),
            injected_proactive_clock(clock),
            intercept_toasts("ok") as toasts,
        ):
            window = open_real_window(app, cfg, deadline=deadline)
            wait_until(
                app,
                lambda: (
                    window is not None
                    and window.routine_bar.isVisible()
                    and "Günün özeti hazır" in window.chat.toPlainText()
                    and len(toasts) == 1
                    and _daily_summary_audit_row(audit_path) is not None
                ),
                timeout=15.0,
                desc="Y:195 gün özeti bildirimi",
                deadline=deadline,
            )

            try:
                assert toasts[0].title == "Günün özeti hazır"
                assert _SYNTHETIC_DAY in toasts[0].body
                assert window.routine_run_button.text() == "Aç"
                assert window.summary_vault_button.isVisible()
                assert fake_telegram.sent == []

                audit_row = _daily_summary_audit_row(audit_path)
                assert audit_row is not None
                assert audit_row.get("delivered") is True
                targets = audit_row.get("targets")
                assert isinstance(targets, dict)
                assert targets.get("panel") == "ok"
                assert targets.get("toast") == "ok"
                assert "telegram" not in targets

                shot195 = grab_neutralized(
                    app,
                    window,
                    out_dir,
                    "Y195_daily_summary_notice.png",
                    real_roots={vault_dir: DEMO_VAULT_DIR},
                )
                results.append(
                    RowResult(
                        "Y:195",
                        "PASS",
                        "Sentetik audit.jsonl, enjekte MutableClock, yakalanan toast, sahte "
                        "Telegram istemcisi, geçici kasa ve RecordingProvider ile gün özeti "
                        "bildirimi panel/toast hedeflerine teslim edildi; Telegram'a hiç gitmedi.",
                        (shot195.name,),
                    )
                )
            except GlobalTimeoutError:
                raise
            except _CHECK_ERRORS as error:
                results.append(RowResult("Y:195", "FAIL", f"{type(error).__name__}: {error}"))

            check_deadline(deadline, "Y:196")
            try:
                summary_file = state_dir / "gunluk" / f"{_SYNTHETIC_DAY}.md"
                assert summary_file.is_file(), f"Özet dosyası bulunamadı: {summary_file}"
                summary_text = summary_file.read_text(encoding="utf-8")
                for expected in (
                    f"# Gün özeti · {_SYNTHETIC_DAY}",
                    "sohbet 1",
                    "create_file: 1",
                    "delete_file: 1",
                    "notlar/gunluk_deneme.txt (oluşturuldu)",
                    "notlar/eski.txt (silindi)",
                    "iletildi 1",
                ):
                    assert expected in summary_text, f"Özet dosyasında eksik ifade: {expected!r}"
                for forbidden in (*_SECRET_MARKERS, "_gecikmeli:"):
                    assert forbidden not in summary_text, (
                        f"Özet dosyasında istenmeyen ifade: {forbidden!r}"
                    )

                captured_shots: list[Path] = []

                def _inspect_viewer(modal: Any) -> None:
                    assert isinstance(modal, TextViewerDialog)
                    assert modal.windowTitle() == "Gün özeti"
                    viewer_text = modal.browser.toPlainText()
                    for expected in (
                        f"Gün özeti · {_SYNTHETIC_DAY}",
                        "notlar/gunluk_deneme.txt (oluşturuldu)",
                        "notlar/eski.txt (silindi)",
                    ):
                        assert expected in viewer_text, (
                            f"Görüntüleyicide eksik ifade: {expected!r}"
                        )
                    for secret in _SECRET_MARKERS:
                        assert secret not in viewer_text, (
                            f"Görüntüleyicide gizli metin sızıntısı: {secret!r}"
                        )
                    shot196 = grab_neutralized(
                        app,
                        modal,
                        out_dir,
                        "Y196_daily_summary_viewer.png",
                        real_roots={vault_dir: DEMO_VAULT_DIR},
                    )
                    captured_shots.append(shot196)
                    QTest.mouseClick(modal.close_button, Qt.MouseButton.LeftButton)

                drive_modal(
                    app,
                    lambda: QTest.mouseClick(window.routine_run_button, Qt.MouseButton.LeftButton),
                    _inspect_viewer,
                )
                assert len(captured_shots) == 1
                results.append(
                    RowResult(
                        "Y:196",
                        "PASS",
                        "Sentetik audit.jsonl, enjekte MutableClock, yakalanan toast ve "
                        "RecordingProvider ile üretilen 2026-10-15.md özeti ve Aç "
                        "görüntüleyicisi gizli metinleri sızdırmadan tüm sayımları gösterdi.",
                        (captured_shots[0].name,),
                    )
                )
            except GlobalTimeoutError:
                raise
            except _CHECK_ERRORS as error:
                results.append(RowResult("Y:196", "FAIL", f"{type(error).__name__}: {error}"))

        return results
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        if not results:
            results.append(RowResult("Y:195", "FAIL", f"{type(error).__name__}: {error}"))
        if len(results) < 2:
            results.append(RowResult("Y:196", "FAIL", f"{type(error).__name__}: {error}"))
        return results
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_y198(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    check_deadline(deadline, "Y:198")
    sub = tmp_root / "y198"
    window: DesktopWindow | None = None
    try:
        cfg, _ws, state_dir = make_temp_config(
            sub,
            overrides={
                "memory": {"vault_path": None, "encrypt_at_rest": False},
                "proactive": {
                    "enabled": True,
                    "poll_seconds": 1.0,
                    "timezone": "Europe/Istanbul",
                },
                "daily_summary": {"enabled": True, "at": "22:30"},
            },
        )
        _write_synthetic_audit(state_dir / "audit.jsonl", state_dir)
        clock = MutableClock(datetime(2026, 10, 15, 19, 31, tzinfo=UTC))

        with (
            fake_provider_bridge(RecordingProvider),
            injected_proactive_clock(clock),
            intercept_toasts("ok") as toasts,
        ):
            window = open_real_window(app, cfg, deadline=deadline)
            wait_until(
                app,
                lambda: (
                    window is not None
                    and window.routine_bar.isVisible()
                    and window.routine_run_button.text() == "Aç"
                    and len(toasts) == 1
                ),
                timeout=15.0,
                desc="Y:198 kasasız özet bildirimi",
                deadline=deadline,
            )
            assert not window.summary_vault_button.isVisible()
            assert window.summary_vault_button.isHidden()

            shot = grab_neutralized(app, window, out_dir, "Y198_daily_summary_no_vault_button.png")
            return RowResult(
                "Y:198",
                "PASS",
                "Sentetik audit.jsonl, enjekte saat, yakalanan toast ve RecordingProvider ile "
                "kasa kapalıyken Aç düğmesi göründü ve Vault'a ekle düğmesi gizli kaldı.",
                (shot.name,),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("Y:198", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_y200(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    check_deadline(deadline, "Y:200")
    shot_name: str | None = None

    # A durumu: briefing_grace_hours=2.0 içinde (> 5 dk gecikmeli)
    sub_in = tmp_root / "y200_in"
    window_in: DesktopWindow | None = None
    try:
        cfg_in, _ws_in, state_in = make_temp_config(
            sub_in,
            overrides={
                "memory": {"vault_path": None, "encrypt_at_rest": False},
                "proactive": {
                    "enabled": True,
                    "poll_seconds": 1.0,
                    "timezone": "Europe/Istanbul",
                    "briefing_grace_hours": 2.0,
                },
                "daily_summary": {"enabled": True, "at": "22:30"},
            },
        )
        _write_synthetic_audit(state_in / "audit.jsonl", state_in)
        clock_in = MutableClock(datetime(2026, 10, 15, 20, 15, tzinfo=UTC))
        summary_in = state_in / "gunluk" / f"{_SYNTHETIC_DAY}.md"

        with (
            fake_provider_bridge(RecordingProvider),
            injected_proactive_clock(clock_in),
            intercept_toasts("ok") as toasts_in,
        ):
            window_in = open_real_window(app, cfg_in, deadline=deadline)
            wait_until(
                app,
                lambda: (
                    window_in is not None
                    and window_in.routine_bar.isVisible()
                    and summary_in.is_file()
                    and len(toasts_in) == 1
                ),
                timeout=15.0,
                desc="Y:200 grace içi gecikmeli özet",
                deadline=deadline,
            )
            text_in = summary_in.read_text(encoding="utf-8")
            assert "_gecikmeli: bu özet 2026-10-15 23:15 civarı üretildi_" in text_in
            assert len(toasts_in) == 1
            assert toasts_in[0].title == "Günün özeti hazır"
            shot_in = grab_neutralized(app, window_in, out_dir, "Y200_daily_summary_late.png")
            shot_name = shot_in.name
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("Y:200", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window_in is not None:
            close_window_cleanly(app, window_in)
    assert_no_leaked_windows(app, "Y:200-in sonrası", max_visible=2, max_desktop=0)

    # B durumu: briefing_grace_hours=2.0 dışında (2s15dk sonra)
    check_deadline(deadline, "Y:200-out")
    sub_out = tmp_root / "y200_out"
    window_out: DesktopWindow | None = None
    try:
        cfg_out, _ws_out, state_out = make_temp_config(
            sub_out,
            overrides={
                "memory": {"vault_path": None, "encrypt_at_rest": False},
                "proactive": {
                    "enabled": True,
                    "poll_seconds": 1.0,
                    "timezone": "Europe/Istanbul",
                    "briefing_grace_hours": 2.0,
                },
                "daily_summary": {"enabled": True, "at": "22:30"},
            },
        )
        _write_synthetic_audit(state_out / "audit.jsonl", state_out)
        clock_out = MutableClock(datetime(2026, 10, 15, 21, 45, tzinfo=UTC))
        summary_out = state_out / "gunluk" / f"{_SYNTHETIC_DAY}.md"

        with (
            fake_provider_bridge(RecordingProvider),
            injected_proactive_clock(clock_out),
            intercept_toasts("ok") as toasts_out,
        ):
            window_out = open_real_window(app, cfg_out, deadline=deadline)
            pump_seconds = remaining_timeout(deadline, 2.3, "Y:200 grace dışı bekleme")
            end_pump = time.monotonic() + pump_seconds
            while time.monotonic() < end_pump:
                app.processEvents()
                time.sleep(0.02)
            app.processEvents()

            assert not summary_out.exists(), f"Süre payı dışında özet yazılmamalıydı: {summary_out}"
            assert not window_out.routine_bar.isVisible()
            assert len(toasts_out) == 0
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult(
            "Y:200",
            "FAIL",
            f"{type(error).__name__}: {error}",
            (shot_name,) if shot_name else (),
        )
    finally:
        if window_out is not None:
            close_window_cleanly(app, window_out)
    assert_no_leaked_windows(app, "Y:200-out sonrası", max_visible=2, max_desktop=0)

    return RowResult(
        "Y:200",
        "PASS",
        "Sentetik audit.jsonl, enjekte saat, yakalanan toast ve RecordingProvider ile "
        "2 saatlik pay içinde gecikmeli özet üretildi; pay dışında dosya ve bildirim oluşmadı.",
        (shot_name,) if shot_name else (),
    )


def run_daily_summary_offline_checks(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    """Çevrimdışı gün sonu özeti kabul senaryolarını (Y:195, Y:196, Y:198, Y:200) çalıştırır."""
    results: list[RowResult] = []
    results.extend(_check_y195_y196(app, tmp_root, out_dir, deadline=deadline))
    assert_no_leaked_windows(app, "Y:195/Y:196 sonrası", max_visible=2, max_desktop=0)

    results.append(_check_y198(app, tmp_root, out_dir, deadline=deadline))
    assert_no_leaked_windows(app, "Y:198 sonrası", max_visible=2, max_desktop=0)

    results.append(_check_y200(app, tmp_root, out_dir, deadline=deadline))
    assert_no_leaked_windows(app, "Y:200 sonrası", max_visible=2, max_desktop=0)
    return results
