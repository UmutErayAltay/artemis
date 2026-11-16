"""Dosya tabanlı canlı donanım gecikme benchmark'ı: gerçek STT/LLM/TTS, mikrofon yerine WAV
dosyası beslenir, ses çıkışı yoktur. Ham ses kaydedilmez."""

from __future__ import annotations

import asyncio
import time
import wave
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path

from artemis.channels.voice.core import AudioFrame, PCMChunk


def read_wav_pcm(path: Path, sample_rate: int = 16000) -> bytes:
    """WAV dosyasını ham PCM baytlarına çevirir; mono/16-bit/istenen örnekleme zorunludur."""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sampwidth = handle.getsampwidth()
        framerate = handle.getframerate()
        if channels != 1:
            raise ValueError(f"WAV dosyası mono olmalı (nchannels=1), gelen: {channels}")
        if sampwidth != 2:
            raise ValueError(f"WAV dosyası 16 bit olmalı (sampwidth=2), gelen: {sampwidth}")
        if framerate != sample_rate:
            raise ValueError(
                f"WAV dosyasının örnekleme hızı {sample_rate} Hz olmalı, gelen: {framerate}"
            )
        return handle.readframes(handle.getnframes())


def _silence_frame_count(seconds: float, frame_ms: int) -> int:
    # Yarım kare yukarı yuvarlanır: int(x + 0.5) (ör. 0.05 s / 20 ms = tam 3 kare).
    return int(seconds * 1000 / frame_ms + 0.5)


class WavMicrophone:
    """WAV kliplerini gerçek zamanlı hızda (pacing'li) sahte mikrofon olarak besler."""

    def __init__(
        self,
        clips: Sequence[bytes],
        *,
        frame_ms: int = 20,
        sample_rate: int = 16000,
        lead_silence_s: float = 1.0,
        gap_silence_s: float = 6.0,
        repeats: int = 1,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        if frame_ms not in (20, 30):
            raise ValueError(f"frame_ms yalnız 20 veya 30 olabilir, gelen: {frame_ms}")
        if repeats < 1:
            raise ValueError(f"repeats en az 1 olmalı, gelen: {repeats}")
        if lead_silence_s < 0:
            raise ValueError(f"lead_silence_s negatif olamaz, gelen: {lead_silence_s}")
        if gap_silence_s < 0:
            raise ValueError(f"gap_silence_s negatif olamaz, gelen: {gap_silence_s}")
        self.clips = list(clips)
        self.frame_ms = frame_ms
        self.sample_rate = sample_rate
        self.lead_silence_s = lead_silence_s
        self.gap_silence_s = gap_silence_s
        self.repeats = repeats
        self.sleep = sleep
        self.clock = clock
        self.on_finished = on_finished
        # 16-bit mono: örnek başına 2 bayt.
        self.frame_bytes = (sample_rate * frame_ms // 1000) * 2
        self._lead_frames = _silence_frame_count(lead_silence_s, frame_ms)
        self._gap_frames = _silence_frame_count(gap_silence_s, frame_ms)
        self._paused = False
        self._closed = False

    @property
    def turns(self) -> int:
        return len(self.clips) * self.repeats

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def discard_pending(self) -> None:
        # Bekleyen ses kuyruğu yok; gerçek mikrofondaki çağrının bilinçli no-op karşılığı.
        return None

    async def close(self) -> None:
        self._closed = True

    async def frames(self) -> AsyncIterator[AudioFrame]:
        # i, şimdiye dek üretilen TÜM kareler üzerinde 0-tabanlı sıra numarasıdır;
        # duraklatma sırasında verilen sessizlik kareleri de aynı pacing'e tabidir.
        start = self.clock()
        index = 0
        async for pcm in self._produce_frames():
            if self._closed:
                return
            target = start + index * (self.frame_ms / 1000)
            await self.sleep(max(0.0, target - self.clock()))
            yield AudioFrame(pcm=pcm, timestamp=self.clock(), sample_rate=self.sample_rate)
            index += 1
        # on_finished yalnız doğal tamamlanmada çağrılır; close() ile durdurmada çağrılmaz.
        if self.on_finished is not None:
            self.on_finished()

    async def _produce_frames(self) -> AsyncGenerator[bytes]:
        zero = bytes(self.frame_bytes)
        for _ in range(self._lead_frames):
            yield zero
        for _ in range(self.repeats):
            for clip in self.clips:
                offset = 0
                while offset < len(clip):
                    # Duraklatıldıysa klip konumu ilerlemez; onun yerine sessizlik verilir.
                    while self._paused:
                        yield zero
                    piece = clip[offset : offset + self.frame_bytes]
                    if len(piece) < self.frame_bytes:
                        piece = piece + zero[: self.frame_bytes - len(piece)]
                    yield piece
                    offset += self.frame_bytes
                for _ in range(self._gap_frames):
                    yield zero


class NullSink:
    """Ses çıkışını yutan sahte sink; gerçek zamanlı pacing uygular ama cihaz açmaz."""

    def __init__(
        self,
        sample_rate: int = 24000,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sample_rate = sample_rate
        self.sleep = sleep
        self.plays = 0
        self.first_pcm_calls = 0

    async def play(
        self,
        chunks: AsyncIterator[PCMChunk],
        cancelled: asyncio.Event,
        on_first_pcm: Callable[[], None],
    ) -> None:
        self.plays += 1
        first = True
        iterator = chunks.__aiter__()
        while True:
            # cancelled, bir sonraki yığın çekilmeden ÖNCE kontrol edilir; böylece iptal
            # edilmiş turda üst akıştan hiçbir yığın tüketilmez.
            if cancelled.is_set():
                break
            try:
                chunk = await anext(iterator)
            except StopAsyncIteration:
                break
            if not chunk.pcm:
                continue
            if first:
                first = False
                on_first_pcm()
                self.first_pcm_calls += 1
            await self.sleep(len(chunk.pcm) / 2 / self.sample_rate)

    async def close(self) -> None:
        return None
