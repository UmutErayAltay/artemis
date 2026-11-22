"""Ses kanalı ses kodlama, yankı süzgeci ve komut yönlendirme yardımcıları."""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
import math
import re
import wave
from array import array
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from difflib import SequenceMatcher
from typing import Any

from artemis.brain.agent_loop import RunResult
from artemis.channels.voice.contracts import (
    AudioSink,
    Microphone,
    PCMChunk,
    SpeechRecognizer,
    SpeechSynthesizer,
)
from artemis.channels.voice.streaming import SpeechStream
from artemis.channels.voice.wake import _edit_distance, _fold, find_wake_word, is_ghost_transcript
from artemis.config.settings import VoiceConfig
from artemis.safety.audit import AuditLog
from artemis.text import turkish_fold

logger = logging.getLogger("artemis.channels.voice.core")

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

PRIVATE_REMOTE_TTS_NOTICE = "Bu yanıt gizli içerik taşıdığı için bulut sesine gönderilmedi."
MAX_DIRECT_AUDIO_BYTES = 2_000_000


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """16-bit mono PCM baytlarını bellekte standart WAV kapsayıcısına sarar."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def encode_direct_audio_wav(
    pcm: bytes, sample_rate: int, max_bytes: int = MAX_DIRECT_AUDIO_BYTES
) -> bytes | None:
    """Doğrudan ses girişi boyut sınırını aşmıyorsa bellekte WAV üretir, yoksa None döner."""
    if not pcm:
        return None
    encoded_wav = pcm_to_wav(pcm, sample_rate)
    return encoded_wav if len(encoded_wav) <= max_bytes else None


def accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    """Hedef çağrılabilenin belirtilen anahtar sözcük argümanını kabul edip etmediğini ölçer."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def call_run_command(
    run_command: Callable[..., Awaitable[RunResult]],
    command: str,
    *,
    stream: SpeechStream | None = None,
    audio_wav: bytes | None = None,
    accepts_audio: bool = False,
) -> Awaitable[RunResult]:
    """Komut çalıştırıcıyı akış ve isteğe bağlı doğrudan ses baytlarıyla çağırır."""
    kwargs: dict[str, Any] = {}
    if stream is not None:
        kwargs["stream"] = stream
    if audio_wav is not None and accepts_audio:
        kwargs["audio_wav"] = audio_wav
    return run_command(command, **kwargs)


async def transcribe_cloud_command(
    cloud_recognizer: SpeechRecognizer,
    pcm: bytes,
    local_command: str,
    wake_word: str,
    *,
    on_cancelled: Callable[[], None],
    on_fallback: Callable[[], None],
) -> str:
    """Komut metnini bulut tanıyıcıdan alır.

    Yalnız istisnada (ağ/HTTP/geçersiz yanıt) yerel metne düşülür. Çağrı başarılıysa bulut
    metni nihaidir: boş ya da yalnız uyandırma sözcüğü ise "" (tek başına uyandırma) döner;
    yerel kuyruk (halüsinasyon) komut olmaz.
    """
    try:
        text = (await cloud_recognizer.transcribe(pcm)).strip()
    except asyncio.CancelledError:
        on_cancelled()
        raise
    except (OSError, RuntimeError, ValueError) as error:
        # Hata metni yanıt gövdesi/anahtar taşımaz ama yine de yalnız tür günlüğe yazılır.
        logger.warning(
            "Bulut tanıma başarısız, yerel metin kullanılıyor (%s).", type(error).__name__
        )
        on_fallback()
        return local_command
    span = find_wake_word(text, wake_word)
    command = (text[span[1] :] if span is not None else text).lstrip(" \t,.;:!?-")
    if is_ghost_transcript(command):
        return ""
    return command if command.strip(" \t,.;:!?-") else ""


def speech_words(text: str) -> list[str]:
    return re.findall(r"\w+", turkish_fold(text))


def continuous_toggle(
    transcript: str,
    wake_word: str,
    *,
    awaiting_followup: bool,
    followup_active: bool,
) -> bool | None:
    """Yalnız (uyandırma sözcüğü + ) kip cümlesinden ibaret söz; LLM'e gitmez."""
    span = find_wake_word(transcript, wake_word)
    if span is not None and not speech_words(transcript[: span[0]]):
        transcript = transcript[span[1] :]
    words = [_fold(word) for word in speech_words(transcript)]
    if tuple(words) in _CONTINUOUS_OFF:
        return False
    if len(words) > _CONTINUOUS_MAX_WORDS or not any(
        # z/s her konumda eşit: "gezintisiz" ile "kesintisiz" yalnız g/k farkıyla eşleşir.
        _edit_distance(word.replace("z", "s"), key, 1) <= 1
        for word in words
        for key in _CONTINUOUS_KEYS
    ):
        return None

    def has_verb(stems: tuple[str, ...]) -> bool:
        return any(
            word.startswith(stem) and word[len(stem) :] in _VERB_SUFFIXES
            for word in words
            for stem in stems
        )

    on = has_verb(_CONTINUOUS_ON_VERBS)
    off = has_verb(_CONTINUOUS_OFF_VERBS)
    if on == off:
        return None
    # TV/arka plan konuşmasının sürekli dinlemeyi açmaması için kipi açma
    # yalnızca uyandırma sözcüğüyle veya açık takip penceresinde kabul edilir.
    if on and not (span is not None or awaiting_followup or followup_active):
        return None
    return on


def is_spoken_echo(
    text: str, speech_start: float, spoken_text: str, spoken_until: float
) -> bool:
    """Asistanın kendi son çıkışıyla eşleşen mikrofon yankısını saptar."""
    if not spoken_text or speech_start > spoken_until:
        return False
    heard = speech_words(text)
    spoken = speech_words(spoken_text)
    if not heard:
        return False
    normalized = " ".join(heard)
    for size in {max(1, len(heard) - 1), len(heard), len(heard) + 1}:
        for index in range(max(0, len(spoken) - size + 1)):
            candidate = " ".join(spoken[index : index + size])
            if candidate == normalized or (
                len(heard) >= 2 and SequenceMatcher(None, normalized, candidate).ratio() >= 0.88
            ):
                return True
    return False


def strip_spoken_prefix(
    text: str, speech_start: float, spoken_text: str, spoken_until: float
) -> str:
    """Söz kendi sesimizle üst üste bindiyse baştaki o sözcükleri atar.

    "Dinliyorum" sırasında mikrofon açık; Whisper "Dinliyorum saat kaç" yazabilir.
    """
    if not spoken_text or speech_start > spoken_until:
        return text
    spoken = [_fold(word) for word in speech_words(spoken_text)]
    tokens = list(re.finditer(r"\w+", text))
    if (
        len(tokens) <= len(spoken)
        or [_fold(token.group()) for token in tokens[: len(spoken)]] != spoken
    ):
        return text
    return text[tokens[len(spoken) - 1].end() :].lstrip(" \t,.;:!?-")


def is_remote_synthesizer(synthesizer: SpeechSynthesizer, tts_backend: str) -> bool:
    """Aktif birincil ses sentezleyicinin buluta metin gönderip göndermediğini döndürür."""
    # Çevrimsel içe aktarmayı önlemek için adaptörler burada geç yüklenir; politika tek yerde.
    from artemis.channels.voice.adapters import (
        EdgeTTSynthesizer,
        EmaSynthesizer,
        FallbackSynthesizer,
        PiperSynthesizer,
    )
    from artemis.channels.voice.cloud_tts import CloudSpeechSynthesizer

    target: object = synthesizer
    if isinstance(target, FallbackSynthesizer):
        target = target.primary
    if isinstance(target, (PiperSynthesizer, EmaSynthesizer)):
        return False
    if isinstance(target, (CloudSpeechSynthesizer, EdgeTTSynthesizer)):
        return True
    return tts_backend in ("edge", "cloud")


def emit_private_tts_blocked(
    audit: AuditLog | None, synthesizer: SpeechSynthesizer, config: VoiceConfig
) -> None:
    """Gizli yanıt nedeniyle engellenen uzak TTS aktarımını denetime yazar."""
    if audit is None:
        return
    from artemis.channels.voice.adapters import EdgeTTSynthesizer, FallbackSynthesizer
    from artemis.channels.voice.cloud_tts import CloudSpeechSynthesizer

    target: object = synthesizer
    if isinstance(target, FallbackSynthesizer):
        target = target.primary
    if isinstance(target, CloudSpeechSynthesizer):
        audit.emit(
            "cloud_tts_transfer",
            service=target.endpoint_id,
            model=target.config.tts_cloud_model,
            content_kind="assistant_response_text",
            allowed=False,
            reason="private",
        )
        return
    service = (
        "edge_tts"
        if isinstance(target, EdgeTTSynthesizer) or config.tts_backend == "edge"
        else (config.tts_cloud_endpoint or config.tts_backend)
    )
    audit.emit(
        "cloud_tts_transfer",
        service=service,
        content_kind="assistant_response_text",
        allowed=False,
        reason="private",
    )


def frame_input_level(pcm: bytes) -> int:
    """16-bit PCM karesinden 0..100 arası mikrofon giriş düzeyi hesaplar."""
    samples = array("h", pcm[: len(pcm) // 2 * 2])
    rms = math.sqrt(sum(value * value for value in samples) / max(1, len(samples)))
    return min(100, int(rms / 32768 * 500))


async def close_voice_resources(
    microphone: Microphone,
    sink: AudioSink,
    synthesizer: SpeechSynthesizer,
    local_synthesizer: SpeechSynthesizer | None,
) -> None:
    """Mikrofon, hoparlör ve ses üreticilerini sırayla ve hataya dayanıklı kapatır."""
    closers: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        ("mikrofon", microphone.close),
        ("ses çıkışı", sink.close),
        ("ses üretici", synthesizer.close),
    ]
    if (
        local_synthesizer is not None
        and local_synthesizer is not synthesizer
        and local_synthesizer is not getattr(synthesizer, "local", None)
    ):
        closers.append(("yerel ses üretici", local_synthesizer.close))
    for label, closer in closers:
        try:
            await closer()
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("%s kapatılamadı (%s).", label, type(error).__name__)


async def play_speech_stream(
    sink: AudioSink,
    speech: SpeechStream,
    cancelled: asyncio.Event,
    first_pcm: Callable[[], None],
    *,
    synth_stream: Callable[..., AsyncGenerator[PCMChunk]],
    on_first_chunk: Callable[[], None],
    on_sentence: Callable[[], None],
    should_stop_private: Callable[[bool], bool],
    privacy_ready: asyncio.Event | None = None,
    is_private: Callable[[], bool] | None = None,
) -> None:
    """Cümleleri sırayla sentezler; hepsi tek sink akışında çalınır (cihaz bir kez açılır)."""

    async def chunks() -> AsyncGenerator[PCMChunk]:
        first = True

        async def next_sentences() -> AsyncGenerator[str]:
            if privacy_ready is not None and not privacy_ready.is_set():
                held = [s async for s in speech.sentences() if not cancelled.is_set()]
                await privacy_ready.wait()
                for sentence in held:
                    yield sentence
                return
            async for sentence in speech.sentences():
                yield sentence

        index = 0
        async for sentence in next_sentences():
            if cancelled.is_set():
                return
            on_sentence()
            private = is_private() if is_private is not None else False
            stream = synth_stream(
                sentence, cancelled, private=private, record_block=(index == 0)
            )
            index += 1
            try:
                async for chunk in stream:
                    if first and chunk.pcm:
                        first = False
                        on_first_chunk()
                    yield chunk
            finally:
                closer = getattr(stream, "aclose", None)
                if closer is not None:
                    await closer()
            if should_stop_private(private):
                return

    async with aclosing(chunks()) as generator:
        await sink.play(generator, cancelled, first_pcm)
