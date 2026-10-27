"""İçeriksiz ses olaylarından eksikleri de gösteren gecikme raporu üretir.

Rapor bench ve masaüstü dışa aktarımı için ortaktır (ADR 0041); ayrı bir rapor zinciri
kurulursa iki çıktı zamanla farklı şeyi ölçmeye başlar.
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from artemis.channels.voice.telemetry import VoiceTimingEvent

STAGES = (
    "stt_queued",
    "stt_start",
    "speech_start",
    "speech_end",
    "endpoint",
    "stt_final",
    "command_start",
    "model_request",
    "model_first_token",
    "model_response",
    "tool_start",
    "tool_end",
    "model_complete",
    "tts_first_chunk",
    "first_pcm",
    "terminal",
    "stop_requested",
    "playback_stopped",
)
# Çok adımlı turda model ve araç birden çok kez çağrılır; bu aşamalar tekrar edebilir.
REPEATABLE = {"model_request", "model_first_token", "model_response", "tool_start", "tool_end"}
OUTCOMES = (
    "completed",
    "rejected",
    "cancelled",
    "error",
    "no_audio",
    "ack",
    "interrupted",
    "unfinished",
)
REASONS = {
    "max_utterance",
    "empty_transcript",
    "echo",
    "output_requires_wake",
    "no_wake",
    "stt_error",
    "stt_cancelled",
    "waiting_previous",
    "approval",
    "capture_stopped",
    "barge_in",
    "stt_queue_full",
    "capture_invalidated",
}
COMMAND_CLASSES = ("chat", "tool")
PROFILES = ("cold", "warm")
PAIRS = {
    "endpoint_wait_ms": ("speech_end", "endpoint"),
    "stt_ms": ("endpoint", "stt_final"),
    "stt_queue_wait_ms": ("stt_queued", "stt_start"),
    "stt_decode_ms": ("stt_start", "stt_final"),
    "dispatch_wait_ms": ("stt_final", "command_start"),
    "command_runner_ms": ("command_start", "model_complete"),
    "command_to_tts_first_chunk_ms": ("command_start", "tts_first_chunk"),
    "tts_chunk_to_first_pcm_ms": ("tts_first_chunk", "first_pcm"),
    "speech_end_to_first_pcm_ms": ("speech_end", "first_pcm"),
    "stop_to_playback_stopped_ms": ("stop_requested", "playback_stopped"),
    "speech_start_to_playback_stopped_ms": ("speech_start", "playback_stopped"),
}
# Aşama listelerinden türeyen metrikler; çiftler değil örnek başına hesaplanır.
DERIVED = (
    "model_first_token_ms",
    "model_first_response_ms",
    "model_total_ms",
    "tool_total_ms",
)
GROUP_METRICS = (
    "speech_end_to_first_pcm_ms",
    "command_runner_ms",
    "model_first_token_ms",
    "model_first_response_ms",
    "model_total_ms",
    "tool_total_ms",
)
# Plan hedefleri; ilk ölçüm ve yeterli örnek olmadan "tuttu" denmez.
MIN_TARGET_SAMPLES = 30
TARGETS = {
    "chat_speech_end_to_first_pcm_p50_lt_1500ms": (
        "chat",
        "speech_end_to_first_pcm_ms",
        "p50",
        1500.0,
    ),
    "chat_speech_end_to_first_pcm_p90_lt_3000ms": (
        "chat",
        "speech_end_to_first_pcm_ms",
        "p90",
        3000.0,
    ),
    "stop_to_playback_stopped_p90_lt_300ms": (
        None,
        "stop_to_playback_stopped_ms",
        "p90",
        300.0,
    ),
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

    return {
        "p50": median(ordered) if ordered else None,
        "p90": percentile(0.9),
        "samples": len(values),
        "eligible_samples": eligible,
        "excluded_samples": eligible - len(values),
    }


def _valid_stamp(stamp: object) -> bool:
    return (
        not isinstance(stamp, bool)
        and isinstance(stamp, int | float)
        and math.isfinite(stamp)
        and stamp >= 0
    )


def _spans(starts: list[float], ends: list[float]) -> tuple[list[tuple[float, float]], bool]:
    """Başlangıç/bitiş listelerini sırayla eşler; yarım kalan son çağrıya izin verilir.

    İptal edilen turda son model/araç çağrısı bitmeden tur kapanabilir; ondan fazla
    eşleşmeyen uç ya da geriye giden aralık geçersizdir.
    """
    invalid = len(ends) > len(starts) or len(starts) - len(ends) > 1
    spans = list(zip(sorted(starts), sorted(ends), strict=False))
    ordered = sorted(starts + ends)
    expected = [stamp for pair in spans for stamp in pair] + sorted(starts)[len(spans) :]
    invalid |= ordered != expected
    return spans, invalid


def _sample(uid: int, items: list[VoiceTimingEvent]) -> dict[str, Any]:
    times: dict[str, float] = {}
    phases: dict[str, list[float]] = {stage: [] for stage in REPEATABLE}
    invalid = False
    generations = {item.generation_id for item in items if item.generation_id is not None}
    terminal = [item for item in items if item.stage == "terminal"]
    outcome = terminal[-1].outcome if terminal else "unfinished"
    ack = any(item.response_kind == "ack" for item in items)
    invalid |= len(generations) > 1 or len(terminal) > 1 or outcome not in OUTCOMES
    command_class = "unknown"
    profile = "unknown"
    for item in items:
        stamp = item.timestamp
        if item.stage not in STAGES or not _valid_stamp(stamp):
            invalid = True
            continue
        if item.stage in REPEATABLE:
            phases[item.stage].append(stamp)
            continue
        if item.stage in times:
            invalid = True
            continue
        times[item.stage] = stamp
        if item.stage == "model_complete" and item.command_class in COMMAND_CLASSES:
            command_class = item.command_class
        if item.stage == "command_start" and item.warm is not None:
            profile = "warm" if item.warm else "cold"
    causal = [
        ("speech_start", "speech_end"),
        ("speech_end", "endpoint"),
        ("endpoint", "stt_final"),
        ("stt_final", "command_start"),
        ("command_start", "model_complete"),
        ("stt_final", "tts_first_chunk"),
        ("command_start", "tts_first_chunk"),
        ("tts_first_chunk", "first_pcm"),
        ("stop_requested", "playback_stopped"),
    ]
    invalid |= any(a in times and b in times and times[a] > times[b] for a, b in causal)
    if "terminal" in times:
        invalid |= any(stamp > times["terminal"] for stamp in times.values())
    model_spans, bad_model = _spans(phases["model_request"], phases["model_response"])
    tool_spans, bad_tool = _spans(phases["tool_start"], phases["tool_end"])
    invalid |= bad_model or bad_tool
    phase_stamps = [stamp for values in phases.values() for stamp in values]
    # Model/araç aşamaları runner turunun içinde kalmalıdır; saat uyuşmazlığı burada görünür.
    if "command_start" in times:
        invalid |= any(stamp < times["command_start"] for stamp in phase_stamps)
    if "model_complete" in times:
        invalid |= any(stamp > times["model_complete"] for stamp in phase_stamps)
    required = {"speech_start"}
    if outcome in {"completed", "ack"}:
        required |= {
            "speech_end",
            "endpoint",
            "stt_final",
            "tts_first_chunk",
            "first_pcm",
            "terminal",
        }
        if not ack:
            required |= {"command_start", "model_complete"}
    invalid |= not required.issubset(times)
    derived: dict[str, float] = {}
    if phases["model_first_token"] and "command_start" in times:
        derived["model_first_token_ms"] = (
            min(phases["model_first_token"]) - times["command_start"]
        ) * 1000
    if model_spans and "command_start" in times:
        derived["model_first_response_ms"] = (model_spans[0][1] - times["command_start"]) * 1000
    if model_spans:
        derived["model_total_ms"] = sum(end - start for start, end in model_spans) * 1000
    if tool_spans:
        derived["tool_total_ms"] = sum(end - start for start, end in tool_spans) * 1000
    reason = terminal[-1].reason if terminal else None
    return {
        "utterance_id": uid,
        "generation_id": next(iter(generations), None),
        "response_kind": "ack" if ack else "task",
        "command_class": command_class,
        "profile": profile,
        "outcome": outcome if outcome in OUTCOMES else "error",
        "reason": reason if reason in REASONS else None,
        "invalid_timing": invalid,
        "timings": times,
        "model_calls": len(phases["model_request"]),
        "tool_calls": len(phases["tool_start"]),
        "streamed": bool(phases["model_first_token"]),
        "derived_ms": derived,
    }


def _value(sample: dict[str, Any], name: str) -> float | None:
    if name in DERIVED:
        value: float | None = sample["derived_ms"].get(name)
        return value
    start, end = PAIRS[name]
    timings = sample["timings"]
    if start in timings and end in timings:
        return float((timings[end] - timings[start]) * 1000)
    return None


def _metrics(samples: list[dict[str, Any]], names: Iterable[str]) -> dict[str, Any]:
    result = {}
    for name in names:
        values = [
            value
            for sample in samples
            if not sample["invalid_timing"] and (value := _value(sample, name)) is not None
        ]
        result[name] = _distribution(values, len(samples))
    return result


def _group(samples: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(sample["outcome"] for sample in samples)
    return {
        "utterances": len(samples),
        "completed": sum(
            sample["outcome"] == "completed" and not sample["invalid_timing"] for sample in samples
        ),
        "aborted": counts["cancelled"] + counts["error"] + counts["unfinished"],
        "stages": _metrics(samples, GROUP_METRICS),
    }


def _targets(
    metrics: dict[str, Any],
    by_class: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    targets = {}
    for name, (command_class, metric, statistic, limit) in TARGETS.items():
        source = metrics if command_class is None else by_class[command_class]["stages"]
        distribution = source[metric]
        value = distribution[statistic]
        samples = distribution["samples"]
        met: bool | None = None
        if value is not None and samples >= MIN_TARGET_SAMPLES:
            met = value < limit
        targets[name] = {
            "met": met,
            "value_ms": value,
            "limit_ms": limit,
            "samples": samples,
            "min_samples": MIN_TARGET_SAMPLES,
        }
    return targets


def timing_report(events: Iterable[VoiceTimingEvent]) -> dict[str, Any]:
    """Kısmi nedensel sıra: streamingde PCM, model_complete'ten önce gelebilir."""
    grouped: dict[int, list[VoiceTimingEvent]] = {}
    capture_health = []
    for event in events:
        if event.stage == "capture_health":
            capture_health.append({"timestamp": event.timestamp, "detail": event.reason})
            continue
        grouped.setdefault(event.utterance_id, []).append(event)
    samples = [_sample(uid, items) for uid, items in grouped.items()]
    counts = Counter(sample["outcome"] for sample in samples)
    # Reddedilen, yarım veya hatalı bir sözün mevcut aşaması da ayrı paydada görünür.
    tasks = [sample for sample in samples if sample["response_kind"] == "task"]
    metrics = _metrics(tasks, (*PAIRS, *DERIVED))
    by_class = {
        name: _group([sample for sample in tasks if sample["command_class"] == name])
        for name in (*COMMAND_CLASSES, "unknown")
    }
    by_profile = {
        name: _group([sample for sample in tasks if sample["profile"] == name])
        for name in (*PROFILES, "unknown")
    }
    valid_completed = sum(
        sample["outcome"] == "completed"
        and not sample["invalid_timing"]
        and sample["response_kind"] == "task"
        for sample in samples
    )
    latency = metrics["speech_end_to_first_pcm_ms"]
    return {
        "capture_health": capture_health,
        "schema_version": 2,
        "total_utterances": len(samples),
        "samples": valid_completed,
        "outcomes": {name: counts[name] for name in OUTCOMES},
        "aborted": counts["cancelled"] + counts["error"] + counts["unfinished"],
        "no_audio": counts["no_audio"],
        "invalid_timing": sum(sample["invalid_timing"] for sample in samples),
        "speech_end_to_first_pcm_ms": {"median": latency["p50"], **latency},
        "stages": metrics,
        "by_command_class": by_class,
        "by_profile": by_profile,
        "targets": _targets(metrics, by_class),
        "model_first_token_ms": metrics["model_first_token_ms"],
        "streamed_utterances": sum(sample["streamed"] for sample in tasks),
        "utterances": samples,
        "false_wake": None,
        "missed_wake": None,
        "false_approval": None,
        "barge_in_latency_ms": metrics["stop_to_playback_stopped_ms"]["p90"],
        "metric_notes": {
            "first_pcm": "player write callback; fiziksel duyulabilirlik doğrulanmadı",
            "completed": "ses oynatma tamamlandı; araç/model başarı iddiası değildir",
            "model_complete": "CommandRunner tamamı; saf LLM süresi değildir",
            "model_first_response_ms": "komut başlangıcından ilk tam model yanıtına",
            "model_first_token_ms": "akan ilk metin parçası; akışsız sağlayıcıda örnek yok",
            "tool_total_ms": "SafetyGate.dispatch süresi; onay bekleme süresi dahildir",
            "playback_stopped": "sink yazımının bittiği an; aygıt tamponu ölçülmez",
            "barge_in": "kesen sözün kimliğinde; speech_start'tan ölçüm algılamayı da içerir",
            "ids": "controller yerel utterance/generation; AgentLoop turn_id değildir",
            "command_class": "araç çağrısı varsa tool, yoksa chat; runner'a ulaşmayan unknown",
            "profile": "controller ömrünün ilk görev turu cold, sonrakiler warm",
            "targets": f"en az {MIN_TARGET_SAMPLES} geçerli örnek yoksa met null",
            "cohort": "ack hariç; geçerli aşama çifti olan iptal/hata örnekleri de dahildir",
            "percentiles": "p50 medyan; p90 en yakın sıra: round((n-1)*0.9)",
        },
    }


def collector_report(collector: EventCollector, mode: str) -> dict[str, Any]:
    """Toplayıcının taşma bilgisini rapora ekler; taşan sözler toplamdan düşmez."""
    report = timing_report(collector.events)
    report.update(
        {
            "mode": mode,
            "dropped_events": collector.dropped_events,
            "overflow_utterances": collector.overflow_utterances,
            "collection_complete": collector.dropped_events == 0,
        }
    )
    report["total_utterances"] += collector.overflow_utterances
    report["outcomes"]["overflow"] = collector.overflow_utterances
    return report


def write_report(report: dict[str, Any], directory: Path) -> Path:
    """Yeni dosyaya yazar (``"x"``); var olan rapor asla ezilmez."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"voice-{stamp}-{os.getpid()}.json"
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(serialized + "\n")
    return path
