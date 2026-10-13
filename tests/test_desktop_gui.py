"""Gerçek PySide pencere testleri yalnız offscreen ve demo runtime kullanır."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

import jarvis.channels.desktop.bridge as bridge_module
from jarvis.brain.agent_loop import RunResult
from jarvis.channels.desktop.app import (
    _STYLE,
    ApprovalDialog,
    DesktopWindow,
    StepBlock,
    build_window,
)
from jarvis.channels.desktop.bridge import Activity, Finished, State, VoiceCommand
from jarvis.channels.desktop.demo import DemoReadTool, DemoWriteTool
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


@pytest.mark.parametrize(
    ("label", "expected_title"),
    [("Demo bellek yazma", "Demo bellek yazma"), ("", "demo_write_memory")],
)
def test_approval_title_is_readable_label_with_raw_name_below(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    expected_title: str,
) -> None:
    # Etiketi olmayan araçta başlık ham ada düşer; ham ad her durumda altta görünür.
    monkeypatch.setattr(DemoWriteTool, "label", label)
    window = _window(app, tmp_path)
    try:
        assert window._submit_text("örnek akış")
        _wait(app, lambda: isinstance(window._approval_dialog, ApprovalDialog))
        dialog = window._approval_dialog
        assert dialog is not None
        title = dialog.findChild(QLabel, "title")
        raw_name = dialog.findChild(QLabel, "rawToolName")
        assert title is not None and title.text() == expected_title
        assert raw_name is not None and raw_name.text() == "demo_write_memory"
        assert dialog.no_button.isDefault()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        _wait(app, lambda: not window._busy)
        assert "Onay verilmedi" in window.chat.toPlainText()
    finally:
        _close_cleanly(app, window)


def test_step_block_shows_verified_lines_and_hides_internal_until_details(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DemoReadTool, "label", "Demo örnek okuma")
    monkeypatch.setattr(DemoReadTool, "done_label", "Demo örneği okundu")
    monkeypatch.setattr(DemoWriteTool, "label", "Demo bellek yazma")
    window = _window(app, tmp_path)
    try:
        request = "gui-steps"
        window._active_request = request
        window._busy = True
        events = [
            Activity(request, "turn-1", 1, "turn_start", {"turn_id": "turn-1"}),
            Activity(request, "turn-1", 2, "tool_requested", {"tool": "demo_read_sample"}),
            Activity(
                request,
                "turn-1",
                3,
                "tool_result",
                {"tool": "demo_read_sample", "result": {"status": "ok", "success": True}},
            ),
            Activity(
                request,
                "turn-1",
                4,
                "tool_result",
                {"tool": "demo_write_memory", "result": {"status": "sent", "success": False}},
            ),
            # Tutarsız kayıt (ok ama success yok) başarı gibi sunulmaz.
            Activity(
                request,
                "turn-1",
                5,
                "tool_result",
                {"tool": "demo_ghost", "result": {"status": "ok", "success": False}},
            ),
            Activity(request, "turn-1", 6, "turn_end", {"stop_reason": "completed"}),
        ]
        for event in events:
            window._handle_event(event)
        result = RunResult(text="Demo tamam.", stop_reason="completed", turn_id="turn-1")
        window._handle_event(Finished(request, result))

        blocks = window.findChildren(StepBlock)
        assert len(blocks) == 1
        block = blocks[0]
        assert not block.isHidden()
        visible = [label.text() for label, _line, _activity in block._rows if not label.isHidden()]
        assert visible == [
            "Demo örneği okundu ✓",
            "Demo bellek yazma: komut gönderildi, sonucu doğrulanamadı",
            "demo_ghost başarısız ✗",
        ]
        assert [text for text in visible if "✓" in text] == ["Demo örneği okundu ✓"]
        hidden = [label.text() for label, _line, _activity in block._rows if label.isHidden()]
        assert "İstek alındı" in hidden and "Yanıt hazır" in hidden

        # Adım bloğu yanıt balonunun hemen üstünde, aynı satırda durur.
        bubble, speaker, _text = window.chat._bubbles[-1]
        assert speaker == "Jarvis"
        bubble_row = bubble.parentWidget()
        column = block.parentWidget().layout()
        assert column.indexOf(block) < column.indexOf(bubble_row)

        block._toggle.click()
        assert all(not label.isHidden() for label, _line, _activity in block._rows)
        tool_rows = [
            label for label, _line, activity in block._rows
            if activity.fields.get("tool") == "demo_read_sample"
        ]
        assert "Ham araç adı: demo_read_sample" in tool_rows[0].toolTip()
        assert "demo_read_sample" not in visible[0]
        block._toggle.click()
        assert all(
            label.isHidden() == line.internal for label, line, _activity in block._rows
        )
    finally:
        window._busy = False
        window._active_request = None
        _close_cleanly(app, window)


def test_status_label_is_plain_text_without_button_rule(app: QApplication, tmp_path: Path) -> None:
    window = _window(app, tmp_path)
    try:
        rule = re.search(r"QLabel#status\s*\{([^}]*)\}", _STYLE)
        assert rule is not None
        body = rule.group(1)
        assert "border: none" in body
        assert "background: transparent" in body
        assert "border-radius" not in body
        # Eski sabit 34 px hap görünümü kalkmıştır.
        assert window.status_label.maximumHeight() > 34
        assert window.status_label.text() == "Hazır"
    finally:
        _close_cleanly(app, window)


@pytest.mark.parametrize(
    ("state", "text", "dot_color"),
    [
        ("listening", "Dinliyor", "#53D6C7"),
        ("thinking", "Düşünüyor", "#FFD78C"),
        ("speaking", "Konuşuyor", "#53D6C7"),
        ("off", "Ses kapalı", "#718096"),
        ("bilinmeyen", "Ses kapalı", "#718096"),
    ],
)
def test_voice_state_text_and_dot_follow_the_state(
    app: QApplication, tmp_path: Path, state: str, text: str, dot_color: str
) -> None:
    window = _window(app, tmp_path)
    try:
        window._show_voice_state(state)
        assert window.voice_state.text() == text
        assert dot_color in window.voice_dot.styleSheet()
    finally:
        _close_cleanly(app, window)


def test_voice_command_adds_user_voice_bubble_and_marks_busy(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    try:
        window._handle_event(VoiceCommand("voice-gui", "dosyaları listele"))
        assert window._busy
        assert window._active_request == "voice-gui"
        assert window.chat._messages[-1] == ("Siz (ses)", "dosyaları listele")
        assert not window.prompt.isEnabled()
        window._handle_event(
            Finished("voice-gui", RunResult(text="Tamam.", stop_reason="completed", turn_id="t"))
        )
        window._handle_event(State("ready", "Hazır.", "voice-gui"))
        assert not window._busy
    finally:
        window._busy = False
        window._active_request = None
        _close_cleanly(app, window)


def test_demo_mode_never_builds_or_starts_the_voice_controller(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    def forbidden(*args: Any, **_kwargs: Any) -> None:
        calls.append(args)
        raise AssertionError("Demo modu ses denetleyicisi kurmamalı")

    monkeypatch.setattr(bridge_module, "build_voice_controller", forbidden)
    window = build_window(tmp_path / "ignored.yaml", demo=True, voice=True, app=app)
    window.show()
    try:
        _wait(app, lambda: window.status_label.text() == "Hazır")
        assert calls == []
        assert window._voice_on_ready is False
        assert not window.voice_button.isEnabled()
    finally:
        _close_cleanly(app, window)
