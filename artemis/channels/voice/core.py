"""Ses kanalı orkestrasyonu; ses adaptörleri araç veya onay kararı vermez."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from array import array
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from time import monotonic
from typing import Protocol

from artemis.brain.agent_loop import RunResult
from artemis.channels.runner import CommandRunner
from artemis.channels.voice.streaming import SpeechStream
from artemis.channels.voice.telemetry import (
    CommandClass,
    Outcome,
    Stage,
    VoiceTimingEvent,
)
from artemis.config.settings import VoiceConfig

logger = logging.getLogger(__name__)

# Kip cümlesi: en çok 5 sözcük, "kesintisiz"/"sürekli" ve tek yönlü bir fiil. Sözcükler katlanmış
# (_fold) karşılaştırılır. Anahtar bir harf farkla da tutar: Whisper "kesintisiz"i "gezintisiz"
# yazdı ve kip açılmadı. Fiil kök + izinli ek olmalı ("geç", "geçelim", "açsana"); "geçmeyi",
# "açıkla" fiil sayılmaz. Uzun komut içindekiler yine kaçırılır.
_CONTINUOUS_MAX_WORDS = 5
_CONTINUOUS_KEYS = ("kesintisis", "surekli")
_CONTINUOUS_ON_VERBS = ("gec", "ac", "baslat", "gir", "dinle")
_CONTINUOUS_OFF_VERBS = ("kapat", "cik", "bitir", "durdur")
_VERB_SUFFIXES = {"", "in", "un", "yin", "elim", "alim", "yelim", "sene", "sana", "er", "ar"}
_CONTINUOUS_OFF = {("normal", "moda", "gec"), ("normal", "moda", "don")}


_FOLD = str.maketrans("ışçğöü", "iscgou")


def _fold(word: str) -> str:
    """Türkçe küçük harf + aksan katlama; sondaki z de s sayılır.

    Whisper "Artemis"i "Artemıs/Artemiş" ve en sık "Artemiz" yazıyor (sondaki s'nin
    yumuşaması); bunlar yazım farkı değil, aynı söz.
    """
    folded = word.replace("I", "ı").replace("İ", "i").lower().translate(_FOLD)
    return folded[:-1] + "s" if folded.endswith("z") else folded


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein; ``limit`` aşılınca erken döner (yalnız eşik karşılaştırması gerekir)."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(
                previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b),
            ))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _substitutions(a: str, b: str, limit: int) -> int:
    """Aynı uzunluktaki iki sözcükte farklı harf sayısı; uzunluk farklıysa ``limit + 1``."""
    if len(a) != len(b):
        return limit + 1
    return sum(char_a != char_b for char_a, char_b in zip(a, b, strict=True))


_GHOST_PHRASES = {
    "abone olmayı unutmayın", "izlediğiniz için teşekkürler",
    "izlediğiniz için teşekkür ederim", "izlediğiniz için teşekkür ederiz",
    "bu videoyu izlediğiniz için teşekkür ederim",
    "bu videoyu izlediğiniz için teşekkür ederiz", "altyazı mk",
}


def is_ghost_transcript(text: str) -> bool:
    """Whisper'ın sessizlik/kısa seste uydurduğu altyazı kalıbı mı.

    Tanıyıcı yalnız tam metne bakar; canlıda "Artemis. Abone olmayı unutmayın." geçti ve
    kuyruk komut sanıldı (2026-10-26). Bu yüzden VoiceController uyandırma sonrası kuyruğa da
    uygular.
    """
    normalized = text.replace("I", "ı").replace("İ", "i").lower()
    normalized = " ".join("".join(
        char for char in normalized if char.isalnum() or char.isspace()
    ).split())
    return normalized in _GHOST_PHRASES


def find_wake_word(transcript: str, wake_word: str) -> tuple[int, int] | None:
    """Uyandırma sözcüğünün transcript'teki (başlangıç, bitiş) konumu; yoksa None.

    Tek sözcüklü uyandırmada yakın yazımlar da kabul edilir: uçtan uca denemede Whisper
    "Artemis"i çoğu kez "Artemiz", "Arteniz", "Ar temiz" yazdı ve komutlar kayboldu.
    Katlamadan sonra 5+ harfli sözcükte en çok bir harf **değişimi** kabul edilir (daha kısada
    tam eşleşme); boşlukla bölünmüş iki parça birleşik de denenir. İki harf farkı fazla
    gevşekti: "artırmış" da "artemis" sayılıyordu. Harf düşmesi/eklenmesi de kabul edilmez:
    "artmış" (fiyatlar artmış) gündelik sözcüktür ve uyandırıyordu (ADR 0045 ölçümü);
    Whisper'ın gerçekte yazdığı biçimlerin hepsi aynı uzunlukta değişimdir. Bu yalnız dinleme
    kapısıdır, onay değildir: her araç yine SafetyGate'ten geçer. Çok sözcüklü uyandırma
    ifadesi tam eşleşme ister.
    """
    wake = _fold(wake_word)
    if not wake.isalnum():
        match = re.search(
            rf"(?<!\w){re.escape(wake_word)}(?!\w)", transcript,
            flags=re.IGNORECASE | re.UNICODE,
        )
        return match.span() if match else None
    limit = 1 if len(wake) >= 5 else 0
    tokens = list(re.finditer(r"\w+", transcript))
    for index, token in enumerate(tokens):
        word = _fold(token.group())
        if _substitutions(word, wake, limit) <= limit:
            return token.span()
        if index + 1 < len(tokens) and transcript[token.end():tokens[index + 1].start()] == " ":
            joined = word + _fold(tokens[index + 1].group())
            if len(word) >= 2 and _substitutions(joined, wake, limit) <= limit:
                return token.start(), tokens[index + 1].end()
    return None


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
        self.voiced_frames = 0

    @property
    def active(self) -> bool:
        return bool(self._active) or self._discarding

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
        self.voiced_frames = 0
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
        on_event: Callable[[VoiceTimingEvent], None] | None = None,
        run_command: Callable[..., Awaitable[RunResult]] | None = None,
        stream_responses: bool = False,
        on_state: Callable[[str], None] | None = None,
        on_input_level: Callable[[int], None] | None = None,
        on_transcript: Callable[[str], None] | None = None,
        cloud_recognizer: SpeechRecognizer | None = None,
    ) -> None:
        self.runner = runner
        # ADR 0046: yalnız komut olacak söz için; uyandırma/yankı kararları hep yerel metinle.
        self.cloud_recognizer = cloud_recognizer
        self.config = config
        self.microphone = microphone
        self.segmenter = AudioSegmenter(config, vad)
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self.sink = sink
        self.clock = clock
        self.on_first_pcm = on_first_pcm or (lambda: None)
        self.on_timing = on_timing or (lambda _stage, _time: None)
        self.on_event = on_event
        self._utterance_sequence = 0
        self._generation_sequence = 0
        self._capturing_id: int | None = None
        # Controller ömrünün ilk görev turu soğuk sayılır (model/önbellek ısınmamış olabilir).
        self._task_turns = 0
        # Hoparlöre yazım sürerken True; söz kesme stop olay çiftini yalnız o zaman üretir.
        self._output_active = False
        # Masaüstü köprüsü komutu kendi istek modelinden (request_id, tek aktif istek)
        # geçirir; varsayılan runner.run CLI/bench davranışını değiştirmez.
        self.run_command = run_command if run_command is not None else runner.run
        # ADR 0043: True ise run_command ``stream=`` kabul etmelidir (runner.run ve masaüstü
        # köprüsü eder); sağlayıcı akmıyorsa yanıt yine tamamı gelince söylenir.
        self.stream_responses = stream_responses
        # on_state yalnız durum bildirir; arbitrary callback'in hatasını burada
        # yakalamak kör except olurdu, bu yüzden çağıran fırlatmamalıdır.
        self.on_state = on_state or (lambda _state: None)
        self.on_input_level = on_input_level or (lambda _level: None)
        self.on_transcript = on_transcript or (lambda _text: None)
        self._next_level_update = self.clock()
        self._awaiting_followup = False
        self._followup_until = 0.0
        self._active: asyncio.Task[None] | None = None
        self._barge_cancelled = False
        self._closed = False
        self._stop = asyncio.Event()
        self._partial_task: asyncio.Task[str] | None = None
        self._partial_frames = 0
        self._playback: asyncio.Task[None] | None = None
        self._playback_cancelled: asyncio.Event | None = None
        self._spoken_text = ""
        self._spoken_until = 0.0
        self._output_requires_wake = False
        # Uyandırma karşılıkları metne göre bir kez sentezlenir, sonra aynı PCM çalınır.
        self._ack_cache: dict[str, list[PCMChunk]] = {}
        # Çalışma anı kipi; config yalnız başlangıç değeridir (ADR 0024).
        self.continuous = config.continuous_mode

    async def run(self) -> None:
        """Mikrofon yalnız bu açıkça çağrılan döngü süresince tüketilir."""
        try:
            frames = self.microphone.frames().__aiter__()
            self._emit_state("listening")
            if self.continuous:
                self._emit_state("mode:on")
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
                    if self._active is not None and not self._active.done():
                        self.runner.cancel_current()
                        self._active.cancel()
                        self._barge_cancelled = True
                    break
                if self.clock() >= self._next_level_update:
                    samples = array("h", frame.pcm[:len(frame.pcm) // 2 * 2])
                    rms = math.sqrt(sum(value * value for value in samples) / max(1, len(samples)))
                    self.on_input_level(min(100, int(rms / 32768 * 500)))
                    self._next_level_update = self.clock() + 0.2
                if self._awaiting_followup and self.clock() > self._followup_until:
                    self._awaiting_followup = False
                    self._emit_state("listening")
                was_active = self.segmenter.active
                utterance = self.segmenter.push(frame.pcm, frame.timestamp, frame.raw_pcm)
                if not was_active and self.segmenter.active:
                    self._capturing_id = self._new_utterance(frame.timestamp)
                if self._capturing_id is not None and self.segmenter._discarding:
                    self._event(self._capturing_id, "terminal", outcome="rejected",
                                reason="max_utterance")
                    self._capturing_id = None
                # VAD gürültüyü/yankıyı da konuşma sanabilir. Oynatma yalnız nihai
                # transcript kabul edilince _handle_utterance içinde kesilir (ADR 0040).
                if self.segmenter.active and self.config.stt_partial_enabled:
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

    def _new_utterance(self, timestamp: float) -> int:
        self._utterance_sequence += 1
        self._event(self._utterance_sequence, "speech_start", timestamp=timestamp)
        return self._utterance_sequence

    def _event(
        self, utterance_id: int, stage: Stage, *, generation_id: int | None = None,
        timestamp: float | None = None, outcome: Outcome | None = None,
        reason: str | None = None, ack: bool = False,
        command_class: CommandClass | None = None, warm: bool | None = None,
    ) -> None:
        if self.on_event is not None:
            self.on_event(VoiceTimingEvent(
                utterance_id, generation_id, stage,
                self.clock() if timestamp is None else timestamp,
                "ack" if ack else "task", outcome, reason, command_class, warm,
            ))

    def _discard_capture(self, reason: str) -> None:
        if self._capturing_id is not None:
            self._event(self._capturing_id, "terminal", outcome="cancelled", reason=reason)
            self._capturing_id = None

    def request_stop(self) -> None:
        self._stop.set()

    def _emit_state(self, state: str) -> None:
        """Yalnız listening/thinking/speaking/off; ekran durumu buradan okur."""
        self.on_state(state)

    async def _handle_utterance(self, segment: AudioSegment) -> None:
        # Kimlik, async yanıtın ömrünce korunur; reddedilen sonraki söz bunu değiştirmez.
        uid = self._capturing_id
        self._capturing_id = None
        if uid is None:
            uid = self._new_utterance(segment.speech_start)
        self.on_timing("speech_start", segment.speech_start)
        self.on_timing("speech_end", segment.speech_end)
        self._event(uid, "speech_end", timestamp=segment.speech_end)
        self._event(uid, "endpoint")
        try:
            await self._settle_partial()
            transcript = (await self.recognizer.transcribe(segment.pcm)).strip()
        except asyncio.CancelledError:
            self._event(uid, "terminal", outcome="cancelled", reason="stt_cancelled")
            raise
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("Ses tanıma başarısız (%s).", type(error).__name__)
            self._event(uid, "terminal", outcome="error", reason="stt_error")
            return
        self._event(uid, "stt_final")
        if not transcript:
            self.on_transcript("Net konuşma algılanmadı; bu ses komut olarak kullanılmadı.")
            self._event(uid, "terminal", outcome="rejected", reason="empty_transcript")
            return
        if self._is_spoken_echo(transcript, segment.speech_start):
            self.on_transcript("Asistan sesinin yankısı yok sayıldı.")
            self._event(uid, "terminal", outcome="rejected", reason="echo")
            return
        transcript = self._strip_spoken_prefix(transcript, segment.speech_start)
        if (
            self.config.require_wake_during_output
            and self._output_requires_wake
            and self._spoken_text
            and segment.speech_start <= self._spoken_until
            and find_wake_word(transcript, self.config.wake_word) is None
        ):
            # ADR 0042: hoparlöre yazım sürerken doğrulanmış söz susturur ama komut olmaz;
            # sonraki söz uyandırmasız takip olarak alınır. Kısa patlama (öksürük, kapı)
            # STT'den geçse bile barge_in_ms altında kalır.
            if (
                self.config.barge_in_without_wake
                and self._output_active
                and (segment.speech_end - segment.speech_start) * 1000 >= self.config.barge_in_ms
            ):
                self.on_transcript("Söz kesildi; dinliyorum.")
                await self._interrupt_output(uid)
                # Kullanıcı bilerek susturdu: takip sözü wake istemez. Yankı filtresi
                # (_is_spoken_echo) son yanıt metniyle çalışmaya devam eder.
                self._output_requires_wake = False
                if self.config.followup_listening:
                    self._followup_until = self.clock() + self.config.followup_seconds
                    self._awaiting_followup = True
                    self._emit_state("awaiting_command")
                else:
                    self._emit_state("listening")
                self._event(uid, "terminal", outcome="interrupted", reason="barge_in")
                return
            self.on_transcript("Ses yok sayıldı; söz kesmek için Artemis diyerek başla.")
            self._event(uid, "terminal", outcome="rejected", reason="output_requires_wake")
            return
        self.on_transcript(transcript)
        self.on_timing("stt_final", self.clock())
        toggle = self._continuous_toggle(transcript)
        command = None if toggle is not None else self._wake_command(transcript)
        if toggle is None and command is None:
            self._event(uid, "terminal", outcome="rejected", reason="no_wake")
            return
        if command and self.cloud_recognizer is not None:
            # Karar yerel metinle bir kez verildi (_wake_command takip durumunu değiştirir);
            # burada yalnız komut metni bulut sürümüyle değişir.
            command = await self._cloud_command(uid, segment, command)
        await self._interrupt_output(uid)
        self._generation_sequence += 1
        gid = self._generation_sequence
        if toggle is not None:
            self.set_continuous(toggle)
            text = "Kesintisiz mod açık." if toggle else "Kesintisiz mod kapalı."
            self._active = asyncio.create_task(self._acknowledge_wake(text, uid=uid, gid=gid))
        elif not command:
            self._active = asyncio.create_task(self._acknowledge_wake(uid=uid, gid=gid))
        else:
            self._barge_cancelled = False
            self._active = asyncio.create_task(self._run_and_speak(command, uid=uid, gid=gid))

    async def _cloud_command(self, uid: int, segment: AudioSegment, local_command: str) -> str:
        """Komut metnini bulut tanıyıcıdan alır.

        Yalnız istisnada (ağ/HTTP/geçersiz yanıt) yerel metne düşülür. Çağrı başarılıysa bulut
        metni nihaidir: boş ya da yalnız uyandırma sözcüğü ise "" (tek başına uyandırma) döner;
        yerel kuyruk (halüsinasyon) komut olmaz.
        """
        assert self.cloud_recognizer is not None
        try:
            text = (await self.cloud_recognizer.transcribe(segment.pcm)).strip()
        except asyncio.CancelledError:
            self._event(uid, "terminal", outcome="cancelled", reason="stt_cancelled")
            raise
        except (OSError, RuntimeError, ValueError) as error:
            # Hata metni yanıt gövdesi/anahtar taşımaz ama yine de yalnız tür günlüğe yazılır.
            logger.warning("Bulut tanıma başarısız, yerel metin kullanılıyor (%s).",
                           type(error).__name__)
            self._event(uid, "stt_cloud_fallback", reason="stt_cloud_fallback")
            return local_command
        span = find_wake_word(text, self.config.wake_word)
        command = (text[span[1]:] if span is not None else text).lstrip(" \t,.;:!?-")
        if is_ghost_transcript(command):
            return ""
        return command if command.strip(" \t,.;:!?-") else ""

    async def _interrupt_output(self, uid: int) -> None:
        """Etkin turu ve oynatmayı durdurur; temizlik bitmeden yeni tur başlamaz.

        Stop çifti kesen sözün kimliğinde tutulur (generation'sız): böylece hem stop
        isteğinden hem kullanıcı sesinin başından oynatmanın durmasına süre ölçülür.
        """
        interrupting = self._output_active
        if self._active is not None and not self._active.done():
            if interrupting:
                self._event(uid, "stop_requested")
            self._barge_cancelled = True
            self.runner.cancel_current()
            self._active.cancel()
        try:
            await self._settle_active()
        except asyncio.CancelledError:
            self._event(uid, "terminal", outcome="cancelled", reason="waiting_previous")
            raise
        if interrupting:
            # Sink'e yazım burada bitmiştir; aygıt tamponundaki son blok ölçülmez.
            self._event(uid, "playback_stopped")

    @staticmethod
    def _speech_words(text: str) -> list[str]:
        return re.findall(r"\w+", text.replace("I", "ı").replace("İ", "i").lower())

    def set_continuous(self, enabled: bool) -> None:
        """Kesintisiz kipi çalışırken değiştirir; arayüze bildirir."""
        self.continuous = enabled
        self._emit_state("mode:on" if enabled else "mode:off")

    def _continuous_toggle(self, transcript: str) -> bool | None:
        """Yalnız (uyandırma sözcüğü + ) kip cümlesinden ibaret söz; LLM'e gitmez."""
        span = find_wake_word(transcript, self.config.wake_word)
        if span is not None and not self._speech_words(transcript[: span[0]]):
            transcript = transcript[span[1]:]
        words = [_fold(word) for word in self._speech_words(transcript)]
        if tuple(words) in _CONTINUOUS_OFF:
            return False
        if len(words) > _CONTINUOUS_MAX_WORDS or not any(
            # z/s her konumda eşit: "gezintisiz" ile "kesintisiz" yalnız g/k farkıyla eşleşir.
            _edit_distance(word.replace("z", "s"), key, 1) <= 1
            for word in words for key in _CONTINUOUS_KEYS
        ):
            return None

        def has_verb(stems: tuple[str, ...]) -> bool:
            return any(
                word.startswith(stem) and word[len(stem):] in _VERB_SUFFIXES
                for word in words for stem in stems
            )

        on = has_verb(_CONTINUOUS_ON_VERBS)
        off = has_verb(_CONTINUOUS_OFF_VERBS)
        return on if on != off else None

    def _is_spoken_echo(self, text: str, speech_start: float) -> bool:
        if not self._spoken_text or speech_start > self._spoken_until:
            return False
        heard = self._speech_words(text)
        spoken = self._speech_words(self._spoken_text)
        if not heard:
            return False
        normalized = " ".join(heard)
        for size in {max(1, len(heard) - 1), len(heard), len(heard) + 1}:
            for index in range(max(0, len(spoken) - size + 1)):
                candidate = " ".join(spoken[index:index + size])
                if candidate == normalized or (
                    len(heard) >= 2
                    and SequenceMatcher(None, normalized, candidate).ratio() >= 0.88
                ):
                    return True
        return False

    def _strip_spoken_prefix(self, text: str, speech_start: float) -> str:
        """Söz kendi sesimizle üst üste bindiyse baştaki o sözcükleri atar.

        "Dinliyorum" sırasında mikrofon açık; Whisper "Dinliyorum saat kaç" yazabilir.
        """
        if not self._spoken_text or speech_start > self._spoken_until:
            return text
        spoken = [_fold(word) for word in self._speech_words(self._spoken_text)]
        tokens = list(re.finditer(r"\w+", text))
        if len(tokens) <= len(spoken) or [
            _fold(token.group()) for token in tokens[: len(spoken)]
        ] != spoken:
            return text
        return text[tokens[len(spoken) - 1].end():].lstrip(" \t,.;:!?-")

    def _mark_spoken(self, text: str, *, requires_wake: bool = True) -> None:
        self._spoken_text = text
        self._spoken_until = math.inf
        self._output_requires_wake = requires_wake

    async def _acknowledge_wake(
        self, text: str | None = None, *, uid: int = 0, gid: int | None = None,
    ) -> None:
        """Tek başına uyandırmada (ve kip değişiminde) LLM çağırmadan kısa karşılık verir.

        Mikrofon durdurulmaz: kullanıcı "Artemis" deyip duraksamadan sürdürünce komutun başı
        "Dinliyorum" sırasında kayboluyordu. Kendi sesimiz yankı filtresiyle (_is_spoken_echo)
        elenir, başa yapışırsa _strip_spoken_prefix ayıklar.
        """
        text = self.config.wake_ack_text if text is None else text
        cancelled = asyncio.Event()
        self._emit_state("awaiting_command")
        played = False
        outcome: Outcome = "no_audio"

        def first_pcm() -> None:
            nonlocal played
            if not played:
                self._event(uid, "first_pcm", generation_id=gid, ack=True)
            played = True
            self._mark_spoken(text, requires_wake=False)

        try:
            self._output_active = True
            async with aclosing(self._timed_chunks(text, cancelled, uid, gid, ack=True)) as chunks:
                await self.sink.play(chunks, cancelled, first_pcm)
            outcome = "ack" if played else "no_audio"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except (OSError, RuntimeError, ValueError) as error:
            outcome = "error"
            logger.warning("Uyandırma sesi üretilemedi (%s).", type(error).__name__)
        finally:
            self._output_active = False
            self._event(uid, "terminal", generation_id=gid, outcome=outcome, ack=True)
            cancelled.set()
            self._spoken_until = self.clock() + 1.2
            if not self._closed:
                self._followup_until = self.clock() + self.config.followup_seconds
                self._awaiting_followup = True
                self._emit_state("awaiting_command")

    def _wake_command(self, transcript: str) -> str | None:
        """None: wake word yok/expired; boş dize: wake word tek başına."""
        span = find_wake_word(transcript, self.config.wake_word)
        if span is not None:
            tail = transcript[span[1] :].lstrip(" \t,.;:!?-")
            # Uydurma altyazı kuyruğu ("Artemis. Abone olmayı unutmayın.") tek başına uyandırmadır.
            if not tail.strip(" \t,.;:!?-") or is_ghost_transcript(tail):
                self._followup_until = self.clock() + self.config.followup_seconds
                return ""
            self._followup_until = 0.0
            return tail
        if self.clock() <= self._followup_until:
            self._followup_until = 0.0
            return transcript
        self._followup_until = 0.0
        # Kesintisiz kipte wake word'süz nihai söz komuttur (çıktı sırasında
        # _handle_utterance zaten wake word ister; yankı da öncesinde elenir).
        return transcript if self.continuous else None

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

    async def _timed_chunks(
        self, text: str, cancelled: asyncio.Event, uid: int, gid: int | None,
        *, ack: bool = False,
    ) -> AsyncGenerator[PCMChunk]:
        first = True
        cached = self._ack_cache.get(text) if ack else None
        if cached is not None:
            for chunk in cached:
                if first and chunk.pcm:
                    first = False
                    self._event(uid, "tts_first_chunk", generation_id=gid, ack=ack)
                yield chunk
            return
        recorded: list[PCMChunk] = []
        # Adaptörün süreç/ağ finally bloğu, sink erken dönse de kapanabilmeli.
        chunks = self.synthesizer.stream(text, cancelled)
        try:
            async for chunk in chunks:
                if first and chunk.pcm:
                    first = False
                    self._event(uid, "tts_first_chunk", generation_id=gid, ack=ack)
                recorded.append(chunk)
                yield chunk
            # Yalnız sonuna dek üretilen ses saklanır; kesilen akış yarım kalıp önbelleğe girmez.
            if ack and recorded and not cancelled.is_set():
                self._ack_cache[text] = recorded
        finally:
            closer = getattr(chunks, "aclose", None)
            if closer is not None:
                await closer()

    async def _play_response(
        self, text: str, cancelled: asyncio.Event, uid: int, gid: int | None,
        first_pcm: Callable[[], None],
    ) -> None:
        async with aclosing(self._timed_chunks(text, cancelled, uid, gid)) as chunks:
            await self.sink.play(chunks, cancelled, first_pcm)

    def _record_result(self, uid: int, gid: int | None, result: RunResult) -> None:
        self.on_timing("model_complete", self.clock())
        # Aşamalar AgentLoop'un monotonik saatindendir; sahte saatli testte sıra
        # bozulursa rapor örneği geçersiz sayar, sessizce düzeltmez.
        for phase in result.phases:
            self._event(uid, phase.stage, generation_id=gid, timestamp=phase.timestamp)
        command_class: CommandClass | None = None
        if result.turn_id:
            command_class = "tool" if result.tool_calls else "chat"
        self._event(uid, "model_complete", generation_id=gid, command_class=command_class)

    async def _play_stream(
        self, speech: SpeechStream, cancelled: asyncio.Event, uid: int, gid: int | None,
        first_pcm: Callable[[], None],
    ) -> None:
        """Cümleleri sırayla sentezler; hepsi tek sink akışında çalınır (cihaz bir kez açılır)."""

        async def chunks() -> AsyncGenerator[PCMChunk]:
            first = True
            async for sentence in speech.sentences():
                if cancelled.is_set():
                    return
                if self._output_active:
                    # Yankı süzgeci şimdiye dek söylenen/söylenecek tüm cümleleri bilsin.
                    self._spoken_text = " ".join(speech.spoken)
                stream = self.synthesizer.stream(sentence, cancelled)
                try:
                    async for chunk in stream:
                        if first and chunk.pcm:
                            first = False
                            self._event(uid, "tts_first_chunk", generation_id=gid)
                        yield chunk
                finally:
                    closer = getattr(stream, "aclose", None)
                    if closer is not None:
                        await closer()

        async with aclosing(chunks()) as generator:
            await self.sink.play(generator, cancelled, first_pcm)

    async def _run_and_speak_streamed(self, command: str, uid: int, gid: int | None) -> None:
        """ADR 0043: model yazarken cümleler söylenir; tek ``generation`` olayı hepsini durdurur."""
        generation = asyncio.Event()
        speech = SpeechStream()
        played = False
        outcome: Outcome = "no_audio"
        playback: asyncio.Task[None] | None = None

        def first_pcm() -> None:
            nonlocal played
            if not played:
                # Hoparlöre ilk yazım: söz kesme (ADR 0042) ve yankı süzgeci ancak şimdi devrede.
                self._mark_spoken(" ".join(speech.spoken))
                self._output_active = True
                self._emit_state("speaking")
                self.on_timing("first_pcm", self.clock())
                self._event(uid, "first_pcm", generation_id=gid)
                self.on_first_pcm()
            played = True

        try:
            self._emit_state("thinking")
            self._task_turns += 1
            self._event(uid, "command_start", generation_id=gid, warm=self._task_turns > 1)
            self._playback_cancelled = generation
            playback = asyncio.create_task(
                self._play_stream(speech, generation, uid, gid, first_pcm)
            )
            self._playback = playback
            result = await self.run_command(command, stream=speech)
            self._record_result(uid, gid, result)
            speech.finish(result.text)
            try:
                await playback
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if not generation.is_set() or (current is not None and current.cancelling()):
                    raise
            if played and self.config.followup_listening:
                self._followup_until = self.clock() + self.config.followup_seconds
            outcome = "completed" if played else "no_audio"
        except asyncio.CancelledError:
            outcome = "cancelled"
            generation.set()
            raise
        except (OSError, RuntimeError, ValueError) as error:
            outcome = "error"
            logger.warning("Ses yanıtı başarısız (%s).", type(error).__name__)
        finally:
            # Kuyrukta kalan cümleler yeni tura geçmez: akış kapanır, oynatma beklenir.
            speech.close()
            if playback is not None and not playback.done():
                if outcome != "completed":
                    generation.set()
                    playback.cancel()
                with suppress(asyncio.CancelledError):
                    await playback
            self._output_active = False
            self._playback = None
            self._playback_cancelled = None
            if played:
                self._spoken_until = self.clock() + 1.2
            self._event(uid, "terminal", generation_id=gid, outcome=outcome)
            if not self._closed:
                self._emit_state("listening")

    async def _run_and_speak(
        self, command: str, *, uid: int = 0, gid: int | None = None,
    ) -> None:
        if self.stream_responses:
            await self._run_and_speak_streamed(command, uid, gid)
            return
        generation = asyncio.Event()
        played = False
        outcome: Outcome = "no_audio"
        try:
            self._emit_state("thinking")
            self._task_turns += 1
            self._event(uid, "command_start", generation_id=gid, warm=self._task_turns > 1)
            result = await self.run_command(command)
            self._record_result(uid, gid, result)
            if result.text.strip() and not generation.is_set():

                def first_pcm() -> None:
                    nonlocal played
                    self._mark_spoken(result.text)
                    if not played:
                        self.on_timing("first_pcm", self.clock())
                        self._event(uid, "first_pcm", generation_id=gid)
                        self.on_first_pcm()
                    played = True

                self._emit_state("speaking")
                self._playback_cancelled = generation
                self._output_active = True
                self._playback = asyncio.create_task(self._play_response(
                    result.text, generation, uid, gid, first_pcm,
                ))
                try:
                    await self._playback
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if not generation.is_set() or (current is not None and current.cancelling()):
                        raise
                finally:
                    self._output_active = False
                    self._playback = None
                    self._playback_cancelled = None
                    self._spoken_until = self.clock() + 1.2
                if self.config.followup_listening:
                    self._followup_until = self.clock() + self.config.followup_seconds
            outcome = "completed" if played else "no_audio"
        except asyncio.CancelledError:
            outcome = "cancelled"
            generation.set()
            raise
        except (OSError, RuntimeError, ValueError) as error:
            outcome = "error"
            logger.warning("Ses yanıtı başarısız (%s).", type(error).__name__)
        finally:
            self._event(uid, "terminal", generation_id=gid, outcome=outcome)
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
        self._discard_capture("approval")
        self.segmenter.reset()

    def pause_for_approval(self) -> None:
        self.microphone.pause()
        self._discard_capture("approval")
        self.segmenter.reset()

    def resume_after_approval(self) -> None:
        self.microphone.discard_pending()
        self._discard_capture("approval")
        self.segmenter.reset()
        self.microphone.resume()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._discard_capture("capture_stopped")
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
