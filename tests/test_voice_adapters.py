"""Opsiyonel ses adaptörleri sahte kitaplıklarla doğrulanır."""

import asyncio
import json
from types import ModuleType

import pytest

from jarvis.channels.voice.adapters import (
    EdgeTTSynthesizer,
    FallbackSynthesizer,
    PiperSynthesizer,
    SoundDeviceMicrophone,
    SoundDeviceSink,
)
from jarvis.channels.voice.core import AudioFrame, PCMChunk
from jarvis.config.settings import VoiceConfig
from jarvis.safety.audit import AuditLog


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
