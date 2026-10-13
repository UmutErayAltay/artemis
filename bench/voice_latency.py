"""Ses yolunun sahte ölçümü ve isteğe bağlı canlı oturum ölçümü."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import platform
import signal
import statistics
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from time import monotonic
from typing import Any

from jarvis.brain.agent_loop import AgentLoop
from jarvis.brain.fake import ScriptedProvider
from jarvis.brain.models import Completion
from jarvis.brain.providers import build_provider
from jarvis.channels.runner import CommandRunner
from jarvis.channels.voice.adapters import (
    EdgeTTSynthesizer,
    FallbackSynthesizer,
    FasterWhisperRecognizer,
    PiperSynthesizer,
    SoundDeviceMicrophone,
    SoundDeviceSink,
    WebRTCVAD,
)
from jarvis.channels.voice.core import AudioFrame, PCMChunk, VoiceController
from jarvis.config.settings import Settings, VoiceConfig, load_settings
from jarvis.safety.audit import AuditLog
from jarvis.safety.gate import SafetyGate
from jarvis.tools.base import ToolContext
from jarvis.tools.registry import ToolRegistry, discover_tools


class FakeMicrophone:
    def __init__(self) -> None:
        self._frames = 7

    async def frames(self) -> AsyncIterator[AudioFrame]:
        for _ in range(self._frames):
            await asyncio.sleep(0.02)
            yield AudioFrame(b"\x01\x00", monotonic())

    def discard_pending(self) -> None:
        return None

    def pause(self) -> None:
        return None

    def resume(self) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeVAD:
    def __init__(self) -> None:
        self._states = iter([True, True, False, False, False, False, False])

    def is_speech(self, _pcm: bytes) -> bool:
        return next(self._states, False)


class FakeRecognizer:
    async def transcribe(self, _pcm: bytes) -> str:
        return "artemis saat kaç"

    async def transcribe_partial(self, _pcm: bytes) -> str:
        return "artemis saat"


class FakeSynthesizer:
    async def stream(self, _text: str, cancelled: asyncio.Event) -> AsyncIterator[PCMChunk]:
        if not cancelled.is_set():
            yield PCMChunk(b"\x00\x00")

    async def close(self) -> None:
        return None


class FakeSink:
    def __init__(self) -> None:
        self.played = False

    async def play(
        self,
        chunks: AsyncIterator[PCMChunk],
        cancelled: asyncio.Event,
        on_first_pcm: Any,
    ) -> None:
        async for chunk in chunks:
            if cancelled.is_set():
                return
            if chunk.pcm:
                self.played = True
                on_first_pcm()

    async def close(self) -> None:
        return None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def group_live_events(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, float]], int, int, int]:
    """Zaman çizgisindeki her konuşmayı tamamlandı/iptal/sessiz/geçersiz sayar."""
    sessions: list[dict[str, float]] = []
    current: dict[str, float] = {}
    for event in events:
        if event["stage"] == "speech_start" and current:
            sessions.append(current)
            current = {}
        current[event["stage"]] = event["timestamp"]
    if current:
        sessions.append(current)

    completed: list[dict[str, float]] = []
    aborted = no_audio = invalid = 0
    order = ["speech_start", "speech_end", "stt_final", "model_complete", "first_pcm"]
    for session in sessions:
        if "first_pcm" not in session:
            if "model_complete" in session:
                no_audio += 1
            else:
                aborted += 1
            continue
        if any(stage not in session for stage in order):
            invalid += 1
            continue
        stamps = [session[stage] for stage in order]
        if stamps != sorted(stamps):
            invalid += 1
            continue
        completed.append(session)
    return completed, aborted, no_audio, invalid


async def _fake_once() -> dict[str, float] | None:
    with tempfile.TemporaryDirectory(prefix="jarvis-voice-bench-") as directory:
        root = Path(directory)
        settings = Settings(
            workspace=root,
            state_dir=root,
            audit_path=root / "audit.jsonl",
            voice=VoiceConfig(frame_ms=20, pre_roll_ms=0, end_silence_ms=100),
        )
        audit = AuditLog(settings.audit_path)
        context = ToolContext(root, settings, audit)
        provider = ScriptedProvider([Completion(content="Deneme yanıtı")])
        runner = CommandRunner(
            AgentLoop(provider, SafetyGate(ToolRegistry()), context, lambda _n, _a: False)
        )
        events: dict[str, float] = {}
        sink = FakeSink()
        controller = VoiceController(
            runner,
            settings.voice,
            FakeMicrophone(),
            FakeVAD(),
            FakeRecognizer(),
            FakeSynthesizer(),
            sink,
            on_timing=lambda stage, timestamp: events.__setitem__(stage, timestamp),
        )
        await controller.run()
        if not sink.played or "first_pcm" not in events:
            return None
        return events


async def fake_report(iterations: int) -> dict[str, Any]:
    samples: list[dict[str, float]] = []
    no_audio = 0
    aborted = 0
    for _ in range(iterations):
        try:
            result = await _fake_once()
        except (OSError, RuntimeError, ValueError):
            aborted += 1
            continue
        if result is None:
            no_audio += 1
        else:
            samples.append(result)
    delays = [item["first_pcm"] - item["speech_end"] for item in samples]
    p90 = _percentile(delays, 0.90)
    return {
        "mode": "sahte (gerçek gecikme ölçümü değildir)",
        "samples": len(samples),
        "aborted": aborted,
        "no_audio": no_audio,
        "speech_end_to_first_pcm_ms": {
            "median": statistics.median(delays) * 1000 if delays else None,
            "p90": p90 * 1000 if p90 is not None else None,
        },
        "false_wake": None,
        "missed_wake": None,
        "false_approval": None,
        "barge_in_latency_ms": None,
    }


async def live_report(config_path: Path, duration: float) -> dict[str, Any]:
    settings = load_settings(config_path)
    if not settings.primary_model:
        raise ValueError("Canlı ölçüm için config içinde primary_model gerekli.")
    if settings.allow_paid:
        raise ValueError("Canlı ses ölçümü allow_paid: false ister.")
    settings.workspace.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(settings.audit_path)
    gate = SafetyGate(discover_tools())
    context = ToolContext(settings.workspace, settings, audit)
    from jarvis.channels.cli import request_approval

    approval_active = False
    runner: CommandRunner
    controller: VoiceController | None = None

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        nonlocal approval_active
        approval_active = True
        if controller is not None:
            controller.pause_for_approval()
        try:
            return request_approval(
                name,
                arguments,
                gate.pending_notice,
                on_interrupt=runner.cancel_current,
            )
        finally:
            approval_active = False
            if controller is not None:
                controller.resume_after_approval()

    runner = CommandRunner(AgentLoop(build_provider(settings, audit), gate, context, approve))
    sink = SoundDeviceSink()
    local_tts = PiperSynthesizer(settings.voice)
    recognizer = FasterWhisperRecognizer(settings.voice)
    await asyncio.to_thread(recognizer.prepare)
    await asyncio.to_thread(local_tts.prepare)
    synthesizer = (
        FallbackSynthesizer(EdgeTTSynthesizer(settings.voice, audit), local_tts)
        if settings.voice.tts_backend == "edge"
        else local_tts
    )
    events: list[dict[str, Any]] = []
    controller = VoiceController(
        runner,
        settings.voice,
        SoundDeviceMicrophone(settings.voice),
        WebRTCVAD(settings.voice),
        recognizer,
        synthesizer,
        sink,
        on_timing=lambda stage, timestamp: events.append({"stage": stage, "timestamp": timestamp}),
    )
    loop = asyncio.get_running_loop()
    loop.call_later(duration, controller.request_stop)
    previous = signal.getsignal(signal.SIGINT)

    def stop(_signum: int, _frame: Any) -> None:
        if approval_active:
            raise KeyboardInterrupt
        if runner.cancel_current():
            loop.call_soon_threadsafe(context.cancelled.set)
        loop.call_soon_threadsafe(controller.request_stop)

    signal.signal(signal.SIGINT, stop)
    try:
        print(f"Canlı ölçüm başladı ({duration:g} sn). Söylemek için 'artemis' deyin.")
        await controller.run()
    finally:
        signal.signal(signal.SIGINT, previous)

    completed, aborted, no_audio, invalid = group_live_events(events)
    delays = [session["first_pcm"] - session["speech_end"] for session in completed]
    p90 = _percentile(delays, 0.90)
    sounddevice = importlib.import_module("sounddevice")
    default_devices = sounddevice.default.device
    input_device = sounddevice.query_devices(default_devices[0], "input")
    output_device = sounddevice.query_devices(default_devices[1], "output")
    return {
        "mode": "canli",
        "samples": len(completed),
        "aborted": aborted,
        "no_audio": no_audio,
        "invalid_timing": invalid,
        "speech_end_to_first_pcm_ms": {
            "median": statistics.median(delays) * 1000 if delays else None,
            "p90": p90 * 1000 if p90 is not None else None,
        },
        "hardware": {
            "system": platform.platform(),
            "machine": platform.machine(),
            "input_device": input_device.get("name"),
            "output_device": output_device.get("name"),
            "stt_model": str(settings.voice.stt_model),
            "stt_device": settings.voice.stt_device,
            "stt_compute_type": settings.voice.stt_compute_type,
            "tts": settings.voice.tts_backend,
            "tts_model": (
                str(settings.voice.piper_model)
                if settings.voice.tts_backend == "piper"
                else settings.voice.edge_voice
            ),
            "sample_rate": settings.voice.sample_rate,
        },
        "false_wake": None,
        "missed_wake": None,
        "false_approval": None,
        "barge_in_latency_ms": None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="F3 ses gecikmesi ölçümü")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sahte", action="store_true")
    mode.add_argument("--canli", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--duration", type=float, default=60)
    args = parser.parse_args()
    if args.iterations < 1 or args.duration <= 0:
        parser.error("iterations ve duration pozitif olmalı")
    try:
        report = asyncio.run(
            fake_report(args.iterations) if args.sahte else live_report(args.config, args.duration)
        )
    except (OSError, RuntimeError, ValueError) as error:
        failure_report: dict[str, Any] = {
            "mode": "canli" if args.canli else "sahte",
            "samples": 0,
            "aborted": 1,
            "no_audio": 0,
            "speech_end_to_first_pcm_ms": {"median": None, "p90": None},
            "failure": type(error).__name__,
        }
        print(json.dumps(failure_report, ensure_ascii=False, indent=2))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
