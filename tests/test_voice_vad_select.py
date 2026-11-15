import pytest

from artemis.channels.voice import factory
from artemis.channels.voice.adapters import WebRTCVAD
from artemis.channels.voice.silero import SileroVAD
from artemis.config.settings import VoiceConfig


def test_auto_uses_bundled_silero() -> None:
    states: list[str] = []
    vad = factory.build_vad(VoiceConfig(), states.append)
    assert isinstance(vad, SileroVAD) and states == []


def test_webrtc_backend_keeps_old_behavior() -> None:
    pytest.importorskip("webrtcvad")
    assert isinstance(factory.build_vad(VoiceConfig(vad_backend="webrtc"), [].append), WebRTCVAD)


@pytest.mark.parametrize("error", [OSError("yok"), ValueError("sha"), RuntimeError("ort")])
def test_silero_failure_falls_back_visibly(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    pytest.importorskip("webrtcvad")  # yedek WebRTC VAD [voice] ekinde

    def broken(**_kwargs: object) -> SileroVAD:
        raise error

    monkeypatch.setattr(factory, "SileroVAD", broken)
    states: list[str] = []
    vad = factory.build_vad(VoiceConfig(vad_backend="silero"), states.append)
    assert isinstance(vad, WebRTCVAD) and states == ["vad:webrtc"]
