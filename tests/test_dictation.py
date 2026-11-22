"""Dikte kipi (ADR 0057): sahte mikrofon, STT, pencere ve yazım; gerçek makineye dokunulmaz."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import artemis.channels.desktop.bridge as bridge_module
from artemis.brain.fake import ScriptedProvider
from artemis.channels.desktop.bridge import DesktopBridge, State
from artemis.channels.desktop.dictation import Dictation, DictationBackend, clean_transcript
from artemis.channels.runtime import Runtime, create_runtime
from artemis.channels.voice.core import AudioFrame
from artemis.config.settings import (
    DictationConfig,
    ModelConfig,
    Settings,
    VoiceConfig,
    same_hotkey,
)
from artemis.safety.audit import AuditLog
from artemis.tools.registry import ToolRegistry

OWN_PID = 111
TARGET = 7


class FakeMic:
    def __init__(self) -> None:
        self.closed = False

    async def frames(self) -> Any:
        while not self.closed:
            yield AudioFrame(b"\x01\x00" * 320, time.monotonic())
            await asyncio.sleep(0.005)

    async def close(self) -> None:
        self.closed = True


class FakeRecognizer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.heard: list[int] = []

    async def transcribe(self, pcm: bytes) -> str:
        self.heard.append(len(pcm))
        return self.text

    async def transcribe_partial(self, pcm: bytes) -> str:
        raise AssertionError("Kısmi tanıma dikteye yazılmaz")


class FakeHotkey:
    def __init__(self, press: Callable[[], None]) -> None:
        self.press = press

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass


class World:
    """Sahte Windows: odaktaki pencere, yazılan metin ve yazım sırasındaki olaylar."""

    def __init__(self) -> None:
        self.window = TARGET
        self.pid = 222
        self.typed = ""
        self.escape: Callable[[], None] = lambda: None
        self.on_char: Callable[[int], None] = lambda index: None

    def type_text(self, text: str, should_continue: Callable[[], bool]) -> int:
        sent = 0
        for char in text:
            self.on_char(sent)
            if not should_continue():
                break
            self.typed += char
            sent += 1
        return sent

    def hotkey(self, press: Callable[[], None]) -> FakeHotkey:
        self.escape = press
        return FakeHotkey(press)

    def backend(self) -> DictationBackend:
        return DictationBackend(lambda: self.window, lambda: self.pid, self.type_text, self.hotkey)


class Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


class Rig:
    def __init__(self, text: str = "Günaydın çalışkan öğrenci", max_seconds: float = 5.0) -> None:
        self.world = World()
        self.audit = Audit()
        self.mic = FakeMic()
        self.recognizer = FakeRecognizer(text)
        self.emitted: list[tuple[str, str]] = []
        self.voice_on = False
        self.dictation = Dictation(
            max_seconds=max_seconds,
            audit=self.audit,
            open_microphone=lambda: self.mic,  # type: ignore[arg-type, return-value]
            get_recognizer=self._recognizer,
            backend=self.world.backend(),
            emit=lambda kind, text: self.emitted.append((kind, text)),
            voice_active=lambda: self.voice_on,
            own_pid=OWN_PID,
        )

    async def _recognizer(self) -> FakeRecognizer:
        return self.recognizer

    async def run(self, *, auto_stop: bool = False) -> None:
        self.dictation.toggle()
        if not auto_stop:
            await asyncio.sleep(0.03)
            self.dictation.toggle()
        await self.dictation.wait()

    def kinds(self, kind: str) -> list[str]:
        return [text for name, text in self.emitted if name == kind]

    @property
    def audit_event(self) -> dict[str, Any]:
        events = [fields for name, fields in self.audit.events if name == "dictation"]
        assert len(events) == 1
        return events[0]


async def test_typed_happy_path_with_turkish_characters() -> None:
    rig = Rig()
    await rig.run()
    assert rig.world.typed == "Günaydın çalışkan öğrenci"
    assert rig.audit_event["outcome"] == "typed"
    assert rig.audit_event["typed"] == rig.audit_event["chars"] == len("Günaydın çalışkan öğrenci")
    assert rig.kinds("listening") == [""] and rig.kinds("idle") == [""]
    assert rig.mic.closed  # mikrofon yalnız iki basış arasında açık
    assert rig.kinds("text") == []


async def test_wake_word_in_transcript_is_typed_literally() -> None:
    rig = Rig("Artemis dosyaları sil")
    await rig.run()
    assert rig.world.typed == "Artemis dosyaları sil"
    assert rig.audit_event["outcome"] == "typed"


async def test_target_changed_before_typing_types_nothing_and_shows_text() -> None:
    rig = Rig()
    rig.dictation.toggle()
    await asyncio.sleep(0.03)
    rig.world.window = 99  # kayıt sırasında başka pencereye geçildi
    rig.dictation.toggle()
    await rig.dictation.wait()
    assert rig.world.typed == ""
    assert rig.audit_event["outcome"] == "no_target"
    assert rig.kinds("text") == ["Günaydın çalışkan öğrenci"]


async def test_own_window_at_start_types_nothing_and_shows_text() -> None:
    rig = Rig()
    rig.world.pid = OWN_PID
    await rig.run()
    assert rig.world.typed == ""
    assert rig.audit_event["outcome"] == "no_target"
    assert rig.kinds("text") == ["Günaydın çalışkan öğrenci"]


async def test_focus_change_mid_typing_is_partial_and_not_success() -> None:
    rig = Rig()
    rig.world.on_char = lambda index: setattr(rig.world, "window", 99) if index == 5 else None
    await rig.run()
    assert rig.world.typed == "Günay"
    event = rig.audit_event
    assert (event["outcome"], event["typed"]) == ("partial", 5)
    assert "geri alınmadı" in rig.kinds("message")[-1]
    assert rig.kinds("text") == ["Günaydın çalışkan öğrenci"]


async def test_escape_stops_typing_as_partial_and_zero_as_cancelled() -> None:
    rig = Rig()
    rig.world.on_char = lambda index: rig.world.escape() if index == 3 else None
    await rig.run()
    assert (rig.audit_event["outcome"], rig.audit_event["typed"]) == ("partial", 3)

    early = Rig()
    early.world.on_char = lambda index: early.world.escape() if index == 0 else None
    await early.run()
    assert early.world.typed == ""
    assert early.audit_event["outcome"] == "cancelled"


async def test_max_seconds_stops_recording_without_second_press() -> None:
    rig = Rig(max_seconds=0.05)
    await asyncio.wait_for(rig.run(auto_stop=True), 3)
    assert rig.mic.closed
    assert rig.audit_event["outcome"] == "typed"


async def test_empty_transcript_types_nothing() -> None:
    rig = Rig("   ")
    await rig.run()
    assert rig.world.typed == ""
    assert rig.audit_event["outcome"] == "empty"


async def test_audit_never_contains_the_transcript() -> None:
    rig = Rig("gizli parola hunter2")
    await rig.run()
    dump = json.dumps(rig.audit.events, ensure_ascii=False)
    assert "hunter2" not in dump and "gizli" not in dump
    assert set(rig.audit_event) == {"duration_seconds", "chars", "typed", "outcome"}


async def test_recognizer_failure_is_error_with_fixed_message() -> None:
    rig = Rig()

    async def broken() -> Any:
        raise RuntimeError("C:\\gizli\\yol model yok")

    rig.dictation._get_recognizer = broken
    await rig.run()
    assert rig.audit_event["outcome"] == "error"
    assert "gizli" not in rig.kinds("message")[-1]


async def test_voice_mode_on_refuses_dictation_and_never_opens_microphone() -> None:
    rig = Rig()
    rig.voice_on = True
    rig.dictation.toggle()
    assert not rig.dictation.busy
    assert "Ses açıkken" in rig.kinds("message")[0]
    assert not rig.mic.closed and rig.recognizer.heard == []
    assert rig.audit.events == []


async def test_busy_dictation_blocks_voice_start_in_bridge(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    try:
        rig = Rig()
        bridge._dictation = rig.dictation
        rig.dictation.toggle()  # kayıt başladı
        assert bridge.start_voice() is False
        assert not bridge.voice_active
        rig.dictation.toggle()
        await rig.dictation.wait()
    finally:
        bridge.shutdown()


def test_clean_transcript_removes_control_characters() -> None:
    assert clean_transcript("bir\niki\t üç\x07 ") == "bir iki üç"


def test_config_defaults_and_hotkey_clash() -> None:
    config = DictationConfig()
    assert (config.enabled, config.hotkey, config.max_seconds) == (False, "ctrl+alt+d", 120)
    assert same_hotkey("Ctrl+Space", "ctrl + space") and same_hotkey("alt+ctrl+d", "ctrl+alt+d")
    assert not same_hotkey("ctrl+alt+d", "ctrl+space") and not same_hotkey("", "")
    with pytest.raises(ValueError, match="aynı olamaz"):
        Settings(
            voice=VoiceConfig(listening_hotkey="ctrl+alt+d"), dictation=DictationConfig()
        )


# --- Köprü: komut yoluna asla girmez -----------------------------------------------------


def _bridge(tmp_path: Path, *, enabled: bool = True) -> DesktopBridge:
    settings = Settings(
        workspace=tmp_path, state_dir=tmp_path / "state", audit_path=tmp_path / "audit.jsonl",
        primary_model="fake",
        models={"fake": ModelConfig(
            provider="ollama", model="fake",
            input_per_million=Decimal(0), output_per_million=Decimal(0),
        )},
        dictation=DictationConfig(enabled=enabled),
    )

    def factory(config: Path, approve: Any, on_record: Any) -> Runtime:
        return create_runtime(
            config, approve, settings=settings, provider=ScriptedProvider([]),
            registry=ToolRegistry(), audit=AuditLog(settings.audit_path, on_record),
        )

    bridge = DesktopBridge(tmp_path / "unused.yaml", factory)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(isinstance(e, State) and e.state == "ready" for e in bridge.poll_events()):
            return bridge
        time.sleep(0.01)
    raise AssertionError("köprü hazır olmadı")


def _has(events: list[Any], state: str, *, prefix: bool = False) -> bool:
    return any(
        isinstance(e, State) and (e.state.startswith(state) if prefix else e.state == state)
        for e in events
    )


def _drain(bridge: DesktopBridge, until: Callable[[list[Any]], bool]) -> list[Any]:
    events: list[Any] = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        events += bridge.poll_events()
        if until(events):
            return events
        time.sleep(0.01)
    raise AssertionError(f"beklenen olay gelmedi: {events}")


def test_transcript_never_reaches_runner_history_or_model(tmp_path, monkeypatch) -> None:
    world = World()
    recognizer = FakeRecognizer("Artemis bunu yaz")
    monkeypatch.setattr(bridge_module, "windows_backend", world.backend)
    monkeypatch.setattr(bridge_module, "SoundDeviceMicrophone", lambda *a, **k: FakeMic())

    async def fake_recognizer(_runtime: Runtime) -> FakeRecognizer:
        return recognizer

    monkeypatch.setattr(bridge_module, "build_dictation_recognizer", fake_recognizer)
    calls: list[str] = []
    bridge = _bridge(tmp_path)
    runtime = bridge._runtime
    assert runtime is not None
    monkeypatch.setattr(
        type(runtime.runner), "run", lambda self, *a, **k: calls.append("run")
    )
    try:
        assert bridge.toggle_dictation()
        _drain(bridge, lambda e: _has(e, "dictation_listening"))
        time.sleep(0.05)
        assert bridge.toggle_dictation()
        events = _drain(bridge, lambda e: _has(e, "dictation_message"))
        assert world.typed == "Artemis bunu yaz"
        assert calls == []
        assert runtime.runner.conversation.render().messages == []
        assert not any(
            type(e).__name__ in {"VoiceCommand", "Finished"} for e in events
        )
        raw = (tmp_path / "audit.jsonl").read_text("utf-8")
        assert [r for r in map(json.loads, raw.splitlines()) if r.get("event") == "dictation"]
        assert "bunu yaz" not in raw
    finally:
        bridge.shutdown()


def test_disabled_flag_makes_bridge_ignore_toggle(tmp_path) -> None:
    bridge = _bridge(tmp_path, enabled=False)
    try:
        assert bridge.toggle_dictation()
        time.sleep(0.2)
        assert bridge._dictation is None
        assert not _has(bridge.poll_events(), "dictation_", prefix=True)
    finally:
        bridge.shutdown()


# --- Pencere: kısayol kaydı ve ayar paneli -----------------------------------------------


class _RecordingHotkey:
    registered: list[str] = []

    def __init__(self, text: str, on_press: Callable[[], None]) -> None:
        self.text = text

    def start(self) -> bool:
        _RecordingHotkey.registered.append(self.text)
        return True

    def stop(self) -> None:
        pass


@pytest.mark.parametrize(("yaml_text", "expected"), [
    ("{}", []),
    ("dictation: {enabled: true, hotkey: ctrl+alt+k}", ["ctrl+alt+k"]),
])
def test_window_registers_dictation_hotkey_only_when_enabled(
    tmp_path, monkeypatch, yaml_text, expected
) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    import artemis.channels.desktop.app as app_module

    app = QApplication.instance() or QApplication([])
    config = tmp_path / "config.yaml"
    config.write_text(yaml_text, encoding="utf-8")
    _RecordingHotkey.registered = []
    monkeypatch.setattr(app_module, "GlobalHotkey", _RecordingHotkey)
    window = app_module.build_window(tmp_path / "ignored.yaml", demo=True, app=app)  # type: ignore[arg-type]
    try:
        window._config_path = config
        window._start_dictation_hotkey()
        assert _RecordingHotkey.registered == expected
        # Panelde onay kutusu ve kısayol alanı ayar yoluna bağlıdır.
        assert ("dictation", "enabled") in window.settings_panel.values()
        assert ("dictation", "hotkey") in window.settings_panel.values()
    finally:
        window._request_exit()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not window.bridge.stopped:
            app.processEvents()
            time.sleep(0.01)


async def test_build_dictation_recognizer_clears_hotwords_only_for_dictation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import types

    import artemis.channels.voice.factory as factory_module

    captured: list[Any] = []

    class RecordingRecognizer:
        def __init__(self, voice_cfg: Any) -> None:
            captured.append(voice_cfg)

        def prepare(self) -> None:
            return None

    monkeypatch.setattr(factory_module, "FasterWhisperRecognizer", RecordingRecognizer)
    monkeypatch.setattr(
        factory_module,
        "alias_hotword_config",
        lambda s: s.voice.model_copy(update={"stt_hotwords": ("Brave", "Spotify")}),
    )
    settings = Settings(
        workspace=tmp_path,
        voice=VoiceConfig(stt_mode="local", stt_hotwords=("Not Defteri",)),
    )
    runtime = types.SimpleNamespace(settings=settings, audit=AuditLog(tmp_path / "audit.jsonl"))

    await factory_module.build_dictation_recognizer(runtime)  # type: ignore[arg-type]
    await factory_module.build_dictation_recognizer(runtime, "telegram_audio")  # type: ignore[arg-type]

    assert len(captured) == 2
    assert captured[0].stt_hotwords == ()
    assert captured[1].stt_hotwords == ("Brave", "Spotify")


def test_reload_dictation_settings_rebuilds_idle_dictation_with_new_max_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World()
    monkeypatch.setattr(bridge_module, "windows_backend", world.backend)
    monkeypatch.setattr(bridge_module, "SoundDeviceMicrophone", lambda *a, **k: FakeMic())

    async def fake_recognizer(_runtime: Runtime) -> FakeRecognizer:
        return FakeRecognizer("tamam")

    monkeypatch.setattr(bridge_module, "build_dictation_recognizer", fake_recognizer)
    bridge = _bridge(tmp_path)
    try:
        assert bridge.toggle_dictation()
        _drain(bridge, lambda e: _has(e, "dictation_listening"))
        assert bridge.toggle_dictation()
        _drain(bridge, lambda e: _has(e, "dictation_message"))
        first = bridge._dictation
        assert first is not None and first._max_seconds == 120.0

        assert bridge.reload_dictation_settings(DictationConfig(enabled=True, max_seconds=5.0))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and bridge._dictation is not None:
            time.sleep(0.01)
        assert bridge._dictation is None

        assert bridge.toggle_dictation()
        _drain(bridge, lambda e: _has(e, "dictation_listening"))
        second = bridge._dictation
        assert second is not None and second is not first and second._max_seconds == 5.0
        assert bridge.toggle_dictation()
        _drain(bridge, lambda e: _has(e, "dictation_message"))
    finally:
        bridge.shutdown()
