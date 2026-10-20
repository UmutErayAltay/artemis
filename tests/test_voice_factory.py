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


async def test_noise_suppression_without_model_dir_or_files_fails_actionably(
    monkeypatch, tmp_path: Path
):
    _patch_voice_deps(monkeypatch, object())
    with pytest.raises(ValueError, match="fetch_noise_model"):
        await factory.build_voice_controller(
            _runtime(VoiceConfig(noise_suppression=True)), lambda _: None, lambda _: None
        )
    config = VoiceConfig(noise_suppression=True, noise_model_dir=tmp_path / "yok")
    with pytest.raises(OSError, match="fetch_noise_model"):
        await factory.build_voice_controller(_runtime(config), lambda _: None, lambda _: None)
