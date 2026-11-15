"""Anlamsal konuşma sonu (ADR 0068): segmenter sabrı, Smart Turn sarmalayıcı, fabrika."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import numpy as np
import pytest

from artemis.channels.voice import factory
from artemis.channels.voice.core import AudioSegment, AudioSegmenter
from artemis.channels.voice.smart_turn import (
    SMART_TURN_MODEL_DIR,
    SMART_TURN_MODEL_NAME,
    SmartTurnDetector,
    verify_smart_turn_model,
)
from artemis.config.settings import VoiceConfig

SPEECH = b"S" * 4
QUIET = b"-" * 4


class FakeVAD:
    def is_speech(self, pcm: bytes) -> bool:
        return pcm == SPEECH


class FakeTurn:
    def __init__(self, answers: list[bool] | Exception) -> None:
        self.answers = answers
        self.calls: list[bytes] = []

    def is_complete(self, pcm16: bytes) -> bool:
        self.calls.append(pcm16)
        if isinstance(self.answers, Exception):
            raise self.answers
        return self.answers.pop(0)


# 20 ms kare: sessizlik sınırı 5 kare, tur uzatma sınırı 10 kare.
CONFIG = VoiceConfig(end_silence_ms=100, turn_max_silence_ms=200, pre_roll_ms=0)


def feed(seg: AudioSegmenter, frames: list[bytes]) -> list[tuple[int, AudioSegment]]:
    ended = []
    for index, frame in enumerate(frames):
        result = seg.push(frame, float(index))
        if result is not None:
            ended.append((index, result))
    return ended


def test_complete_ends_at_end_silence() -> None:
    turn = FakeTurn([True])
    seg = AudioSegmenter(CONFIG, FakeVAD(), turn)
    ended = feed(seg, [SPEECH] * 3 + [QUIET] * 12)
    assert [i for i, _ in ended] == [7] and len(turn.calls) == 1


def test_detector_sees_audio_trimmed_at_last_speech_frame() -> None:
    # Sondaki sessizlik modeli "bitti"ye iter (gerçek konuşma ölçümü); dedektöre verilmez.
    turn = FakeTurn([True])
    seg = AudioSegmenter(CONFIG, FakeVAD(), turn)
    feed(seg, [SPEECH] * 3 + [QUIET] * 6)
    assert turn.calls == [SPEECH * 3]


def test_incomplete_extends_to_turn_max_silence() -> None:
    turn = FakeTurn([False])
    seg = AudioSegmenter(CONFIG, FakeVAD(), turn)
    ended = feed(seg, [SPEECH] * 3 + [QUIET] * 14)
    assert [i for i, _ in ended] == [12] and len(turn.calls) == 1


def test_speech_resuming_judges_next_pause_again() -> None:
    turn = FakeTurn([False, True])
    seg = AudioSegmenter(CONFIG, FakeVAD(), turn)
    frames = [SPEECH] * 3 + [QUIET] * 7 + [SPEECH] * 3 + [QUIET] * 6
    ended = feed(seg, frames)
    assert len(ended) == 1 and len(turn.calls) == 2
    assert ended[0][1].pcm.count(SPEECH) == 6  # iki parça tek segmentte


def test_detector_error_ends_turn_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    turn = FakeTurn(RuntimeError("ort"))
    seg = AudioSegmenter(CONFIG, FakeVAD(), turn)
    with caplog.at_level(logging.WARNING):
        first = feed(seg, [SPEECH] * 3 + [QUIET] * 6)
        second = feed(seg, [SPEECH] * 3 + [QUIET] * 6)
    assert [i for i, _ in first] == [7] and [i for i, _ in second] == [7]
    assert sum("dedektörü" in r.message for r in caplog.records) == 1


def test_no_detector_is_unchanged() -> None:
    seg = AudioSegmenter(CONFIG, FakeVAD())
    ended = feed(seg, [SPEECH] * 3 + [QUIET] * 12)
    assert [i for i, _ in ended] == [7]


class FakeSession:
    def __init__(self, probability: float) -> None:
        self.probability = probability
        self.feeds: list[dict[str, object]] = []

    def run(self, output_names: object, feeds: dict[str, object]) -> list[np.ndarray]:
        self.feeds.append(feeds)
        return [np.array([[self.probability]], np.float32)]


def test_detector_input_shape_threshold_and_padding() -> None:
    pytest.importorskip("faster_whisper")  # öznitelik çıkarıcı [voice] ekinde
    session = FakeSession(0.6)
    detector = SmartTurnDetector(threshold=0.7, session=session)
    short = (np.full(16000, 3000, dtype="<i2")).tobytes()  # 1 sn: sola sıfırla dolar
    assert detector.is_complete(short) is False and detector.probability == pytest.approx(0.6)
    features = session.feeds[0]["input_features"]
    assert isinstance(features, np.ndarray)
    assert features.shape == (1, 80, 800) and features.dtype == np.float32
    session.probability = 0.8
    assert detector.is_complete(short) is True


def test_whisper_features_pad_and_truncate() -> None:
    pytest.importorskip("faster_whisper")
    from artemis.channels.voice.smart_turn import whisper_features

    for seconds in (1, 12):
        pcm = (np.full(16000 * seconds, 1000, dtype="<i2")).tobytes()
        out = whisper_features(pcm)
        assert out.shape == (1, 80, 800) and out.dtype == np.float32


def _reference_signals() -> dict[str, tuple[np.ndarray, float]]:
    t = np.arange(80000) / 16000
    rng = np.random.default_rng(7)
    sine = 0.3 * np.sin(2 * np.pi * 220 * t) + 0.05 * rng.standard_normal(t.size)
    chirp = 0.4 * np.sin(2 * np.pi * (100 + 300 * t) * t)
    rng = np.random.default_rng(7)
    burst = 0.02 * rng.standard_normal(t.size)
    burst[-16000:] *= 10
    return {
        "sine_noise": (sine, 0.97684),
        "chirp": (chirp, 0.93806),
        "noise_burst": (burst, 0.97937),
    }


@pytest.mark.parametrize("name", ["sine_noise", "chirp", "noise_burst"])
def test_real_model_matches_official_pipeline(name: str) -> None:
    pytest.importorskip("onnxruntime")
    pytest.importorskip("faster_whisper")
    signal, expected = _reference_signals()[name]
    pcm = (np.clip(signal.astype(np.float32), -1, 1) * 32767).astype("<i2").tobytes()
    detector = SmartTurnDetector()
    detector.is_complete(pcm)
    assert abs(detector.probability - expected) < 0.01


def test_verify_smart_turn_model(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        verify_smart_turn_model(tmp_path)
    (tmp_path / SMART_TURN_MODEL_NAME).write_bytes(b"bozuk")
    with pytest.raises(ValueError):
        verify_smart_turn_model(tmp_path)
    shutil.copy(SMART_TURN_MODEL_DIR / SMART_TURN_MODEL_NAME, tmp_path / SMART_TURN_MODEL_NAME)
    assert verify_smart_turn_model(tmp_path) == (
        SMART_TURN_MODEL_DIR / SMART_TURN_MODEL_NAME
    ).read_bytes()


def test_factory_disabled_returns_none() -> None:
    states: list[str] = []
    assert factory.build_turn_detector(VoiceConfig(turn_detection=False), states.append) is None
    assert states == []


@pytest.mark.parametrize("error", [OSError("yok"), ValueError("sha"), RuntimeError("ort")])
def test_factory_failure_is_visible(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def broken(**_kwargs: object) -> SmartTurnDetector:
        raise error

    monkeypatch.setattr(factory, "SmartTurnDetector", broken)
    states: list[str] = []
    assert factory.build_turn_detector(VoiceConfig(), states.append) is None
    assert states == ["turn:off"]
