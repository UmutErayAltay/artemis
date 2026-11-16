"""Ses kanalı sözleşmeleri: çerçeve/bölüt veri tipleri, adaptör protokolleri ve AudioSegmenter."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Protocol

from artemis.config.settings import VoiceConfig

# Kayıtlar eskisi gibi core adı altında görünsün (log filtreleri/testler bu adı kullanır).
logger = logging.getLogger("artemis.channels.voice.core")


@dataclass(frozen=True)
class AudioFrame:
    pcm: bytes = field(repr=False)
    timestamp: float
    # Mikrofon gürültü engelleme için 48 kHz yakalıyorsa 48000; DenoisedMicrophone bunu 16 kHz'e
    # indirir, yani VAD/seviye/barge-in tüketicileri her zaman 16000 görür.
    sample_rate: int = 16000
    # Gürültü engelleme açıkken: pcm temizlenmiş (algılama için), raw_pcm aynı aralığın ham sesi
    # (STT için, pcm ile aynı uzunluk ve hizada). None ise STT de pcm'i kullanır.
    raw_pcm: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AudioSegment:
    pcm: bytes = field(repr=False)
    speech_start: float
    speech_end: float


class Microphone(Protocol):
    def frames(self) -> AsyncIterator[AudioFrame]: ...

    def discard_pending(self) -> None: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    async def close(self) -> None: ...


class VoiceActivityDetector(Protocol):
    def is_speech(self, pcm: bytes) -> bool: ...


class TurnDetector(Protocol):
    """Sessizlikte "cümle bitti mi?" sorusu (ADR 0068); yalnız sabır ekler, turu erken bitirmez."""

    def is_complete(self, pcm16: bytes) -> bool: ...


class SpeechRecognizer(Protocol):
    async def transcribe(self, pcm: bytes) -> str: ...

    async def transcribe_partial(self, pcm: bytes) -> str: ...


@dataclass(frozen=True)
class PCMChunk:
    pcm: bytes = field(repr=False)
    sample_rate: int = 24000
    channels: int = 1
    sample_width: int = 2


class SpeechSynthesizer(Protocol):
    def stream(self, text: str, cancelled: asyncio.Event) -> AsyncIterator[PCMChunk]: ...

    async def close(self) -> None: ...


class AudioSink(Protocol):
    async def play(
        self,
        chunks: AsyncIterator[PCMChunk],
        cancelled: asyncio.Event,
        on_first_pcm: Callable[[], None],
    ) -> None: ...

    async def close(self) -> None: ...


class AudioSegmenter:
    """VAD segmentasyonu; kısa sessizlikleri ve konuşma öncesi PCM'i korur."""

    def __init__(
        self,
        config: VoiceConfig,
        vad: VoiceActivityDetector,
        turn_detector: TurnDetector | None = None,
    ) -> None:
        self.config = config
        self.vad = vad
        self.frame_limit = max(1, config.pre_roll_ms // config.frame_ms)
        self.silence_limit = max(1, config.end_silence_ms // config.frame_ms)
        # ADR 0068: cümle bitmemiş görünürse sessizlik bu kareye kadar beklenir (hiç kısalmaz).
        self.turn_silence_limit = max(
            self.silence_limit, config.turn_max_silence_ms // config.frame_ms
        )
        self.turn_detector = turn_detector
        self._judged = False
        self._turn_warned = False
        self.max_frames = max(1, int(config.max_utterance_seconds * 1000 / config.frame_ms))
        self._pre: deque[bytes] = deque(maxlen=self.frame_limit)
        self._active: list[bytes] = []
        self._silence = 0
        self._discarding = False
        self._speech_start = 0.0
        self._speech_end = 0.0
        self.voiced_frames = 0

    @property
    def active(self) -> bool:
        return bool(self._active) or self._discarding

    discarding = property(lambda self: self._discarding)
    speech_start = property(lambda self: self._speech_start)

    @property
    def trailing_silence_frames(self) -> int:
        return self._silence if self._active else 0

    def voiced_snapshot(self) -> bytes:
        """Sondaki sessizlik kareleri hariç ses (ADR 0071; tur dedektörüyle aynı kırpma)."""
        return b"".join(self._active[: len(self._active) - self._silence])

    def _turn_over(self) -> bool:
        """Sessizlik eşiğinde dedektöre bu duraklama için bir kez sorulur; hata turu bloklamaz."""
        if self.turn_detector is None or self._silence >= self.turn_silence_limit:
            return True
        if self._judged:
            return False
        self._judged = True
        # Model sondaki sessizliği "bitti" diye okur: gerçek Türkçe konuşmada (Common Voice, 80
        # klip) cümle ortası kesimde 600 ms sessizlikle %5, sessizlik kırpılınca %30 "yarım" dedi.
        # Bu yüzden ses son konuşma karesinde kesilip verilir (2026-11-14 ölçümü, ADR 0068).
        voiced = self._active[: len(self._active) - self._silence]
        try:
            return self.turn_detector.is_complete(b"".join(voiced))
        except (OSError, ValueError, RuntimeError) as error:
            if not self._turn_warned:
                self._turn_warned = True
                logger.warning("Konuşma sonu dedektörü başarısız, sessizlikle sürülüyor: %s", error)
            return True

    def push(
        self, pcm: bytes, timestamp: float, stt_pcm: bytes | None = None
    ) -> AudioSegment | None:
        """VAD ``pcm`` (temizlenmiş olabilir) üzerinde çalışır; segmentte ``stt_pcm`` taşınır."""
        speech = self.vad.is_speech(pcm)
        keep = pcm if stt_pcm is None else stt_pcm
        self.voiced_frames = self.voiced_frames + 1 if speech else 0
        if self._discarding:
            self._silence = 0 if speech else self._silence + 1
            if self._silence >= self.silence_limit:
                self._discarding = False
                self._silence = 0
                self._pre.clear()
            return None
        if not self._active:
            self._pre.append(keep)
            if not speech:
                return None
            self._active = [*self._pre]
            self._pre.clear()
            self._silence = 0
            self._speech_start = timestamp
            self._speech_end = timestamp
            return None
        self._active.append(keep)
        if speech:
            self._speech_end = timestamp
        self._silence = 0 if speech else self._silence + 1
        if speech:
            self._judged = False
        if self._silence >= self.silence_limit and self._turn_over():
            result = AudioSegment(b"".join(self._active), self._speech_start, self._speech_end)
            self.reset()
            return result
        if len(self._active) >= self.max_frames:
            # Aşırı uzun cümlenin başını kesip komut diye tanımayız; bütünü atılır.
            self._active.clear()
            self._pre.clear()
            self._discarding = True
            self._silence = 0 if speech else self._silence
            self._speech_start = 0.0
            self._speech_end = 0.0
        return None

    def reset(self) -> None:
        self.voiced_frames = 0
        self._active.clear()
        self._pre.clear()
        self._silence = 0
        self._judged = False
        self._discarding = False
        self._speech_start = 0.0
        self._speech_end = 0.0

    def snapshot(self) -> bytes:
        """Geçici, sonlu pencereli STT girdisi; kalıcı kayda yazılmaz."""
        return b"".join(self._active)


class LiveTranscriber(Protocol):
    """ADR 0047: yalnız gösterim metni; ``feed`` iş parçacığında çağrılır."""

    def feed(self, pcm16: bytes) -> str: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


class WakeSpotterLike(Protocol):
    """ADR 0069: cihaz içi uyandırma ön-kapısı; ``feed`` iş parçacığında çağrılır."""

    def feed(self, pcm16: bytes) -> bool: ...

    def close(self) -> None: ...
