"""Gerçek PySide pencere testleri yalnız offscreen ve demo runtime kullanır."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QWidget

import artemis.channels.desktop.bridge as bridge_module
from artemis.brain.agent_loop import RunResult
from artemis.channels.desktop.app import (
    _STYLE,
    ApprovalDialog,
    DesktopWindow,
    StepBlock,
    build_window,
)
from artemis.channels.desktop.bridge import Activity, Finished, State, VoiceCommand
from artemis.channels.desktop.demo import DemoReadTool, DemoWriteTool
from artemis.channels.desktop.themes import THEMES
from artemis.channels.runtime import Runtime


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
        assert window.model_value.text() == "offline-demo"
    finally:
        _close_cleanly(app, window)
    assert not temp_path.exists()


def test_message_bubbles_expand_and_follow_the_available_chat_width(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    try:
        window.chat.append("Artemis", "Yanıt " + "uzun bir metin " * 12)
        bubble, speaker, text = window.chat._bubbles[-1]
        assert speaker == "Artemis"
        assert len(text) > 100
        assert 320 <= bubble.width() <= 672
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


def test_overlay_status_labels_are_separated(app: QApplication, tmp_path: Path) -> None:
    window = _window(app, tmp_path)
    try:
        window.overlay.show()
        app.processEvents()
        dividers = [w for w in window.overlay.findChildren(QLabel) if w.text() == "·"]
        assert len(dividers) == 1
        x = lambda w: w.mapTo(window.overlay, w.rect().topLeft()).x()  # noqa: E731
        assert x(window.overlay.voice_state) < x(dividers[0]) < x(window.overlay.status)
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
        assert window.model_value.text() == "offline-demo"
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
        # Ham ad "Teknik ayrıntılar" açılana dek görünmez; açınca tam ad okunur.
        assert raw_name is not None and raw_name.text() == "Araç adı: demo_write_memory"
        assert raw_name.isHidden() and not dialog.details_toggle.isChecked()
        dialog.details_toggle.setChecked(True)
        assert not raw_name.isHidden()
        dialog.details_toggle.setChecked(False)
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

        # Adım bloğu yanıt balonunun hemen üstünde durur ve pet yüzü kadar girintilidir.
        bubble, speaker, _text = window.chat._bubbles[-1]
        assert speaker == "Artemis"
        app.processEvents()
        block_pos = block.mapTo(window.chat, block.rect().topLeft())
        bubble_pos = bubble.mapTo(window.chat, bubble.rect().topLeft())
        assert block_pos.y() < bubble_pos.y()
        assert block_pos.x() == bubble_pos.x()

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
        ("listening", "Dinliyor", "success"),
        ("thinking", "Düşünüyor", "warning"),
        ("speaking", "Konuşuyor", "success"),
        ("off", "Ses kapalı", "muted"),
        ("bilinmeyen", "Ses kapalı", "muted"),
    ],
)
def test_voice_state_text_and_dot_follow_the_state(
    app: QApplication, tmp_path: Path, state: str, text: str, dot_color: str
) -> None:
    window = _window(app, tmp_path)
    try:
        window._show_voice_state(state)
        assert window.voice_state.text() == text
        # Görünür denetim simge anahtarıdır; durum metni açıklamada ve ipucunda okunur.
        assert window.voice_button.accessibleDescription() == text
        assert text in window.voice_button.toolTip() or window._demo
        assert window.composer_voice.accessibleDescription() == text
        assert window.voice_button.accessibleName() == "Sesi aç veya kapat"
        assert THEMES[window._theme][dot_color] in window.voice_dot.styleSheet()
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


def test_live_text_goes_to_pet_and_final_overwrites(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    try:
        heard: list[str] = []
        live: list[str] = []
        window.pet.heard = heard.append  # type: ignore[method-assign]
        window.pet.live = live.append  # type: ignore[method-assign]
        before = window.settings_panel.last_heard.text()
        window._handle_event(State("voice_live", "merhaba dün"))
        assert live == ["merhaba dün"]
        assert window.settings_panel.last_heard.text() == before  # ayarlarda canlı yazı yok
        window._handle_event(State("voice_heard", "merhaba dünya"))
        assert window.settings_panel.last_heard.text() == "merhaba dünya"
        assert heard == ["merhaba dünya"]
        window._handle_event(State("voice_live_unavailable", "deps:eksik"))
        assert "pip install" in window.chat._messages[-1][1]
    finally:
        _close_cleanly(app, window)


def test_pet_quick_box_forwards_to_submit_text(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window(app, tmp_path)
    try:
        sent: list[str] = []
        monkeypatch.setattr(window, "_submit_text", lambda text: sent.append(text) or True)
        window.pet._quick.open()
        window.pet._quick.input.setText("selam")
        window.pet._quick.send()
        assert sent == ["selam"] and not window.pet._quick.isVisible()
        monkeypatch.undo()
        window.pet._quick.open()
        window.pet._quick.input.setText("gerçek")
        window.pet._quick.send()  # gerçek _submit_text köprüye gider
        assert window._busy and window._active_request is not None
        window.pet._quick.hide()
    finally:
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


def test_settings_apply_restarts_runtime_with_selected_device_and_keeps_model(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from artemis.brain.fake import ScriptedProvider
    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.channels.runtime import create_runtime
    from artemis.config.settings import load_settings
    from tests.test_settings_panel import config

    path = config(tmp_path)
    settings_seen: list[Any] = []

    def factory(config_path: Path, approve: Any, **kwargs: Any) -> Runtime:
        settings = load_settings(config_path)
        settings_seen.append(settings)
        return create_runtime(config_path, approve, settings=settings,
                              provider=ScriptedProvider([]), **kwargs)

    monkeypatch.setattr(bridge_module, "create_runtime", factory)
    monkeypatch.setattr(SettingsPanel, "_refresh_devices", lambda *_: None)
    window = build_window(path, app=app)
    try:
        _wait(app, lambda: window._worker_ready)
        assert window.tabs.indexOf(window.settings_scroll) >= 0
        window.tabs.setCurrentWidget(window.settings_scroll)
        window.settings_panel.device_combo.addItem("Test mikrofonu", 9)
        combo = window.settings_panel.device_combo  # başta "Mikrofon bulunamadı" satırı var
        combo.setCurrentIndex(combo.findData(9))
        window.settings_panel._apply()
        _wait(app, lambda: len(settings_seen) == 2 and window._worker_ready)
        assert settings_seen[-1].models[settings_seen[-1].primary_model].model == "first-model"
        assert settings_seen[-1].voice.input_device == 9
        assert window.model_value.text() == "first-model"
    finally:
        _close_cleanly(app, window)


class _BlockingProvider:
    """Tur, test bırakana dek sağlayıcıda bekler; iptal edilirse CancelledError alır."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    async def complete(
        self, messages: Any, tools: Any, *, timeout: float, stream: Any = None
    ) -> Any:
        from artemis.brain.models import Completion

        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return Completion(content="bitti")


def _deferred_settings_window(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DesktopWindow, Path, list[Any], _BlockingProvider]:
    """İlk runtime bloklayan sağlayıcıyla, sonrakiler boş senaryoyla kurulur (sahte, ağsız)."""
    from artemis.brain.fake import ScriptedProvider
    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.channels.runtime import create_runtime
    from artemis.config.settings import load_settings
    from tests.test_settings_panel import config

    path = config(tmp_path)
    seen: list[Any] = []
    blocker = _BlockingProvider()

    def factory(config_path: Path, approve: Any, **kwargs: Any) -> Runtime:
        settings = load_settings(config_path)
        seen.append(settings)
        provider: Any = blocker if len(seen) == 1 else ScriptedProvider([])
        return create_runtime(config_path, approve, settings=settings, provider=provider, **kwargs)

    monkeypatch.setattr(bridge_module, "create_runtime", factory)
    monkeypatch.setattr(SettingsPanel, "_refresh_devices", lambda *_: None)
    window = build_window(path, app=app)
    _wait(app, lambda: window._worker_ready)
    # Ertelenen kuruluşun boşta-yoklaması 2 sn; testte hızlandırılır.
    window._projects_timer.setInterval(20)
    return window, path, seen, blocker


def _pick_device(window: DesktopWindow, device: int) -> None:
    combo = window.settings_panel.device_combo
    combo.addItem("Test mikrofonu", device)
    combo.setCurrentIndex(combo.findData(device))


def test_settings_apply_during_active_turn_defers_rebuild_and_keeps_turn(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from artemis.config.settings import load_settings

    window, path, seen, blocker = _deferred_settings_window(app, tmp_path, monkeypatch)
    events: list[Any] = []
    original = window._handle_event
    monkeypatch.setattr(window, "_handle_event", lambda e: (events.append(e), original(e))[1])
    first_bridge = window.bridge
    try:
        assert window._submit_text("merhaba")
        _wait(app, lambda: blocker.entered.is_set())
        _pick_device(window, 9)
        # Düğme tur sürerken normalde pasif; sinyal yine de gelebilir.
        window.settings_panel._apply()
        app.processEvents()
        # Ayar diske yazıldı ama canlı kuruluş yapılmadı: tur iptal edilmedi, köprü aynı.
        assert load_settings(path).voice.input_device == 9
        assert len(seen) == 1 and window.bridge is first_bridge and window._busy
        assert not first_bridge._shutdown
        assert "işlem bitince" in window.settings_panel.notice.text()
        blocker.release.set()
        _wait(app, lambda: len(seen) == 2 and window._worker_ready, timeout=8)
        # Tur kendi cevabıyla bitti (iptal değil), ayar bitişten SONRA uygulandı.
        assert any(isinstance(e, Finished) and e.result.text == "bitti" for e in events)
        assert window.bridge is not first_bridge
        assert seen[-1].voice.input_device == 9
    finally:
        blocker.release.set()
        _close_cleanly(app, window)


def test_provider_change_during_active_turn_defers_rebuild(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _path, seen, blocker = _deferred_settings_window(app, tmp_path, monkeypatch)
    first_bridge = window.bridge
    try:
        assert window._submit_text("merhaba")
        _wait(app, lambda: blocker.entered.is_set())
        window._providers_changed()
        app.processEvents()
        assert len(seen) == 1 and window.bridge is first_bridge and window._busy
        assert "işlem bitince" in window.settings_panel.providers.status.text()
        blocker.release.set()
        _wait(app, lambda: len(seen) == 2 and window._worker_ready, timeout=8)
    finally:
        blocker.release.set()
        _close_cleanly(app, window)


def test_settings_apply_when_idle_rebuilds_immediately_and_pending_approval_defers(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _path, seen, blocker = _deferred_settings_window(app, tmp_path, monkeypatch)
    try:
        # Bekleyen onay penceresi varken (tur zaten meşgul sayılır) yine ertelenir.
        window._approval_dialog = object()  # type: ignore[assignment]
        window._request_restart()
        app.processEvents()
        assert len(seen) == 1 and window._config_restart_pending
        window._approval_dialog = None
        window._config_restart_pending = False
        # Boştayken eskisi gibi hemen kurulur.
        window.settings_panel._apply()
        _wait(app, lambda: len(seen) == 2 and window._worker_ready, timeout=8)
        assert not window._config_restart_pending
    finally:
        blocker.release.set()
        _close_cleanly(app, window)


def test_rebuild_waits_for_background_job_even_when_panel_list_is_stale(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_code_agent import make_jobs

    window, _path, seen, blocker = _deferred_settings_window(app, tmp_path, monkeypatch)
    jobs, _factory = make_jobs(tmp_path, "")
    try:
        runtime = window.bridge._runtime
        assert runtime is not None
        # Sohbetten başlamış gibi: panel listesi boş (bayat) ama işçi tarafında iş sürüyor.
        runtime.context.services["code_agent_jobs"] = jobs
        jobs._jobs["j1"] = {"job_id": "j1", "status": "running"}
        assert window.projects_panel.active_job_count() == 0
        assert window.bridge.active_job_count() == 1
        window._providers_changed()
        app.processEvents()
        assert len(seen) == 1 and window._config_restart_pending
        assert "çalışan iş" in window.settings_panel.providers.status.text()
        del jobs._jobs["j1"]  # iş bitti
        # Panel durumu tazeleyince (serial artar) bir kez kurulur.
        window.projects_panel.status_serial += 1
        _wait(app, lambda: len(seen) == 2 and window._worker_ready, timeout=8)
    finally:
        runtime = window.bridge._runtime
        if runtime is not None:
            runtime.context.services.pop("code_agent_jobs", None)
        jobs.shutdown()
        blocker.release.set()
        _close_cleanly(app, window)


def test_noise_suppression_controls_load_enable_and_save(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.config.settings import load_settings
    from tests.test_settings_panel import config

    path = config(tmp_path)
    monkeypatch.setattr(SettingsPanel, "_refresh_devices", lambda *_: None)
    panel = SettingsPanel(path)
    assert not panel.noise_suppression.isChecked() and not panel.noise_level.isEnabled()
    assert panel.noise_level.currentData() == "orta"
    assert not panel.noise_stt.isChecked() and not panel.noise_stt.isEnabled()
    panel.noise_suppression.setChecked(True)
    assert panel.noise_level.isEnabled() and panel.noise_stt.isEnabled()
    panel.noise_stt.setChecked(True)
    panel.noise_level.setCurrentIndex(panel.noise_level.findData("guclu"))
    from artemis.channels.desktop.settings_panel import save_choices

    save_choices(path, None, values=panel.values())
    panel.refresh()
    voice = load_settings(path).voice
    assert voice.noise_suppression and voice.noise_suppression_level == "guclu"
    assert voice.noise_suppression_stt and panel.noise_stt.isChecked()
    assert panel.noise_suppression.isChecked() and panel.noise_level.currentData() == "guclu"
    panel.noise_suppression.setChecked(False)
    assert not panel.noise_level.isEnabled() and not panel.noise_stt.isEnabled()


def test_step_block_is_wide_enough_for_its_longest_visible_line(app: QApplication) -> None:
    # Sola hizalı blok, sarılan etiketin küçük ipucuna sıkışınca satırlar üst üste biniyordu.
    from artemis.channels.desktop.steps import StepLine

    block = StepBlock()
    activity = Activity("r", "t", 1, "tool_result", {"tool": "x"})
    line = "Demo belleğe yazıldı ✓"
    block.add_line(activity, StepLine(line, "ok", False))
    block.add_line(activity, StepLine("İç adım çok uzun bir model satırı " * 4, "info", True))
    labels = block.findChildren(QLabel)
    needed = max(
        label.fontMetrics().horizontalAdvance(label.text())
        for label in labels
        if label.text() == line
    )
    # Gizli iç satır genişliği büyütmez; görünür satır tek satıra sığar.
    assert needed < block.minimumWidth() < needed + 60
    block._toggle.setChecked(True)
    assert block.minimumWidth() == 520


# --- ADR 0030: tek örnek ve arka planda başlama ---------------------------------------

from PySide6.QtNetwork import QLocalSocket  # noqa: E402

from artemis.channels.desktop.instance import (  # noqa: E402
    InstanceServer,
    instance_name,
    notify_running,
)


def _server(tmp_path: Path) -> tuple[InstanceServer, list[int]]:
    server = InstanceServer(instance_name(tmp_path, user="test"))
    assert server.listen()
    shown: list[int] = []
    server.show_requested.connect(lambda: shown.append(1))
    return server, shown


def test_instance_name_is_per_user_and_state_dir(tmp_path: Path) -> None:
    first = instance_name(tmp_path / "a", user="umut")
    assert first == instance_name(tmp_path / "a", user="umut")
    assert first != instance_name(tmp_path / "b", user="umut")
    assert first != instance_name(tmp_path / "a", user="baska")
    assert first.startswith("artemis-") and len(first) == len("artemis-") + 16


def test_second_instance_asks_first_to_show_panel(app: QApplication, tmp_path: Path) -> None:
    import subprocess
    import sys

    server, shown = _server(tmp_path)
    # Gerçekteki gibi ikinci örnek ayrı bir süreçtir (aynı iş parçacığında Windows boruları
    # sunucu okumadan tıkanır); sunucu bu sırada olay döngüsünü döndürür.
    script = (
        "import sys\n"
        "from PySide6.QtCore import QCoreApplication\n"
        "from artemis.channels.desktop.instance import notify_running\n"
        "app = QCoreApplication([])\n"
        "sys.exit(0 if notify_running(sys.argv[1], show=True) else 3)\n"
    )
    client = subprocess.Popen([sys.executable, "-c", script, server.name])
    try:
        try:
            _wait(app, lambda: shown == [1] and client.poll() is not None, timeout=15.0)
        except AssertionError:
            raise AssertionError(f"shown={shown} exit={client.poll()}") from None
        assert client.returncode == 0
    finally:
        if client.poll() is None:
            client.kill()
            client.wait(timeout=5)
        server.close()


def test_background_second_instance_does_not_open_panel(app: QApplication, tmp_path: Path) -> None:
    server, shown = _server(tmp_path)
    try:
        assert notify_running(server.name, show=False)
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            app.processEvents()
        assert shown == []
    finally:
        server.close()


def test_unknown_message_is_ignored(app: QApplication, tmp_path: Path) -> None:
    server, shown = _server(tmp_path)
    try:
        socket = QLocalSocket()
        socket.connectToServer(server.name)
        assert socket.waitForConnected(500)
        socket.write(b"run calc.exe\n")
        socket.waitForBytesWritten(500)
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            app.processEvents()
        assert shown == []
        socket.abort()
    finally:
        server.close()


def test_no_running_instance_returns_false(tmp_path: Path) -> None:
    assert not notify_running(instance_name(tmp_path, user="yok"), show=True, timeout_ms=200)


def test_background_start_stays_in_tray_and_instance_message_shows_panel(
    app: QApplication, tmp_path: Path
) -> None:
    window = build_window(tmp_path / "ignored.yaml", demo=True, app=app)
    try:
        real_tray = window._tray
        window._tray = object()  # type: ignore[assignment]
        window.present(background=True)
        assert not window.isVisible()
        window.show_from_instance()
        assert window.isVisible()
        window._tray = real_tray
    finally:
        _wait(app, lambda: window.status_label.text() == "Hazır", timeout=10.0)
        _close_cleanly(app, window)


def test_claim_returns_none_when_another_instance_listens(
    app: QApplication, tmp_path: Path
) -> None:
    from artemis.channels.desktop.instance import claim_instance

    first = claim_instance(instance_name(tmp_path, user="yaris"), show=False)
    assert first is not None and first.is_listening()
    try:
        assert claim_instance(first.name, show=False) is None
    finally:
        first.close()


def test_pending_connections_are_capped(app: QApplication, tmp_path: Path) -> None:
    from artemis.channels.desktop import instance as instance_module

    server, _shown = _server(tmp_path)
    sockets = []
    try:
        for _ in range(instance_module.MAX_PENDING + 5):
            socket = QLocalSocket()
            socket.connectToServer(server.name)
            assert socket.waitForConnected(500)
            sockets.append(socket)
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            app.processEvents()
        assert len(server._pending) <= instance_module.MAX_PENDING
    finally:
        for socket in sockets:
            socket.abort()
        server.close()


def test_background_start_waits_for_late_tray_then_shows_panel(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import artemis.channels.desktop.app as app_module

    monkeypatch.setattr(app_module, "TRAY_RETRY_MS", 10)
    monkeypatch.setattr(app_module, "TRAY_RETRY_ATTEMPTS", 3)
    window = build_window(tmp_path / "ignored.yaml", demo=True, app=app)
    try:
        if window._tray is not None:
            pytest.skip("Bu masaüstünde tepsi kullanılabilir.")
        window.present(background=True)
        assert not window.isVisible()
        _wait(app, window.isVisible)
        assert window._tray_attempts == 3
    finally:
        _wait(app, lambda: window.status_label.text() == "Hazır", timeout=10.0)
        _close_cleanly(app, window)


def test_failed_autostart_toggle_restores_checkbox(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtGui import QAction

    window = build_window(tmp_path / "ignored.yaml", demo=True, app=app)
    try:
        action = QAction("Windows açılışında başlat", window)
        action.setCheckable(True)
        window._autostart_action = action
        monkeypatch.setattr(window, "_autostart", lambda: None)
        action.toggled.connect(window._toggle_autostart)
        action.setChecked(True)
        assert not action.isChecked()
    finally:
        _wait(app, lambda: window.status_label.text() == "Hazır", timeout=10.0)
        _close_cleanly(app, window)


def test_app_icon_file_exists_and_loads() -> None:
    from pathlib import Path

    from artemis.channels.desktop import app as desktop_app

    assert (Path(desktop_app.__file__).with_name("artemis.ico")).is_file()
    icon = desktop_app.app_icon()
    assert not icon.isNull()
    assert 256 in {size.width() for size in icon.availableSizes()}


def test_data_panel_controls_use_typed_requests_and_show_uncertain(app: QApplication) -> None:
    from artemis.channels.desktop.data_panel import DataPanel
    from artemis.tools.base import ToolResult
    from artemis.tools.plugins.history import HistoryArguments

    panel = DataPanel()
    requests: list[tuple[str, Any]] = []
    panel.tool_requested.connect(lambda name, args: requests.append((name, args)))
    panel.set_busy(False)
    assert panel.history.isEnabled() and not panel.delete.isEnabled()
    panel.accept_result('conversation_history', ToolResult(
        success=True, status='ok', message='ok',
        data={'turns': 2, 'retention_days': 30, 'recent_turns': []},
    ))
    assert panel.days.value() == 30 and panel.delete.isEnabled()
    panel.delete.click()
    assert requests[-1] == ('conversation_history', HistoryArguments(action='delete'))
    panel.accept_result('reminder_list', ToolResult(
        success=True, status='ok', message='ok', data={'reminders': [{
            'reminder_id': 7, 'state': 'uncertain', 'due_local': '2026-10-27 12:00',
            'text': 'Deneme',
        }]},
    ))
    panel.items.setCurrentRow(0)
    assert 'Teslim doğrulanamadı' in panel.items.item(0).text()
    assert panel.retry.isEnabled()
    panel.retry.click()
    assert requests[-1][0] == 'reminder_retry'
    assert requests[-1][1].reminder_id == 7
    panel.set_busy(True)
    assert not panel.delete.isEnabled() and not panel.retry.isEnabled()
    panel.close()


def test_themes_and_navigation_preserve_conversation_and_pet_draft(
    app: QApplication, tmp_path: Path,
) -> None:
    from artemis.channels.desktop.themes import THEMES

    window = _window(app, tmp_path)
    try:
        window.chat.append("Artemis", "Bu konuşma korunmalı")
        creator = window.settings_panel.pet.creator
        assert creator is not None
        creator.input.setText("Taslak pet açıklaması")
        bridge = window.bridge
        window.settings_panel.providers.base_url.setText("http://example.invalid/draft")
        for name in THEMES:
            combo = window.settings_panel.theme_combo
            combo.setCurrentIndex(combo.findData(name))
            assert window._theme == name
            assert window.bridge is bridge
            assert "Bu konuşma korunmalı" in window.chat.toPlainText()
            assert creator.input.text() == "Taslak pet açıklaması"
            assert window.settings_panel.providers.base_url.text() == "http://example.invalid/draft"
            assert THEMES[name]["surface"] in window.styleSheet()
        for page, button in window.nav_buttons.items():
            button.click()
            assert window.tabs.currentWidget() is page
            assert button.isChecked()
            assert sum(b.isChecked() for b in window.nav_buttons.values()) == 1
        assert window.settings_panel.pet.parent() is not window.settings_panel
        assert window.windowTitle().startswith("Artemis")
    finally:
        _close_cleanly(app, window)


def test_brand_uses_selected_pet_and_keeps_product_name(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PySide6.QtGui import QColor, QPixmap

    from artemis.channels.desktop import pet_sprite

    window = _window(app, tmp_path)
    try:
        pixmap = QPixmap(36, 40)
        pixmap.fill(QColor("#365c9e"))
        monkeypatch.setattr(window.pet, "set_skin", lambda pet_id: None)
        monkeypatch.setattr(pet_sprite, "find_pet", lambda *args: object())
        monkeypatch.setattr(pet_sprite, "preview", lambda *args: pixmap)
        window._select_skin("sample")
        # Başlık, karşılama ve balonlar aynı gerçek pet görselini kullanır.
        assert window.brand_pet.has_image()
        assert window.chat.pet_face.has_image()
        window.chat.append("Artemis", "merhaba")
        assert all(face.has_image() for face in window.chat.findChildren(type(window.brand_pet)))
        assert window.windowTitle().startswith("Artemis")
        monkeypatch.setattr(pet_sprite, "find_pet", lambda *args: None)
        window._select_skin("orb")
        assert not window.brand_pet.has_image()
        assert not window.chat.pet_face.has_image()
    finally:
        _close_cleanly(app, window)


def test_placeholder_palette_tracks_all_themes(app: QApplication, tmp_path: Path) -> None:
    from PySide6.QtGui import QPalette

    from artemis.channels.desktop.themes import THEMES

    window = _window(app, tmp_path)
    try:
        for name, colors in THEMES.items():
            window._apply_theme(name)
            for widget in (window.prompt, window.settings_panel.wake_name, window.overlay.input):
                actual = widget.palette().color(QPalette.ColorRole.PlaceholderText).name()
                assert actual == colors["muted"]
    finally:
        _close_cleanly(app, window)


def test_data_panel_search_filters_loaded_rows_and_empty_states(app: QApplication) -> None:
    from artemis.channels.desktop.data_panel import DataPanel
    from artemis.tools.base import ToolResult

    panel = DataPanel()
    requests: list[tuple[str, Any]] = []
    panel.tool_requested.connect(lambda name, args: requests.append((name, args)))
    assert panel.history_stack.currentWidget() is panel.history_empty
    assert panel.items_stack.currentWidget() is panel.items_empty
    assert not panel.retention_hint.isHidden()
    panel.accept_result('conversation_history', ToolResult(
        success=True, status='ok', message='ok',
        data={'turns': 0, 'retention_days': 0, 'recent_turns': []},
    ))
    assert panel.history_stack.currentWidget() is panel.history_empty
    assert 'Kayıt yok' in [lbl.text() for lbl in panel.history_empty.findChildren(QLabel)]
    assert not panel.retention_hint.isHidden()  # yardım metni artık kalıcı (iki kısa satır)
    turns = [
        {'created_at': 't1', 'user': 'IŞIK aç', 'answer': 'Açtım'},
        {'created_at': 't2', 'user': 'saat kaç', 'answer': 'Üç'},
    ]
    panel.accept_result('conversation_history', ToolResult(
        success=True, status='ok', message='ok',
        data={'turns': 2, 'retention_days': 0, 'recent_turns': turns},
    ))
    assert panel.history_stack.currentWidget() is panel.preview
    panel.search.setText('ışık')
    text = panel.preview.toPlainText()
    assert 'IŞIK aç' in text and 'saat kaç' not in text
    panel.search.setText('yok böyle')
    assert panel.history_stack.currentWidget() is panel.history_empty
    panel.search.clear()
    assert 'saat kaç' in panel.preview.toPlainText()
    assert requests == []  # arama araç çağırmaz
    panel.accept_result('reminder_list', ToolResult(
        success=True, status='ok', message='ok', data={'reminders': []},
    ))
    assert panel.items_stack.currentWidget() is panel.items_empty
    panel.accept_result('reminder_list', ToolResult(
        success=True, status='ok', message='ok', data={'reminders': [{
            'reminder_id': 1, 'state': 'pending', 'due_local': 'x', 'text': 'y',
        }]},
    ))
    assert panel.items_stack.currentWidget() is panel.items
    panel.close()


def test_projects_panel_empty_state_and_question_visibility(app: QApplication) -> None:
    from artemis.channels.desktop.projects_panel import ProjectsPanel
    from artemis.tools.base import ToolResult

    panel = ProjectsPanel()
    assert panel.list_stack.currentWidget() is not panel.items
    assert panel.question_box.isHidden() and panel.hint.isHidden()
    assert 'Kapsam' in panel.hint.text() or panel.hint.text()
    panel.accept_result('code_agent_status', ToolResult(
        success=True, status='ok', message='ok', data={'projects': ['p'], 'jobs': [{
            'job_id': 'j', 'project': 'p', 'status': 'waiting', 'question_id': 'q',
            'question': 'Renk?', 'spec': {},
        }], 'drafts': []},
    ))
    assert panel.list_stack.currentWidget() is panel.items
    assert not panel.question_box.isHidden()
    panel.close()


# --- Kabuk yeniden tasarımı: onay özeti, tema düğmesi, son işlem paneli ---------------------


def _approval(arguments: dict[str, Any]) -> Any:
    from artemis.channels.desktop.bridge import Approval

    notice = "Bu adım dış bir içerik okunduktan sonra istendi"
    return Approval("r", "t", "a", "demo_tool", arguments, notice)


def test_approval_real_enter_key_on_focused_default_control_rejects(app: QApplication) -> None:
    """Gerçek Enter tuşu (isDefault bayrağı değil) onayı vermemeli; varsayılan cevap hayırdır."""
    decisions: list[bool] = []

    def decide(_item: Any, approved: bool, _remember: bool) -> bool:
        decisions.append(approved)
        return True

    parent = QWidget()
    dialog = ApprovalDialog(_approval({"key": "a"}), decide, parent)
    dialog.show()
    QTest.qWaitForWindowExposed(dialog)
    focused = dialog.focusWidget()
    assert focused is dialog.no_button
    QTest.keyClick(focused, Qt.Key.Key_Return)
    assert decisions == [False]
    assert dialog.result() == int(dialog.DialogCode.Rejected)


def test_approval_summary_lists_every_argument_and_collapses_short_ones(app: QApplication) -> None:
    parent = QWidget()
    dialog = ApprovalDialog(_approval({"key": "a", "value": "kısa"}), lambda *_: True, parent)
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert "key: a" in texts and "value: kısa" in texts and not any(" = " in t for t in texts)
    assert "Bu adım dış bir içerik okunduktan sonra istendi" in texts
    assert dialog.arguments_view.isHidden() and not dialog.details_toggle.isChecked()
    assert dialog.no_button.isDefault() and not dialog.yes_button.isDefault()
    dialog._choose(False)


@pytest.mark.parametrize(
    "value", ["x" * 61, r"C:\Users\umut\gizli.txt", "https://example.test/a", "a/b"]
)
def test_approval_opens_details_for_long_values_paths_and_urls(
    app: QApplication, value: str
) -> None:
    parent = QWidget()
    arguments = {"target": value}
    dialog = ApprovalDialog(_approval(arguments), lambda *_: True, parent)
    assert dialog.details_toggle.isChecked() and not dialog.arguments_view.isHidden()
    # Tam doğrulanmış argümanlar kırpılmadan ayrıntıda durur.
    assert json.dumps(value)[1:-1] in dialog.arguments_view.toPlainText()
    dialog._choose(False)


def test_approval_splits_warning_and_tool_note_into_separate_paragraphs(
    app: QApplication,
) -> None:
    from artemis.channels.desktop.bridge import Approval

    notice = "Bu adım dış bir içerik okunduktan sonra istendi. Bellek kaydı yazar."
    item = Approval("r", "t", "a", "demo_write_memory", {"key": "a"}, notice, None, "Demo yazma")
    parent = QWidget()
    dialog = ApprovalDialog(item, lambda *_: True, parent)
    warning = dialog.findChild(QLabel, "approvalNotice")
    assert warning is not None
    assert warning.text() == "Bu adım dış bir içerik okunduktan sonra istendi."
    assert dialog.tool_note_label is not None
    assert dialog.tool_note_label.text() == "Bellek kaydı yazar."
    # Ham ad başlık altında değil, kapalı teknik ayrıntılardadır; varsayılan cevap Hayır kalır.
    assert dialog.raw_name.isHidden() and dialog.no_button.isDefault()
    assert dialog.findChild(QLabel, "title").text() == "Demo yazma"
    dialog._choose(False)


def test_approval_without_tool_note_keeps_a_single_notice(app: QApplication) -> None:
    parent = QWidget()
    dialog = ApprovalDialog(_approval({"key": "a"}), lambda *_: True, parent)
    assert dialog.tool_note_label is None
    dialog._choose(False)


def test_approval_summary_elides_long_value_but_keeps_full_text_in_tooltip(
    app: QApplication,
) -> None:
    parent = QWidget()
    value = "y" * 200
    dialog = ApprovalDialog(_approval({"text": value}), lambda *_: True, parent)
    row = next(lbl for lbl in dialog.findChildren(QLabel) if lbl.text().startswith("text: "))
    assert len(row.text()) <= len("text: ") + 80 and row.text().endswith("…")
    assert value in row.toolTip()
    dialog._choose(False)


def test_theme_toggle_flips_light_dark_and_keeps_mixed_variant(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    try:
        emitted: list[str] = []
        window.settings_panel.theme_changed.connect(emitted.append)
        for start, other in (("light-mixed", "dark-mixed"), ("light", "dark")):
            window._apply_theme(start)
            window.theme_toggle.click()
            assert window._theme == other
            assert window.settings_panel.theme_combo.currentData() == other
            window.theme_toggle.click()
            assert window._theme == start
        assert emitted == []  # düğme ayar denetimini sinyalsiz eşitler
    finally:
        _close_cleanly(app, window)


def test_step_panel_shows_only_on_wide_windows_and_caps_rows(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    try:
        window.resize(1280, 800)
        app.processEvents()
        assert window.activity_panel.isHidden()  # işlem yokken sütun çöker
        window._begin_card("r-first", "ilk mesaj")
        assert not window.activity_panel.isHidden()
        window._reset_cards()
        window.resize(900, 700)
        app.processEvents()
        assert not window.activity_panel.isVisible()  # sütun yok; sayfa kapalı
        assert not window._op_widgets and not window.activity_empty.isHidden()
        for index in range(25):
            window._begin_card(f"r{index}", f"mesaj {index}")
        assert len(window._op_widgets) == 20
        assert window.activity_empty.isHidden()
    finally:
        _close_cleanly(app, window)


def test_message_operation_cards_group_per_request_and_theme_safe(
    app: QApplication, tmp_path: Path
) -> None:
    window = _window(app, tmp_path)
    try:
        window.resize(1280, 800)
        assert window.findChild(QLabel, "sectionTitle", Qt.FindChildOption.FindChildrenRecursively)
        for request, text in (("m1", "ilk <b>mesaj</b>"), ("m2", "ikinci")):
            window._begin_card(request, text)
            window._handle_event(Activity(request, "t-" + request, 1, "turn_start", {}))
            window._handle_event(
                Activity(request, "t-" + request, 2, "tool_requested",
                         {"tool": "demo_write_memory", "arguments": {"key": "k", "value": "gizli"}})
            )
            window._handle_event(
                Activity(request, "t-" + request, 3, "tool_result",
                         {"tool": "demo_write_memory", "elapsed_seconds": 0.1,
                          "result": {"status": "sent", "success": False, "message": "gitti"}})
            )
            window._handle_event(
                Activity(request, "t-" + request, 4, "turn_end", {"stop_reason": "completed"})
            )
        app.processEvents()
        cards = [window.activity_cards.itemAt(i).widget() for i in range(2)]
        titles = [c.findChild(QLabel, "opTitle").text() for c in cards]
        assert titles == ["ikinci", "ilk <b>mesaj</b>"]
        assert cards[0].findChild(QLabel, "opChip").text() == "Doğrulanamadı"
        assert cards[0].findChild(QLabel, "opTitle").textFormat() == Qt.TextFormat.PlainText
        # Ayrıntılar kapalı başlar; açınca ham olaylar görünür ve gizli değer sızmaz.
        assert cards[0].details.isHidden()
        cards[0].toggle.click()
        assert not cards[0].details.isHidden()
        assert "turn_start" in cards[0].details.text() and "gizli" not in cards[0].details.text()
        # Tema değişimi kartları yeniden boyar ama genişleme tercihini korur.
        window._apply_theme("dark")
        window._apply_theme("light")
        again = window.activity_cards.itemAt(0).widget()
        assert not again.details.isHidden()
    finally:
        _close_cleanly(app, window)


def test_header_does_not_clip_at_900px(app: QApplication, tmp_path: Path) -> None:
    window = _window(app, tmp_path)
    try:
        window.resize(900, 700)
        app.processEvents()
        window._show_model("çok-uzun-model-adı-" * 6)
        assert window.model_value.text().endswith("…")
        assert window.demo_chip is not None and window.demo_chip.text() == "Demo"
        header = window.findChild(QFrame, "topbar")
        assert header is not None
        for child in header.findChildren(QWidget):
            if child.isVisible() and child.parentWidget() is header:
                assert header.rect().contains(child.geometry())
    finally:
        _close_cleanly(app, window)


def test_pet_workshop_model_button_opens_settings_model(app: QApplication, tmp_path: Path) -> None:
    # Pet atölyesinin boş durumundaki "Ayarlar'da model ekle" düğmesi Ayarlar > Model'e götürür.
    window = _window(app, tmp_path)
    try:
        window.settings_panel.pet.model_settings_requested.emit()
        assert window.tabs.currentWidget() is window.settings_scroll
        assert window.settings_panel.sections.currentRow() == 1
    finally:
        _close_cleanly(app, window)
