"""Ses denetleyicisini ortak runtime'dan kurar; masaüstü köprüsü bunu işçide çağırır."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from jarvis.brain.agent_loop import RunResult
from jarvis.channels.runtime import Runtime
from jarvis.channels.voice.adapters import (
    EdgeTTSynthesizer,
    FallbackSynthesizer,
    FasterWhisperRecognizer,
    PiperSynthesizer,
    SoundDeviceMicrophone,
    SoundDeviceSink,
    WebRTCVAD,
)
from jarvis.channels.voice.core import Microphone, SpeechSynthesizer, VoiceController
from jarvis.channels.voice.denoise import (
    MODEL_SR,
    NOISE_LEVEL_ATTEN_DB,
    DeepFilterNetDenoiser,
    DenoiseDependencyError,
    DenoisedMicrophone,
)
from jarvis.channels.voice.echo import (
    EchoCancelledMicrophone,
    EchoCanceller,
    EchoReference,
)
from jarvis.config.model_paths import DEFAULT_NOISE_MODEL_DIR

logger = logging.getLogger(__name__)


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
    runtime: Runtime, run_command: RunCommand, on_state: StateCallback
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
    microphone: Microphone = SoundDeviceMicrophone(
        settings.voice,
        device=settings.voice.input_device,
        capture_rate=MODEL_SR if denoiser is not None else None,
    )
    echo_reference: EchoReference | None = None
    if settings.voice.echo_cancellation:
        reference = EchoReference(MODEL_SR if denoiser is not None else settings.voice.sample_rate)
        echo_reference = reference

        def make_canceller(rate: int) -> EchoCanceller:
            reference.retarget(rate)
            return EchoCanceller(rate, reference, delay_ms=settings.voice.echo_delay_ms)

        microphone = EchoCancelledMicrophone(microphone, make_canceller, on_state)
    if denoiser is not None:
        microphone = DenoisedMicrophone(
            microphone, denoiser, on_state, raw_for_stt=not settings.voice.noise_suppression_stt
        )
    if echo_reference is None:
        sink = SoundDeviceSink()
    else:
        sink = SoundDeviceSink(
            echo_reference=echo_reference,
            on_echo_failure=lambda: on_state("echo:off"),
        )
    recognizer = FasterWhisperRecognizer(settings.voice)
    await asyncio.to_thread(recognizer.prepare)
    synthesizer: SpeechSynthesizer
    if settings.voice.tts_backend == "edge":
        primary_tts = EdgeTTSynthesizer(settings.voice, runtime.audit)
        if settings.voice.tts_local_fallback:
            local_tts = PiperSynthesizer(settings.voice)
            await asyncio.to_thread(local_tts.prepare)
            synthesizer = FallbackSynthesizer(primary_tts, local_tts)
        else:
            synthesizer = primary_tts
    else:
        local_tts = PiperSynthesizer(settings.voice)
        await asyncio.to_thread(local_tts.prepare)
        synthesizer = local_tts
    return VoiceController(
        runtime.runner,
        settings.voice,
        microphone,
        WebRTCVAD(settings.voice),
        recognizer,
        synthesizer,
        sink,
        run_command=run_command,
        on_state=on_state,
        on_input_level=lambda level: on_state(f"level:{level}"),
        on_transcript=lambda text: on_state("heard:" + text[:200]),
    )
