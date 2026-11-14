"""Silero VAD sarmalayıcı testleri: sahte oturumla akış mantığı, gerçek modelle duman testi."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from artemis.channels.voice.silero import (
    SILERO_MODEL_DIR,
    SILERO_MODEL_NAME,
    SileroVAD,
    verify_silero_model,
)


class FakeSession:
    """Sahte oturum: betikteki olasılıkları sırayla döner; gelen her girdiyi kaydeder."""

    def __init__(self, probabilities: list[float]) -> None:
        self._probabilities = list(probabilities)
        self.feeds: list[dict[str, object]] = []

    def run(self, output_names: object, feeds: dict[str, object]) -> list[np.ndarray]:
        self.feeds.append(feeds)
        p = self._probabilities.pop(0) if self._probabilities else 0.0
        state = feeds["state"]
        assert isinstance(state, np.ndarray)
        return [np.array([[p]], np.float32), state + 1]


def zeros_frame(n: int) -> bytes:
    return np.zeros(n, dtype="<i2").tobytes()


def test_partial_frames_run_model_once() -> None:
    session = FakeSession([0.9])
    vad = SileroVAD(threshold=0.5, session=session)
    frame = zeros_frame(320)
    # 320 örnek tek pencere doldurmaz: model hiç çalışmaz, başlangıç olasılığı 0.0.
    assert vad.is_speech(frame) is False
    assert len(session.feeds) == 0
    # 640 örneğe ulaşıldı: bir pencere tüketilir ve olasılık betikteki 0.9 olur.
    assert vad.is_speech(frame) is True
    assert len(session.feeds) == 1
    feed = session.feeds[0]
    inputs = feed["input"]
    assert isinstance(inputs, np.ndarray)
    assert inputs.shape == (1, 576)
    # 128 + 320 = 448 < 512: üçüncü kare yeni pencere doldurmaz.
    assert vad.is_speech(frame) is True
    assert len(session.feeds) == 1


def test_context_carries_between_runs() -> None:
    session = FakeSession([0.1, 0.2])
    vad = SileroVAD(session=session)
    data = (np.arange(1024) % 1000).astype("<i2").tobytes()
    vad.is_speech(data)
    assert len(session.feeds) == 2
    first, second = session.feeds
    first_input = first["input"]
    second_input = second["input"]
    assert isinstance(first_input, np.ndarray) and isinstance(second_input, np.ndarray)
    # Bağlam, önceki girdinin son 64 örneğidir.
    np.testing.assert_array_equal(second_input[:, :64], first_input[:, -64:])


def test_state_carries_between_runs() -> None:
    session = FakeSession([0.1, 0.2])
    vad = SileroVAD(session=session)
    vad.is_speech((np.arange(1024) % 1000).astype("<i2").tobytes())
    assert len(session.feeds) == 2
    first_state = session.feeds[0]["state"]
    second_state = session.feeds[1]["state"]
    assert isinstance(first_state, np.ndarray) and isinstance(second_state, np.ndarray)
    # Sahte oturum state + 1 döner: ilk girdi sıfırlar, ikinci girdi ilk çıktının kendisidir.
    np.testing.assert_array_equal(first_state, np.zeros((2, 1, 128), dtype=np.float32))
    np.testing.assert_array_equal(second_state, np.ones((2, 1, 128), dtype=np.float32))
    np.testing.assert_array_equal(vad._state, np.full((2, 1, 128), 2, dtype=np.float32))


def test_sr_feed_is_int64_scalar_16000() -> None:
    session = FakeSession([0.5])
    vad = SileroVAD(session=session)
    vad.is_speech(zeros_frame(512))
    sr = session.feeds[0]["sr"]
    assert isinstance(sr, np.ndarray)
    assert sr.dtype == np.int64
    assert sr.ndim == 0
    assert int(sr) == 16000


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
def test_threshold_validation_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(ValueError):
        SileroVAD(threshold=bad, session=FakeSession([]))


def test_threshold_validation_accepts_valid() -> None:
    vad = SileroVAD(threshold=0.5, session=FakeSession([]))
    assert vad.threshold == 0.5


def test_reset_clears_stream_state() -> None:
    session = FakeSession([0.9])
    vad = SileroVAD(session=session)
    vad.is_speech(zeros_frame(512))
    vad.is_speech(zeros_frame(320))  # kısmi kare: tamponda kalır
    assert vad.probability == pytest.approx(0.9)
    vad.reset()
    assert vad.probability == 0.0
    assert vad._buffer.size == 0
    assert np.all(vad._state == 0.0)
    assert np.all(vad._context == 0.0)


def test_verify_silero_model_accepts_real_copy(tmp_path: Path) -> None:
    source = SILERO_MODEL_DIR / SILERO_MODEL_NAME
    target = tmp_path / SILERO_MODEL_NAME
    # Yalnız dosya kopyası; model çalıştırılmaz ve gerçek dizine yazılmaz.
    shutil.copy(source, target)
    assert verify_silero_model(tmp_path) == source.read_bytes()


def test_verify_silero_model_rejects_corrupted_copy(tmp_path: Path) -> None:
    source = SILERO_MODEL_DIR / SILERO_MODEL_NAME
    target = tmp_path / SILERO_MODEL_NAME
    shutil.copy(source, target)
    blob = bytearray(target.read_bytes())
    blob[-1] ^= 0x01
    target.write_bytes(bytes(blob))
    with pytest.raises(ValueError):
        verify_silero_model(tmp_path)


def test_verify_silero_model_missing_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        verify_silero_model(tmp_path)


def test_real_model_smoke_silence_is_not_speech() -> None:
    pytest.importorskip("onnxruntime")
    vad = SileroVAD()
    pcm = zeros_frame(16000)
    for offset in range(0, 16000, 320):
        assert not vad.is_speech(pcm[offset * 2 : (offset + 320) * 2])
    assert vad.probability < 0.5
