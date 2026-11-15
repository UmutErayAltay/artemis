"""bench.wav_source için birim testleri; gerçek bekleme yok, sahte saat/uyku kullanılır."""

from __future__ import annotations

import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from artemis.channels.voice.core import PCMChunk
from bench.wav_source import NullSink, WavMicrophone, read_wav_pcm


class FakeClock:
    """Sanal saat: zaman yalnız sahte sleep tarafından ilerletilir."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration

    def clock(self) -> float:
        return self.now


def _write_wav(path: Path, payload: bytes, *, channels: int, framerate: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        handle.writeframes(payload)


def test_read_wav_pcm_roundtrip(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 10
    path = tmp_path / "sample.wav"
    _write_wav(path, payload, channels=1, framerate=16000)
    assert read_wav_pcm(path) == payload


def test_read_wav_pcm_rejects_stereo(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    _write_wav(path, b"\x00\x00" * 100, channels=2, framerate=16000)
    with pytest.raises(ValueError):
        read_wav_pcm(path)


def test_read_wav_pcm_rejects_8khz(tmp_path: Path) -> None:
    path = tmp_path / "lowrate.wav"
    _write_wav(path, b"\x00\x00" * 100, channels=1, framerate=8000)
    with pytest.raises(ValueError):
        read_wav_pcm(path)


async def test_frame_count_sizes_and_padding() -> None:
    fake = FakeClock()
    clip = b"\x01" * 1600  # 800 örnek = 50 ms @ 16 kHz 16-bit
    mic = WavMicrophone(
        [clip],
        frame_ms=20,
        lead_silence_s=0.04,
        gap_silence_s=0.04,
        repeats=2,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    frames = [frame async for frame in mic.frames()]
    # 2 (lead) + (3 klip + 2 gap) * 2 tekrar = 12 kare
    assert len(frames) == 12
    assert all(len(frame.pcm) == 640 for frame in frames)
    for index in (4, 9):  # her iki klipin son (yarım) karesi
        frame = frames[index]
        assert frame.pcm[:320] == b"\x01" * 320
        assert frame.pcm[320:] == b"\x00" * 320


async def test_pacing_matches_frame_timeline() -> None:
    fake = FakeClock()
    mic = WavMicrophone(
        [b"\x01" * 1600],
        frame_ms=20,
        lead_silence_s=0.04,
        gap_silence_s=0.04,
        repeats=2,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    frames = [frame async for frame in mic.frames()]
    assert len(frames) == 12
    assert sum(fake.sleeps) == pytest.approx(11 * 0.02, abs=1e-6)
    assert frames[0].timestamp == 0.0


async def test_pause_emits_silence_without_advancing_clip() -> None:
    fake = FakeClock()
    clip = b"\x01" * 1600
    mic = WavMicrophone(
        [clip],
        frame_ms=20,
        lead_silence_s=0.04,
        gap_silence_s=0.04,
        repeats=2,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    mic.pause()
    iterator = mic.frames()
    paused_frames = [await anext(iterator) for _ in range(3)]
    assert all(frame.pcm == b"\x00" * 640 for frame in paused_frames)
    mic.resume()
    rest = [frame async for frame in iterator]
    non_silent = b"".join(frame.pcm for frame in rest if any(frame.pcm))
    assert non_silent == (clip + b"\x00" * 320) * 2


async def test_on_finished_called_once_and_turns() -> None:
    fake = FakeClock()
    calls: list[int] = []
    mic = WavMicrophone(
        [b"\x01" * 640, b"\x02" * 640],
        frame_ms=20,
        lead_silence_s=0.0,
        gap_silence_s=0.0,
        repeats=3,
        sleep=fake.sleep,
        clock=fake.clock,
        on_finished=lambda: calls.append(1),
    )
    frames = [frame async for frame in mic.frames()]
    assert len(frames) == 6
    assert calls == [1]
    assert mic.turns == 6


async def test_null_sink_first_pcm_called_once() -> None:
    fake = FakeClock()
    sink = NullSink(sample_rate=24000, sleep=fake.sleep)
    calls: list[int] = []

    async def chunks() -> AsyncIterator[PCMChunk]:
        for payload in (b"\x01\x02" * 100, b"\x03\x04" * 100, b"\x05\x06" * 100):
            yield PCMChunk(pcm=payload)

    await sink.play(chunks(), asyncio.Event(), lambda: calls.append(1))
    assert calls == [1]
    assert sink.first_pcm_calls == 1
    assert sink.plays == 1
    assert len(fake.sleeps) == 3


async def test_null_sink_skips_empty_chunks() -> None:
    fake = FakeClock()
    sink = NullSink(sample_rate=24000, sleep=fake.sleep)
    calls: list[int] = []

    async def chunks() -> AsyncIterator[PCMChunk]:
        yield PCMChunk(pcm=b"")
        yield PCMChunk(pcm=b"\x01\x02" * 200)

    await sink.play(chunks(), asyncio.Event(), lambda: calls.append(1))
    assert calls == [1]
    assert sink.first_pcm_calls == 1
    assert len(fake.sleeps) == 1
    assert fake.sleeps[0] == pytest.approx(400 / 2 / 24000)


async def test_null_sink_cancelled_before_play_consumes_nothing() -> None:
    fake = FakeClock()
    sink = NullSink(sample_rate=24000, sleep=fake.sleep)
    calls: list[int] = []
    consumed: list[int] = []

    async def chunks() -> AsyncIterator[PCMChunk]:
        consumed.append(1)
        yield PCMChunk(pcm=b"\x01\x02" * 100)

    cancelled = asyncio.Event()
    cancelled.set()
    await sink.play(chunks(), cancelled, lambda: calls.append(1))
    assert calls == []
    assert consumed == []
    assert sink.first_pcm_calls == 0
    assert fake.sleeps == []
    assert sink.plays == 1


def test_invalid_frame_ms_raises() -> None:
    with pytest.raises(ValueError):
        WavMicrophone([b"\x00" * 640], frame_ms=10)


def test_invalid_repeats_raises() -> None:
    with pytest.raises(ValueError):
        WavMicrophone([b"\x00" * 640], repeats=0)
