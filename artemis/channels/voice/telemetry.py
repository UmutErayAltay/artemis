"""Ses zamanları içerik taşımaz; kimlikler controller'a yereldir, AgentLoop turu değildir."""

from dataclasses import dataclass
from typing import Literal

Stage = Literal[
    "speech_start", "speech_end", "endpoint", "stt_final", "command_start",
    "model_request", "model_response", "tool_start", "tool_end",
    "model_complete", "tts_first_chunk", "first_pcm", "terminal",
    "stop_requested", "playback_stopped",
]
Outcome = Literal["completed", "rejected", "cancelled", "error", "no_audio", "ack"]
# Araç çağrısı olan tur ``tool``; model hiç araç çağırmadıysa ``chat``. Runner'a hiç
# ulaşmayan (meşgul/iptal) tur sınıflandırılmaz.
CommandClass = Literal["chat", "tool"]


@dataclass(frozen=True)
class VoiceTimingEvent:
    utterance_id: int
    generation_id: int | None
    stage: Stage
    timestamp: float
    response_kind: Literal["task", "ack"] = "task"
    outcome: Outcome | None = None
    reason: str | None = None
    # Yalnız ``model_complete`` olayında dolar.
    command_class: CommandClass | None = None
    # Yalnız ``command_start`` olayında dolar: controller ömrünün ilk görev turu soğuktur.
    warm: bool | None = None
