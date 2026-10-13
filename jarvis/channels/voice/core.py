"""Ses kanalı orkestrasyonu; ses adaptörleri araç veya onay kararı vermez."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol

from jarvis.brain.agent_loop import RunResult
from jarvis.channels.runner import CommandRunner
from jarvis.config.settings import VoiceConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioFrame:
    pcm: bytes = field(repr=False)
    timestamp: float


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

    def __init__(self, config: VoiceConfig, vad: VoiceActivityDetector) -> None:
        self.config = config
        self.vad = vad
        self.frame_limit = max(1, config.pre_roll_ms // config.frame_ms)
        self.silence_limit = max(1, config.end_silence_ms // config.frame_ms)
        self.max_frames = max(1, int(config.max_utterance_seconds * 1000 / config.frame_ms))
        self._pre: deque[bytes] = deque(maxlen=self.frame_limit)
        self._active: list[bytes] = []
        self._silence = 0
        self._discarding = False
        self._speech_start = 0.0
        self._speech_end = 0.0

    @property
    def active(self) -> bool:
        return bool(self._active) or self._discarding

    def push(self, pcm: bytes, timestamp: float) -> AudioSegment | None:
        speech = self.vad.is_speech(pcm)
        if self._discarding:
            self._silence = 0 if speech else self._silence + 1
            if self._silence >= self.silence_limit:
                self._discarding = False
                self._silence = 0
                self._pre.clear()
            return None
        if not self._active:
            self._pre.append(pcm)
            if not speech:
                return None
            self._active = [*self._pre]
            self._pre.clear()
            self._silence = 0
            self._speech_start = timestamp
            self._speech_end = timestamp
            return None
        self._active.append(pcm)
        if speech:
            self._speech_end = timestamp
        self._silence = 0 if speech else self._silence + 1
        if self._silence >= self.silence_limit:
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
        self._active.clear()
        self._pre.clear()
        self._silence = 0
        self._discarding = False
        self._speech_start = 0.0
        self._speech_end = 0.0

    def snapshot(self) -> bytes:
        """Geçici, sonlu pencereli STT girdisi; kalıcı kayda yazılmaz."""
        return b"".join(self._active)


class VoiceController:
    """Wake word, CommandRunner ve TTS arasındaki tek voice-channel sınırı."""

    def __init__(
        self,
        runner: CommandRunner,
        config: VoiceConfig,
        microphone: Microphone,
        vad: VoiceActivityDetector,
        recognizer: SpeechRecognizer,
        synthesizer: SpeechSynthesizer,
        sink: AudioSink,
        *,
        clock: Callable[[], float] = monotonic,
        on_first_pcm: Callable[[], None] | None = None,
        on_timing: Callable[[str, float], None] | None = None,
        run_command: Callable[[str], Awaitable[RunResult]] | None = None,
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        self.runner = runner
        self.config = config
        self.microphone = microphone
        self.segmenter = AudioSegmenter(config, vad)
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self.sink = sink
        self.clock = clock
        self.on_first_pcm = on_first_pcm or (lambda: None)
        self.on_timing = on_timing or (lambda _stage, _time: None)
        # Masaüstü köprüsü komutu kendi istek modelinden (request_id, tek aktif istek)
        # geçirir; varsayılan runner.run CLI/bench davranışını değiştirmez.
        self.run_command = run_command if run_command is not None else runner.run
        # on_state yalnız durum bildirir; arbitrary callback'in hatasını burada
        # yakalamak kör except olurdu, bu yüzden çağıran fırlatmamalıdır.
        self.on_state = on_state or (lambda _state: None)
        self._followup_until = 0.0
        self._active: asyncio.Task[None] | None = None
        self._barge_cancelled = False
        self._closed = False
        self._stop = asyncio.Event()
        self._partial_task: asyncio.Task[str] | None = None
        self._partial_frames = 0

    async def run(self) -> None:
        """Mikrofon yalnız bu açıkça çağrılan döngü süresince tüketilir."""
        try:
            frames = self.microphone.frames().__aiter__()
            self._emit_state("listening")
            while not self._closed:

                async def next_audio_frame() -> AudioFrame:
                    return await anext(frames)

                next_frame: asyncio.Task[AudioFrame] = asyncio.create_task(next_audio_frame())
                stop_wait = asyncio.create_task(self._stop.wait())
                done, pending = await asyncio.wait(
                    {next_frame, stop_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in pending:
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await task
                if stop_wait in done and self._stop.is_set():
                    break
                try:
                    frame = next_frame.result()
                except StopAsyncIteration:
                    break
                if self.segmenter.active and self._active is not None and not self._barge_cancelled:
                    self._barge_cancelled = True
                    self.runner.cancel_current()
                    self._active.cancel()
                utterance = self.segmenter.push(frame.pcm, frame.timestamp)
                if self.segmenter.active:
                    self._partial_frames += 1
                    if self._partial_frames >= max(1, 1000 // self.config.frame_ms):
                        self._partial_frames = 0
                        self._start_partial_decode()
                if utterance is not None:
                    await self._handle_utterance(utterance)
            if not self._stop.is_set():
                await self._settle_active()
        finally:
            await self.close()

    def request_stop(self) -> None:
        self._stop.set()

    def _emit_state(self, state: str) -> None:
        """Yalnız listening/thinking/speaking/off; ekran durumu buradan okur."""
        self.on_state(state)

    async def _handle_utterance(self, segment: AudioSegment) -> None:
        # VAD yalnız aday söz kesmesini gösterir; transcript kesinleşmeden komut yoktur.
        if self._active is not None:
            await self._settle_active()
        self.on_timing("speech_start", segment.speech_start)
        self.on_timing("speech_end", segment.speech_end)
        await self._settle_partial()
        try:
            transcript = (await self.recognizer.transcribe(segment.pcm)).strip()
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("Ses tanıma başarısız (%s).", type(error).__name__)
            return
        if not transcript:
            return
        self.on_timing("stt_final", self.clock())
        command = self._wake_command(transcript)
        if command is None:
            return
        if not command:
            return
        self._barge_cancelled = False
        self._active = asyncio.create_task(self._run_and_speak(command))

    def _wake_command(self, transcript: str) -> str | None:
        """None: wake word yok/expired; boş dize: wake word tek başına."""
        word = re.escape(self.config.wake_word)
        match = re.search(rf"(?<!\w){word}(?!\w)", transcript, flags=re.IGNORECASE | re.UNICODE)
        if match:
            tail = transcript[match.end() :].lstrip(" \t,.;:!?-")
            if not tail.strip(" \t,.;:!?-"):
                self._followup_until = self.clock() + self.config.followup_seconds
                return ""
            self._followup_until = 0.0
            return tail
        if self.clock() <= self._followup_until:
            self._followup_until = 0.0
            return transcript
        self._followup_until = 0.0
        return None

    def _start_partial_decode(self) -> None:
        method = getattr(self.recognizer, "transcribe_partial", None)
        if method is None or self._partial_task is not None:
            return
        pcm = self.segmenter.snapshot()
        if pcm:
            # Bu çıktı hiçbir zaman wake word veya komut yürütmesinde kullanılmaz.
            self._partial_task = asyncio.create_task(method(pcm))
            self._partial_task.add_done_callback(self._partial_finished)

    def _partial_finished(self, task: asyncio.Task[str]) -> None:
        if self._partial_task is task:
            self._partial_task = None
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.debug("Ara STT penceresi başarısız (%s).", type(error).__name__)

    async def _settle_partial(self) -> None:
        task, self._partial_task = self._partial_task, None
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, OSError, RuntimeError, ValueError):
                logger.debug("Ara STT penceresi kullanılamadı.")

    async def _run_and_speak(self, command: str) -> None:
        generation = asyncio.Event()
        try:
            self._emit_state("thinking")
            result = await self.run_command(command)
            self.on_timing("model_complete", self.clock())
            if result.text.strip() and not generation.is_set():

                def first_pcm() -> None:
                    self.on_timing("first_pcm", self.clock())
                    self.on_first_pcm()

                self._emit_state("speaking")
                await self.sink.play(
                    self.synthesizer.stream(result.text, generation),
                    generation,
                    first_pcm,
                )
                if not generation.is_set():
                    self._followup_until = self.clock() + self.config.followup_seconds
        except asyncio.CancelledError:
            generation.set()
            raise
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("Ses yanıtı başarısız (%s).", type(error).__name__)
        finally:
            # Komut bitti, hata aldı ya da söz kesmeyle iptal edildi: denetleyici
            # hâlâ açıksa mikrofon dinlemeye döner. close() sırasında "off" tek başına yazılır.
            if not self._closed:
                self._emit_state("listening")

    async def _settle_active(self) -> None:
        active = self._active
        if active is None:
            return
        if not active.done() and self._barge_cancelled:
            active.cancel()
        with suppress(asyncio.CancelledError):
            await active
        self._active = None
        self._barge_cancelled = False

    def discard_during_approval(self) -> None:
        """Senkron terminal onayı dönüşünde eski ses kuyruğunu ve segmenti sıfırlar."""
        self.microphone.discard_pending()
        self.segmenter.reset()

    def pause_for_approval(self) -> None:
        self.microphone.pause()
        self.segmenter.reset()

    def resume_after_approval(self) -> None:
        self.microphone.discard_pending()
        self.segmenter.reset()
        self.microphone.resume()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._active is not None and not self._active.done():
            self.runner.cancel_current()
            self._active.cancel()
        await self._settle_active()
        await self._settle_partial()
        for label, closer in (
            ("mikrofon", self.microphone.close),
            ("ses çıkışı", self.sink.close),
            ("ses üretici", self.synthesizer.close),
        ):
            try:
                await closer()
            except (OSError, RuntimeError, ValueError) as error:
                logger.warning("%s kapatılamadı (%s).", label, type(error).__name__)
        # Mikrofon gerçekten kapandıktan sonra bildirilir; ekran "açık" demeye devam etmez.
        self._emit_state("off")
