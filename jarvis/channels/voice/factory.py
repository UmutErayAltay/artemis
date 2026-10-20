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
    NOISE_LEVEL_ATTEN_DB,
    DeepFilterNetDenoiser,
    DenoisedMicrophone,
)

logger = logging.getLogger(__name__)


def _denoise_reason(error: Exception) -> str:
    if isinstance(error, OSError):
        return "model dosyaları bulunamadı veya okunamadı"
    if isinstance(error, ValueError):
        return "model dosyası doğrulanamadı veya dizin ayarlı değil"
    return "onnxruntime/numpy eksik ya da model yüklenemedi"


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
    microphone: Microphone = SoundDeviceMicrophone(
        settings.voice, device=settings.voice.input_device
    )
    if settings.voice.noise_suppression:
        # Gürültü engelleme isteğe bağlı bir iyileştirmedir: kurulamazsa ses hattı ham
        # mikrofonla sürer, ama durum görünür olur (sessizce çalışıyormuş gibi davranılmaz).
        try:
            model_dir = settings.voice.noise_model_dir
            if model_dir is None:
                raise ValueError("model dizini ayarlı değil")
            denoiser = await asyncio.to_thread(
                DeepFilterNetDenoiser,
                model_dir,
                atten_lim_db=NOISE_LEVEL_ATTEN_DB[settings.voice.noise_suppression_level],
                frame_samples=settings.voice.sample_rate * settings.voice.frame_ms // 1000,
            )
        except (OSError, ValueError, RuntimeError, ImportError) as error:
            logger.warning("Gürültü engelleme açılamadı, ham mikrofonla sürülüyor: %s", error)
            on_state("denoise:unavailable:" + _denoise_reason(error))
        else:
            microphone = DenoisedMicrophone(microphone, denoiser, on_state)
    sink = SoundDeviceSink()
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
