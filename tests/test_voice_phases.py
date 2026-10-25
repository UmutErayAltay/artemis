"""Model/araç aşamaları, komut sınıfı, soğuk/ısınmış profil ve söz kesme stop çifti (ADR 0041)."""

import asyncio
import json
from time import monotonic

from artemis.brain.agent_loop import AgentLoop, PhaseTiming, RunResult
from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import Completion, ToolCall
from artemis.channels.voice.core import AudioSegment, PCMChunk, VoiceController
from artemis.channels.voice.metrics import timing_report
from artemis.config.settings import Settings, VoiceConfig
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate
from artemis.tools.base import ToolContext
from artemis.tools.registry import ToolRegistry
from bench.voice_latency import BenchClockTool
from tests.test_voice_core import FakeMic, FakeRecognizer, FakeSink, FakeTTS, FakeVAD


def _loop(tmp_path, responses):
    registry = ToolRegistry()
    registry.register(BenchClockTool)
    context = ToolContext(
        workspace=tmp_path, settings=Settings(), audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    return AgentLoop(ScriptedProvider(responses), SafetyGate(registry), context, lambda *_: False)


async def test_chat_turn_records_one_model_span(tmp_path):
    result = await _loop(tmp_path, [Completion(content="Merhaba")]).run("selam")
    assert [phase.stage for phase in result.phases] == ["model_request", "model_response"]
    assert result.phases[0].timestamp <= result.phases[1].timestamp


async def test_tool_turn_records_model_and_tool_spans_in_order(tmp_path):
    result = await _loop(tmp_path, [
        Completion(tool_calls=[ToolCall(id="1", name="bench_clock")]),
        Completion(content="Saat 10."),
    ]).run("saat kaç")
    assert result.tool_calls == 1
    assert [phase.stage for phase in result.phases] == [
        "model_request", "model_response", "tool_start", "tool_end",
        "model_request", "model_response",
    ]
    stamps = [phase.timestamp for phase in result.phases]
    assert stamps == sorted(stamps)


async def test_phases_stay_out_of_turn_end_audit(tmp_path):
    await _loop(tmp_path, [Completion(content="Merhaba")]).run("selam")
    rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    end = next(row for row in rows if row["event"] == "turn_end")
    assert "phases" not in end


class ScriptedRunner:
    """Gerçek AgentLoop'a gerek duymadan aşama/sınıf aktarımını sınar."""

    def __init__(self, results):
        self.results = list(results)
        self.cancelled = 0

    def cancel_current(self):
        self.cancelled += 1
        return True

    async def run(self, _text):
        start = monotonic()
        result = self.results.pop(0)
        if result.turn_id:
            # Gerçek AgentLoop gibi yalnız saat okunur; yapay ofset Windows'un ~15 ms'lik
            # monotonic adımında model_complete damgasını geçip örneği geçersiz kılıyordu.
            stamps = [start] * 4
            stages = ["model_request", "model_response", "tool_start", "tool_end"]
            phases = [PhaseTiming(stage=stage, timestamp=stamp)
                      for stage, stamp in zip(stages, stamps, strict=True)]
            if not result.tool_calls:
                phases = phases[:2]
            result = result.model_copy(update={"phases": phases})
        return result


def _result(tool_calls=0, turn_id="t"):
    return RunResult(text="yanıt", stop_reason="completed", turn_id=turn_id,
                     tool_calls=tool_calls)


async def test_controller_forwards_phases_class_and_warm_profile():
    events = []
    runner = ScriptedRunner([_result(), _result(tool_calls=1), _result(turn_id="")])
    controller = VoiceController(
        runner, VoiceConfig(), FakeMic(), FakeVAD([]),
        FakeRecognizer(["artemis selam", "artemis saat kaç", "artemis meşgul mü"]),
        FakeTTS(), FakeSink(), on_event=events.append,
    )
    try:
        for _ in range(3):
            await controller._handle_utterance(AudioSegment(b"a", monotonic(), monotonic()))
            await controller._settle_active()
    finally:
        await controller.close()
    starts = [event for event in events if event.stage == "command_start"]
    assert [event.warm for event in starts] == [False, True, True]
    completes = [event for event in events if event.stage == "model_complete"]
    # Runner'a ulaşmayan (turn_id boş: meşgul/iptal) tur sınıflandırılmaz.
    assert [event.command_class for event in completes] == ["chat", "tool", None]
    report = timing_report(events)
    assert report["invalid_timing"] == 0
    classes = [sample["command_class"] for sample in report["utterances"]]
    assert classes == ["chat", "tool", "unknown"]
    assert [sample["profile"] for sample in report["utterances"]] == ["cold", "warm", "warm"]
    assert report["utterances"][1]["tool_calls"] == 1
    assert report["by_command_class"]["tool"]["stages"]["tool_total_ms"]["samples"] == 1
    assert report["by_command_class"]["chat"]["completed"] == 1


async def test_barge_in_records_stop_pair_on_interrupting_utterance():
    playing = asyncio.Event()

    class EndlessTTS(FakeTTS):
        async def stream(self, _text, cancelled):
            while not cancelled.is_set():
                yield PCMChunk(b"\x01\x00")
                playing.set()
                await asyncio.sleep(0.001)

    events = []
    runner = ScriptedRunner([_result(), _result()])
    controller = VoiceController(
        runner, VoiceConfig(), FakeMic(), FakeVAD([]),
        FakeRecognizer(["artemis uzun anlat", "artemis dur"]),
        EndlessTTS(), FakeSink(), on_event=events.append,
    )
    try:
        await controller._handle_utterance(AudioSegment(b"a", monotonic(), monotonic()))
        await asyncio.wait_for(playing.wait(), 1)
        await controller._handle_utterance(AudioSegment(b"b", monotonic(), monotonic()))
    finally:
        await controller.close()
    stops = [(event.utterance_id, event.stage, event.generation_id) for event in events
             if event.stage in {"stop_requested", "playback_stopped"}]
    assert stops == [(2, "stop_requested", None), (2, "playback_stopped", None)]
    first_terminal = next(event for event in events
                          if event.stage == "terminal" and event.utterance_id == 1)
    assert first_terminal.outcome == "cancelled"
    report = timing_report(events)
    assert report["stages"]["stop_to_playback_stopped_ms"]["samples"] == 1
    assert report["stages"]["speech_start_to_playback_stopped_ms"]["samples"] == 1
    assert report["barge_in_latency_ms"] is not None


async def test_command_during_thinking_has_no_stop_pair():
    started, release = asyncio.Event(), asyncio.Event()

    class SlowRunner(ScriptedRunner):
        async def run(self, text):
            started.set()
            await release.wait()
            return await super().run(text)

    events = []
    controller = VoiceController(
        SlowRunner([_result(), _result()]), VoiceConfig(), FakeMic(), FakeVAD([]),
        FakeRecognizer(["artemis bir", "artemis iki"]), FakeTTS(), FakeSink(),
        on_event=events.append,
    )
    try:
        await controller._handle_utterance(AudioSegment(b"a", monotonic(), monotonic()))
        await asyncio.wait_for(started.wait(), 1)
        release.set()
        await controller._handle_utterance(AudioSegment(b"b", monotonic(), monotonic()))
        await controller._settle_active()
    finally:
        await controller.close()
    # Hoparlöre yazım yokken stop çifti üretilmez; aksi halde düşünme iptali söz kesme sayılırdı.
    assert not any(event.stage in {"stop_requested", "playback_stopped"} for event in events)
