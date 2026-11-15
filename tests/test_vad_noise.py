import pytest

pytest.importorskip("onnxruntime")
pytest.importorskip("webrtcvad")

from bench import vad_noise  # noqa: E402


def test_silero_rejects_noise_that_webrtc_flags() -> None:
    # Ölçüm iddiası (ADR 0067): gürültüde Silero neredeyse hiç "konuşma" demez.
    result = vad_noise.report()
    assert all(row["silero"] <= 0.05 for row in result.values())
