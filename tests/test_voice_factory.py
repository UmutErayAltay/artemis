from types import SimpleNamespace

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
