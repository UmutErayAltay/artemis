"""Feature 16: 3 adımlı Mikrofon Test Sihirbazı ve Ayarlar > Ses > Dinleme testleri."""

from __future__ import annotations

import asyncio
import os
import struct
import time
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from artemis.channels.desktop.app import _STYLE
from artemis.channels.desktop.mic_wizard import (
    MicTestWizard,
    compute_recording_stats,
    measure_rms_level,
    run_step2_record_and_playback,
    run_step3_stt_check,
)
from artemis.channels.desktop.settings_io import PANEL_BINDINGS, save_choices
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.voice.contracts import AudioFrame
from artemis.config.settings import VoiceConfig, load_settings
from artemis.i18n import set_language
from tests.test_settings_no_squeeze import _assert_no_squeeze
from tests.test_settings_panel import config


def _pcm_tone(amplitude: int, samples: int = 320) -> bytes:
    return struct.pack(f"<{samples}h", *([amplitude] * samples))


class _FakeMic:
    def __init__(self, pcms: list[bytes], *, delay: float = 0.0) -> None:
        self._pcms = pcms
        self._delay = delay
        self.closed = False

    async def frames(self):
        for idx, pcm in enumerate(self._pcms):
            if self._delay:
                await asyncio.sleep(self._delay)
            yield AudioFrame(pcm=pcm, timestamp=float(idx) * 0.02, sample_rate=16000)

    def discard_pending(self) -> None:
        pass

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class _BlockingMic:
    def __init__(self) -> None:
        self.closed = False

    async def frames(self):
        while not self.closed:
            await asyncio.sleep(0.05)
            yield AudioFrame(pcm=_pcm_tone(2000), timestamp=0.0, sample_rate=16000)

    def discard_pending(self) -> None:
        pass

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class _FakeSink:
    def __init__(self) -> None:
        self.played: list[bytes] = []
        self.closed = False

    async def play(self, chunks: Any, cancelled: asyncio.Event, on_first_pcm: Any) -> None:
        async for chunk in chunks:
            if cancelled.is_set():
                return
            self.played.append(chunk.pcm)
            on_first_pcm()

    async def close(self) -> None:
        self.closed = True


class _FakeLocalRecognizer:
    def __init__(self, text: str = "merhaba artemis", *, fail: Exception | None = None) -> None:
        self._text = text
        self._fail = fail
        self.prepared = False
        self.unloaded = False
        self.received_pcm: bytes | None = None

    def prepare(self) -> None:
        if self._fail is not None:
            raise self._fail
        self.prepared = True

    def unload(self) -> None:
        self.unloaded = True

    async def transcribe(self, pcm: bytes) -> str:
        self.received_pcm = pcm
        return self._text

    async def transcribe_partial(self, _pcm: bytes) -> str:
        return ""


def test_measure_rms_and_recording_stats_quality() -> None:
    assert measure_rms_level(b"") == 0
    assert measure_rms_level(_pcm_tone(16384)) == 100

    quiet = compute_recording_stats([_pcm_tone(50), _pcm_tone(60)])
    assert quiet.quality == "too_quiet"
    assert quiet.rms_peak < 5

    noisy = compute_recording_stats([_pcm_tone(4000), _pcm_tone(5000)])
    assert noisy.quality == "noisy"
    assert noisy.noise_floor >= 40

    good = compute_recording_stats([_pcm_tone(200)] * 2 + [_pcm_tone(3000)] * 8)
    assert good.quality == "ok"
    assert good.frame_count == 10
    assert good.pcm == b"".join([_pcm_tone(200)] * 2 + [_pcm_tone(3000)] * 8)


async def test_step2_in_memory_record_playback_and_step3_local_stt(tmp_path: Path) -> None:
    frames = [_pcm_tone(200)] + [_pcm_tone(2500)] * 4
    mic = _FakeMic(frames)
    sink = _FakeSink()
    stats = await run_step2_record_and_playback(
        mic, sink, duration_seconds=0.2, sample_rate=16000, frame_ms=20
    )
    assert mic.closed is True
    assert sink.closed is True
    assert stats.frame_count == 5
    assert sink.played == [stats.pcm]
    assert list(tmp_path.iterdir()) == []  # Diske hiçbir şey yazılmaz.

    recognizer = _FakeLocalRecognizer("merhaba dünya")
    transcript = await run_step3_stt_check(stats.pcm, recognizer)
    assert recognizer.prepared is True
    assert recognizer.unloaded is True
    assert recognizer.received_pcm == stats.pcm
    assert transcript == "merhaba dünya"


def test_mic_wizard_three_step_flow_and_thread_cleanup() -> None:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)

    sink = _FakeSink()
    rec = _FakeLocalRecognizer("test komutu")
    mics: list[Any] = []

    def mic_factory() -> Any:
        mic = _FakeMic([_pcm_tone(300), _pcm_tone(3200), _pcm_tone(2800)], delay=0.005)
        mics.append(mic)
        return mic

    wizard = MicTestWizard(
        VoiceConfig(),
        device_label="Mikrofon (Test)",
        mic_factory=mic_factory,
        sink_factory=lambda: sink,
        recognizer_factory=lambda: rec,
        duration_seconds=0.2,
        auto_start=True,
    )
    try:
        # 1. adım: cihaz adı ve canlı RMS ölçümü
        assert wizard.stack.currentIndex() == 0
        assert wizard.device_value.text() == "Mikrofon (Test)"
        assert not wizard.back_button.isEnabled()
        deadline = time.monotonic() + 2.0
        while wizard.meter_bar.value() == 0 and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert wizard.meter_bar.value() > 0

        # 2. adım: bellek içi kayıt ve oynatma
        wizard.next_button.click()
        app.processEvents()
        assert wizard.stack.currentIndex() == 1
        assert wizard.back_button.isEnabled()
        wizard.record_button.click()
        deadline = time.monotonic() + 2.0
        while not wizard.replay_button.isEnabled() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert wizard.replay_button.isEnabled()
        assert sink.played
        assert "%" in wizard.record_summary.text()

        # Tekrar çal düğmesi
        played_before = len(sink.played)
        wizard.replay_button.click()
        deadline = time.monotonic() + 2.0
        while len(sink.played) == played_before and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert len(sink.played) == played_before + 1

        # 3. adım: yerel STT doğrulaması
        wizard.next_button.click()
        deadline = time.monotonic() + 2.0
        while wizard.stt_result.text() != "test komutu" and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert wizard.stack.currentIndex() == 2
        assert wizard.next_button.text() == "Bitir"
        assert wizard.stt_result.text() == "test komutu"
        assert rec.prepared is True
        assert rec.unloaded is True
    finally:
        wizard.reject()
        wizard.deleteLater()
        app.processEvents()


def test_mic_wizard_cancels_running_meter_thread_cleanly() -> None:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    blocking = _BlockingMic()
    wizard = MicTestWizard(
        VoiceConfig(),
        mic_factory=lambda: blocking,
        sink_factory=_FakeSink,
        recognizer_factory=_FakeLocalRecognizer,
        auto_start=True,
    )
    time.sleep(0.05)
    app.processEvents()
    assert wizard._meter_thread is not None and wizard._meter_thread.isRunning()
    wizard.reject()
    wizard.deleteLater()
    app.processEvents()
    assert blocking.closed is True


@pytest.mark.parametrize("lang", ["tr", "en"])
def test_mic_wizard_no_squeeze_tr_and_en(lang: str) -> None:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    font_path = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "Fonts" / "segoeui.ttf"
    font_id = QFontDatabase.addApplicationFont(str(font_path)) if font_path.is_file() else -1
    prev_font = app.font()
    prev_style = app.styleSheet()
    if font_id >= 0:
        app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(_STYLE)
    set_language(lang)
    wizard: MicTestWizard | None = None
    try:
        wizard = MicTestWizard(
            VoiceConfig(),
            device_label="Test Microphone",
            mic_factory=lambda: _FakeMic([]),
            sink_factory=_FakeSink,
            recognizer_factory=_FakeLocalRecognizer,
            auto_start=False,
        )
        wizard.show()
        for step in range(3):
            wizard.go_to_step(step)
            for _ in range(10):
                app.processEvents()
            _assert_no_squeeze(wizard, f"mic-wizard:{lang}:step{step}")
    finally:
        if wizard is not None:
            wizard.reject()
            wizard.deleteLater()
            app.processEvents()
        set_language("tr")
        app.setFont(prev_font)
        app.setStyleSheet(prev_style)
        if font_id >= 0:
            QFontDatabase.removeApplicationFont(font_id)


def test_settings_panel_ptt_bindings_and_mic_test_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    path = config(tmp_path)
    panel = SettingsPanel(path)
    demo_panel = SettingsPanel(path, demo=True)
    try:
        assert not demo_panel.mic_test_button.isEnabled()
        assert panel.mic_test_button.isEnabled()
        assert {b.path for b in PANEL_BINDINGS} >= {
            ("voice", "ptt_enabled"),
            ("voice", "ptt_hotkey"),
        }

        panel.ptt_enabled.setChecked(True)
        panel.ptt_hotkey.setText("ctrl+alt+v")
        assert save_choices(path, None, values=panel.values()) is True
        loaded = load_settings(path)
        assert loaded.voice.ptt_enabled is True
        assert loaded.voice.ptt_hotkey == "ctrl+alt+v"

        opened: list[Any] = []
        monkeypatch.setattr(
            "artemis.channels.desktop.mic_wizard.open_mic_wizard",
            lambda p: (opened.append(p), 1)[1],
        )
        panel.mic_test_button.click()
        assert opened == [panel]
    finally:
        panel.deleteLater()
        demo_panel.deleteLater()
        app.processEvents()


async def test_step2_closes_sink_when_mic_raises() -> None:
    class _ErrorMic:
        def __init__(self) -> None:
            self.closed = False

        async def frames(self):
            if False:
                yield AudioFrame(pcm=b"", timestamp=0.0, sample_rate=16000)
            raise RuntimeError("mikrofon kesildi")

        def discard_pending(self) -> None:
            pass

        def pause(self) -> None:
            pass

        def resume(self) -> None:
            pass

        async def close(self) -> None:
            self.closed = True

    mic = _ErrorMic()
    sink = _FakeSink()
    with pytest.raises(RuntimeError, match="mikrofon kesildi"):
        await run_step2_record_and_playback(mic, sink, duration_seconds=0.2)
    assert mic.closed is True
    assert sink.closed is True


def test_mic_wizard_cancels_stalled_record_promptly_and_closes_devices() -> None:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)

    class _StalledMic:
        def __init__(self) -> None:
            self.closed = False
            self.waiting = False

        async def frames(self):
            self.waiting = True
            await asyncio.sleep(3.5)
            yield AudioFrame(pcm=_pcm_tone(1000), timestamp=0.0, sample_rate=16000)

        def discard_pending(self) -> None:
            pass

        def pause(self) -> None:
            pass

        def resume(self) -> None:
            pass

        async def close(self) -> None:
            self.closed = True

    mic = _StalledMic()
    sink = _FakeSink()
    wizard = MicTestWizard(
        VoiceConfig(),
        mic_factory=lambda: mic,
        sink_factory=lambda: sink,
        recognizer_factory=_FakeLocalRecognizer,
        duration_seconds=1.0,
        auto_start=False,
    )
    try:
        wizard.go_to_step(1)
        wizard.start_record()
        record_thread = wizard._record_thread
        assert record_thread is not None
        deadline = time.monotonic() + 1.0
        while not mic.waiting and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert mic.waiting is True
        started = time.monotonic()
        wizard.reject()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert not record_thread.isRunning()
        assert wizard._record_thread is None
        assert mic.closed is True
        assert sink.closed is True
    finally:
        wizard.deleteLater()
        app.processEvents()


def test_mic_wizard_advancing_to_step3_mid_record_enables_stt_button_when_done() -> None:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)

    sink = _FakeSink()
    wizard = MicTestWizard(
        VoiceConfig(),
        mic_factory=lambda: _FakeMic(
            [_pcm_tone(300), _pcm_tone(2500), _pcm_tone(2400), _pcm_tone(2200)],
            delay=0.04,
        ),
        sink_factory=lambda: sink,
        recognizer_factory=_FakeLocalRecognizer,
        duration_seconds=0.3,
        auto_start=False,
    )
    try:
        wizard.go_to_step(1)
        wizard.start_record()
        assert wizard._record_thread is not None and wizard._record_thread.isRunning()
        wizard._next_step()
        app.processEvents()
        assert wizard.stack.currentIndex() == 2
        assert not wizard.stt_run_button.isEnabled()

        deadline = time.monotonic() + 3.0
        while wizard._record_thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        assert wizard._record_thread is None
        assert wizard._recorded_pcm
        assert wizard.stt_run_button.isEnabled()
    finally:
        wizard.reject()
        wizard.deleteLater()
        app.processEvents()
