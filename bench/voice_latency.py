"""Ses yolunun sahte ölçümü ve isteğe bağlı canlı oturum ölçümü."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import signal
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import Any

from pydantic import BaseModel, ConfigDict

from artemis.brain.agent_loop import AgentLoop
from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import Completion, ToolCall
from artemis.channels.runner import CommandRunner
from artemis.channels.runtime import Runtime, create_runtime
from artemis.channels.voice.core import AudioFrame, PCMChunk, VoiceController
from artemis.channels.voice.factory import build_voice_controller
from artemis.channels.voice.metrics import EventCollector, collector_report
from artemis.channels.voice.telemetry import VoiceTimingEvent
from artemis.config.settings import Settings, VoiceConfig, load_settings
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import ToolRegistry
from bench.voice_resources import GpuSampler, process_peak_memory_bytes


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchClockTool(BaseTool):
    """Yalnız sahte ölçümün araçlı tur yolunu sınar; global registry'ye girmez."""

    name = "bench_clock"
    description = "Sahte ölçüm için sabit saat döndürür."
    args_model = _NoArguments
    risk = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(success=True, status="ok", message="Saat 10.00.")


class FakeMicrophone:
    def __init__(self, completed: asyncio.Event | None = None) -> None:
        self._frames = 7
        self.completed = completed

    async def frames(self) -> AsyncIterator[AudioFrame]:
        for _ in range(self._frames):
            await asyncio.sleep(0.02)
            yield AudioFrame(b"\x01\x00", monotonic())
        if self.completed is not None:
            # Sahte EOF yanıt görevini yarışarak iptal etmesin; gerçek EOF kuralı korunur.
            await asyncio.wait_for(self.completed.wait(), 2)

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


def group_live_events(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, float]], int, int, int]:
    """Eski kimliksiz kayıtlar için uyumluluk; yeni rapor bu gruplamayı kullanmaz."""
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


async def _fake_once(*, with_tool: bool = False) -> list[VoiceTimingEvent]:
    with tempfile.TemporaryDirectory(prefix="artemis-voice-bench-") as directory:
        root = Path(directory)
        settings = Settings(
            workspace=root,
            state_dir=root,
            audit_path=root / "audit.jsonl",
            voice=VoiceConfig(frame_ms=20, pre_roll_ms=0, end_silence_ms=100),
        )
        audit = AuditLog(settings.audit_path)
        context = ToolContext(root, settings, audit)
        registry = ToolRegistry()
        if with_tool:
            registry.register(BenchClockTool)
            provider = ScriptedProvider([
                Completion(tool_calls=[ToolCall(id="bench-1", name="bench_clock")]),
                Completion(content="Saat 10.00."),
            ])
        else:
            provider = ScriptedProvider([Completion(content="Deneme yanıtı")])
        runner = CommandRunner(
            AgentLoop(provider, SafetyGate(registry), context, lambda _n, _a: False)
        )
        events: list[VoiceTimingEvent] = []
        completed = asyncio.Event()

        def on_event(event: VoiceTimingEvent) -> None:
            events.append(event)
            if event.stage == "terminal":
                completed.set()

        sink = FakeSink()
        controller = VoiceController(
            runner,
            settings.voice,
            FakeMicrophone(completed),
            FakeVAD(),
            FakeRecognizer(),
            FakeSynthesizer(),
            sink,
            on_event=on_event,
            # Üretimdeki factory gibi akışlı yanıt yolu sınanır (ADR 0043).
            stream_responses=True,
        )
        await controller.run()
        return events


async def fake_report(iterations: int) -> dict[str, Any]:
    collector = EventCollector()
    failures: list[dict[str, str]] = []
    for index in range(iterations):
        try:
            # Tek ve çift turlar sohbet ile araçlı turun aynı rapor şemasından geçtiğini sınar.
            for event in await _fake_once(with_tool=index % 2 == 1):
                collector(replace(event, utterance_id=event.utterance_id + index * 1_000))
        except (OSError, RuntimeError, ValueError) as error:
            failures.append({"phase": "fake_iteration", "error_type": type(error).__name__})
    report = _report(collector, "sahte (gerçek gecikme ölçümü değildir)")
    # Sahte adaptörün süresi hedef kanıtı değildir; örnek sayısı yetse de hedef değerlendirilmez.
    for target in report["targets"].values():
        target["met"] = None
        target["not_evaluated"] = "sahte"
    report["requested_iterations"] = iterations
    report["session_failures"] = failures
    report["failed_iterations"] = len(failures)
    report["hardware"] = None
    return report


def _report(collector: EventCollector, mode: str) -> dict[str, Any]:
    return collector_report(collector, mode)


def _hardware(
    settings: Settings, states: set[str], output: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    voice = settings.voice
    output = output or {}
    resources = resources or {}
    return {
        "system": platform.platform(), "machine": platform.machine(),
        "requested_input_device": voice.input_device,
        "input_device": None, "output_device": output.get("device"),
        "input_sample_rate": None, "output_sample_rate": output.get("sample_rate"),
        # PortAudio'nun bildirdiği çıkış gecikmesi; Windows loopback/manuel kabul değildir.
        "output_latency_seconds": output.get("latency_seconds"),
        "requested_stt_model": str(voice.stt_model) if voice.stt_model is not None else None,
        "requested_stt_device": voice.stt_device,
        "requested_stt_compute_type": voice.stt_compute_type,
        "requested_stt_beam_size": voice.stt_beam_size,
        "requested_tts_backend": voice.tts_backend, "actual_tts_backend": None,
        "requested_tts_model": (
            str(voice.piper_model) if voice.tts_backend == "piper" and voice.piper_model
            else None if voice.tts_backend == "piper"
            else voice.edge_voice if voice.tts_backend == "edge" else "ema"
        ),
        "pipeline_sample_rate": voice.sample_rate,
        "noise_suppression": {
            "requested": voice.noise_suppression,
            "actual": False if (not voice.noise_suppression or "denoise:off" in states
                                or "denoise:unavailable" in states) else None,
            "stt_requested": voice.noise_suppression_stt,
        },
        "echo_cancellation": {
            "requested": voice.echo_cancellation,
            "actual": False if not voice.echo_cancellation or "echo:off" in states else None,
        },
        "processing_states": sorted(states),
        "cpu_peak_memory_bytes": resources.get("cpu_peak_memory_bytes"),
        "gpu_peak_memory_bytes": resources.get("gpu_peak_memory_bytes"),
        "free_vram_bytes": resources.get("free_vram_bytes"),
        "total_vram_bytes": resources.get("total_vram_bytes"),
        "gpu_samples": resources.get("gpu_samples", 0),
        "contention": resources.get("contention"),
        "resource_notes": {
            "cpu_peak_memory_bytes": "süreç RSS/working set tepe değeri (model yükleme dahil)",
            "gpu_peak_memory_bytes": "aygıt geneli VRAM tepe değeri; diğer süreçler dahil",
            "free_vram_bytes": "örneklenen en düşük boş VRAM",
        },
    }


async def live_report(config_path: Path, duration: float) -> dict[str, Any]:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration sonlu ve pozitif olmalı")
    settings = load_settings(config_path)
    if settings.allow_paid:
        raise ValueError("Canlı ses ölçümü allow_paid: false ister.")
    from artemis.channels.cli import request_approval

    collector = EventCollector()
    runtime: Runtime | None = None
    controller: VoiceController | None = None
    approval_active = False
    timer: asyncio.TimerHandle | None = None
    previous: Any = None
    signal_installed = False
    states: set[str] = set()
    failures: list[dict[str, str]] = []
    loop = asyncio.get_running_loop()
    phase = "runtime_setup"
    gpu = GpuSampler()
    sampling_stopped = asyncio.Event()
    sampler: asyncio.Task[None] | None = None

    def on_state(state: str) -> None:
        # heard/level durumları kişisel içerik taşır; rapora yalnız sabit durumlar girer.
        if state.startswith("denoise:unavailable:"):
            states.add("denoise:unavailable")
        elif state in {"denoise:off", "echo:off"}:
            states.add(state)

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        nonlocal approval_active
        assert runtime is not None
        approval_active = True
        try:
            if controller is not None:
                controller.pause_for_approval()
            # Terminal onay istemi de JSON stdout'unu bozmamalı.
            with redirect_stdout(sys.stderr):
                return request_approval(
                    name, arguments, runtime.gate.pending_notice,
                    on_interrupt=runtime.runner.cancel_current,
                )
        finally:
            approval_active = False
            if controller is not None:
                controller.resume_after_approval()

    def stop(_signum: int, _frame: Any) -> None:
        if approval_active:
            raise KeyboardInterrupt
        if runtime is not None:
            runtime.runner.cancel_current()
        if controller is not None:
            loop.call_soon_threadsafe(controller.request_stop)

    try:
        runtime = create_runtime(config_path, approve, settings=settings)
        phase = "voice_setup"
        controller = await build_voice_controller(
            runtime, runtime.runner.run, on_state, on_event=collector,
        )
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, stop)
        signal_installed = True
        timer = loop.call_later(duration, controller.request_stop)
        sampler = asyncio.create_task(gpu.run(os.getpid(), sampling_stopped))
        phase = "capture"
        print(f"Canlı ölçüm başladı ({duration:g} sn). 'artemis' deyin.", file=sys.stderr)
        await controller.run()
    except (OSError, RuntimeError, ValueError, ImportError) as error:
        failures.append({"phase": phase, "error_type": type(error).__name__})
    finally:
        if timer is not None:
            timer.cancel()
        sampling_stopped.set()
        if sampler is not None:
            await sampler
        try:
            if signal_installed:
                signal.signal(signal.SIGINT, previous)
        except (OSError, RuntimeError, ValueError) as error:
            failures.append({"phase": "signal_restore", "error_type": type(error).__name__})
        try:
            if controller is not None:
                try:
                    await controller.close()
                except (OSError, RuntimeError, ValueError) as error:
                    failures.append({"phase": "voice_close", "error_type": type(error).__name__})
        finally:
            if runtime is not None:
                try:
                    runtime.shutdown()
                except (OSError, RuntimeError, ValueError) as error:
                    failures.append({
                        "phase": "runtime_shutdown", "error_type": type(error).__name__,
                    })
    report = _report(collector, "canli")
    # Yalnız gerçek sounddevice çıkışı bu bilgiyi taşır; sahte/başka sink için null kalır.
    sink_output = getattr(getattr(controller, "sink", None), "output_info", None)
    resources = {"cpu_peak_memory_bytes": _peak_memory(failures), **gpu.report()}
    report["hardware"] = _hardware(settings, states, sink_output, resources)
    report["session_failures"] = failures
    return report


def _peak_memory(failures: list[dict[str, str]]) -> int | None:
    try:
        return process_peak_memory_bytes()
    except (OSError, AttributeError, ImportError) as error:
        failures.append({"phase": "resource_read", "error_type": type(error).__name__})
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Ses gecikmesi ölçümü")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sahte", action="store_true")
    mode.add_argument("--canli", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--rapor", type=Path, help="İsteğe bağlı JSON rapor dosyası")
    args = parser.parse_args()
    if args.iterations < 1 or not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("iterations pozitif, duration sonlu ve pozitif olmalı")
    failed = False
    try:
        report = asyncio.run(
            fake_report(args.iterations) if args.sahte else live_report(args.config, args.duration)
        )
    except (OSError, RuntimeError, ValueError, ImportError) as error:
        report = _report(EventCollector(), "canli" if args.canli else "sahte")
        report["session_failures"] = [{"phase": "setup", "error_type": type(error).__name__}]
        failed = True
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.rapor is not None:
        try:
            args.rapor.parent.mkdir(parents=True, exist_ok=True)
            args.rapor.write_text(serialized + "\n", encoding="utf-8")
        except OSError as error:
            report.setdefault("session_failures", []).append({
                "phase": "report_write", "error_type": type(error).__name__,
            })
            serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
            failed = True
    print(serialized)
    if failed or report.get("session_failures"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
