"""Canlı bench ortak runtime/factory'ye bağlanır; test gerçek ses/ağ açmaz."""

import asyncio
import json
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from artemis.channels.voice.telemetry import VoiceTimingEvent as Event
from artemis.config.settings import Settings, VoiceConfig
from bench import voice_latency as bench
from bench.voice_resources import GpuSampler


@pytest.fixture(autouse=True)
def no_real_gpu_query(monkeypatch):
    # Kural 8: test gerçek makinede nvidia-smi bile çalıştırmaz; okunamayan GPU null kalır.
    monkeypatch.setattr(bench, "GpuSampler", lambda: GpuSampler(lambda _arguments: None))


@pytest.mark.parametrize("failure", [None, "init", "run", "signal"])
async def test_live_uses_shared_runtime_factory_and_restores_resources(
    monkeypatch, tmp_path, capsys, failure,
):
    calls, timers, handlers = [], [], []
    previous = object()
    settings = Settings(workspace=tmp_path, voice=VoiceConfig(
        input_device=7, tts_backend="ema", noise_suppression=True, echo_cancellation=True,
    ))
    monkeypatch.setattr(bench, "load_settings", lambda _: settings)
    approval = None

    async def run_command(text):
        calls.append(("command", text))

    runner = SimpleNamespace(run=run_command, cancel_current=lambda: calls.append("cancel"))
    runtime = SimpleNamespace(settings=settings, runner=runner,
                              gate=SimpleNamespace(pending_notice="notice"),
                              shutdown=lambda: calls.append("shutdown"))

    def create_runtime(path, approve, **kwargs):
        nonlocal approval
        assert path == tmp_path / "config.yaml" and kwargs["settings"] is settings
        approval = approve
        calls.append("runtime")
        return runtime

    class Controller:
        def pause_for_approval(self):
            calls.append("pause")

        def resume_after_approval(self):
            calls.append("resume")

        async def run(self):
            calls.append("run")
            assert approval("example_tool", {"value": "shown only in terminal"}) is False
            if failure == "run":
                raise RuntimeError("secret failed device")

        def request_stop(self):
            calls.append("stop")

        async def close(self):
            calls.append("close")

    async def build_voice_controller(received, command, state, *, on_event):
        assert received is runtime and command is run_command
        assert received.settings.voice.tts_backend == "ema"
        assert received.settings.voice.noise_suppression
        assert received.settings.voice.echo_cancellation
        calls.append("factory")
        state("heard:private transcript")
        state("level:secret")
        state("denoise:unavailable:model:secret local path")
        state("echo:off")
        on_event(Event(1, None, "speech_start", 1.))
        on_event(Event(1, None, "terminal", 2., outcome="rejected", reason="no_wake"))
        if failure == "init":
            raise RuntimeError("secret model path")
        return Controller()

    def install_signal(signum, handler):
        assert signum == signal.SIGINT
        if failure == "signal":
            raise ValueError("secret thread information")
        handlers.append(handler)

    def request_approval(*args, **kwargs):
        assert args == ("example_tool", {"value": "shown only in terminal"}, "notice")
        assert kwargs["on_interrupt"] is runner.cancel_current
        print("private approval prompt")
        return False

    from artemis.channels import cli

    monkeypatch.setattr(cli, "request_approval", request_approval)
    monkeypatch.setattr(bench, "create_runtime", create_runtime)
    monkeypatch.setattr(bench, "build_voice_controller", build_voice_controller)
    monkeypatch.setattr(bench.signal, "getsignal", lambda _: previous)
    monkeypatch.setattr(bench.signal, "signal", install_signal)
    loop = asyncio.get_running_loop()
    original_call_later = loop.call_later

    def call_later(delay, callback, *args, **kwargs):
        handle = original_call_later(delay, callback, *args, **kwargs)
        timers.append(handle)
        return handle

    monkeypatch.setattr(loop, "call_later", call_later)
    report = await bench.live_report(tmp_path / "config.yaml", 60)
    assert calls[:2] == ["runtime", "factory"] and calls[-1] == "shutdown"
    assert ("close" in calls) == (failure != "init")
    if failure in {None, "run"}:
        assert handlers[-1] is previous and len(handlers) == 2
        assert "pause" in calls and "resume" in calls
        assert timers and all(timer.cancelled() for timer in timers)
    else:
        assert not handlers and not timers
    assert bool(report["session_failures"]) == bool(failure)
    hardware = report["hardware"]
    assert hardware["requested_input_device"] == 7 and hardware["input_device"] is None
    assert hardware["noise_suppression"]["actual"] is False
    assert hardware["echo_cancellation"]["actual"] is False
    assert hardware["gpu_samples"] == 0 and hardware["gpu_peak_memory_bytes"] is None
    assert hardware["output_latency_seconds"] is None  # Sahte controller'ın sink'i yok.
    serialized = json.dumps(report)
    assert "private transcript" not in serialized and "secret" not in serialized
    captured = capsys.readouterr()
    assert not captured.out
    if failure in {None, "run"}:
        assert "private approval prompt" in captured.err


async def test_live_cancel_restores_signal_and_shuts_down(monkeypatch, tmp_path):
    started = asyncio.Event()
    calls = []
    settings = Settings(workspace=tmp_path)
    runner = SimpleNamespace(run=lambda _: None, cancel_current=lambda: False)
    runtime = SimpleNamespace(runner=runner, shutdown=lambda: calls.append("shutdown"))
    monkeypatch.setattr(bench, "load_settings", lambda _: settings)
    monkeypatch.setattr(bench, "create_runtime", lambda *a, **k: runtime)

    class Controller:
        async def run(self):
            started.set()
            await asyncio.Event().wait()

        async def close(self):
            calls.append("close")

        def request_stop(self):
            pass

    async def build(*args, **kwargs):
        return Controller()

    monkeypatch.setattr(bench, "build_voice_controller", build)
    monkeypatch.setattr(bench.signal, "getsignal", lambda _: "previous")
    monkeypatch.setattr(bench.signal, "signal", lambda _, value: calls.append(value))
    task = asyncio.create_task(bench.live_report(tmp_path / "config.yaml", 60))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls[-3:] == ["previous", "close", "shutdown"]


async def test_paid_settings_are_rejected_before_runtime_or_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, "load_settings", lambda _: Settings(allow_paid=True))
    monkeypatch.setattr(bench, "create_runtime", lambda *a, **k: pytest.fail("paid runtime"))
    monkeypatch.setattr(bench, "build_voice_controller", lambda *a, **k: pytest.fail("audio"))
    with pytest.raises(ValueError, match="allow_paid"):
        await bench.live_report(tmp_path / "config.yaml", 1)


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), 0., -1.])
async def test_invalid_duration_never_loads_runtime(monkeypatch, duration):
    monkeypatch.setattr(bench, "load_settings", lambda _: pytest.fail("config accessed"))
    with pytest.raises(ValueError):
        await bench.live_report(Path("unused"), duration)


async def test_fake_report_finishes_each_response_without_hardware():
    report = await bench.fake_report(3)
    assert report["samples"] == report["outcomes"]["completed"] == 3
    assert report["total_utterances"] == report["requested_iterations"] == 3
    assert not report["session_failures"] and not report["outcomes"]["cancelled"]
    assert report["speech_end_to_first_pcm_ms"]["samples"] == 3
    assert report["hardware"] is None and "gerçek gecikme" in report["mode"]


def test_cli_json_stdout_and_nested_artifact(monkeypatch, tmp_path, capsys):
    path = tmp_path / "reports" / "latency.json"
    monkeypatch.setattr("sys.argv", ["voice_latency", "--sahte", "--iterations", "1",
                                     "--rapor", str(path)])
    bench.main()
    report = json.loads(capsys.readouterr().out)
    assert report == json.loads(path.read_text(encoding="utf-8"))
    assert report["samples"] == 1


def test_artifact_write_failure_is_sanitized_json(monkeypatch, tmp_path, capsys):
    parent_file = tmp_path / "file"
    parent_file.write_text("existing", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["voice_latency", "--sahte", "--iterations", "1",
                                     "--rapor", str(parent_file / "latency.json")])
    with pytest.raises(SystemExit) as error:
        bench.main()
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["session_failures"][-1]["phase"] == "report_write"
    assert str(parent_file) not in json.dumps(report)
