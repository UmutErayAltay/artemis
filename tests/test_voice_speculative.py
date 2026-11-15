"""ADR 0071: bitiş sessizliğinde spekülatif nihai STT."""

import asyncio

from artemis.channels.voice.core import AudioFrame, VoiceController
from artemis.config.settings import VoiceConfig
from tests.test_voice_core import FakeMic, FakeRecognizer, FakeRunner, FakeSink, FakeTTS, FakeVAD

CFG = {"frame_ms": 20, "pre_roll_ms": 0, "end_silence_ms": 600}


def _frames(n):
    return [AudioFrame(bytes([i % 250]) * 2, float(i)) for i in range(n)]


def _controller(voiced, recognizer, mic_delay=0, **overrides):
    runner = FakeRunner()
    timings = []
    controller = VoiceController(
        runner, VoiceConfig(**CFG, **overrides),
        FakeMic(_frames(len(voiced)), delay=mic_delay), FakeVAD(voiced),
        recognizer, FakeTTS(), FakeSink(), on_timing=lambda k, v: timings.append((k, v)),
    )
    return controller, runner, timings


class SlowRecognizer(FakeRecognizer):
    def __init__(self, texts, fail_first=False):
        super().__init__(texts)
        self.fail_first = fail_first
        self.pcms = []

    async def transcribe(self, pcm):
        self.pcms.append(pcm)
        await asyncio.sleep(0.01)
        if self.fail_first and self.calls == 0:
            self.calls += 1
            raise RuntimeError("boom")
        return await super().transcribe(pcm)

    async def transcribe_partial(self, _pcm):
        return ""


NORMAL = [True] * 5 + [False] * 30


async def test_speculation_adopted_single_decode():
    recognizer = SlowRecognizer(["artemis saat kaç?"])
    controller, runner, timings = _controller(NORMAL, recognizer)
    await controller.run()
    assert recognizer.calls == 1
    assert runner.commands == ["saat kaç?"]
    assert ("stt_speculative_hit", 1.0) in timings


async def test_resumed_speech_discards_speculation():
    # 12 sessizlik karesi (>=10: spekülasyon başlar) sonra konuşma devam eder.
    voiced = [True] * 5 + [False] * 12 + [True] * 3 + [False] * 30
    recognizer = SlowRecognizer(["artemis erken", "artemis tam komut"])
    # Kareler arası bekleme: spekülasyon (0,01 sn) konuşma dönmeden kesin biter;
    # beklemesiz kareler yavaş CI'da spekülasyonu bitmeden iptal ettiriyordu (yarış).
    controller, runner, _ = _controller(voiced, recognizer, mic_delay=0.03)
    await controller.run()
    assert recognizer.calls == 2
    assert runner.commands == ["tam komut"]
    # İlk (kısa) anlık görüntü atıldı; nihai metin tam bölütün yeni spekülasyonundan gelir.
    assert len(recognizer.pcms[0]) < len(recognizer.pcms[-1])


async def test_approval_pause_during_speculation_never_adopted():
    recognizer = SlowRecognizer(["artemis eski ses"])
    controller, runner, _ = _controller(NORMAL, recognizer)
    uid = controller._capturing_id = 1
    controller._spec.step(10, b"ab", uid, controller._capture_epoch, True)
    assert controller._spec._current is not None
    controller.pause_for_approval()
    assert controller._spec._current is None
    assert await controller._spec.result(uid, controller._capture_epoch) is None
    await asyncio.sleep(0.05)
    assert runner.commands == []


async def test_epoch_change_after_seal_is_not_adopted():
    recognizer = SlowRecognizer(["artemis eski ses"])
    controller, runner, _ = _controller([], recognizer)
    spec = controller._spec
    spec.step(10, b"ab", 1, controller._capture_epoch, True)
    spec.seal(1, b"abcd", controller._capture_epoch)
    controller._invalidate_segments()
    assert await spec.result(1, controller._capture_epoch) is None
    assert runner.commands == []


async def test_speculative_error_falls_back():
    recognizer = SlowRecognizer(["artemis saat kaç?"], fail_first=True)
    controller, runner, timings = _controller(NORMAL, recognizer)
    await controller.run()
    assert runner.commands == ["saat kaç?"]
    assert len(recognizer.pcms) == 2
    assert ("stt_speculative_hit", 0.0) in timings


async def test_disabled_decodes_once_after_close():
    recognizer = SlowRecognizer(["artemis saat kaç?"])
    controller, runner, timings = _controller(NORMAL, recognizer, stt_speculative=False)
    await controller.run()
    assert recognizer.calls == 1
    assert runner.commands == ["saat kaç?"]
    assert not any(k == "stt_speculative_hit" for k, _ in timings)


async def test_partial_transcript_never_dispatches():
    recognizer = SlowRecognizer([])
    recognizer.transcribe_partial = lambda _pcm: _async("artemis delete_file x")
    controller, runner, _ = _controller([True] * 60 + [False] * 5, recognizer)
    await controller.run()
    assert runner.commands == []


async def _async(value):
    return value


# --- ADR 0069: KWS kapısı spekülasyonu da kapatır -----------------------------------------


class _Spotter:
    def feed(self, *_a, **_k):
        return None

    def close(self):
        pass


def _kws(voiced, recognizer):
    controller, runner, timings = _controller(voiced, recognizer)
    controller._wake_spotter = _Spotter()
    return controller, runner, timings


async def test_kws_no_hit_never_reaches_stt():
    recognizer = SlowRecognizer(["artemis saat kaç?"])
    controller, runner, timings = _kws(NORMAL, recognizer)
    await controller.run()
    assert recognizer.calls == 0 and recognizer.pcms == []
    assert runner.commands == []
    assert not any(k == "stt_speculative_hit" for k, _ in timings)


async def test_kws_hit_allows_speculation_and_adopts():
    recognizer = SlowRecognizer(["artemis saat kaç?"])
    controller, runner, timings = _kws(NORMAL, recognizer)
    controller._wake_heard_at = 1000.0
    await controller.run()
    assert recognizer.calls == 1
    assert runner.commands == ["saat kaç?"]
    assert ("stt_speculative_hit", 1.0) in timings
