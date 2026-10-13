"""Gerçek PySide pencere testleri yalnız offscreen ve demo runtime kullanır."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from jarvis.channels.desktop.app import ApprovalDialog, DesktopWindow, build_window
from jarvis.channels.desktop.bridge import State
from jarvis.channels.runtime import Runtime


@pytest.fixture
def app() -> QApplication:
    current = QApplication.instance()
    instance = current if isinstance(current, QApplication) else QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def _wait(app: QApplication, condition: Any, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("GUI durumu zamanında oluşmadı")


def _window(app: QApplication, tmp_path: Path) -> DesktopWindow:
    window = build_window(tmp_path / "ignored.yaml", demo=True, app=app)
    window.show()
    _wait(app, lambda: window.status_label.text() == "Hazır")
    return window


def _close_cleanly(app: QApplication, window: DesktopWindow) -> None:
    if not window.bridge.stopped:
        window._request_exit()
    _wait(app, lambda: window.bridge.stopped)
    _wait(app, lambda: window._temporary is None)


def test_demo_window_submits_plain_text_and_cleans_temporary_root(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    temporary = window._temporary
    assert temporary is not None
    temp_path = Path(temporary.name)
    try:
        html_text = '<img src="https://invalid.example/tracker">'
        assert window._submit_text(html_text)
        _wait(app, lambda: not window._busy)
        visible = window.chat.toPlainText()
        assert html_text in visible
        assert "DEMO ·" in visible
        assert window.model_value.text() == "demo"
    finally:
        _close_cleanly(app, window)
    assert not temp_path.exists()


def test_message_bubbles_expand_and_follow_the_available_chat_width(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    try:
        window.chat.append("Jarvis", "Yanıt " + "uzun bir metin " * 12)
        bubble, speaker, text = window.chat._bubbles[-1]
        assert speaker == "Jarvis"
        assert len(text) > 100
        assert 320 <= bubble.width() <= 520
        window.resize(720, 650)
        app.processEvents()
        assert bubble.width() <= window.chat.width() - 28
    finally:
        _close_cleanly(app, window)


@pytest.mark.parametrize("dismiss", ["enter", "escape", "close"])
def test_approval_defaults_to_no_and_dialog_dismissal_cannot_approve(
    app: QApplication, tmp_path: Path, dismiss: str
) -> None:
    window = _window(app, tmp_path)
    heartbeat = 0
    timer = QTimer()

    def tick() -> None:
        nonlocal heartbeat
        heartbeat += 1

    timer.timeout.connect(tick)
    timer.start(5)
    try:
        assert window._submit_text("örnek akış")
        _wait(app, lambda: isinstance(window._approval_dialog, ApprovalDialog))
        dialog = window._approval_dialog
        assert dialog is not None
        assert dialog.no_button.isDefault()
        QTest.qWait(60)
        assert heartbeat > 0
        if dismiss == "enter":
            QTest.keyClick(dialog, Qt.Key.Key_Return)
        elif dismiss == "escape":
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
        else:
            dialog.close()
        _wait(app, lambda: not window._busy)
        runtime: Runtime | None = window.bridge._runtime
        assert runtime is not None
        assert not runtime.context.services["demo_records"].get("son_örnek")
        assert "Onay verilmedi" in window.chat.toPlainText()
        assert not window.bridge.decide(
            dialog.item.request_id, dialog.item.turn_id, dialog.item.approval_id, True
        )
    finally:
        timer.stop()
        _close_cleanly(app, window)


def test_explicit_yes_runs_only_the_in_memory_demo_tool(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    try:
        assert window._submit_text("örnek akış")
        _wait(app, lambda: isinstance(window._approval_dialog, ApprovalDialog))
        dialog = window._approval_dialog
        assert dialog is not None
        assert dialog.item.tool_name == "demo_write_memory"
        assert dialog.item.arguments == {
            "key": "son_örnek",
            "value": "geçici demo kaydı",
        }
        assert "dış bir içerik okunduktan sonra" in dialog.item.notice
        QTest.mouseClick(dialog.yes_button, Qt.MouseButton.LeftButton)
        _wait(app, lambda: not window._busy)
        runtime: Runtime | None = window.bridge._runtime
        assert runtime is not None
        assert runtime.context.services["demo_records"]["son_örnek"] == (
            "geçici demo kaydı"
        )
        assert "yalnız geçici demo belleği güncellendi" in window.chat.toPlainText()
    finally:
        _close_cleanly(app, window)


def test_overlay_shares_bridge_and_active_state(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    try:
        window.overlay.show()
        window.overlay.input.setPlainText("sıradan demo girdisi")
        window._submit_overlay()
        assert window.overlay.input.isEnabled() is False
        assert window.prompt.isEnabled() is False
        _wait(app, lambda: not window._busy)
        assert "sıradan demo girdisi" in window.chat.toPlainText()
    finally:
        _close_cleanly(app, window)


def test_close_exits_cleanly_when_tray_is_unavailable(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    if window._tray is not None:
        _close_cleanly(app, window)
        pytest.skip("Bu masaüstünde tepsi kullanılabilir.")
    window.close()
    _wait(app, lambda: window._temporary is None)
    assert window._temporary is None


def test_stale_ready_events_do_not_enable_or_reset_the_active_turn(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    try:
        window._busy = True
        window._active_request = "current-request"
        window._set_controls()
        window._handle_event(State("ready", "Hazır · stale-model", "old-request"))
        window._handle_event(State("ready", "Hazır · late-startup"))
        assert window._busy
        assert window._active_request == "current-request"
        assert not window.prompt.isEnabled()
        assert window.model_value.text() == "demo"
    finally:
        window._busy = False
        window._active_request = None
        _close_cleanly(app, window)


def test_approval_timeout_closes_stale_dialog_without_waiting_for_user(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    heartbeat = 0
    timer = QTimer()

    def tick() -> None:
        nonlocal heartbeat
        heartbeat += 1

    timer.timeout.connect(tick)
    timer.start(5)
    try:
        runtime: Runtime | None = window.bridge._runtime
        assert runtime is not None
        runtime.context.settings = runtime.context.settings.model_copy(
            update={"max_seconds": 1.0}
        )
        assert window._submit_text("örnek akış")
        _wait(app, lambda: isinstance(window._approval_dialog, ApprovalDialog))
        dialog = window._approval_dialog
        assert dialog is not None
        _wait(app, lambda: not window._busy, timeout=3)
        assert dialog.isVisible() is False
        assert heartbeat > 0
        runtime = window.bridge._runtime
        assert runtime is not None
        assert not runtime.context.services["demo_records"].get("son_örnek")
        assert "Süre sınırı" in window.chat.toPlainText() or "İşlem" in window.chat.toPlainText()
    finally:
        timer.stop()
        _close_cleanly(app, window)

