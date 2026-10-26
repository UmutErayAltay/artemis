"""Ses denetleyicisini ortak runtime'dan kurar; masaüstü köprüsü bunu işçide çağırır."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Protocol

from artemis.brain.agent_loop import RunResult
from artemis.channels.runtime import Runtime
from artemis.channels.voice.adapters import (
    EdgeTTSynthesizer,
    EmaSynthesizer,
    FallbackSynthesizer,
    FasterWhisperRecognizer,
    OpenAICompatibleTranscriber,
    PiperSynthesizer,
    SoundDeviceMicrophone,
    SoundDeviceSink,
    WebRTCVAD,
)
from artemis.channels.voice.core import Microphone, SpeechSynthesizer, VoiceController
from artemis.channels.voice.denoise import (
    MODEL_SR,
    NOISE_LEVEL_ATTEN_DB,
    DeepFilterNetDenoiser,
    DenoiseDependencyError,
    DenoisedMicrophone,
)
from artemis.channels.voice.echo import (
    EchoCancelledMicrophone,
    EchoCanceller,
    EchoReference,
)
from artemis.channels.voice.telemetry import VoiceTimingEvent
from artemis.config.model_paths import DEFAULT_NOISE_MODEL_DIR
from artemis.config.settings import Settings, VoiceConfig
from artemis.tools.app_resolver import AliasStoreError, alias_store

logger = logging.getLogger(__name__)


def alias_hotword_config(settings: Settings) -> VoiceConfig:
    """``stt_alias_hotwords`` açıksa öğretilmiş takma adların hedef adlarını ipucuna ekler.

    Yalnız kullanıcının düzelterek öğrettiği uygulamalar eklenir, katalog değil: geniş
    hotword listesi doğruluğu ve uyandırmayı bozabilir (ADR 0044). Ekleme ayarın sınırlarını
    (100 sözcük, 2048 karakter) aşmaz; sığmayan ad atlanır. Takma ad dosyası okunamazsa ses
    yine açılır, ipucu eklenmez.
    """
    voice = settings.voice
    if not voice.stt_alias_hotwords or not settings.persist_memory:
        return voice
    try:
        aliases = alias_store(settings.state_dir).load()
    except AliasStoreError as error:
        logger.warning("Takma ad ipuçları eklenemedi: %s", error)
        return voice
    words = list(voice.stt_hotwords)
    for name in sorted(set(aliases.values())):
        if name not in settings.applications or name in words:
            continue
        if len(words) >= 100 or sum(map(len, words)) + len(name) > 2048:
            break
        words.append(name)
    return VoiceConfig.model_validate({**voice.model_dump(), "stt_hotwords": tuple(words)})


def _denoise_reason(error: Exception) -> str:
    """``kind:metin``; kind `deps` (paket eksik) ve `model` (dosya sorunu) farklı çözüm ister."""
    if isinstance(error, DenoiseDependencyError | ImportError):
        return "deps:onnxruntime veya numpy kurulu değil"
    if isinstance(error, OSError):
        return "model:model dosyaları bulunamadı veya okunamadı"
    if isinstance(error, ValueError):
        return "model:model dosyası sha256 doğrulamasından geçmedi"
    return "model:model yüklenemedi (dosya bozuk olabilir)"


RunCommand = Callable[[str], Awaitable[RunResult]]
StateCallback = Callable[[str], None]


async def build_voice_controller(
    runtime: Runtime, run_command: RunCommand, on_state: StateCallback,
    *, on_timing: Callable[[str, float], None] | None = None,
    on_event: Callable[[VoiceTimingEvent], None] | None = None,
) -> VoiceController:
    """STT/TTS modellerini hazırlar; eksik model OSError/RuntimeError/ValueError olarak çıkar.

    Hazırlık engelleyici dosya/model yüklemesi olduğu için ``to_thread`` ile yapılır:
    işçi döngüsü (onay iptali, kapanış) hazırlık sürerken tıkanmaz. Onay çağrısı
    burada bağlanmaz; onay yalnız ``create_runtime``'a verilen ekran onayıdır.
    """
    settings = runtime.settings
    denoiser: DeepFilterNetDenoiser | None = None
    if settings.voice.noise_suppression:
        # Gürültü engelleme isteğe bağlı bir iyileştirmedir: kurulamazsa ses hattı ham
        # mikrofonla sürer, ama durum görünür olur (sessizce çalışıyormuş gibi davranılmaz).
        try:
            model_dir = settings.voice.noise_model_dir or DEFAULT_NOISE_MODEL_DIR
            denoiser = await asyncio.to_thread(
                DeepFilterNetDenoiser,
                model_dir,
                atten_lim_db=NOISE_LEVEL_ATTEN_DB[settings.voice.noise_suppression_level],
                frame_samples=settings.voice.sample_rate * settings.voice.frame_ms // 1000,
            )
        except (OSError, ValueError, RuntimeError, ImportError) as error:
            logger.warning("Gürültü engelleme açılamadı, ham mikrofonla sürülüyor: %s", error)
            on_state("denoise:unavailable:" + _denoise_reason(error))
    # Gürültü engelleme açıksa mikrofon yerel 48 kHz yakalanır (modelin doğal hızı, >8 kHz bilgi
    # korunur); açılamazsa adaptör uyarıyla 16 kHz'e döner. Aşağı akış hep 16 kHz görür.
    async with AsyncExitStack() as cleanup:
        microphone: Microphone = SoundDeviceMicrophone(
            settings.voice,
            device=settings.voice.input_device,
            capture_rate=MODEL_SR if denoiser is not None else None,
        )
        cleanup.push_async_callback(_close_optional, microphone)
        echo_reference: EchoReference | None = None
        if settings.voice.echo_cancellation:
            reference = EchoReference(
                MODEL_SR if denoiser is not None else settings.voice.sample_rate
            )
            echo_reference = reference

            def make_canceller(rate: int) -> EchoCanceller:
                reference.retarget(rate)
                return EchoCanceller(rate, reference, delay_ms=settings.voice.echo_delay_ms)

            echo_off_reported = False

            def report_echo_state(state: str) -> None:
                # Mikrofon sarmalayıcısı ve hoparlör beslemesi aynı arızayı ayrı ayrı görebilir
                # (ör. livekit yok); kullanıcıya tek bildirim yeter.
                nonlocal echo_off_reported
                if state == "echo:off":
                    if echo_off_reported:
                        return
                    echo_off_reported = True
                on_state(state)

            microphone = EchoCancelledMicrophone(microphone, make_canceller, report_echo_state)
        if denoiser is not None:
            microphone = DenoisedMicrophone(
                microphone, denoiser, on_state, raw_for_stt=not settings.voice.noise_suppression_stt
            )
        if echo_reference is None:
            sink = SoundDeviceSink()
        else:
            sink = SoundDeviceSink(
                echo_reference=echo_reference,
                on_echo_failure=lambda: report_echo_state("echo:off"),
            )
        cleanup.push_async_callback(_close_optional, sink)
        cloud_recognizer: OpenAICompatibleTranscriber | None = None
        if settings.voice.stt_mode == "hybrid":
            # İzin kapalıysa kurucu ValueError verir ve ses kanalı başlamaz (Edge TTS ile aynı);
            # model yüklemeden önce denetlenir ki boşuna GPU belleği ayrılmasın.
            endpoint_id = settings.voice.stt_cloud_endpoint
            cloud_recognizer = OpenAICompatibleTranscriber(
                alias_hotword_config(settings), endpoint_id,
                settings.endpoints[endpoint_id], runtime.audit,
            )
        recognizer = FasterWhisperRecognizer(alias_hotword_config(settings))
        await _prepare(recognizer)
        synthesizer: SpeechSynthesizer
        if settings.voice.tts_backend == "edge":
            edge_tts = EdgeTTSynthesizer(settings.voice, runtime.audit)
            cleanup.push_async_callback(_close_optional, edge_tts)
            # Yedek yalnız Piper modeli varken kurulur: `tts_local_fallback` varsayılan açıktır ve
            # model yokken Piper kurucusu çalışabilecek Edge sesini de kapatırdı.
            if settings.voice.tts_local_fallback and settings.voice.piper_model:
                local_tts = PiperSynthesizer(settings.voice)
                await _prepare(local_tts)
                cleanup.push_async_callback(_close_optional, local_tts)
                synthesizer = FallbackSynthesizer(edge_tts, local_tts)
            else:
                synthesizer = edge_tts
        elif settings.voice.tts_backend == "ema":
            primary_tts = EmaSynthesizer(settings.voice)
            await _prepare(primary_tts)
            cleanup.push_async_callback(_close_optional, primary_tts)
            if settings.voice.tts_local_fallback and settings.voice.piper_model:
                local_tts = PiperSynthesizer(settings.voice)
                await _prepare(local_tts)
                cleanup.push_async_callback(_close_optional, local_tts)
                synthesizer = FallbackSynthesizer(primary_tts, local_tts)
            else:
                synthesizer = primary_tts
        else:
            local_tts = PiperSynthesizer(settings.voice)
            await _prepare(local_tts)
            cleanup.push_async_callback(_close_optional, local_tts)
            synthesizer = local_tts
        controller = VoiceController(
            runtime.runner,
            settings.voice,
            microphone,
            WebRTCVAD(settings.voice),
            recognizer,
            synthesizer,
            sink,
            run_command=run_command,
            stream_responses=settings.voice.streaming,
            on_state=on_state,
            on_timing=on_timing,
            on_event=on_event,
            on_input_level=lambda level: on_state(f"level:{level}"),
            on_transcript=lambda text: on_state("heard:" + text[:200]),
            cloud_recognizer=cloud_recognizer,
        )
        cleanup.pop_all()
        return controller


async def _close_optional(resource: object) -> None:
    closer = getattr(resource, "close", None)
    if closer is not None:
        try:
            await closer()
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("Ses kaynağı kapatılamadı (%s).", type(error).__name__)


class PreparedResource(Protocol):
    def prepare(self) -> None: ...


async def _prepare(resource: PreparedResource) -> None:
    """to_thread iptali yüklemeyi durdurmaz; bitince kapat, yarışarak dispose etme."""
    task = asyncio.create_task(asyncio.to_thread(resource.prepare))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        def finish_and_close(done: asyncio.Task[None]) -> None:
            if done.cancelled():
                # Döngü kapanıyorsa çalışan thread'e yarışarak dokunulmaz.
                return
            error = done.exception()
            if error is not None:
                logger.debug("İptal edilen hazırlık başarısız (%s).", type(error).__name__)
            asyncio.create_task(_close_optional(resource))

        task.add_done_callback(finish_and_close)
        raise
    except (OSError, RuntimeError, ValueError, ImportError):
        await _close_optional(resource)
        raise
