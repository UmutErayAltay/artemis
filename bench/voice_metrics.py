"""İçeriksiz ses olaylarından eksikleri de gösteren gecikme raporu üretir."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from statistics import median
from typing import Any

from artemis.channels.voice.telemetry import VoiceTimingEvent

STAGES = (
    "speech_start", "speech_end", "endpoint", "stt_final", "command_start",
    "model_complete", "tts_first_chunk", "first_pcm", "terminal",
    "stop_requested", "playback_stopped",
)
OUTCOMES = ("completed", "rejected", "cancelled", "error", "no_audio", "ack", "unfinished")
REASONS = {
    "max_utterance", "empty_transcript", "echo", "output_requires_wake", "no_wake",
    "stt_error", "stt_cancelled", "waiting_previous", "approval", "capture_stopped",
}
PAIRS = {
    "endpoint_wait_ms": ("speech_end", "endpoint"),
    "stt_ms": ("endpoint", "stt_final"),
    "dispatch_wait_ms": ("stt_final", "command_start"),
    "command_runner_ms": ("command_start", "model_complete"),
    "command_to_tts_first_chunk_ms": ("command_start", "tts_first_chunk"),
    "tts_chunk_to_first_pcm_ms": ("tts_first_chunk", "first_pcm"),
    "speech_end_to_first_pcm_ms": ("speech_end", "first_pcm"),
    "stop_to_playback_stopped_ms": ("stop_requested", "playback_stopped"),
    "speech_start_to_playback_stopped_ms": ("speech_start", "playback_stopped"),
}


class EventCollector:
    """Sınırlı bellek; sınır aşımı ve kaybolan sözler görünür kalır."""

    def __init__(self, limit: int = 100_000) -> None:
        self.limit = limit
        self.events: list[VoiceTimingEvent] = []
        self.dropped_events = 0
        self.overflow_utterances = 0

    def __call__(self, event: VoiceTimingEvent) -> None:
        if len(self.events) < self.limit:
            self.events.append(event)
        else:
            self.dropped_events += 1
            if event.stage == "speech_start":
                self.overflow_utterances += 1


def _distribution(values: list[float], eligible: int) -> dict[str, Any]:
    ordered = sorted(values)
    def percentile(fraction: float) -> float | None:
        if not ordered:
            return None
        return ordered[round((len(ordered) - 1) * fraction)]
    return {"p50": median(ordered) if ordered else None, "p90": percentile(0.9),
            "samples": len(values), "eligible_samples": eligible,
            "excluded_samples": eligible - len(values)}


def timing_report(events: Iterable[VoiceTimingEvent]) -> dict[str, Any]:
    """Kısmi nedensel sıra: streamingde PCM, model_complete'ten önce gelebilir."""
    grouped: dict[int, list[VoiceTimingEvent]] = {}
    for event in events:
        grouped.setdefault(event.utterance_id, []).append(event)
    samples: list[dict[str, Any]] = []
    for uid, items in grouped.items():
        times: dict[str, float] = {}
        invalid = False
        generations = {item.generation_id for item in items if item.generation_id is not None}
        terminal = [item for item in items if item.stage == "terminal"]
        outcome = terminal[-1].outcome if terminal else "unfinished"
        ack = any(item.response_kind == "ack" for item in items)
        invalid |= len(generations) > 1 or len(terminal) > 1 or outcome not in OUTCOMES
        for item in items:
            stamp = item.timestamp
            if (item.stage not in STAGES or item.stage in times
                    or isinstance(stamp, bool) or not isinstance(stamp, int | float)
                    or not math.isfinite(stamp) or stamp < 0):
                invalid = True
                continue
            times[item.stage] = stamp
        causal = [
            ("speech_start", "speech_end"), ("speech_end", "endpoint"),
            ("endpoint", "stt_final"), ("stt_final", "command_start"),
            ("command_start", "model_complete"), ("stt_final", "tts_first_chunk"),
            ("command_start", "tts_first_chunk"), ("tts_first_chunk", "first_pcm"),
            ("stop_requested", "playback_stopped"),
        ]
        invalid |= any(a in times and b in times and times[a] > times[b] for a, b in causal)
        if "terminal" in times:
            invalid |= any(stamp > times["terminal"] for stamp in times.values())
        required = {"speech_start"}
        if outcome in {"completed", "ack"}:
            required |= {"speech_end", "endpoint", "stt_final", "tts_first_chunk",
                         "first_pcm", "terminal"}
            if not ack:
                required |= {"command_start", "model_complete"}
        invalid |= not required.issubset(times)
        reason = terminal[-1].reason if terminal else None
        samples.append({
            "utterance_id": uid, "generation_id": next(iter(generations), None),
            "response_kind": "ack" if ack else "task", "command_class": "unknown",
            "outcome": outcome if outcome in OUTCOMES else "error",
            "reason": reason if reason in REASONS else None,
            "invalid_timing": invalid, "timings": times,
        })
    counts = Counter(sample["outcome"] for sample in samples)
    metrics = {}
    # Reddedilen, yarım veya hatalı bir sözün mevcut aşaması da ayrı paydada görünür.
    tasks = [sample for sample in samples if sample["response_kind"] == "task"]
    for name, (start, end) in PAIRS.items():
        values = [
            (sample["timings"][end] - sample["timings"][start]) * 1000
            for sample in tasks if not sample["invalid_timing"]
            and start in sample["timings"] and end in sample["timings"]
        ]
        metrics[name] = _distribution(values, len(tasks))
    valid_completed = sum(sample["outcome"] == "completed" and not sample["invalid_timing"]
                          and sample["response_kind"] == "task" for sample in samples)
    latency = metrics["speech_end_to_first_pcm_ms"]
    return {
        "schema_version": 1, "total_utterances": len(samples), "samples": valid_completed,
        "outcomes": {name: counts[name] for name in OUTCOMES},
        "aborted": counts["cancelled"] + counts["error"] + counts["unfinished"],
        "no_audio": counts["no_audio"],
        "invalid_timing": sum(sample["invalid_timing"] for sample in samples),
        "speech_end_to_first_pcm_ms": {"median": latency["p50"], **latency},
        "stages": metrics, "utterances": samples,
        "false_wake": None, "missed_wake": None, "false_approval": None,
        "barge_in_latency_ms": metrics["stop_to_playback_stopped_ms"]["p90"],
        "metric_notes": {
            "first_pcm": "player write callback; fiziksel duyulabilirlik doğrulanmadı",
            "completed": "ses oynatma tamamlandı; araç/model başarı iddiası değildir",
            "model_complete": "CommandRunner tamamı; saf LLM süresi değildir",
            "ids": "controller yerel utterance/generation; AgentLoop turn_id değildir",
            "command_class": "ilk adımda sınıflandırılmadı; sohbet/araç hedefi değerlendirilmez",
            "cohort": "ack hariç; geçerli aşama çifti olan iptal/hata örnekleri de dahildir",
            "percentiles": "p50 medyan; p90 en yakın sıra: round((n-1)*0.9)",
        },
    }

