"""Opsiyonel ses adaptörleri sahte kitaplıklarla doğrulanır."""

import asyncio
import json
from types import ModuleType

import pytest

import jarvis.channels.voice.adapters as voice_adapters
from jarvis.channels.voice.adapters import (
    EdgeTTSynthesizer,
    FallbackSynthesizer,
    FasterWhisperRecognizer,
    PiperSynthesizer,
    SoundDeviceMicrophone,
    SoundDeviceSink,
)
from jarvis.channels.voice.core import AudioFrame, PCMChunk
from jarvis.config.settings import VoiceConfig
from jarvis.safety.audit import AuditLog


async def test_faster_whisper_uses_configured_final_and_partial_profiles(monkeypatch, tmp_path):
    class Samples:
        def astype(self, _dtype):
            return self

        def __truediv__(self, _value):
            return self

    class Numpy:
        int16 = object()
        float32 = object()

        @staticmethod
        def frombuffer(_pcm, dtype):
            assert dtype is Numpy.int16
            return Samples()

    options = []

    class Model:
        def transcribe(self, _samples, **kwargs):
            options.append(kwargs)
            return ([type("Segment", (), {"text": " test "})()], None)

    class WhisperModule:
        @staticmethod
        def WhisperModel(*_args, **_kwargs):
            return Model()

    original_import = voice_adapters.importlib.import_module

    def fake_import(name):
        if name == "numpy":
            return Numpy
        if name == "faster_whisper":
            return WhisperModule
        return original_import(name)

    monkeypatch.setattr("jarvis.channels.voice.adapters.importlib.import_module", fake_import)
    config = VoiceConfig(
        stt_model=tmp_path,
        stt_language="auto",
        stt_beam_size=6,
        stt_partial_beam_size=2,
        stt_hotwords=("artemis", "Visual Studio Code"),
    )
    recognizer = FasterWhisperRecognizer(config)
    assert await recognizer.transcribe(b"\x00\x00") == "test"
    assert await recognizer.transcribe_partial(b"\x00\x00") == "test"
    assert [call["beam_size"] for call in options] == [6, 2]
    assert all(call["language"] is None for call in options)
    assert all(call["task"] == "transcribe" and call["temperature"] == 0 for call in options)
    assert all(call["hotwords"] == "artemis Visual Studio Code" for call in options)
    assert "initial_prompt" not in options[0]
    assert all(call["vad_filter"] for call in options)
    assert all(call["condition_on_previous_text"] is False for call in options)


@pytest.mark.parametrize("text,ghost", [
    ("Abone olmayı unutmayın!", True),
    ("Bu videoyu izlediğiniz için teşekkür ederim.", True),
    ('Not defterine "Abone olmayı unutmayın" yaz.', False),
    ("Artemis open Visual Studio Code ve tarayıcıyı aç", False),
])
def test_ghost_filter_only_rejects_complete_boilerplate(text, ghost):
    assert voice_adapters.is_ghost_transcript(text) is ghost


async def test_whisper_rejects_uncertain_and_no_speech_segments(tmp_path):
    from types import SimpleNamespace

    class Model:
        def transcribe(self, _samples, **_kwargs):
            return [
                SimpleNamespace(text="wrong", avg_logprob=-2, no_speech_prob=0.1),
                SimpleNamespace(text="noise", avg_logprob=-0.1, no_speech_prob=0.9),
                SimpleNamespace(text="open Brave ve hesap makinesi", avg_logprob=-0.2,
                                no_speech_prob=0.01),
            ], None

    recognizer = FasterWhisperRecognizer(VoiceConfig(stt_model=tmp_path))
    recognizer._model = Model()
    assert await recognizer.transcribe(b"\x00\x00") == "open Brave ve hesap makinesi"


@pytest.mark.parametrize("detected,confidence,probabilities,expected", [
    ("ru", 0.95, [("ru", 0.95), ("tr", 0.02), ("en", 0.01)], "tr"),
    ("en", 0.5, [("en", 0.5), ("tr", 0.4)], "tr"),
    ("tr", 0.9, [("tr", 0.9), ("en", 0.05)], "tr"),
    ("en", 0.95, [("en", 0.95), ("tr", 0.01)], "en"),
])
async def test_bilingual_mode_prefers_turkish_and_never_selects_russian(
    tmp_path, detected, confidence, probabilities, expected,
):
    from types import SimpleNamespace

    options = []

    class Model:
        def detect_language(self, **_kwargs):
            return detected, confidence, probabilities

        def transcribe(self, _samples, **kwargs):
            options.append(kwargs)
            return [SimpleNamespace(text="tarayıcı aç")], None

    recognizer = FasterWhisperRecognizer(VoiceConfig(stt_model=tmp_path, stt_language="tr_en"))
    recognizer._model = Model()
    assert await recognizer.transcribe(b"\x00\x00") == "tarayıcı aç"
    assert options[0]["language"] == expected
    assert options[0]["task"] == "transcribe"


async def test_bilingual_mode_rejects_cyrillic_hallucination(tmp_path):
    from types import SimpleNamespace

    class Model:
        def detect_language(self, **_kwargs):
            return "tr", 0.99, [("tr", 0.99)]

        def transcribe(self, _samples, **_kwargs):
            return [SimpleNamespace(text="Игорь Негода")], None

    recognizer = FasterWhisperRecognizer(VoiceConfig(stt_model=tmp_path, stt_language="tr_en"))
    recognizer._model = Model()
    assert await recognizer.transcribe(b"\x00\x00") == ""


@pytest.mark.parametrize("text,repeating", [
    ("Tarayıcı aç. " * 22, True),
    ("Tarayıcıyı kapat. " * 5, True),
    ("tarayıcı aç tarayıcı aç tarayıcı aç", True),
    ("Tarayıcı aç ve YouTube'a git", False),
    ('Not defterine tarayıcı aç tarayıcı aç tarayıcı aç yaz', False),
    ("tarayıcı aç", False),
])
def test_repeated_whole_transcript_is_not_a_command(text, repeating):
    assert voice_adapters.is_repeating_transcript(text) is repeating


async def test_whisper_rejects_repetition_before_command_dispatch(tmp_path):
    from types import SimpleNamespace

    class Model:
        def transcribe(self, _samples, **_kwargs):
            return [SimpleNamespace(text="Tarayıcı aç. " * 22,
                                    avg_logprob=-0.1, no_speech_prob=0.01)], None

    recognizer = FasterWhisperRecognizer(VoiceConfig(stt_model=tmp_path))
    recognizer._model = Model()
    assert await recognizer.transcribe(b"\x00\x00") == ""


def test_microphone_queue_is_bounded_and_pause_discards_frames():
    microphone = SoundDeviceMicrophone(VoiceConfig(queue_frames=2))
    for _ in range(10):
        microphone._callback(b"x", 1, None, None)
    assert microphone._queue.qsize() == 2
    assert microphone.dropped_frames == 8

    microphone.pause()
    microphone._callback(b"stale", 1, None, None)
    assert microphone._queue.empty()
    microphone.resume()
    microphone._callback(b"fresh", 1, None, None)
    assert microphone._queue.get_nowait().pcm == b"fresh"


async def test_sink_marks_first_real_short_pcm_write(monkeypatch):
    class Stream:
        def __init__(self, **_kwargs):
            self.writes = []

        def start(self):
            pass

        def write(self, pcm):
            self.writes.append(pcm)

        def stop(self):
            pass

        def abort(self):
            pass

        def close(self):
            pass

    fake_module = ModuleType("sounddevice")
    fake_module.RawOutputStream = Stream
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", fake_module)
    sink = SoundDeviceSink()
    first_lengths = []

    async def chunks():
        yield PCMChunk(b"x" * 4800, 24000, 1, 2)

    def on_first():
        first_lengths.append(len(next(iter(sink._streams)).writes[0]))

    await sink.play(chunks(), asyncio.Event(), on_first)
    assert first_lengths == [960]  # 20 ms, not the whole 100 ms chunk.


async def test_cancel_aborts_device_before_slow_generator_cleanup(monkeypatch):
    events = []
    written = asyncio.Event()

    class Stream:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def write(self, _pcm):
            pass

        def abort(self):
            events.append("abort")

        def stop(self):
            events.append("stop")

        def close(self):
            events.append("close")

    fake_module = ModuleType("sounddevice")
    fake_module.RawOutputStream = Stream
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", fake_module)

    class Chunks:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if written.is_set():
                await asyncio.Future()
            return PCMChunk(b"\x01\x00" * 240)

        async def aclose(self):
            events.append("generator_cleanup")

    cancelled = asyncio.Event()
    playback = asyncio.create_task(SoundDeviceSink().play(Chunks(), cancelled, written.set))
    await written.wait()
    playback.cancel()
    with pytest.raises(asyncio.CancelledError):
        await playback
    assert cancelled.is_set()
    assert events == ["abort", "generator_cleanup", "close"]


async def test_microphone_drops_frame_already_dequeued_before_approval(monkeypatch):
    class Stream:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    fake_module = ModuleType("sounddevice")
    fake_module.RawInputStream = Stream
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", fake_module)
    microphone = SoundDeviceMicrophone(VoiceConfig(queue_frames=2))
    microphone._queue.put_nowait(AudioFrame(b"old", 1.0))
    dequeued = asyncio.Event()
    allow_return = asyncio.Event()
    call_count = 0

    async def delayed_thread_call(function, *args):
        nonlocal call_count
        result = function(*args)
        call_count += 1
        if call_count == 1:
            dequeued.set()
            await allow_return.wait()
        return result

    monkeypatch.setattr(asyncio, "to_thread", delayed_thread_call)
    frames = microphone.frames()
    pending = asyncio.create_task(anext(frames))
    await dequeued.wait()

    microphone.pause()
    microphone.resume()
    microphone._offer(b"fresh")
    allow_return.set()
    assert (await pending).pcm == b"fresh"
    await frames.aclose()


async def test_portaudio_open_failure_is_reported_and_stream_closes(monkeypatch):
    class PortAudioError(Exception):
        pass

    class Stream:
        def __init__(self, **_kwargs):
            self.closed = False

        def start(self):
            raise PortAudioError("device detail")

        def stop(self):
            pass

        def close(self):
            self.closed = True

    fake_module = ModuleType("sounddevice")
    fake_module.PortAudioError = PortAudioError
    streams = []

    def create_stream(**kwargs):
        stream = Stream(**kwargs)
        streams.append(stream)
        return stream

    fake_module.RawInputStream = create_stream
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", fake_module)
    microphone = SoundDeviceMicrophone(VoiceConfig())

    with pytest.raises(RuntimeError, match="Mikrofon aygıtı açılamadı"):
        await anext(microphone.frames())
    await microphone.close()

    assert streams[0].closed


async def test_piper_uses_local_model_and_yields_api_pcm(monkeypatch, tmp_path):
    loaded = {}

    class Audio:
        audio_int16_bytes = b"\x01\x00"
        sample_rate = 22050
        sample_channels = 1
        sample_width = 2

    class Voice:
        def synthesize(self, _text):
            yield Audio()
            yield Audio()

    class PiperVoice:
        @classmethod
        def load(cls, model, **kwargs):
            loaded.update(model=model, kwargs=kwargs)
            return Voice()

    module = ModuleType("piper")
    module.PiperVoice = PiperVoice
    monkeypatch.setattr(
        "jarvis.channels.voice.adapters.importlib.import_module",
        lambda name: module if name == "piper" else None,
    )
    config = VoiceConfig(piper_model=tmp_path / "voice.onnx", piper_config=tmp_path / "voice.json")
    synth = PiperSynthesizer(config)
    chunks = [chunk async for chunk in synth.stream("merhaba", asyncio.Event())]

    assert len(chunks) == 2
    assert chunks[0].pcm == b"\x01\x00"
    assert loaded["model"].endswith("voice.onnx")
    assert loaded["kwargs"]["config_path"] == tmp_path / "voice.json"
    assert loaded["kwargs"]["use_cuda"] is False


class FailingSpeech:
    def __init__(self, partial=False):
        self.partial = partial

    async def stream(self, _text, _cancelled):
        if self.partial:
            yield PCMChunk(b"first")
        raise RuntimeError("test failure")

    async def close(self):
        pass


class CountingSpeech:
    def __init__(self):
        self.calls = 0

    async def stream(self, _text, _cancelled):
        self.calls += 1
        yield PCMChunk(b"fallback")

    async def close(self):
        pass


async def test_edge_fallback_only_before_first_pcm_and_cancel_never_falls_back():
    local = CountingSpeech()
    fallback = FallbackSynthesizer(FailingSpeech(), local)
    assert [chunk.pcm async for chunk in fallback.stream("response", asyncio.Event())] == [
        b"fallback"
    ]
    assert local.calls == 1

    local = CountingSpeech()
    fallback = FallbackSynthesizer(FailingSpeech(partial=True), local)
    chunks = fallback.stream("response", asyncio.Event())
    assert (await anext(chunks)).pcm == b"first"
    with pytest.raises(RuntimeError):
        await anext(chunks)
    assert local.calls == 0


def test_edge_defaults_to_no_text_transfer(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path)
    with pytest.raises(ValueError):
        EdgeTTSynthesizer(VoiceConfig(tts_backend="edge"), audit)
    assert not audit_path.exists()


async def test_edge_stream_yields_pcm_before_remote_stream_finishes(monkeypatch, tmp_path):
    allow_finish = asyncio.Event()
    output: asyncio.Queue[bytes | None] = asyncio.Queue()

    class Stdin:
        def write(self, _data):
            output.put_nowait(b"pcm-from-ffmpeg")

        async def drain(self):
            pass

        def close(self):
            output.put_nowait(None)

        async def wait_closed(self):
            pass

    class Stdout:
        async def read(self, _size):
            return await output.get() or b""

    class Process:
        def __init__(self):
            self.stdin = Stdin()
            self.stdout = Stdout()
            self.returncode = None

        async def wait(self):
            self.returncode = self.returncode or 0
            return self.returncode

        async def communicate(self):
            return b"", b""

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    process = Process()

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    class Communicate:
        def __init__(self, _text, _voice):
            pass

        async def stream(self):
            yield {"type": "audio", "data": b"mp3"}
            await allow_finish.wait()
            yield {"type": "audio", "data": b"last"}

    edge_module = ModuleType("edge_tts")
    edge_module.Communicate = Communicate
    monkeypatch.setattr(
        "jarvis.channels.voice.adapters.importlib.import_module",
        lambda name: edge_module if name == "edge_tts" else None,
    )
    audit_path = tmp_path / "audit.jsonl"
    synth = EdgeTTSynthesizer(
        VoiceConfig(
            tts_backend="edge",
            allow_cloud_tts_text=True,
            edge_voice="tr-TR-Voice",
        ),
        AuditLog(audit_path),
    )
    cancelled = asyncio.Event()
    chunks = synth.stream("yanıt", cancelled)
    first = await asyncio.wait_for(anext(chunks), 1)
    assert first.pcm == b"pcm-from-ffmpeg"
    assert not allow_finish.is_set()
    cancelled.set()
    await chunks.aclose()
    assert process.returncode == -15
    audit = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert audit[0]["event"] == "cloud_tts_transfer"
    assert "yanıt" not in audit_path.read_text(encoding="utf-8")


async def test_edge_exception_family_normalizes_for_local_fallback(monkeypatch, tmp_path):
    class EdgeServiceError(Exception):
        pass

    class Stdin:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    class Stdout:
        async def read(self, _size):
            return b""

    class Process:
        stdin = Stdin()
        stdout = Stdout()
        returncode = None

        async def wait(self):
            return self.returncode or 0

        async def communicate(self):
            return b"", b""

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    async def create_process(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    class Communicate:
        def __init__(self, _text, _voice):
            pass

        async def stream(self):
            raise EdgeServiceError("remote protocol detail")
            yield {}

    edge_module = ModuleType("edge_tts")
    edge_module.Communicate = Communicate
    edge_module.exceptions = type("Exceptions", (), {"EdgeTTSException": EdgeServiceError})
    monkeypatch.setattr(
        "jarvis.channels.voice.adapters.importlib.import_module",
        lambda name: edge_module if name == "edge_tts" else (_ for _ in ()).throw(ImportError()),
    )
    local = CountingSpeech()
    edge = EdgeTTSynthesizer(
        VoiceConfig(tts_backend="edge", allow_cloud_tts_text=True, edge_voice="voice"),
        AuditLog(tmp_path / "audit.jsonl"),
    )
    fallback = FallbackSynthesizer(edge, local)

    assert [chunk.pcm async for chunk in fallback.stream("text", asyncio.Event())] == [b"fallback"]
    assert local.calls == 1


async def test_edge_cancel_does_not_cancel_windows_pipe_closed_future(monkeypatch, tmp_path):
    closed = asyncio.get_running_loop().create_future()
    closing = asyncio.Event()

    class Stdin:
        def write(self, _data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            closing.set()
            await closed

    class Stdout:
        async def read(self, _size):
            await closing.wait()
            return b"pcm"

    class Process:
        stdin = Stdin()
        stdout = Stdout()
        returncode = None

        def terminate(self):
            assert not closed.cancelled()
            closed.set_result(None)
            self.returncode = -15

        async def wait(self):
            return self.returncode

        async def communicate(self):
            return b"", b""

    process = Process()

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    class Communicate:
        def __init__(self, *_args):
            pass

        async def stream(self):
            yield {"type": "audio", "data": b"mp3"}

    edge = ModuleType("edge_tts")
    edge.Communicate = Communicate
    monkeypatch.setitem(__import__("sys").modules, "edge_tts", edge)
    synth = EdgeTTSynthesizer(
        VoiceConfig(allow_cloud_tts_text=True, edge_voice="test"),
        AuditLog(tmp_path / "audit.jsonl"),
    )
    stream = synth.stream("synthetic", asyncio.Event())
    assert (await anext(stream)).pcm == b"pcm"
    await asyncio.wait_for(stream.aclose(), 1)
    assert closed.done() and not closed.cancelled()
    assert synth._process is None


def _fake_sounddevice(monkeypatch, fail_rates=()):
    class PortAudioError(Exception):
        pass

    opened = []

    class Stream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            opened.append(self)

        def start(self):
            if self.kwargs["samplerate"] in fail_rates:
                raise PortAudioError("Invalid sample rate")

        def stop(self):
            pass

        def close(self):
            self.closed = True

    fake_module = ModuleType("sounddevice")
    fake_module.PortAudioError = PortAudioError
    fake_module.RawInputStream = Stream
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", fake_module)
    return opened


async def test_capture_rate_48k_opens_native_rate_and_tags_frames(monkeypatch):
    opened = _fake_sounddevice(monkeypatch)
    microphone = SoundDeviceMicrophone(VoiceConfig(), capture_rate=48000)
    frames = microphone.frames()
    pending = asyncio.ensure_future(anext(frames))
    await asyncio.sleep(0.05)
    assert opened[0].kwargs["samplerate"] == 48000 and opened[0].kwargs["blocksize"] == 960
    microphone._offer(b"x" * 1920)
    frame = await pending
    assert frame.sample_rate == 48000 and len(frame.pcm) == 1920
    await frames.aclose()
    await microphone.close()


async def test_default_capture_stays_16k_with_untagged_default(monkeypatch):
    opened = _fake_sounddevice(monkeypatch)
    microphone = SoundDeviceMicrophone(VoiceConfig())
    frames = microphone.frames()
    pending = asyncio.ensure_future(anext(frames))
    await asyncio.sleep(0.05)
    assert opened[0].kwargs["samplerate"] == 16000 and opened[0].kwargs["blocksize"] == 320
    microphone._offer(b"y" * 640)
    assert (await pending).sample_rate == 16000
    await frames.aclose()
    await microphone.close()


async def test_48k_open_failure_falls_back_to_16k_with_one_warning(monkeypatch, caplog):
    opened = _fake_sounddevice(monkeypatch, fail_rates=(48000,))
    microphone = SoundDeviceMicrophone(VoiceConfig(), capture_rate=48000)
    frames = microphone.frames()
    with caplog.at_level("WARNING"):
        pending = asyncio.ensure_future(anext(frames))
        await asyncio.sleep(0.05)
    assert [s.kwargs["samplerate"] for s in opened] == [48000, 16000]
    assert opened[0].closed  # başarısız akış kapatıldı
    warnings = [r for r in caplog.records if "48000 Hz açılamadı" in r.getMessage()]
    assert len(warnings) == 1 and "16 kHz" in warnings[0].getMessage()
    microphone._offer(b"z" * 640)
    frame = await pending
    assert frame.sample_rate == 16000 and microphone.capture_rate == 16000
    await frames.aclose()
    await microphone.close()


async def test_open_failure_at_both_rates_still_reports_runtime_error(monkeypatch):
    _fake_sounddevice(monkeypatch, fail_rates=(48000, 16000))
    microphone = SoundDeviceMicrophone(VoiceConfig(), capture_rate=48000)
    with pytest.raises(RuntimeError, match="Mikrofon aygıtı açılamadı"):
        await anext(microphone.frames())
    await microphone.close()
