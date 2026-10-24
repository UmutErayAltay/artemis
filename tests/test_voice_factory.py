from pathlib import Path
from types import SimpleNamespace

import pytest

from artemis.channels.voice import factory
from artemis.config.settings import VoiceConfig


async def test_edge_without_fallback_does_not_load_local_male_voice(monkeypatch):
    config = VoiceConfig(tts_backend="edge", tts_local_fallback=False,
                         allow_cloud_tts_text=True, edge_voice="tr-TR-EmelNeural")
    runtime = SimpleNamespace(
        settings=SimpleNamespace(voice=config), audit=object(), runner=object(),
    )
    primary = object()

    def forbidden(*_args):
        raise AssertionError("Yerel ses bu profilde yüklenmemeli")

    monkeypatch.setattr(factory, "PiperSynthesizer", forbidden)
    monkeypatch.setattr(factory, "FasterWhisperRecognizer",
                        lambda _config: SimpleNamespace(prepare=lambda: None))
    monkeypatch.setattr(factory, "EdgeTTSynthesizer", lambda *_args: primary)
    monkeypatch.setattr(factory, "SoundDeviceMicrophone", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(factory, "SoundDeviceSink", lambda: object())
    monkeypatch.setattr(factory, "WebRTCVAD", lambda _config: object())
    controller = await factory.build_voice_controller(runtime, lambda _: None, lambda _: None)
    assert controller.synthesizer is primary


async def test_edge_with_default_fallback_but_no_piper_model_still_builds(monkeypatch):
    """Varsayılan `tts_local_fallback=True` iken Piper modeli yoksa Edge sesi tek başına açılır.

    Gerçek PiperSynthesizer kullanılır: kurucusu model yokken ValueError fırlatır ve eskiden
    bu, Edge çalışabilecekken ses kanalını tamamen kapatıyordu (EMA dalı bunu zaten denetliyordu).
    """
    config = VoiceConfig(
        tts_backend="edge", allow_cloud_tts_text=True, edge_voice="tr-TR-EmelNeural"
    )
    assert config.tts_local_fallback and config.piper_model is None
    primary = object()
    monkeypatch.setattr(factory, "FasterWhisperRecognizer",
                        lambda _config: SimpleNamespace(prepare=lambda: None))
    monkeypatch.setattr(factory, "EdgeTTSynthesizer", lambda *_args: primary)
    monkeypatch.setattr(factory, "SoundDeviceMicrophone", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(factory, "SoundDeviceSink", lambda: object())
    monkeypatch.setattr(factory, "WebRTCVAD", lambda _config: object())
    controller = await factory.build_voice_controller(
        _runtime(config), lambda _: None, lambda _: None
    )
    assert controller.synthesizer is primary


def _patch_voice_deps(monkeypatch, mic):
    monkeypatch.setattr(factory, "FasterWhisperRecognizer",
                        lambda _config: SimpleNamespace(prepare=lambda: None))
    monkeypatch.setattr(factory, "PiperSynthesizer",
                        lambda _config: SimpleNamespace(prepare=lambda: None))
    opened: list[dict] = []

    def make_mic(*_args, **kwargs):
        opened.append(kwargs)
        return mic

    monkeypatch.setattr(factory, "SoundDeviceMicrophone", make_mic)

    monkeypatch.setattr(factory, "SoundDeviceSink", lambda: object())
    monkeypatch.setattr(factory, "WebRTCVAD", lambda _config: object())
    return opened


def _runtime(config):
    return SimpleNamespace(settings=SimpleNamespace(voice=config), audit=object(), runner=object())


async def test_ema_backend_uses_ema_synthesizer(monkeypatch):
    _patch_voice_deps(monkeypatch, object())
    created = []

    class FakeEma:
        def __init__(self, config):
            self.config = config
            self.prepared = False
            created.append(self)

        def prepare(self):
            self.prepared = True

    monkeypatch.setattr(factory, "EmaSynthesizer", FakeEma)
    config = VoiceConfig(tts_backend="ema")
    controller = await factory.build_voice_controller(
        _runtime(config), lambda _: None, lambda _: None
    )

    assert controller.synthesizer is created[0]
    assert created[0].prepared


async def test_ema_backend_uses_piper_fallback_when_model_is_set(monkeypatch, tmp_path):
    _patch_voice_deps(monkeypatch, object())
    ema = SimpleNamespace(prepare=lambda: None)
    piper = SimpleNamespace(prepare=lambda: None)
    monkeypatch.setattr(factory, "EmaSynthesizer", lambda _config: ema)
    monkeypatch.setattr(factory, "PiperSynthesizer", lambda _config: piper)
    config = VoiceConfig(tts_backend="ema", piper_model=tmp_path / "voice.onnx")
    controller = await factory.build_voice_controller(
        _runtime(config), lambda _: None, lambda _: None
    )

    assert isinstance(controller.synthesizer, factory.FallbackSynthesizer)
    assert controller.synthesizer.primary is ema
    assert controller.synthesizer.local is piper


async def test_noise_suppression_off_keeps_raw_microphone(monkeypatch):
    mic = object()
    _patch_voice_deps(monkeypatch, mic)
    controller = await factory.build_voice_controller(
        _runtime(VoiceConfig()), lambda _: None, lambda _: None
    )
    assert controller.microphone is mic


async def test_noise_suppression_wraps_microphone_with_level_mapping(monkeypatch, tmp_path):
    mic = object()
    _patch_voice_deps(monkeypatch, mic)
    seen = {}

    class FakeDenoiser:
        frame_samples = 320
        delay_samples = 1824

        def __init__(self, model_dir, **kwargs):
            seen.update(model_dir=model_dir, **kwargs)

    monkeypatch.setattr(factory, "DeepFilterNetDenoiser", FakeDenoiser)
    config = VoiceConfig(noise_suppression=True, noise_suppression_level="hafif",
                         noise_model_dir=tmp_path)
    controller = await factory.build_voice_controller(
        _runtime(config), lambda _: None, lambda _: None
    )
    assert isinstance(controller.microphone, factory.DenoisedMicrophone)
    assert seen == {"model_dir": tmp_path, "atten_lim_db": 12.0, "frame_samples": 320}
    assert controller.microphone._raw_for_stt is True  # varsayılan: STT ham ses

    config = VoiceConfig(
        noise_suppression=True, noise_suppression_stt=True, noise_model_dir=tmp_path
    )
    controller = await factory.build_voice_controller(
        _runtime(config), lambda _: None, lambda _: None
    )
    assert controller.microphone._raw_for_stt is False


async def test_noise_suppression_works_out_of_the_box_with_vendored_model(monkeypatch):
    """noise_model_dir verilmeden pakete dahil model yüklenir: indirme adımı yok."""
    pytest.importorskip("onnxruntime")
    mic = object()
    _patch_voice_deps(monkeypatch, mic)
    states: list[str] = []
    controller = await factory.build_voice_controller(
        _runtime(VoiceConfig(noise_suppression=True)), lambda _: None, states.append
    )
    assert isinstance(controller.microphone, factory.DenoisedMicrophone)
    assert not [s for s in states if s.startswith("denoise:unavailable")]


async def test_noise_suppression_failure_degrades_to_raw_mic_with_visible_state(
    monkeypatch, tmp_path: Path, caplog
):
    mic = object()
    _patch_voice_deps(monkeypatch, mic)
    monkeypatch.setattr(factory, "DEFAULT_NOISE_MODEL_DIR", tmp_path / "varsayilan-yok")
    cases = [
        (VoiceConfig(noise_suppression=True), "model:model dosyaları bulunamadı"),
        (VoiceConfig(noise_suppression=True, noise_model_dir=tmp_path / "yok"),
         "model:model dosyaları bulunamadı"),
    ]
    for config, reason in cases:
        states: list[str] = []
        caplog.clear()
        controller = await factory.build_voice_controller(
            _runtime(config), lambda _: None, states.append
        )
        assert controller.microphone is mic
        unavailable = [s for s in states if s.startswith("denoise:unavailable:")]
        assert len(unavailable) == 1 and reason in unavailable[0]
        assert sum(r.levelno >= 30 for r in caplog.records if "Gürültü" in r.getMessage()) == 1
    # sha256 uyuşmazlığı da düşürmez
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "dfn3_step.onnx").write_bytes(b"x")
    states = []
    controller = await factory.build_voice_controller(
        _runtime(VoiceConfig(noise_suppression=True, noise_model_dir=tmp_path / "bad")),
        lambda _: None, states.append,
    )
    assert controller.microphone is mic
    assert any("model:model dosyası sha256" in s for s in states)


async def test_missing_dependencies_are_reported_as_deps_not_model(monkeypatch):
    _patch_voice_deps(monkeypatch, object())

    def no_onnxruntime(*_args, **_kwargs):
        raise factory.DenoiseDependencyError("onnxruntime yok")

    monkeypatch.setattr(factory, "DeepFilterNetDenoiser", no_onnxruntime)
    states: list[str] = []
    await factory.build_voice_controller(
        _runtime(VoiceConfig(noise_suppression=True)), lambda _: None, states.append
    )
    assert any(s.startswith("denoise:unavailable:deps:") for s in states)


async def test_denoise_on_captures_microphone_at_48k_and_off_stays_16k(monkeypatch):
    pytest.importorskip("onnxruntime")
    opened = _patch_voice_deps(monkeypatch, object())
    await factory.build_voice_controller(
        _runtime(VoiceConfig(noise_suppression=True)), lambda _: None, lambda _: None
    )
    assert opened[-1]["capture_rate"] == 48000
    await factory.build_voice_controller(_runtime(VoiceConfig()), lambda _: None, lambda _: None)
    assert opened[-1]["capture_rate"] is None
    # Gürültü engelleme açılamazsa mikrofon 16 kHz kalır.
    await factory.build_voice_controller(
        _runtime(VoiceConfig(noise_suppression=True, noise_model_dir=Path("/yok/yok"))),
        lambda _: None, lambda _: None,
    )
    assert opened[-1]["capture_rate"] is None


def _capture_sink(monkeypatch):
    calls = []

    class FakeSink:
        def __init__(self, echo_reference=None, on_echo_failure=None):
            self._echo_reference = echo_reference
            self._on_echo_failure = on_echo_failure

    def make_sink(*_args, **kwargs):
        sink = FakeSink(**kwargs)
        calls.append((kwargs, sink))
        return sink

    monkeypatch.setattr(factory, "SoundDeviceSink", make_sink)
    return calls


def _capture_canceller(monkeypatch):
    created = []

    class FakeCanceller:
        def __init__(self, rate, reference, *, delay_ms):
            self.rate = rate
            self.reference = reference
            self.delay_ms = delay_ms

        def process(self, pcm):
            return pcm

        def reset(self):
            return None

    def make_canceller(rate, reference, *, delay_ms):
        canceller = FakeCanceller(rate, reference, delay_ms=delay_ms)
        created.append(canceller)
        return canceller

    monkeypatch.setattr(factory, "EchoCanceller", make_canceller)
    return created


async def test_echo_cancellation_off_keeps_raw_microphone_and_sink(monkeypatch):
    mic = object()
    _patch_voice_deps(monkeypatch, mic)
    sink_calls = _capture_sink(monkeypatch)

    controller = await factory.build_voice_controller(
        _runtime(VoiceConfig()), lambda _: None, lambda _: None
    )

    assert controller.microphone is mic
    assert not isinstance(controller.microphone, factory.EchoCancelledMicrophone)
    assert len(sink_calls) == 1
    kwargs, sink = sink_calls[0]
    assert kwargs == {}
    assert sink._echo_reference is None


async def test_echo_cancellation_wraps_microphone_and_shares_sink_reference(monkeypatch):
    mic = object()
    _patch_voice_deps(monkeypatch, mic)
    sink_calls = _capture_sink(monkeypatch)
    cancellers = _capture_canceller(monkeypatch)
    config = VoiceConfig(echo_cancellation=True)
    states: list[str] = []

    controller = await factory.build_voice_controller(
        _runtime(config), lambda _: None, states.append
    )

    microphone = controller.microphone
    assert isinstance(microphone, factory.EchoCancelledMicrophone)
    assert microphone._inner is mic

    canceller = microphone._canceller_for(config.sample_rate)
    assert cancellers == [canceller]

    kwargs, sink = sink_calls[0]
    assert set(kwargs) == {"echo_reference", "on_echo_failure"}
    assert isinstance(sink._echo_reference, factory.EchoReference)
    assert kwargs["echo_reference"] is sink._echo_reference
    assert canceller.reference is sink._echo_reference

    on_echo_failure = kwargs["on_echo_failure"]
    assert callable(on_echo_failure)
    on_echo_failure()
    assert states == ["echo:off"]
    # Aynı arızayı mikrofon sarmalayıcısı da bildirirse sohbette ikinci mesaj çıkmaz.
    microphone._on_state("echo:off")
    on_echo_failure()
    assert states == ["echo:off"]


async def test_echo_cancellation_is_applied_before_denoising(monkeypatch, tmp_path):
    mic = object()
    _patch_voice_deps(monkeypatch, mic)
    sink_calls = _capture_sink(monkeypatch)
    cancellers = _capture_canceller(monkeypatch)

    class FakeDenoiser:
        frame_samples = 320
        delay_samples = 0

        def __init__(self, _model_dir, **_kwargs):
            pass

    monkeypatch.setattr(factory, "DeepFilterNetDenoiser", FakeDenoiser)
    config = VoiceConfig(
        noise_suppression=True,
        echo_cancellation=True,
        noise_model_dir=tmp_path,
    )

    controller = await factory.build_voice_controller(
        _runtime(config), lambda _: None, lambda _: None
    )

    denoised = controller.microphone
    assert isinstance(denoised, factory.DenoisedMicrophone)
    assert denoised._raw_for_stt is True

    microphone = denoised._inner
    assert isinstance(microphone, factory.EchoCancelledMicrophone)
    assert microphone._inner is mic

    canceller = microphone._canceller_for(48000)
    _kwargs, sink = sink_calls[0]
    assert isinstance(sink._echo_reference, factory.EchoReference)
    assert canceller.reference is sink._echo_reference
    assert cancellers == [canceller]


async def test_factory_forwards_optional_timing_hooks(monkeypatch):
    _patch_voice_deps(monkeypatch, object())
    timing, events = [], []
    def on_timing(stage, timestamp):
        timing.append((stage, timestamp))
    controller = await factory.build_voice_controller(
        _runtime(VoiceConfig()), lambda _: None, lambda _: None,
        on_timing=on_timing, on_event=events.append,
    )
    assert controller.on_timing is on_timing
    controller._event(1, "speech_start", timestamp=1.)
    assert events[0].utterance_id == 1


async def test_factory_cleans_partial_setup_after_model_failure(monkeypatch):
    closed = []

    class Resource:
        def __init__(self, name):
            self.name = name

        async def close(self):
            closed.append(self.name)

    mic, sink, tts = Resource("mic"), Resource("sink"), Resource("tts")
    _patch_voice_deps(monkeypatch, mic)
    monkeypatch.setattr(factory, "SoundDeviceSink", lambda: sink)

    def fail_prepare():
        raise RuntimeError("secret model path")

    tts.prepare = fail_prepare
    monkeypatch.setattr(factory, "PiperSynthesizer", lambda _: tts)
    with pytest.raises(RuntimeError):
        await factory.build_voice_controller(
            _runtime(VoiceConfig()), lambda _: None, lambda _: None,
        )
    assert closed == ["tts", "sink", "mic"]


async def test_factory_cancelled_prepare_waits_to_dispose_model(monkeypatch):
    import asyncio
    import threading

    started, release = threading.Event(), threading.Event()
    disposed = asyncio.Event()

    class LoadingModel:
        def prepare(self):
            started.set()
            assert release.wait(5)

        async def close(self):
            assert release.is_set()
            disposed.set()

    _patch_voice_deps(monkeypatch, object())
    monkeypatch.setattr(factory, "PiperSynthesizer", lambda _: LoadingModel())
    task = asyncio.create_task(factory.build_voice_controller(
        _runtime(VoiceConfig()), lambda _: None, lambda _: None,
    ))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not disposed.is_set()
    finally:
        release.set()
    await asyncio.wait_for(disposed.wait(), 5)
