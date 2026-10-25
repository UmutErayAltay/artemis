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


def _with_phases(uid, phases, *, command_class="chat", warm=True, offset=0.):
    base = events(uid, offset=offset)
    extra = [Event(uid, uid, stage, stamp + offset) for stage, stamp in phases]
    result = []
    for event in base:
        if event.stage == "command_start":
            event = Event(uid, uid, "command_start", event.timestamp, warm=warm)
        if event.stage == "model_complete":
            result.extend(extra)
            event = Event(uid, uid, "model_complete", event.timestamp,
                          command_class=command_class)
        result.append(event)
    return result


def test_repeated_model_and_tool_phases_are_summed_not_invalid():
    phases = [("model_request", 3.2), ("model_response", 3.4), ("tool_start", 3.45),
              ("tool_end", 3.6), ("model_request", 3.65), ("model_response", 3.9)]
    report = timing_report(_with_phases(1, phases, command_class="tool"))
    sample = report["utterances"][0]
    assert not sample["invalid_timing"]
    assert sample["model_calls"] == 2 and sample["tool_calls"] == 1
    assert sample["derived_ms"]["model_total_ms"] == pytest.approx(450)
    assert sample["derived_ms"]["tool_total_ms"] == pytest.approx(150)
    assert sample["derived_ms"]["model_first_response_ms"] == pytest.approx(300)
    assert report["by_command_class"]["tool"]["completed"] == 1
    assert report["by_command_class"]["chat"]["utterances"] == 0
    # Akışsız sağlayıcı: ilk token örneği yok, uydurulmaz.
    assert report["model_first_token_ms"]["samples"] == 0 and not sample["streamed"]
    assert report["streamed_utterances"] == 0


@pytest.mark.parametrize("phases", [
    [("model_response", 3.2), ("model_request", 3.4)],
    [("model_request", 3.2), ("model_request", 3.3), ("model_request", 3.4)],
    [("model_request", 3.0), ("model_response", 3.2)],
    [("model_request", 3.2), ("model_response", 4.5)],
])
def test_inverted_extra_or_out_of_turn_phases_are_invalid(phases):
    report = timing_report(_with_phases(1, phases))
    assert report["invalid_timing"] == 1 and report["samples"] == 0


def test_unfinished_last_model_call_is_allowed_for_cancelled_turn():
    items = [Event(1, None, "speech_start", 1.), Event(1, 1, "command_start", 2.),
             Event(1, 1, "model_request", 2.1),
             Event(1, 1, "terminal", 3., outcome="cancelled")]
    report = timing_report(items)
    assert report["invalid_timing"] == 0 and report["aborted"] == 1


def test_targets_need_minimum_samples_and_split_by_class():
    small = timing_report(_with_phases(1, [("model_request", 3.2), ("model_response", 3.3)]))
    assert small["targets"]["chat_speech_end_to_first_pcm_p50_lt_1500ms"]["met"] is None
    items = []
    for index in range(30):
        items += _with_phases(index + 1, [("model_request", 3.2), ("model_response", 3.3)],
                              offset=index * 10)
    # Araçlı turların gecikmesi sohbet hedefine karışmaz.
    for index in range(30, 40):
        slow = [event if event.stage != "first_pcm" else Event(
            event.utterance_id, event.generation_id, "first_pcm", event.timestamp + 0.5)
            for event in _with_phases(index + 1, [], command_class="tool", offset=index * 10)]
        items += [event for event in slow if event.stage != "terminal"] + [
            Event(index + 1, index + 1, "terminal", index * 10 + 7., outcome="completed")]
    report = timing_report(items)
    target = report["targets"]["chat_speech_end_to_first_pcm_p50_lt_1500ms"]
    assert target["samples"] == 30 and target["met"] is False
    assert target["value_ms"] == pytest.approx(2200)
    assert report["by_command_class"]["tool"]["stages"]["speech_end_to_first_pcm_ms"][
        "p50"] == pytest.approx(2700)
    assert report["targets"]["stop_to_playback_stopped_p90_lt_300ms"]["met"] is None
    assert report["by_profile"]["warm"]["utterances"] == 40


def test_fake_benchmark_never_claims_targets(monkeypatch):
    import asyncio

    from artemis.channels.voice import metrics
    from bench.voice_latency import fake_report

    # Örnek eşiği tek başına korumasın: sahte kip yeterli örnekte de hedef iddia etmemeli.
    monkeypatch.setattr(metrics, "MIN_TARGET_SAMPLES", 1)
    report = asyncio.run(fake_report(2))
    assert all(target["met"] is None and target["not_evaluated"] == "sahte"
               for target in report["targets"].values())
    assert report["by_command_class"]["chat"]["completed"] == 1
    assert report["by_command_class"]["tool"]["completed"] == 1
