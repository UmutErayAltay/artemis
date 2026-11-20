"""Masaüstü kabul denetimleri: Dikte ayarları ve kısayol çakışması (Y:40, Y:41)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from artemis.channels.desktop.app import DesktopWindow
from artemis.config.settings import load_settings
from scripts.kabul_masaustu_checks.common import (
    GlobalTimeoutError,
    RecordingProvider,
    RowResult,
    assert_no_leaked_windows,
    check_deadline,
    close_window_cleanly,
    fake_global_hotkeys,
    fake_provider_bridge,
    grab_neutralized,
    make_temp_config,
    open_real_window,
    select_settings_section,
    wait_until,
    wait_window_ready,
)

_CHECK_ERRORS = (
    AssertionError,
    OSError,
    ValueError,
    RuntimeError,
    TimeoutError,
    yaml.YAMLError,
    ValidationError,
)


def _check_y40(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    check_deadline(deadline, "Y:40")
    sub = tmp_root / "y40"
    window: DesktopWindow | None = None
    try:
        cfg, _ws, _state = make_temp_config(
            sub,
            overrides={
                "dictation": {"enabled": False, "hotkey": "ctrl+alt+d"},
                "voice": {"listening_hotkey": ""},
            },
        )
        with fake_provider_bridge(RecordingProvider), fake_global_hotkeys() as hotkeys:
            window = open_real_window(app, cfg, deadline=deadline)
            select_settings_section(app, window, "Ses")
            panel = window.settings_panel

            panel.sections.scroll_area.ensureWidgetVisible(panel.dictation_hotkey)
            app.processEvents()

            assert panel.dictation_enabled.isVisible()
            assert panel.dictation_enabled.text() == "Dikteyi kullan"
            assert not panel.dictation_enabled.isChecked()
            assert panel.dictation_hotkey.isVisible()
            assert panel.dictation_hotkey.accessibleName() == "Dikte kısayolu"

            voice_page = panel.stack.currentWidget()
            assert voice_page is not None
            assert any(
                label.isVisible() and label.text() == "Dikte tuşu"
                for label in voice_page.findChildren(QLabel)
            )

            QTest.mouseClick(
                panel.dictation_enabled,
                Qt.MouseButton.LeftButton,
                pos=QPoint(12, max(1, panel.dictation_enabled.height() // 2)),
            )
            app.processEvents()
            assert panel.dictation_enabled.isChecked()

            panel.sections.scroll_area.ensureWidgetVisible(panel.dictation_hotkey)
            window.activateWindow()
            app.processEvents()
            panel.dictation_hotkey.setFocus()
            panel.dictation_hotkey.clear()
            QTest.keyClicks(panel.dictation_hotkey, "ctrl+alt+k")
            app.processEvents()
            assert panel.dictation_hotkey.text() == "ctrl+alt+k"

            assert panel.save_button.isVisible()
            assert panel.save_button.text() == "Kaydet"
            old_rt = window.bridge._runtime
            QTest.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)

            wait_until(
                app,
                lambda: (
                    window is not None
                    and window.bridge._runtime is not None
                    and window.bridge._runtime is not old_rt
                ),
                desc="Y:40 runtime restart",
                deadline=deadline,
            )
            wait_window_ready(app, window, deadline=deadline)

            saved = load_settings(cfg)
            assert saved.dictation.enabled is True
            assert saved.dictation.hotkey == "ctrl+alt+k"
            assert window._dictation_hotkey is not None
            assert "ctrl+alt+k" in hotkeys.started
            assert "Ctrl+Alt+K" in window.chat.description.text()

            panel.sections.scroll_area.ensureWidgetVisible(panel.dictation_hotkey)
            app.processEvents()
            shot = grab_neutralized(app, window, out_dir, "Y40_dictation_settings.png")
            return RowResult(
                "Y:40",
                "PASS",
                "Ses sayfasında dikte alanları ('Dikteyi kullan', 'Dikte tuşu', 'Dikte kısayolu') "
                "görünür doğrulandı; dictation.enabled=True ve 'ctrl+alt+k' kaydedilip runtime "
                "yeniden kuruldu ve karşılama metninde 'Ctrl+Alt+K' göründü (GlobalHotkey.start "
                "sahte kesmeyle izlendi, gerçek Win32 RegisterHotKey / mikrofon / sesli dikte "
                "çalıştırılmadı ve RecordingProvider kullanıldı).",
                (shot.name,),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("Y:40", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def _check_y41(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> RowResult:
    check_deadline(deadline, "Y:41")
    sub = tmp_root / "y41"
    window: DesktopWindow | None = None
    try:
        cfg, _ws, _state = make_temp_config(
            sub,
            overrides={
                "voice": {"listening_hotkey": "ctrl+space"},
                "dictation": {"enabled": True, "hotkey": "ctrl+alt+d"},
            },
        )
        with fake_provider_bridge(RecordingProvider), fake_global_hotkeys():
            window = open_real_window(app, cfg, deadline=deadline)
            select_settings_section(app, window, "Ses")
            panel = window.settings_panel

            panel.sections.scroll_area.ensureWidgetVisible(panel.dictation_hotkey)
            window.activateWindow()
            app.processEvents()
            panel.dictation_hotkey.setFocus()
            panel.dictation_hotkey.clear()
            QTest.keyClicks(panel.dictation_hotkey, "ctrl+space")
            app.processEvents()
            assert panel.dictation_hotkey.text() == "ctrl+space"
            assert panel.save_button.isVisible()

            QTest.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
            app.processEvents()

            expected_notice = (
                "Dikte kısayolu ses kısayoluyla aynı olamaz; başka bir tuş seç."
            )
            assert panel._failed is True
            assert panel.has_unsaved is True
            assert panel.retry_button.isVisible()
            assert panel.notice.text() == expected_notice
            assert load_settings(cfg).dictation.hotkey == "ctrl+alt+d"

            shot = grab_neutralized(app, window, out_dir, "Y41_dictation_hotkey_clash.png")
            return RowResult(
                "Y:41",
                "PASS",
                "Ses kısayoluyla çakışan 'ctrl+space' dikte kısayolu Kaydet ile reddedildi, "
                "beklenen Türkçe uyarı ve Tekrar dene düğmesi göründü, diskte 'ctrl+alt+d' "
                "değişmeden korundu (GlobalHotkey.start sahtelendi; gerçek Ctrl+Space OS "
                "yakalaması yapılmadı ve RecordingProvider kullanıldı).",
                (shot.name,),
            )
    except GlobalTimeoutError:
        raise
    except _CHECK_ERRORS as error:
        return RowResult("Y:41", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        if window is not None:
            close_window_cleanly(app, window)


def run_dictation_panel_checks(
    app: QApplication,
    tmp_root: Path,
    out_dir: Path,
    *,
    deadline: float | None = None,
) -> list[RowResult]:
    """Dikte ayarları ve kısayol çakışması kabul senaryolarını geçici klasörlerde çalıştırır."""
    results: list[RowResult] = []
    for label, check_fn in (
        ("Y:40", _check_y40),
        ("Y:41", _check_y41),
    ):
        results.append(check_fn(app, tmp_root, out_dir, deadline=deadline))
        assert_no_leaked_windows(app, f"{label} sonrası", max_visible=2, max_desktop=0)
    return results
