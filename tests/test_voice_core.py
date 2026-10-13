"""Ses kanalı sözleşmeleri sahte adaptörlerle sınanır."""

import asyncio
from time import monotonic

from bench.voice_latency import group_live_events
from jarvis.channels.voice.core import (
    AudioFrame,
    AudioSegment,
    PCMChunk,
    VoiceController,
)
from jarvis.config.settings import VoiceConfig


class FakeMic:
    def __init__(self, frames=(), delay=0):
        self.items = list(frames)
        self.delay = delay
        self.discarded = 0
        self.closed = False

    async def frames(self):
        for frame in self.items:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield frame

    def discard_pending(self):
        self.discarded += 1

    def pause(self):
        pass

    def resume(self):
        pass

    async def close(self):
        self.closed = True


class FakeVAD:
    def __init__(self, voiced):
        self.voiced = iter(voiced)

    def is_speech(self, _pcm):
        return next(self.voiced, False)


class FakeRecognizer:
    def __init__(self, texts):
        self.texts = iter(texts)
        self.calls = 0
        self.partial_calls = 0

    async def transcribe(self, _pcm):
        self.calls += 1
        return next(self.texts, "")

    async def transcribe_partial(self, _pcm):
        self.partial_calls += 1
        return "artemis delete_file path"


class FakeRunner:
    def __init__(self):
        self.commands = []
        self.cancelled = 0

    def cancel_current(self):
        self.cancelled += 1
        return True

    async def run(self, text):
        self.commands.append(text)
        return type("Result", (), {"text": "yanıt"})()


class FakeTTS:
    def __init__(self):
        self.closed = False

    async def stream(self, _text, _cancelled):
        yield PCMChunk(b"\x01\x00")

    async def close(self):
        self.closed = True


class FakeSink:
    def __init__(self):
        self.chunks = []
        self.closed = False

    async def play(self, chunks, cancelled, on_first_pcm):
        async for chunk in chunks:
            if cancelled.is_set():
                return
            self.chunks.append(chunk.pcm)
            on_first_pcm()

    async def close(self):
        self.closed = True


def test_wake_word_uses_word_boundaries_and_casefold():
    controller = object.__new__(VoiceController)
    controller.config = VoiceConfig(wake_word="artemis", followup_seconds=8)
    controller.clock = lambda: 10.0
    controller._followup_until = 0.0

    assert controller._wake_command("ARTEMİS, saat kaç?") == "saat kaç?"
    assert controller._wake_command("heyartemis saat kaç") is None
    assert controller._wake_command("artemis") == ""
    assert controller._wake_command("saat kaç") == "saat kaç"
    assert controller._wake_command("Artemis?") == ""
    assert controller._wake_command("Artemis? saat kaç?") == "saat kaç?"
    assert controller._wake_command("saat kaç") is None


def test_expired_followup_does_not_dispatch():
    controller = object.__new__(VoiceController)
    controller.config = VoiceConfig(followup_seconds=2)
    controller.clock = lambda: 20.0
    controller._followup_until = 19.0

    assert controller._wake_command("saat kaç") is None


async def test_only_final_vad_segment_dispatches_and_resources_close():
    # İki konuşma çerçevesi ve bitiş sessizliği tek nihai STT çağrısı üretir.
    frames = [AudioFrame(b"a", float(i)) for i in range(7)]
    mic = FakeMic(frames)
    runner = FakeRunner()
    recognizer = FakeRecognizer(["artemis saat kaç?"])
    sink = FakeSink()
    tts = FakeTTS()
    controller = VoiceController(
        runner,
        VoiceConfig(frame_ms=20, pre_roll_ms=0, end_silence_ms=100),
        mic,
        FakeVAD([True, True, False, False, False, False, False]),
        recognizer,
        tts,
        sink,
    )

    await controller.run()
    await asyncio.sleep(0)

    assert recognizer.calls == 1
    assert runner.commands == ["saat kaç?"]
    assert sink.chunks == [b"\x01\x00"]
    assert mic.closed and sink.closed and tts.closed


async def test_timeline_keeps_capture_speech_end_and_real_sink_marker():
    now = [20.0]
    events = []
    frames = [AudioFrame(b"a", float(i)) for i in range(7)]
    recognizer = FakeRecognizer(["artemis saat kaç?"])
    original_transcribe = recognizer.transcribe

    async def slow_transcribe(pcm):
        now[0] += 0.8
        return await original_transcribe(pcm)

    recognizer.transcribe = slow_transcribe
    runner = FakeRunner()
    sink = FakeSink()
    controller = VoiceController(
        runner,
        VoiceConfig(frame_ms=20, pre_roll_ms=0, end_silence_ms=100),
        FakeMic(frames),
        FakeVAD([True, True, False, False, False, False, False]),
        recognizer,
        FakeTTS(),
        sink,
        clock=lambda: now[0],
        on_timing=lambda stage, timestamp: events.append((stage, timestamp)),
    )
    original_play = sink.play

    async def clocked_play(chunks, cancelled, on_first_pcm):
        now[0] += 0.4
        await original_play(chunks, cancelled, on_first_pcm)

    sink.play = clocked_play
    await controller.run()

    stamps = dict(events)
    assert stamps["speech_end"] == 1.0  # Son konuşma karesi, son sessizlikten önce.
    assert stamps["stt_final"] >= 1.8
    assert stamps["model_complete"] >= stamps["stt_final"]
    assert stamps["first_pcm"] >= stamps["model_complete"] + 0.4


async def test_partial_transcript_never_dispatches():
    frames = [AudioFrame(b"a", monotonic() + index * 0.02) for index in range(65)]
    recognizer = FakeRecognizer(["delete_file önemli.txt"])
    runner = FakeRunner()
    controller = VoiceController(
        runner,
        VoiceConfig(frame_ms=20, pre_roll_ms=0, end_silence_ms=100),
        FakeMic(frames),
        FakeVAD([True] * 60 + [False] * 5),
        recognizer,
        FakeTTS(),
        FakeSink(),
    )

    await controller.run()

    assert recognizer.partial_calls >= 1
    assert runner.commands == []


async def test_successful_reply_renews_followup_window():
    now = [100.0]
    controller = VoiceController(
        FakeRunner(),
        VoiceConfig(followup_seconds=5),
        FakeMic(),
        FakeVAD([]),
        FakeRecognizer([]),
        FakeTTS(),
        FakeSink(),
        clock=lambda: now[0],
    )
    await controller._run_and_speak("saat kaç")
    assert controller._followup_until == 105.0
    assert controller._wake_command("hava nasıl") == "hava nasıl"


class BlockingRunner(FakeRunner):
    async def run(self, text):
        self.commands.append(text)
        await asyncio.Future()


async def test_speech_during_provider_cancels_active_turn_before_next_stt():
    start = monotonic()
    frames = [
        *[AudioFrame(b"a", start + index * 0.02) for index in range(7)],
        *[AudioFrame(b"b", start + 1 + index * 0.02) for index in range(7)],
    ]
    recognizer = FakeRecognizer(["artemis do it", "noise"])
    runner = BlockingRunner()
    controller = VoiceController(
        runner,
        VoiceConfig(frame_ms=20, pre_roll_ms=0, end_silence_ms=100),
        FakeMic(frames, delay=0.01),
        FakeVAD([True, True, *([False] * 5), True, True, *([False] * 5)]),
        recognizer,
        FakeTTS(),
        FakeSink(),
    )

    await controller.run()

    assert runner.commands == ["do it"]
    assert runner.cancelled == 1
    assert recognizer.calls == 2


class BlockingSink(FakeSink):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.aborted = False

    async def play(self, _chunks, _cancelled, _on_first_pcm):
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.aborted = True
            raise


async def test_speech_during_tts_aborts_playback_and_cancels_runner():
    start = monotonic()
    frames = [
        *[AudioFrame(b"a", start + index * 0.02) for index in range(7)],
        *[AudioFrame(b"b", start + 1 + index * 0.02) for index in range(7)],
    ]
    recognizer = FakeRecognizer(["artemis do it", "noise"])
    runner = FakeRunner()
    sink = BlockingSink()
    controller = VoiceController(
        runner,
        VoiceConfig(frame_ms=20, pre_roll_ms=0, end_silence_ms=100),
        FakeMic(frames, delay=0.01),
        FakeVAD([True, True, *([False] * 5), True, True, *([False] * 5)]),
        recognizer,
        FakeTTS(),
        sink,
    )

    await controller.run()

    assert sink.aborted
    assert runner.cancelled == 1
    assert runner.commands == ["do it"]


async def test_capped_utterance_is_dropped_then_next_wake_command_runs():
    states = [True] * 7 + [False] * 3 + [True, True, False, False, False]
    frames = [
        AudioFrame(bytes([index]), monotonic() + index * 0.02) for index in range(len(states))
    ]
    recognizer = FakeRecognizer(["artemis sonraki komut"])
    runner = FakeRunner()
    controller = VoiceController(
        runner,
        VoiceConfig(
            frame_ms=30,
            pre_roll_ms=0,
            end_silence_ms=100,
            max_utterance_seconds=0.15,
        ),
        FakeMic(frames),
        FakeVAD(states),
        recognizer,
        FakeTTS(),
        FakeSink(),
    )

    await controller.run()

    assert recognizer.calls == 1
    assert runner.commands == ["sonraki komut"]
    assert b"\x00" not in repr(AudioSegment(b"\x00private", 1.0, 2.0)).encode()


def test_live_benchmark_counts_invalid_backward_and_missing_audio_sessions():
    stages = ["speech_start", "speech_end", "stt_final", "model_complete", "first_pcm"]
    valid = [{"stage": stage, "timestamp": float(index)} for index, stage in enumerate(stages)]
    backward = [
        {"stage": stage, "timestamp": stamp}
        for stage, stamp in zip(stages, [10.0, 11.0, 12.0, 9.0, 13.0], strict=True)
    ]
    missing_pcm = [
        {"stage": stage, "timestamp": float(index)} for index, stage in enumerate(stages[:-1])
    ]
    aborted = [
        {"stage": "speech_start", "timestamp": 20.0},
        {"stage": "speech_end", "timestamp": 21.0},
    ]

    completed, aborted_count, no_audio, invalid = group_live_events(
        valid + backward + missing_pcm + aborted
    )

    assert len(completed) == 1
    assert (aborted_count, no_audio, invalid) == (1, 1, 1)


async def test_on_state_reports_one_command_as_listening_thinking_speaking_off():
    # Durum sırası ekran için sözleşmedir: komut bitince dinlemeye dönülür, kapanışta "off".
    states: list[str] = []
    frames = [AudioFrame(b"a", float(i)) for i in range(7)]
    controller = VoiceController(
        FakeRunner(),
        VoiceConfig(frame_ms=20, pre_roll_ms=0, end_silence_ms=100),
        FakeMic(frames),
        FakeVAD([True, True, False, False, False, False, False]),
        FakeRecognizer(["artemis saat kaç?"]),
        FakeTTS(),
        FakeSink(),
        on_state=states.append,
    )

    await controller.run()

    assert states == ["listening", "thinking", "speaking", "listening", "off"]


async def test_run_command_kwarg_replaces_runner_and_default_is_runner_run():
    runner = FakeRunner()
    seen: list[str] = []

    async def run_command(text):
        seen.append(text)
        return type("Result", (), {"text": ""})()

    frames = [AudioFrame(b"a", float(i)) for i in range(7)]
    controller = VoiceController(
        runner,
        VoiceConfig(frame_ms=20, pre_roll_ms=0, end_silence_ms=100),
        FakeMic(frames),
        FakeVAD([True, True, False, False, False, False, False]),
        FakeRecognizer(["artemis saat kaç?"]),
        FakeTTS(),
        FakeSink(),
        run_command=run_command,
    )

    await controller.run()

    assert seen == ["saat kaç?"]
    assert runner.commands == []
    default = VoiceController(
        runner, VoiceConfig(), FakeMic(), FakeVAD([]), FakeRecognizer([]), FakeTTS(), FakeSink()
    )
    assert default.run_command == runner.run
