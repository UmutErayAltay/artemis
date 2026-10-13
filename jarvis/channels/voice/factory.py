"""Ses denetleyicisini ortak runtime'dan kurar; masaüstü köprüsü bunu işçide çağırır."""

from __future__ import annotations

import asyncio
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
from jarvis.channels.voice.core import SpeechSynthesizer, VoiceController

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
    microphone = SoundDeviceMicrophone(settings.voice)
    sink = SoundDeviceSink()
    local_tts = PiperSynthesizer(settings.voice)
    recognizer = FasterWhisperRecognizer(settings.voice)
    await asyncio.to_thread(recognizer.prepare)
    await asyncio.to_thread(local_tts.prepare)
    synthesizer: SpeechSynthesizer
    if settings.voice.tts_backend == "edge":
        primary_tts = EdgeTTSynthesizer(settings.voice, runtime.audit)
        synthesizer = FallbackSynthesizer(primary_tts, local_tts)
    else:
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
    )
