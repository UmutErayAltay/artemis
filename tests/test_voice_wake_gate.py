"""ADR 0069: KWS ön-kapısı yalnız eşleşmesiz bölütleri STT'den önce eler; karar transkriptte."""

import asyncio
import threading

from artemis.brain.agent_loop import RunResult
from artemis.channels.voice import factory
from artemis.channels.voice.core import AudioFrame, AudioSegment, PCMChunk, VoiceController
from artemis.config.settings import VoiceConfig

PCM = b"\x01\x00" * 320  # 20 ms @ 16 kHz


class FakeMic:
    def __init__(self, frames):
        self.items = list(frames)

    async def frames(self):
        for frame in self.items:
            await asyncio.sleep(0)
            yield frame

    def discard_pending(self):
        pass

    def pause(self):
        pass

    def resume(self):
        pass

    async def close(self):
        pass


class FakeVAD:
    def __init__(self, voiced):
        self.voiced = iter(voiced)

    def is_speech(self, _pcm):
        return next(self.voiced, False)


class FakeRecognizer:
    def __init__(self, texts):
        self.texts = iter(texts)
        self.calls = 0

    async def transcribe(self, _pcm):
        self.calls += 1
        return next(self.texts, "")

    async def transcribe_partial(self, _pcm):
        return ""


class FakeRunner:
    def __init__(self):
        self.commands = []

    def cancel_current(self):
        return True

    async def run(self, text):
        self.commands.append(text)
        return RunResult(text="yanıt", stop_reason="completed", turn_id="t")


class FakeTTS:
    async def stream(self, _text, _cancelled):
        yield PCMChunk(b"\x01\x00")

    async def close(self):
        pass


class FakeSink:
    async def play(self, chunks, cancelled, on_first_pcm):
        async for _chunk in chunks:
            on_first_pcm()

    async def close(self):
        pass


class FakeSpotter:
    """``script``: feed çağrılarının sonuçları; bir Exception ise fırlatılır."""

    def __init__(self, script=(), delay=0.0):
        self.script = list(script)
        self.delay = delay
        self.calls = 0
        self.closed = False

    def feed(self, _pcm):
        self.calls += 1
        if self.delay:
            threading.Event().wait(self.delay)
        item = self.script.pop(0) if self.script else False
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


# 20 ms kare, 100 ms parti = 5 kare; 12 konuşma + 6 sessizlik karesi (end_silence_ms=100 → 5).
def _frames(n=14, start=100.0):
    return [AudioFrame(PCM, start + i * 0.02) for i in range(n)]


def _config(**kw):
    return VoiceConfig(frame_ms=20, pre_roll_ms=0, end_silence_ms=100, **kw)


def _controller(spotter, texts, *, frames=None, events=None, states=None, **kw):
    frames = _frames() if frames is None else frames
    voiced = [True] * 8 + [False] * 20
    recognizer = FakeRecognizer(texts)
    runner = FakeRunner()
    controller = VoiceController(
        runner, _config(**kw), FakeMic(frames), FakeVAD(voiced), recognizer, FakeTTS(),
        FakeSink(), wake_spotter=spotter,
        on_event=None if events is None else events.append,
        on_state=None if states is None else states.append,
        on_transcript=lambda _t: None,
    )
    return controller, recognizer, runner


def _terminals(events):
    return [e for e in events if e.stage == "terminal"]


async def test_transcript_backend_unchanged():
    events = []
    controller, recognizer, runner = _controller(None, ["saat kaç"], events=events)
    await controller.run()
    assert recognizer.calls == 1
    assert all(e.reason != "no_wake_kws" for e in _terminals(events))


async def test_kws_without_hit_drops_segment_before_stt():
    events = []
    spotter = FakeSpotter()
    controller, recognizer, runner = _controller(spotter, ["artemis saat kaç"], events=events)
    await controller.run()
    assert recognizer.calls == 0
    assert runner.commands == []
    assert [(e.outcome, e.reason) for e in _terminals(events)] == [("rejected", "no_wake_kws")]
    assert spotter.calls >= 1 and spotter.closed


async def test_kws_hit_runs_stt_and_wake_logic_still_applies():
    spotter = FakeSpotter([True])
    controller, recognizer, runner = _controller(spotter, ["artemis saat kaç"])
    await controller.run()
    assert recognizer.calls == 1
    assert runner.commands == ["saat kaç"]


async def test_hit_without_wake_word_in_transcript_is_not_a_command():
    spotter = FakeSpotter([True])
    controller, recognizer, runner = _controller(spotter, ["saat kaç"])
    await controller.run()
    assert recognizer.calls == 1
    assert runner.commands == []


async def _gate(controller, segment=None):
    segment = segment or AudioSegment(b"\x00\x00", 100.0, 101.0)
    return await controller._wake_gate_drops(segment)


async def test_gate_skipped_for_followup_continuous_and_barge_in_output():
    controller, *_ = _controller(FakeSpotter(), [])
    assert await _gate(controller) is True
    controller._awaiting_followup = True
    assert await _gate(controller) is False
    controller._awaiting_followup = False
    controller._followup_until = controller.clock() + 5
    assert await _gate(controller) is False
    controller._followup_until = 0.0
    controller.continuous = True
    assert await _gate(controller) is False
    controller.continuous = False
    controller._output_active = True
    assert await _gate(controller) is False
    controller.config = _config(barge_in_without_wake=False)
    assert await _gate(controller) is True
    await controller.close()


async def test_spotter_error_falls_back_to_transcript_with_one_warning(caplog):
    states = []
    spotter = FakeSpotter([RuntimeError("boom"), RuntimeError("boom")])
    controller, recognizer, runner = _controller(
        spotter, ["artemis saat kaç"], states=states
    )
    with caplog.at_level("WARNING"):
        await controller.run()
    assert recognizer.calls == 1
    assert runner.commands == ["saat kaç"]
    assert states.count("wake:unavailable:runtime") == 1
    assert len([r for r in caplog.records if "ön-kapısı" in r.message]) == 1
    assert spotter.calls == 1


async def test_gate_awaits_in_flight_feed_briefly():
    spotter = FakeSpotter(delay=0.1)
    controller, *_ = _controller(spotter, [])
    spotter.script = [True]
    controller._wake_step(AudioFrame(PCM * 5, 100.2))  # 100 ms: bir uçuş başlar
    assert controller._wake_task is not None
    assert await _gate(controller, AudioSegment(b"\x00\x00", 100.1, 101.0)) is False
    assert controller._wake_heard_at == 100.2
    await controller.close()


async def test_approval_clears_wake_evidence_and_stale_hits():
    # Onay sırasındaki/öncesindeki ses sonradan uyandırma kanıtı olmaz (CLAUDE.md ses kuralı).
    spotter = FakeSpotter(delay=0.05)
    controller, *_ = _controller(spotter, [])
    spotter.script = [True]
    controller._wake_step(AudioFrame(PCM * 5, 100.2))
    in_flight = controller._wake_task
    controller.pause_for_approval()
    assert in_flight is not None
    await asyncio.wait({in_flight})
    await asyncio.sleep(0)
    assert controller._wake_heard_at == float("-inf")
    controller._wake_heard_at = 100.5
    controller.resume_after_approval()
    assert controller._wake_heard_at == float("-inf")
    await controller.close()


async def test_gate_wait_is_bounded():
    spotter = FakeSpotter(delay=0.8)
    controller, *_ = _controller(spotter, [])
    controller._wake_step(AudioFrame(PCM * 5, 100.2))
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await _gate(controller) is True
    assert loop.time() - started < 0.6
    await controller.close()


async def test_factory_empty_model_returns_none_with_state():
    states = []
    voice = VoiceConfig(wake_backend="kws")
    assert await factory.build_wake_spotter(voice, states.append) is None
    assert states == ["wake:unavailable:model:live_transcript_model/revision boş"]


async def test_factory_transcript_backend_none_without_state():
    states = []
    assert await factory.build_wake_spotter(VoiceConfig(), states.append) is None
    assert states == []


async def test_factory_prepare_failure_returns_none_with_state(monkeypatch):
    def broken(self):
        raise RuntimeError("pip install .[stream]")

    monkeypatch.setattr(factory.WakeSpotter, "prepare", broken)
    states = []
    voice = VoiceConfig(
        wake_backend="kws", live_transcript_model="x/y", live_transcript_revision="abc"
    )
    assert await factory.build_wake_spotter(voice, states.append) is None
    assert states and states[0].startswith("wake:unavailable:deps:")
