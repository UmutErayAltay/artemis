"""ADR 0047: canlı yazı yalnız gösterimdir; sahte tanıyıcı, ağ ve model yok."""

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from artemis.channels.voice import factory
from artemis.channels.voice.core import AudioFrame, VoiceController
from artemis.channels.voice.live_text import MODEL_FILES, SherpaLiveTranscriber
from artemis.config.settings import VoiceConfig
from tests.test_voice_core import FakeMic, FakeRecognizer, FakeRunner, FakeSink, FakeTTS, FakeVAD

PCM = b"\x01\x00" * 320  # 20 ms @ 16 kHz


class FakeLive:
    def __init__(self, texts=(), fail_at=None):
        self.texts = list(texts)
        self.fail_at = fail_at
        self.fed = 0
        self.resets = 0
        self.closed = False

    def feed(self, pcm16):
        self.fed += 1
        if self.fail_at == self.fed:
            raise RuntimeError("model çöktü")
        return self.texts[min(self.fed, len(self.texts)) - 1] if self.texts else ""

    def reset(self):
        self.resets += 1

    def close(self):
        self.closed = True


def _controller(live, voiced, *, texts=("artemis delete_file x",), clock=None):
    frames = [AudioFrame(PCM, index * 0.02) for index in range(len(voiced))]
    runner = FakeRunner()
    live_texts: list[str] = []
    states: list[str] = []
    config = VoiceConfig(
        frame_ms=20, pre_roll_ms=0, end_silence_ms=100, stt_partial_enabled=False
    )
    controller = VoiceController(
        runner, config, FakeMic(frames, delay=0.005), FakeVAD(voiced), FakeRecognizer(texts),
        FakeTTS(), FakeSink(), live_transcriber=live, on_live_text=live_texts.append,
        on_state=states.append, **({"clock": clock} if clock else {}),
    )
    return controller, runner, live_texts, states


async def test_live_text_is_display_only_and_never_dispatches():
    live = FakeLive(["artemis delete_file önemli.txt"])
    controller, runner, live_texts, _ = _controller(
        live, [True] * 30 + [False] * 8, texts=("selam",)
    )
    await controller.run()
    assert live.fed >= 1
    assert live_texts and live_texts[0] == "artemis delete_file önemli.txt"
    assert runner.commands == []  # canlı metin run_command'a hiç gitmez
    assert live.closed


async def test_dispatched_command_comes_from_final_stt_not_live_text():
    live = FakeLive(["artemis delete_file önemli.txt"])
    controller, runner, live_texts, _ = _controller(
        live, [True] * 30 + [False] * 8, texts=("artemis saat kaç",)
    )
    await controller.run()
    assert live_texts and runner.commands == ["saat kaç"]


async def test_live_text_does_not_change_wake_decision():
    # Canlı metin "artemis saat kaç" dese de nihai STT uyandırma içermiyorsa komut çıkmaz.
    live = FakeLive(["artemis saat kaç"])
    controller, runner, live_texts, _ = _controller(
        live, [True] * 30 + [False] * 8, texts=("selam",)
    )
    await controller.run()
    assert live_texts and runner.commands == []


async def test_resets_at_utterance_end_and_throttles_updates():
    ticks = iter(range(100_000))
    live = FakeLive([f"kelime {i}" for i in range(50)])
    # clock her çağrıda 10 ms ilerler: ~150 ms'den sık güncelleme geçmemeli.
    controller, _, live_texts, _ = _controller(
        live, [True] * 30 + [False] * 8, clock=lambda: next(ticks) * 0.01
    )
    await controller.run()
    assert live.resets >= 1
    assert 0 < len(live_texts) < live.fed


async def test_runtime_error_warns_once_disables_and_voice_continues(caplog):
    live = FakeLive(["a"], fail_at=2)
    controller, runner, _, states = _controller(
        live, [True] * 30 + [False] * 8 + [True] * 30 + [False] * 8,
        texts=("artemis saat kaç", "artemis tarih ne"),
    )
    await controller.run()
    assert states.count("live:unavailable:runtime") == 1
    assert sum("Canlı yazı durdu" in r.getMessage() for r in caplog.records) == 1
    assert live.fed == 2  # kapandıktan sonra beslenmez
    assert runner.commands  # ses kanalı komutları işlemeye devam etti


async def test_approval_reset_points_reset_live():
    live = FakeLive()
    controller, _, _, _ = _controller(live, [])
    controller.pause_for_approval()
    controller.resume_after_approval()
    controller.discard_during_approval()
    assert live.resets == 3
    await controller.close()


# --- factory ---


def _patch(monkeypatch):
    monkeypatch.setattr(factory, "FasterWhisperRecognizer",
                        lambda _c: SimpleNamespace(prepare=lambda: None))
    monkeypatch.setattr(factory, "PiperSynthesizer",
                        lambda _c: SimpleNamespace(prepare=lambda: None))
    monkeypatch.setattr(factory, "SoundDeviceMicrophone", lambda *_a, **_k: object())
    monkeypatch.setattr(factory, "SoundDeviceSink", lambda: object())
    monkeypatch.setattr(factory, "WebRTCVAD", lambda _c: object())


def _rt(config):
    return SimpleNamespace(settings=SimpleNamespace(voice=config), audit=object(), runner=object())


def _live_config():
    return VoiceConfig(
        live_transcript=True, live_transcript_model="o/m", live_transcript_revision="abc"
    )


async def test_factory_off_builds_nothing(monkeypatch):
    _patch(monkeypatch)
    built = []
    monkeypatch.setattr(factory, "SherpaLiveTranscriber", lambda *a: built.append(a))
    controller = await factory.build_voice_controller(
        _rt(VoiceConfig()), lambda _: None, lambda _: None
    )
    assert built == [] and controller._live is None


async def test_factory_empty_model_reports_model_unavailable(monkeypatch):
    _patch(monkeypatch)
    states: list[str] = []
    controller = await factory.build_voice_controller(
        _rt(VoiceConfig(live_transcript=True)), lambda _: None, states.append
    )
    assert controller._live is None
    assert [s for s in states if s.startswith("live:unavailable:model:")]


async def test_factory_missing_deps_reports_deps(monkeypatch):
    _patch(monkeypatch)

    class Missing:
        def __init__(self, *_a):
            pass

        def prepare(self):
            raise RuntimeError("Canlı yazı için `pip install .[stream]` gerekli.")

    monkeypatch.setattr(factory, "SherpaLiveTranscriber", Missing)
    states: list[str] = []
    controller = await factory.build_voice_controller(
        _rt(_live_config()), lambda _: None, states.append
    )
    assert controller._live is None
    assert any(s.startswith("live:unavailable:deps:") for s in states)


async def test_factory_wires_live_text_to_state_prefix(monkeypatch):
    _patch(monkeypatch)

    class Ok:
        def __init__(self, *_a):
            pass

        def prepare(self):
            pass

    monkeypatch.setattr(factory, "SherpaLiveTranscriber", Ok)
    states: list[str] = []
    controller = await factory.build_voice_controller(
        _rt(_live_config()), lambda _: None, states.append
    )
    controller.on_live_text("merhaba")
    assert "live:merhaba" in states


# --- live_text.py ---


def test_sherpa_transcriber_uses_pinned_revision_and_converts_pcm(monkeypatch):
    np = pytest.importorskip("numpy")
    downloads, waves = [], []

    class Stream:
        def accept_waveform(self, rate, samples):
            waves.append((rate, samples))

    class Recognizer:
        ready = 1
        kwargs: dict = {}

        @classmethod
        def from_transducer(cls, **kwargs):
            cls.kwargs = kwargs
            return cls()

        def create_stream(self):
            return Stream()

        def is_ready(self, _s):
            self.ready, ready = 0, self.ready
            return bool(ready)

        def decode_stream(self, _s):
            pass

        def get_result(self, _s):
            return SimpleNamespace(text=" merhaba ")

        def reset(self, _s):
            waves.append("reset")

    def download(**kw):
        downloads.append(kw)
        return f"/c/{kw['filename']}"

    monkeypatch.setitem(
        sys.modules, "sherpa_onnx", types.SimpleNamespace(OnlineRecognizer=Recognizer)
    )
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", types.SimpleNamespace(hf_hub_download=download)
    )
    live = SherpaLiveTranscriber("o/m", "sha1")
    live.prepare()
    assert [d["filename"] for d in downloads] == list(MODEL_FILES)
    assert {d["revision"] for d in downloads} == {"sha1"}
    assert {d["repo_id"] for d in downloads} == {"o/m"}
    assert Recognizer.kwargs["encoder"] == "/c/encoder.int8.onnx"
    assert Recognizer.kwargs["decoding_method"] == "modified_beam_search"
    assert live.feed(np.array([16384, -32768], dtype=np.int16).tobytes()) == "merhaba"
    rate, samples = waves[0]
    assert rate == 16000 and samples.dtype == np.float32 and list(samples) == [0.5, -1.0]
    live.reset()
    assert waves[-1] == "reset"


def test_sherpa_transcriber_missing_package_points_to_extra(monkeypatch):
    pytest.importorskip("numpy")
    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)  # import ImportError verir
    with pytest.raises(RuntimeError, match=r"pip install \.\[stream\]"):
        SherpaLiveTranscriber("o/m", "sha1").prepare()


def test_stream_extra_pinned_in_pyproject():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'stream = ["sherpa-onnx==2.0.1", "huggingface-hub==' in text
