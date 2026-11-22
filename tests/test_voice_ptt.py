"""Feature 16: Bas-konuş (Push-to-Talk) yapılandırma, kısayol, ses hattı ve köprü testleri."""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from artemis.brain.agent_loop import RunResult
from artemis.brain.fake import ScriptedProvider
from artemis.channels.desktop import app as app_module
from artemis.channels.desktop import bridge as bridge_module
from artemis.channels.desktop import hotkey_manager, window_restart
from artemis.channels.desktop.bridge import DesktopBridge, State
from artemis.channels.desktop.hotkey import PushToTalkHotkey, hotkey_virtual_keys
from artemis.channels.runtime import Runtime, create_runtime
from artemis.channels.voice.contracts import AudioFrame, AudioSegment, AudioSegmenter, PCMChunk
from artemis.channels.voice.core import VoiceController
from artemis.config.settings import (
    DictationConfig,
    ModelConfig,
    SelectionActionsConfig,
    Settings,
    VoiceConfig,
)
from artemis.safety.audit import AuditLog
from artemis.tools.registry import ToolRegistry
from tests.test_desktop_gui import _close_cleanly
from tests.test_settings_panel import config


def test_ptt_config_defaults_syntax_and_four_way_conflicts() -> None:
    cfg = VoiceConfig()
    assert cfg.ptt_enabled is False
    assert cfg.ptt_hotkey == ""

    # Boş dize bas-konuş etkinken de geçerlidir (yalnız kısayol kapalıdır).
    s = Settings(voice=VoiceConfig(ptt_enabled=True, ptt_hotkey=""))
    assert s.voice.ptt_enabled is True

    for bad in ("space", "a", "ctrl+badkey", "ctrl+ctrl+v", "ctrl+"):
        with pytest.raises(ValueError, match="geçersiz"):
            Settings(voice=VoiceConfig(ptt_hotkey=bad))

    with pytest.raises(ValueError, match="aynı olamaz"):
        Settings(voice=VoiceConfig(listening_hotkey="ctrl+space", ptt_hotkey="ctrl+space"))
    with pytest.raises(ValueError, match="aynı olamaz"):
        Settings(
            dictation=DictationConfig(hotkey="ctrl+alt+d"),
            voice=VoiceConfig(ptt_hotkey="alt+ctrl+d"),
        )
    with pytest.raises(ValueError, match="aynı olamaz"):
        Settings(
            selection_actions=SelectionActionsConfig(hotkey="ctrl+alt+s"),
            voice=VoiceConfig(ptt_hotkey="alt+ctrl+s"),
        )


def test_validate_hotkeys_and_start_ptt_hotkey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        hotkey_manager.validate_hotkeys(
            listening="ctrl+space", dictation="ctrl+alt+d", selection="ctrl+alt+s", ptt="ctrl+alt+v"
        )
        is None
    )
    assert (
        hotkey_manager.validate_hotkeys(
            listening="ctrl+space", dictation="ctrl+alt+d", selection="ctrl+alt+s", ptt="space"
        )
        is not None
    )
    assert (
        hotkey_manager.validate_hotkeys(
            listening="ctrl+space", dictation="ctrl+alt+d", selection="ctrl+alt+s", ptt="alt+ctrl+d"
        )
        is not None
    )

    path = config(tmp_path)
    events: list[str] = []
    created: list[Any] = []

    class _FakePtt:
        def __init__(self, text: str, on_press: Any, on_release: Any) -> None:
            self.text = text
            self.on_press = on_press
            self.on_release = on_release
            self.stopped = 0
            created.append(self)

        def start(self) -> bool:
            return True

        def stop(self) -> None:
            self.stopped += 1

    win = SimpleNamespace(
        _config_path=path,
        _ptt_hotkey=None,
        _ptt_pressed=SimpleNamespace(emit=lambda: events.append("press")),
        _ptt_released=SimpleNamespace(emit=lambda: events.append("release")),
        _hotkey_problem=lambda msg: events.append(f"err:{msg}"),
    )
    hotkey_manager.start_ptt_hotkey(win, _FakePtt)  # type: ignore[arg-type]
    assert created == []  # ptt_enabled=False

    raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw_data.setdefault("voice", {})["ptt_enabled"] = True
    raw_data["voice"]["ptt_hotkey"] = "ctrl+alt+v"
    path.write_text(yaml.safe_dump(raw_data), encoding="utf-8")

    hotkey_manager.start_ptt_hotkey(win, _FakePtt)  # type: ignore[arg-type]
    hotkey_manager.start_ptt_hotkey(win, _FakePtt)  # type: ignore[arg-type]
    assert len(created) == 2
    assert created[0].stopped == 1
    assert created[1].text == "ctrl+alt+v"
    created[1].on_press()
    created[1].on_release()
    assert events == ["press", "release"]


def test_apply_settings_rejects_ptt_conflict(tmp_path: Path) -> None:
    path = config(tmp_path)
    failed: list[str] = []
    marked: list[bool] = []
    values = {
        ("voice", "listening_hotkey"): "ctrl+space",
        ("voice", "ptt_hotkey"): "space+ctrl",
        ("dictation", "hotkey"): "ctrl+alt+d",
        ("selection_actions", "hotkey"): "ctrl+alt+s",
    }
    panel = SimpleNamespace(
        values=lambda: values,
        applications={"brave": "brave.exe"},
        code_agent_values=lambda: {},
        stt_model_path=lambda: None,
        mark_saved=marked.append,
        save_failed=failed.append,
    )
    window = SimpleNamespace(
        settings_panel=panel,
        bridge=SimpleNamespace(reload_dictation_settings=lambda _cfg: True),
        _reload_hotkeys=lambda: None,
        _demo=False,
        _exiting=False,
        _config_path=path,
    )
    window_restart.apply_settings(window, None)  # type: ignore[arg-type]
    assert len(failed) == 1 and marked == []


def test_push_to_talk_hotkey_press_release_short_tap_and_stop_during_hold() -> None:
    vk, mod_groups = hotkey_virtual_keys("ctrl+alt+v")
    assert vk == ord("V")
    assert mod_groups == ((0x11,), (0x12,))

    events: list[str] = []
    held_keys: set[int] = set()
    trigger_box: list[Any] = []
    ready_gate = threading.Event()

    def register_loop(
        _flags: int, _vk: int, on_ready: Any, trigger: Any, stop_event: threading.Event
    ) -> None:
        trigger_box.append(trigger)
        on_ready(True)
        ready_gate.set()
        stop_event.wait(2.0)

    hk = PushToTalkHotkey(
        "ctrl+alt+v",
        lambda: events.append("press"),
        lambda: events.append("release"),
        poll_interval=0.005,
        is_key_down=lambda code: code in held_keys,
        register_loop=register_loop,
    )
    assert hk.start() is True
    assert ready_gate.wait(1.0)
    trigger = trigger_box[0]

    # 1) Normal basılı tutma -> bırakma (basılıyken ikinci WM_HOTKEY yoksayılır).
    held_keys.update({ord("V"), 0x11, 0x12})
    trigger()
    trigger()
    time.sleep(0.025)
    assert events == ["press"]
    held_keys.discard(ord("V"))
    deadline = time.monotonic() + 1.0
    while events != ["press", "release"] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert events == ["press", "release"]

    # 2) Çok kısa dokunuş (tuş daha kontrol edilmeden bırakılmış): yine 1:1 press -> release.
    events.clear()
    held_keys.clear()
    trigger()
    deadline = time.monotonic() + 1.0
    while events != ["press", "release"] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert events == ["press", "release"]

    # 3) Basılı tutulurken stop() çağrısı: temiz biçimde release üretir.
    events.clear()
    held_keys.update({ord("V"), 0x11, 0x12})
    trigger()
    time.sleep(0.015)
    assert events == ["press"]
    hk.stop()
    assert events == ["press", "release"]


def test_audio_segmenter_ptt_collects_without_vad_and_flushes_immediately() -> None:
    class _NeverSpeechVAD:
        def is_speech(self, _pcm: bytes) -> bool:
            return False

    class _NeverEndTurn:
        def is_complete(self, _pcm16: bytes) -> bool:
            return False

    cfg = VoiceConfig(pre_roll_ms=40, frame_ms=20, end_silence_ms=100)
    seg = AudioSegmenter(cfg, _NeverSpeechVAD(), _NeverEndTurn())
    assert seg.push(b"pre1", 1.0) is None
    seg.begin_ptt(1.02)
    # Sessizlik eşiği geçse bile PTT bırakılana dek kendiliğinden kesmez.
    for idx in range(10):
        assert seg.push(f"f{idx}".encode(), 1.04 + idx * 0.02) is None
    flushed = seg.flush_active(1.30)
    assert isinstance(flushed, AudioSegment)
    assert flushed.pcm.startswith(b"pre1f0")
    assert flushed.pcm.endswith(b"f9")
    assert seg.flush_active(1.40) is None


async def test_voice_controller_ptt_bypasses_wake_and_preserves_continuous() -> None:
    class _FakeMic:
        def __init__(self) -> None:
            self.paused = 0

        async def frames(self):
            if False:
                yield AudioFrame(b"", 0.0)

        def discard_pending(self) -> None:
            pass

        def pause(self) -> None:
            self.paused += 1

        def resume(self) -> None:
            pass

        async def close(self) -> None:
            pass

    class _FakeVAD:
        def is_speech(self, _pcm: bytes) -> bool:
            return False

    class _FakeRecognizer:
        def __init__(self, texts: list[str]) -> None:
            self._texts = iter(texts)

        async def transcribe(self, _pcm: bytes) -> str:
            return next(self._texts, "")

        async def transcribe_partial(self, _pcm: bytes) -> str:
            return ""

    class _FakeRunner:
        def __init__(self) -> None:
            self.commands: list[str] = []
            self.cancelled = 0

        def cancel_current(self) -> bool:
            self.cancelled += 1
            return True

        async def run(self, text: str) -> RunResult:
            self.commands.append(text)
            return RunResult(text="tamam", stop_reason="completed", turn_id="t1")

    class _FakeTTS:
        async def stream(self, _text: str, _cancelled: asyncio.Event):
            yield PCMChunk(b"\x01\x00")

        async def close(self) -> None:
            pass

    class _FakeSink:
        async def play(self, chunks: Any, cancelled: asyncio.Event, on_first_pcm: Any) -> None:
            async for _chunk in chunks:
                if cancelled.is_set():
                    return
                on_first_pcm()

        async def close(self) -> None:
            pass

    class _BlockAllWakeSpotter:
        def feed(self, _pcm16: bytes) -> bool:
            return False

        def close(self) -> None:
            pass

    runner = _FakeRunner()
    controller = VoiceController(
        runner,  # type: ignore[arg-type]
        VoiceConfig(wake_word="artemis", continuous_mode=False),
        _FakeMic(),
        _FakeVAD(),
        _FakeRecognizer(["saat kaç", "Artemis hava nasıl", "uyandırmasız"]),
        _FakeTTS(),
        _FakeSink(),
        wake_spotter=_BlockAllWakeSpotter(),
    )
    decoder = asyncio.create_task(controller._decode_segments())
    try:
        # 1) Wake word ve KWS olmadan tek PTT sözü çalışır, continuous değişmez.
        controller.ptt_press()
        controller.segmenter.push(b"\x10\x00" * 160, 1.0)
        seg1 = controller.ptt_release()
        assert seg1 is not None
        await asyncio.wait_for(controller._segments.join(), 2.0)
        await controller._settle_active()
        assert runner.commands == ["saat kaç"]
        assert controller.continuous is False

        # 2) Wake word söylendiyse öneki temizler.
        controller.ptt_press()
        controller.segmenter.push(b"\x10\x00" * 160, 2.0)
        assert controller.ptt_release() is not None
        await asyncio.wait_for(controller._segments.join(), 2.0)
        await controller._settle_active()
        assert runner.commands == ["saat kaç", "hava nasıl"]
        assert controller.continuous is False

        # 3) Normal (PTT olmayan) söz wake word olmadan hâlâ reddedilir.
        await controller._handle_utterance(AudioSegment(b"\x10\x00" * 160, 3.0, 3.1))
        assert runner.commands == ["saat kaç", "hava nasıl"]

        # 4) pause_for_approval etkin PTT kaydını sıfırlar.
        controller.ptt_press()
        controller.segmenter.push(b"\x10\x00" * 160, 4.0)
        controller.pause_for_approval()
        assert controller.ptt_release() is None
    finally:
        decoder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await decoder
        await controller.close()


def _make_bridge(
    tmp_path: Path, voice_factory: Any
) -> DesktopBridge:
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        primary_model="fake",
        models={
            "fake": ModelConfig(
                provider="ollama",
                model="fake",
                input_per_million=Decimal(0),
                output_per_million=Decimal(0),
            )
        },
        dictation=DictationConfig(enabled=True),
    )

    def runtime_factory(path: Path, approve: Any, on_record: Any) -> Runtime:
        return create_runtime(
            path,
            approve,
            settings=settings,
            provider=ScriptedProvider([]),
            registry=ToolRegistry(),
            audit=AuditLog(settings.audit_path, on_record),
        )

    bridge = DesktopBridge(tmp_path / "unused.yaml", runtime_factory, voice_factory=voice_factory)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if any(isinstance(e, State) and e.state == "ready" for e in bridge.poll_events()):
            return bridge
        time.sleep(0.01)
    raise AssertionError("köprü hazır olmadı")


def test_desktop_bridge_ptt_auto_voice_attach_and_dictation_guard(tmp_path: Path) -> None:
    calls: list[str] = []

    class _FakeController:
        def __init__(self, on_state: Any) -> None:
            self.on_state = on_state
            self.on_event: Any = None
            self.sink = SimpleNamespace(output_info=None)
            self._stop = asyncio.Event()
            self._ptt_active = False

        async def run(self) -> None:
            self.on_state("listening")
            await self._stop.wait()

        def ptt_press(self) -> None:
            self._ptt_active = True
            calls.append("press")

        def ptt_release(self) -> None:
            self._ptt_active = False
            calls.append("release")
            return None

        async def drain_and_stop(self) -> None:
            calls.append("drain_and_stop")
            self.request_stop()

        def request_stop(self) -> None:
            self._stop.set()

        async def close(self) -> None:
            self._stop.set()

    async def fake_voice_factory(
        _runtime: Runtime, _run_command: Any, on_state: Any
    ) -> Any:
        return _FakeController(on_state)

    bridge = _make_bridge(tmp_path, fake_voice_factory)
    try:
        # 1) Dikte meşgulken PTT reddedilir.
        bridge._dictation = SimpleNamespace(busy=True, shutdown=lambda: None)  # type: ignore[assignment]
        assert bridge.ptt_press() is False
        events = bridge.poll_events()
        assert any(isinstance(e, State) and e.state == "dictation_message" for e in events)
        bridge._dictation = None

        # 2) Ses kapalıyken bas-konuş sesi otomatik açar, bırakınca tek turda kapatır.
        assert bridge.ptt_press() is True
        deadline = time.monotonic() + 5.0
        while "press" not in calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "press" in calls
        assert bridge.ptt_release() is True
        deadline = time.monotonic() + 5.0
        while bridge.voice_active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not bridge.voice_active
        assert calls == ["press", "release", "drain_and_stop"]

        # 3) Ses zaten açıkken bağlanır, bırakınca sesi kapatmaz.
        calls.clear()
        assert bridge.start_voice() is True
        deadline = time.monotonic() + 5.0
        while bridge._voice_controller is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bridge.ptt_press() is True
        assert bridge.ptt_release() is True
        deadline = time.monotonic() + 5.0
        while calls != ["press", "release"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bridge.voice_active is True
        assert bridge.stop_voice() is True
        deadline = time.monotonic() + 5.0
        while bridge.voice_active and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        bridge.shutdown()
        deadline = time.monotonic() + 5.0
        while not bridge.stopped and time.monotonic() < deadline:
            time.sleep(0.01)


def test_desktop_window_ptt_signals_and_finish_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    path = config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("voice", {})["ptt_enabled"] = True
    raw["voice"]["ptt_hotkey"] = "ctrl+alt+v"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    created: list[Any] = []

    class _FakeGlobal:
        def __init__(self, _text: str, _on_press: Any) -> None:
            pass

        def start(self) -> bool:
            return True

        def stop(self) -> None:
            pass

    class _FakePtt:
        def __init__(self, text: str, on_press: Any, on_release: Any) -> None:
            self.text = text
            self.on_press = on_press
            self.on_release = on_release
            self.stopped = 0
            created.append(self)

        def start(self) -> bool:
            return True

        def stop(self) -> None:
            self.stopped += 1

    def factory(config_path: Path, approve: Any, **kwargs: Any) -> Any:
        return create_runtime(
            config_path,
            approve,
            settings=Settings(
                workspace=tmp_path,
                state_dir=tmp_path / "state",
                audit_path=tmp_path / "audit.jsonl",
            ),
            provider=ScriptedProvider([]),
            **kwargs,
        )

    monkeypatch.setattr(bridge_module, "create_runtime", factory)
    monkeypatch.setattr(app_module, "GlobalHotkey", _FakeGlobal)
    monkeypatch.setattr(app_module, "PushToTalkHotkey", _FakePtt)

    window = app_module.build_window(path, app=app)
    seen: list[str] = []
    monkeypatch.setattr(window.bridge, "ptt_press", lambda: (seen.append("press"), True)[1])
    monkeypatch.setattr(window.bridge, "ptt_release", lambda: (seen.append("release"), True)[1])
    try:
        assert len(created) == 1
        created[0].on_press()
        created[0].on_release()
        app.processEvents()
        assert seen == ["press", "release"]
    finally:
        _close_cleanly(app, window)
        deadline = time.monotonic() + 2.0
        while window._ptt_hotkey is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
    assert window._ptt_hotkey is None
    assert created[0].stopped >= 1
