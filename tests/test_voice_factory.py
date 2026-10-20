from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.channels.voice import factory
from jarvis.config.settings import VoiceConfig


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


def _patch_voice_deps(monkeypatch, mic):
    monkeypatch.setattr(factory, "FasterWhisperRecognizer",
                        lambda _config: SimpleNamespace(prepare=lambda: None))
    monkeypatch.setattr(factory, "PiperSynthesizer",
                        lambda _config: SimpleNamespace(prepare=lambda: None))
    monkeypatch.setattr(factory, "SoundDeviceMicrophone", lambda *_args, **_kwargs: mic)
    monkeypatch.setattr(factory, "SoundDeviceSink", lambda: object())
    monkeypatch.setattr(factory, "WebRTCVAD", lambda _config: object())


def _runtime(config):
    return SimpleNamespace(settings=SimpleNamespace(voice=config), audit=object(), runner=object())


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
