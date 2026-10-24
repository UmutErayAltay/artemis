"""Ses kanalı sözleşmeleri sahte adaptörlerle sınanır."""

import asyncio
import math
from time import monotonic

import pytest

from artemis.channels.voice.core import (
    AudioFrame,
    AudioSegment,
    PCMChunk,
    VoiceController,
    find_wake_word,
)
from artemis.config.settings import VoiceConfig
from bench.voice_latency import group_live_events


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


async def test_standalone_wake_acknowledges_without_model_and_opens_followup():
    runner = FakeRunner()
    sink = FakeSink()
    states = []
    heard = []
    controller = VoiceController(
        runner, VoiceConfig(), FakeMic(), FakeVAD([]), FakeRecognizer(["Artemis"]),
        FakeTTS(), sink, on_state=states.append, on_transcript=heard.append,
    )
    try:
        await controller._handle_utterance(AudioSegment(b"\x00\x00", 0, 1))
        await controller._settle_active()
        assert runner.commands == []
        assert sink.chunks
        assert heard == ["Artemis"]
        assert states[-1] == "awaiting_command"
        assert controller._wake_command("saat kaç") == "saat kaç"
    finally:
        await controller.close()


async def test_input_level_and_heard_text_do_not_dispatch_non_wake_speech():
    runner = FakeRunner()
    levels = []
    heard = []
    controller = VoiceController(
        runner, VoiceConfig(), FakeMic([AudioFrame(b"\xff\x7f", monotonic())]),
        FakeVAD([False]), FakeRecognizer(["selam"]), FakeTTS(), FakeSink(),
        on_input_level=levels.append, on_transcript=heard.append,
    )
    await controller._handle_utterance(AudioSegment(b"\x00\x00", 0, 1))
    await controller.run()
    assert heard == ["selam"]
    assert levels == [100]
    assert runner.commands == []


def test_wake_word_uses_word_boundaries_and_casefold():
    controller = object.__new__(VoiceController)
    controller.config = VoiceConfig(wake_word="artemis", followup_seconds=8)
    controller.clock = lambda: 10.0
    controller._followup_until = 0.0
    controller.continuous = False

    assert controller._wake_command("ARTEMİS, saat kaç?") == "saat kaç?"
    assert controller._wake_command("heyartemis saat kaç") is None
    assert controller._wake_command("artemis") == ""
    assert controller._wake_command("saat kaç") == "saat kaç"
    assert controller._wake_command("Artemis?") == ""
    assert controller._wake_command("Artemis? saat kaç?") == "saat kaç?"
    assert controller._wake_command("saat kaç") is None


@pytest.mark.parametrize(
    ("transcript", "command"),
    [
        # Uçtan uca denemede Whisper'ın gerçekten yazdığı biçimler.
        ("Artemiz tarayıcıyı aç", "tarayıcıyı aç"),
        ("Arteniz, ses seviyesini kıs.", "ses seviyesini kıs."),
        ("Ar temiz bilgisayarı kapat", "bilgisayarı kapat"),
        ("Arkemiz müziği durdur", "müziği durdur"),
        ("artemiş saat kaç", "saat kaç"),
        ("Artemıs", ""),
    ],
)
def test_wake_word_accepts_close_spellings(transcript: str, command: str):
    controller = object.__new__(VoiceController)
    controller.config = VoiceConfig(wake_word="artemis", followup_seconds=8)
    controller.clock = lambda: 10.0
    controller._followup_until = 0.0
    controller.continuous = False

    assert controller._wake_command(transcript) == command


@pytest.mark.parametrize(
    "transcript",
    [
        "sesi artırmış", "hatemiz son", "Artemisia çiçeği", "temiz bir oda", "Ar, temiz",
        "ertesi gün",
    ],
)
def test_wake_word_rejects_other_words(transcript: str):
    assert find_wake_word(transcript, "artemis") is None


def test_short_wake_word_needs_exact_match():
    assert find_wake_word("artemis aç", "artemis") == (0, 7)
    assert find_wake_word("artemiz aç", "artemis") == (0, 7)
    assert find_wake_word("ev aç", "eva") is None
    assert find_wake_word("eva aç", "eva") == (0, 3)


def test_expired_followup_does_not_dispatch():
    controller = object.__new__(VoiceController)
    controller.config = VoiceConfig(followup_seconds=2)
    controller.clock = lambda: 20.0
    controller._followup_until = 19.0
    controller.continuous = False

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


async def test_assistant_echo_does_not_start_turn_or_consume_followup():
    runner = FakeRunner()
    controller = VoiceController(
        runner, VoiceConfig(require_wake_during_output=False), FakeMic(), FakeVAD([]),
        FakeRecognizer(["Tarayıcıyı kapat.", "Tarayıcıyı aç."]),
        FakeTTS(), FakeSink(), clock=lambda: 12.0,
    )
    controller._spoken_text = "Tarayıcıyı kapattım. Başka bir şey ister misin?"
    controller._spoken_until = 11.2
    controller._followup_until = 30.0
    try:
        # STT geç bitse bile yakalama zamanı yankı penceresi içindedir.
        await controller._handle_utterance(AudioSegment(b"\x00\x00", 10, 11))
        assert runner.commands == []
        assert controller._followup_until == 30
        # Farklı yeni komut aynı zaman penceresinde alınır.
        await controller._handle_utterance(AudioSegment(b"\x00\x00", 10.5, 11))
        await controller._settle_active()
        assert runner.commands == ["Tarayıcıyı aç."]
    finally:
        await controller.close()


async def test_unknown_echo_cannot_use_followup_but_new_wake_command_can():
    runner = FakeRunner()
    controller = VoiceController(
        runner, VoiceConfig(), FakeMic(), FakeVAD([]),
        FakeRecognizer(["Tereyiciyi kapat", "Artemis tarayıcı aç"]),
        FakeTTS(), FakeSink(), clock=lambda: 12.0,
    )
    controller._spoken_text = "Tarayıcıyla ilgili işlem tamamlandı."
    controller._spoken_until = 11.2
    controller._output_requires_wake = True
    controller._followup_until = 30.0
    try:
        await controller._handle_utterance(AudioSegment(b"\x00\x00", 10, 11))
        assert runner.commands == []
        assert controller._followup_until == 30
        await controller._handle_utterance(AudioSegment(b"\x00\x00", 10.5, 11))
        await controller._settle_active()
        assert runner.commands == ["tarayıcı aç"]
    finally:
        await controller.close()


async def test_wake_ack_does_not_require_repeating_wake_for_followup():
    runner = FakeRunner()
    controller = VoiceController(
        runner, VoiceConfig(), FakeMic(), FakeVAD([]),
        FakeRecognizer(["Artemis", "tarayıcı aç"]),
        FakeTTS(), FakeSink(), clock=lambda: 12.0,
    )
    try:
        await controller._handle_utterance(AudioSegment(b"\x00\x00", 10, 11))
        await controller._settle_active()
        assert controller._spoken_text == "Dinliyorum."
        await controller._handle_utterance(AudioSegment(b"\x00\x00", 12.1, 13))
        await controller._settle_active()
        assert runner.commands == ["tarayıcı aç"]
    finally:
        await controller.close()


def test_same_human_words_after_echo_window_are_allowed():
    controller = VoiceController(
        FakeRunner(), VoiceConfig(), FakeMic(), FakeVAD([]), FakeRecognizer([]),
        FakeTTS(), FakeSink(),
    )
    controller._spoken_text = "Tarayıcı açmak için istek gönderdim."
    controller._spoken_until = 10
    assert controller._is_spoken_echo("Tarayıcı aç", 9)
    assert not controller._is_spoken_echo("Tarayıcı aç", 11)


class BlockingRunner(FakeRunner):
    async def run(self, text):
        self.commands.append(text)
        await asyncio.Future()


async def test_stream_eof_cancels_pending_provider_after_non_wake_noise():
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

    async def play(self, _chunks, _cancelled, on_first_pcm):
        on_first_pcm()
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.aborted = True
            raise


async def test_noise_does_not_interrupt_but_final_wake_command_does():
    runner = BlockingRunner()
    controller = VoiceController(
        runner, VoiceConfig(), FakeMic(), FakeVAD([]),
        FakeRecognizer(["", "background noise", "Artemis open Brave"]),
        FakeTTS(), FakeSink(),
    )
    controller._active = asyncio.create_task(controller._run_and_speak("first command"))
    await asyncio.sleep(0)
    original = controller._active
    try:
        for _ in range(2):
            await controller._handle_utterance(AudioSegment(b"\x00\x00", 0, 1))
            assert controller._active is original
            assert not original.done()
            assert runner.cancelled == 0
        await controller._handle_utterance(AudioSegment(b"\x00\x00", 0, 1))
        await asyncio.sleep(0)
        assert original.cancelled()
        assert runner.cancelled == 1
        assert runner.commands == ["first command", "open Brave"]
    finally:
        await controller.close()


@pytest.mark.parametrize(
    ("transcript", "config", "followup", "command"),
    [
        ("", {}, False, None),
        ("background noise", {}, True, None),
        ("background noise", {"continuous_mode": True}, False, None),
        ("yanıt", {"continuous_mode": True, "require_wake_during_output": False}, False, None),
        ("Artemis dur", {}, False, "dur"),
        ("Artemis saat kaç", {"continuous_mode": True}, False, "saat kaç"),
        ("dur", {"continuous_mode": True, "require_wake_during_output": False}, False, "dur"),
        ("dur", {"require_wake_during_output": False}, True, "dur"),
    ],
)
async def test_run_interrupts_playback_only_after_accepted_final_transcript(
    transcript, config, followup, command,
):
    runner = FakeRunner()
    sink = BlockingSink()
    final_started = asyncio.Event()
    allow_final = asyncio.Event()
    processed = asyncio.Event()

    class Recognizer(FakeRecognizer):
        async def transcribe(self, pcm):
            final_started.set()
            await allow_final.wait()
            return await super().transcribe(pcm)

    class Mic(FakeMic):
        async def frames(self):
            await sink.started.wait()
            # Eski 180 ms sınırından uzun gürültü ve sahte Artemis kısmi sonucu
            # oynatmayı kesmemeli; yalnız kabul edilen nihai söz kesebilir.
            for _ in range(60):
                yield AudioFrame(b"\x01\x00", monotonic())
            for _ in range(5):
                yield AudioFrame(b"\x00\x00", monotonic())
            processed.set()
            # EOF ayrı kapatma nedeni; bu denetimler açık mikrofonla yapılır.
            await asyncio.Future()

    recognizer = Recognizer([transcript])
    controller = VoiceController(
        runner, VoiceConfig(pre_roll_ms=0, end_silence_ms=100, **config), Mic(),
        FakeVAD([True] * 60 + [False] * 5), recognizer, FakeTTS(), sink,
    )
    if followup:
        controller._followup_until = monotonic() + 60
    followup_until = controller._followup_until
    controller._active = asyncio.create_task(controller._run_and_speak("first"))
    original = controller._active
    listening = asyncio.create_task(controller.run())
    try:
        await asyncio.wait_for(final_started.wait(), 1)
        assert recognizer.partial_calls >= 1
        assert not sink.aborted
        assert runner.cancelled == 0
        assert runner.commands == ["first"]
        assert controller._followup_until == followup_until
        allow_final.set()
        await asyncio.wait_for(processed.wait(), 1)
        await asyncio.sleep(0)
        if command is None:
            assert not sink.aborted
            assert runner.cancelled == 0
            assert controller._active is original and not original.done()
            assert runner.commands == ["first"]
            assert controller._followup_until == followup_until
        else:
            assert sink.aborted
            assert original.cancelled()
            assert runner.cancelled == 1
            assert runner.commands == ["first", command]
    finally:
        allow_final.set()
        controller.request_stop()
        await asyncio.wait_for(listening, 1)
    assert original.done()
    assert controller.microphone.closed and sink.closed and controller.synthesizer.closed


async def test_run_accepts_followup_during_dinliyorum_without_repeating_wake():
    runner = FakeRunner()
    sink = BlockingSink()
    processed = asyncio.Event()
    acknowledgements = []

    class Mic(FakeMic):
        async def frames(self):
            for _ in range(2 + 5):
                yield AudioFrame(b"\x01\x00", monotonic())
            await sink.started.wait()
            acknowledgements.append(controller._active)
            assert controller._spoken_text == "Dinliyorum."
            for _ in range(60 + 5):
                yield AudioFrame(b"\x01\x00", monotonic())
            processed.set()
            await asyncio.Future()

    controller = VoiceController(
        runner, VoiceConfig(pre_roll_ms=0, end_silence_ms=100), Mic(),
        FakeVAD([True] * 2 + [False] * 5 + [True] * 60 + [False] * 5),
        FakeRecognizer(["Artemis", "Dinliyorum, saat kaç"]), FakeTTS(), sink,
    )
    listening = asyncio.create_task(controller.run())
    try:
        await asyncio.wait_for(processed.wait(), 1)
        await asyncio.sleep(0)
        assert acknowledgements[0].cancelled()
        assert sink.aborted
        assert runner.commands == ["saat kaç"]
    finally:
        controller.request_stop()
        await asyncio.wait_for(listening, 1)


async def test_stream_eof_aborts_pending_playback_after_non_wake_noise():
    start = monotonic()
    frames = [
        *[AudioFrame(b"a", start + index * 0.02) for index in range(7)],
        *[AudioFrame(b"b", start + 1 + index * 0.02) for index in range(15)],
    ]
    recognizer = FakeRecognizer(["artemis do it", "noise"])
    runner = FakeRunner()
    sink = BlockingSink()

    class Mic(FakeMic):
        async def frames(self):
            async for frame in super().frames():
                yield frame
            # Gürültü reddedildi; oynatma yalnız bundan sonraki EOF ile kapanır.
            assert sink.started.is_set() and not sink.aborted
            assert runner.cancelled == 0

    controller = VoiceController(
        runner,
        VoiceConfig(frame_ms=20, pre_roll_ms=0, end_silence_ms=100),
        Mic(frames, delay=0.01),
        FakeVAD([True, True, *([False] * 5), *([True] * 10), *([False] * 5)]),
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


def _continuous(texts, *, continuous=True, **config):
    runner = FakeRunner()
    sink = FakeSink()
    states = []
    controller = VoiceController(
        runner, VoiceConfig(continuous_mode=continuous, **config), FakeMic(), FakeVAD([]),
        FakeRecognizer(texts), FakeTTS(), sink, clock=lambda: 100.0, on_state=states.append,
    )
    return controller, runner, sink, states


async def _say(controller, start=50.0):
    await controller._handle_utterance(AudioSegment(b"\x00\x00", start, start + 1))
    await controller._settle_active()


async def test_continuous_mode_runs_commands_without_wake_word_and_default_does_not():
    on, runner_on, _, _ = _continuous(["saat kaç", "Artemis hava nasıl"])
    off, runner_off, _, _ = _continuous(["saat kaç"], continuous=False)
    try:
        await _say(on)
        await _say(on)
        await _say(off)
        assert runner_on.commands == ["saat kaç", "hava nasıl"]
        assert runner_off.commands == []
    finally:
        await on.close()
        await off.close()


async def test_continuous_wake_alone_still_acknowledges():
    controller, runner, sink, _ = _continuous(["Artemis"])
    try:
        await _say(controller)
        assert runner.commands == []
        assert controller._spoken_text == "Dinliyorum."
        assert sink.chunks
    finally:
        await controller.close()


@pytest.mark.parametrize("phrase", [
    "kesintisiz moda geç", "Kesintisiz modu aç", "Artemis, kesintisiz mod aç",
    "SÜREKLİ DİNLE", "artemis sürekli dinle", "Artemis, kesintisiz sohbete geç.",
    "kesintisiz sohbet modunu başlat", "Gezintisiz sohbet moduna geç bakayım.",
])
async def test_voice_toggle_enables_locally_with_spoken_confirmation(phrase):
    controller, runner, _, states = _continuous([phrase], continuous=False)
    try:
        await _say(controller)
        assert runner.commands == []
        assert controller.continuous is True
        assert controller._spoken_text == "Kesintisiz mod açık."
        assert "mode:on" in states
    finally:
        await controller.close()


@pytest.mark.parametrize("phrase", [
    "kesintisiz modu kapat", "Kesintisiz moddan çık", "Artemis normal moda geç",
    "kesintisiz sohbeti bitir",
])
async def test_voice_toggle_disables_locally_with_spoken_confirmation(phrase):
    controller, runner, _, states = _continuous([phrase])
    try:
        await _say(controller)
        assert runner.commands == []
        assert controller.continuous is False
        assert controller._spoken_text == "Kesintisiz mod kapalı."
        assert "mode:off" in states
    finally:
        await controller.close()


async def test_toggle_phrase_inside_longer_sentence_is_a_normal_command():
    controller, runner, _, _ = _continuous(
        ["kesintisiz moda geç ve saati söyle"], continuous=False
    )
    try:
        await _say(controller)
        assert controller.continuous is False
        assert runner.commands == []
        controller.continuous = True
        controller.recognizer = FakeRecognizer(["Artemis kesintisiz moda geçmeyi anlat"])
        await _say(controller)
        assert controller.continuous is True
        assert runner.commands == ["kesintisiz moda geçmeyi anlat"]
    finally:
        await controller.close()


async def test_continuous_still_needs_wake_while_assistant_is_speaking():
    controller, runner, _, _ = _continuous(["tarayıcıyı aç", "Artemis tarayıcıyı aç"])
    controller._spoken_text = "Hava bugün güneşli olacak."
    controller._spoken_until = math.inf
    controller._output_requires_wake = True
    try:
        await _say(controller, start=60.0)
        assert runner.commands == []
        await _say(controller, start=61.0)
        assert runner.commands == ["tarayıcıyı aç"]
    finally:
        await controller.close()


async def test_continuous_still_ignores_assistant_echo():
    controller, runner, _, _ = _continuous(["Hava bugün güneşli olacak."])
    controller._spoken_text = "Hava bugün güneşli olacak."
    controller._spoken_until = 101.0
    try:
        await _say(controller, start=99.0)
        assert runner.commands == []
    finally:
        await controller.close()


def test_segmenter_detects_on_denoised_but_keeps_raw_audio_for_stt():
    from artemis.channels.voice.core import AudioSegmenter
    from artemis.config.settings import VoiceConfig

    config = VoiceConfig()
    voiced = [False] * 3 + [True] * 4 + [False] * (config.end_silence_ms // config.frame_ms)
    segmenter = AudioSegmenter(config, FakeVAD(voiced))
    segment = None
    for index in range(len(voiced)):
        denoised, raw = bytes([index]) * 4, bytes([100 + index]) * 4
        segment = segmenter.push(denoised, float(index), raw) or segment
        if index == 4:
            assert segmenter.snapshot().startswith(bytes([100]) * 4)  # önyuvarlanma ham
            assert bytes([index]) * 4 not in segmenter.snapshot()
    assert segment is not None
    assert segment.pcm.startswith(bytes([100]) * 4)  # konuşma öncesi (pre-roll) de ham
    assert all(chunk >= 100 for chunk in segment.pcm)  # temizlenmiş bayt yok
    # Ham verilmezse (gürültü engelleme kapalı) davranış değişmez.
    plain = AudioSegmenter(config, FakeVAD(voiced))
    for index in range(len(voiced)):
        result = plain.push(bytes([index]) * 4, float(index))
    assert result is not None and result.pcm.startswith(bytes([0]) * 4)


async def test_wake_ack_keeps_microphone_open_and_strips_own_words():
    class CountingMic(FakeMic):
        paused = 0

        def pause(self):
            self.paused += 1

    mic = CountingMic()
    runner = FakeRunner()
    controller = VoiceController(
        runner, VoiceConfig(), mic, FakeVAD([]),
        FakeRecognizer(["Artemis", "Dinliyorum, saat kaç"]),
        FakeTTS(), FakeSink(), clock=lambda: 12.0,
    )
    try:
        await controller._handle_utterance(AudioSegment(b"\x00\x00", 10, 11))
        await controller._settle_active()
        assert mic.paused == 0
        # Konuşma "Dinliyorum" ile üst üste bindi; yalnız kullanıcının sözü komut olur.
        await controller._handle_utterance(AudioSegment(b"\x00\x00", 12.1, 13))
        await controller._settle_active()
        assert runner.commands == ["saat kaç"]
    finally:
        await controller.close()


async def test_structured_telemetry_keeps_original_generation_after_rejected_noise():
    ready, release = asyncio.Event(), asyncio.Event()

    class DelayedTTS(FakeTTS):
        async def stream(self, _text, _cancelled):
            ready.set()
            await release.wait()
            yield PCMChunk(b"\x01\x00")

    events = []
    controller = VoiceController(
        FakeRunner(), VoiceConfig(), FakeMic(), FakeVAD([]),
        FakeRecognizer(["artemis saat kaç", ""]), DelayedTTS(), FakeSink(),
        on_event=events.append,
    )
    try:
        await controller._handle_utterance(AudioSegment(b"a", monotonic(), monotonic()))
        await ready.wait()
        await controller._handle_utterance(AudioSegment(b"b", monotonic(), monotonic()))
        release.set()
        await controller._settle_active()
        pcm = [event for event in events if event.stage == "first_pcm"]
        assert len(pcm) == 1 and pcm[0].utterance_id == pcm[0].generation_id == 1
        rejected = [event for event in events if event.outcome == "rejected"]
        assert rejected[0].utterance_id == 2 and rejected[0].reason == "empty_transcript"
        assert not controller.runner.cancelled
    finally:
        await controller.close()


async def test_ack_has_own_outcome_and_no_task_first_pcm():
    events = []
    legacy = []
    controller = VoiceController(
        FakeRunner(), VoiceConfig(), FakeMic(), FakeVAD([]), FakeRecognizer(["artemis"]),
        FakeTTS(), FakeSink(), on_event=events.append,
        on_timing=lambda stage, timestamp: legacy.append(stage),
    )
    try:
        await controller._handle_utterance(AudioSegment(b"a", monotonic(), monotonic()))
        await controller._settle_active()
        assert events[-1].outcome == "ack" and events[-1].response_kind == "ack"
        assert all(event.response_kind == "ack" for event in events if event.stage == "first_pcm")
        assert "first_pcm" not in legacy and not controller.runner.commands
    finally:
        await controller.close()


async def test_early_sink_return_closes_underlying_synthesizer_iterator():
    finalized = asyncio.Event()

    class TrackingTTS(FakeTTS):
        async def stream(self, _text, _cancelled):
            try:
                yield PCMChunk(b"\x01\x00")
                await asyncio.Event().wait()
            finally:
                finalized.set()

    class EarlySink(FakeSink):
        async def play(self, chunks, cancelled, on_first_pcm):
            await anext(chunks)
            on_first_pcm()

    controller = VoiceController(FakeRunner(), VoiceConfig(), FakeMic(), FakeVAD([]),
                                 FakeRecognizer([]), TrackingTTS(), EarlySink())
    try:
        await controller._run_and_speak("deneme")
        assert finalized.is_set()
    finally:
        await controller.close()
