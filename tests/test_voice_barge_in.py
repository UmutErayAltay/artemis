"""ADR 0042: konuşurken uyandırmasız doğrulanmış söz susturur, komut çalıştırmaz."""

import asyncio

import pytest

from artemis.channels.voice.core import AudioSegment, PCMChunk, VoiceController
from artemis.config.settings import VoiceConfig
from tests.test_voice_core import FakeMic, FakeRecognizer, FakeRunner, FakeSink, FakeTTS, FakeVAD


class EndlessTTS(FakeTTS):
    def __init__(self):
        super().__init__()
        self.playing = asyncio.Event()

    async def stream(self, _text, cancelled):
        while not cancelled.is_set():
            yield PCMChunk(b"\x01\x00")
            self.playing.set()
            await asyncio.sleep(0.001)


async def _until(condition) -> None:
    while not condition():
        await asyncio.sleep(0.001)


def _segment(start: float, seconds: float) -> AudioSegment:
    return AudioSegment(b"\x00\x00", start, start + seconds)


async def _speaking_controller(texts, **config):
    events = []
    tts = EndlessTTS()
    controller = VoiceController(
        FakeRunner(), VoiceConfig(**config), FakeMic(), FakeVAD([]),
        FakeRecognizer(["artemis uzun anlat", *texts]), tts, FakeSink(),
        on_event=events.append,
    )
    await controller._handle_utterance(_segment(controller.clock(), 0.5))
    await asyncio.wait_for(tts.playing.wait(), 1)
    return controller, events


async def test_plain_speech_silences_output_without_running_it():
    controller, events = await _speaking_controller(
        ["bir saniye bekle", "hava nasıl"], followup_listening=True
    )
    try:
        active = controller._active
        await controller._handle_utterance(_segment(controller.clock(), 0.4))
        assert active is not None and active.cancelled()
        assert not controller._output_active
        await asyncio.sleep(0.01)
        # Susturan söz komut değildir: yeni tur başlamadı, yalnız ilk komut runner'a gitti.
        assert controller._active is None
        assert controller.runner.commands == ["uzun anlat"]
        interrupted = [event for event in events if event.outcome == "interrupted"]
        assert len(interrupted) == 1 and interrupted[0].reason == "barge_in"
        stops = [event.stage for event in events if event.utterance_id == 2
                 and event.stage in {"stop_requested", "playback_stopped"}]
        assert stops == ["stop_requested", "playback_stopped"]
        # Ardından gelen söz uyandırmasız takip komutu olarak alınır.
        await controller._handle_utterance(_segment(controller.clock(), 0.4))
        # Sahte TTS bitmeden konuşur; komutun runner'a ulaşması yeterli, close turu iptal eder.
        await asyncio.wait_for(_until(lambda: len(controller.runner.commands) == 2), 1)
        assert controller.runner.commands == ["uzun anlat", "hava nasıl"]
    finally:
        await controller.close()


async def test_barge_in_without_followup_option_only_stops_output():
    controller, events = await _speaking_controller(["bir saniye bekle", "hava nasıl"])
    try:
        await controller._handle_utterance(_segment(controller.clock(), 0.4))
        assert not controller._output_active
        assert [e.reason for e in events if e.outcome == "interrupted"] == ["barge_in"]
        assert not controller._awaiting_followup and controller._followup_until == 0.0
        # Uyandırmasız sonraki söz yok sayılır.
        await controller._handle_utterance(_segment(controller.clock(), 0.4))
        await asyncio.sleep(0.01)
        assert controller.runner.commands == ["uzun anlat"]
        assert events[-1].reason == "no_wake"
    finally:
        await controller.close()


@pytest.mark.parametrize(("texts", "seconds", "config"), [
    (["öhö"], 0.05, {}),  # barge_in_ms (180) altı kısa patlama
    ([""], 0.5, {}),  # STT boş: gürültü
    (["bir saniye bekle"], 0.5, {"barge_in_without_wake": False}),
    (["bir saniye bekle"], 0.5, {"require_wake_during_output": False}),
])
async def test_short_empty_or_disabled_speech_keeps_playing(texts, seconds, config):
    controller, events = await _speaking_controller(texts, **config)
    try:
        active = controller._active
        await controller._handle_utterance(_segment(controller.clock(), seconds))
        await asyncio.sleep(0.01)
        assert active is not None and not active.done()
        assert controller._output_active
        assert not any(event.outcome == "interrupted" for event in events)
    finally:
        await controller.close()


async def test_own_voice_echo_does_not_silence_output():
    controller, events = await _speaking_controller(["yanıt"])
    try:
        active = controller._active
        await controller._handle_utterance(_segment(controller.clock(), 0.5))
        assert active is not None and not active.done()
        assert any(event.reason == "echo" for event in events)
    finally:
        await controller.close()


async def test_speech_while_thinking_is_not_a_barge_in():
    started = asyncio.Event()

    class SlowRunner(FakeRunner):
        async def run(self, text):
            self.commands.append(text)
            started.set()
            await asyncio.Future()

    events = []
    controller = VoiceController(
        SlowRunner(), VoiceConfig(), FakeMic(), FakeVAD([]),
        FakeRecognizer(["artemis bir şey yap", "bekle"]), FakeTTS(), FakeSink(),
        on_event=events.append,
    )
    try:
        await controller._handle_utterance(_segment(controller.clock(), 0.5))
        await asyncio.wait_for(started.wait(), 1)
        active = controller._active
        await controller._handle_utterance(_segment(controller.clock(), 0.5))
        # Hoparlöre yazım yokken uyandırmasız söz turu iptal etmez (eski kural sürer).
        assert active is not None and not active.done()
        assert events[-1].reason == "no_wake"
    finally:
        await controller.close()
