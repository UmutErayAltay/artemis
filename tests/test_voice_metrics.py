"""Rapor kimlikleri, nedensellik ve açık dışlama paydaları donanımsız sınanır."""

import json
import math

import pytest

from artemis.channels.voice.telemetry import VoiceTimingEvent as Event
from bench.voice_metrics import EventCollector, timing_report


def events(uid=1, *, ack=False, streaming=False, offset=0):
    stamps = {
        "speech_start": 1., "speech_end": 2., "endpoint": 2.5, "stt_final": 3.,
        "command_start": 3.1, "model_complete": 5. if streaming else 4.,
        "tts_first_chunk": 4.1, "first_pcm": 4.2, "terminal": 6.,
    }
    return [Event(uid, None if stage in {"speech_start", "speech_end", "endpoint", "stt_final"}
                  else uid, stage, stamp + offset, "ack" if ack else "task",
                  ("ack" if ack else "completed") if stage == "terminal" else None)
            for stage, stamp in stamps.items()
            if not ack or stage not in {"command_start", "model_complete"}]


def test_interleaved_rejection_does_not_steal_response_and_ack_is_excluded():
    first = events()
    rejected = [Event(2, None, "speech_start", 3.5),
                Event(2, None, "terminal", 3.8, outcome="rejected", reason="no_wake")]
    report = timing_report(first[:5] + rejected + first[5:] + events(3, ack=True, offset=10))
    assert report["samples"] == 1
    assert report["total_utterances"] == sum(report["outcomes"].values()) == 3
    assert report["outcomes"]["ack"] == report["outcomes"]["rejected"] == 1
    metric = report["speech_end_to_first_pcm_ms"]
    assert metric["p50"] == pytest.approx(2200)
    assert metric["samples"] == 1 and metric["eligible_samples"] == 2
    assert report["stages"]["endpoint_wait_ms"]["p50"] == 500
    assert report["barge_in_latency_ms"] is None


def test_first_pcm_may_precede_model_complete_without_claiming_streaming():
    report = timing_report(events(streaming=True))
    assert report["samples"] == 1
    assert report["invalid_timing"] == 0


@pytest.mark.parametrize("stamp", [float("nan"), float("inf"), -1., "private transcript"])
def test_invalid_timestamps_are_explicit_and_never_serialized(stamp):
    items = events()
    items[2] = Event(1, None, "endpoint", stamp)
    report = timing_report(items)
    assert report["samples"] == 0 and report["invalid_timing"] == 1
    assert report["outcomes"]["completed"] == 1
    assert report["speech_end_to_first_pcm_ms"]["samples"] == 0
    serialized = json.dumps(report, allow_nan=False)
    assert "private transcript" not in serialized


def test_incomplete_failed_cancelled_and_no_audio_samples_reconcile():
    items = [Event(1, None, "speech_start", 1.)]
    for uid, outcome in [(2, "error"), (3, "cancelled"), (4, "no_audio")]:
        items.extend([Event(uid, None, "speech_start", float(uid)),
                      Event(uid, None, "terminal", float(uid + 1), outcome=outcome)])
    report = timing_report(items)
    assert report["total_utterances"] == sum(report["outcomes"].values()) == 4
    assert report["outcomes"]["unfinished"] == 1
    assert report["aborted"] == 3 and report["no_audio"] == 1
    assert report["stages"]["stt_ms"]["samples"] == 0


def test_missing_completed_stage_and_backward_order_are_invalid():
    missing = [event for event in events() if event.stage != "endpoint"]
    backward = events(2)
    backward[3] = Event(2, None, "stt_final", 1.)
    report = timing_report(missing + backward)
    assert report["invalid_timing"] == 2
    assert report["samples"] == 0


def test_report_has_no_arbitrary_reason_text_and_uses_real_median():
    items = events() + events(2)
    items[-2] = Event(2, 2, "first_pcm", 5.2)
    report = timing_report(items)
    assert math.isclose(report["speech_end_to_first_pcm_ms"]["p50"], 2700)
    items += [Event(3, None, "speech_start", 10.),
              Event(3, None, "terminal", 11., outcome="error", reason="secret transcript")]
    assert "secret transcript" not in json.dumps(timing_report(items))


def test_bounded_collector_discloses_overflow():
    from bench.voice_latency import _report

    collector = EventCollector(limit=1)
    for event in events() + events(2):
        collector(event)
    report = _report(collector, "test")
    assert report["dropped_events"] == 17
    assert report["overflow_utterances"] == 1
    assert report["total_utterances"] == sum(report["outcomes"].values()) == 2
    assert not report["collection_complete"]
