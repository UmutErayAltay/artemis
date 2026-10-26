"""Karma tanıma (ADR 0046): bulut adaptörü, denetleyici kararı, ayar doğrulaması.

Ağ yalnız loopback sahte sunucudur; gerçek sır kullanılmaz.
"""

import io
import wave
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from artemis.channels.voice import adapters, factory
from artemis.channels.voice.adapters import OpenAICompatibleTranscriber
from artemis.channels.voice.core import AudioSegment, VoiceController
from artemis.config.settings import EndpointConfig, Settings, VoiceConfig
from tests.test_voice_core import (
    FakeMic,
    FakeRecognizer,
    FakeRunner,
    FakeSink,
    FakeTTS,
    FakeVAD,
)


@dataclass
class Server:
    url: str = ""
    status: int = 200
    body: bytes = b'{"text": "bulut metni"}'
    headers: dict[str, str] = field(default_factory=dict)
    hold: bool = False
    release: Event = field(default_factory=Event)
    requests: list[tuple[str, dict[str, str], bytes]] = field(default_factory=list)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    state = Server()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            """Erişim günlüğü anahtar veya gövde yazmaz."""

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            state.requests.append(
                (self.path, dict(self.headers.items()), self.rfile.read(length))
            )
            if state.hold:
                state.release.wait(timeout=5)
                return
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(state.body)))
            for key, value in state.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(state.body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{httpd.server_port}/v1"
    thread = Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state.release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class Audit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.records.append((event, fields))


def _transcriber(
    server: Server, *, api_key: bool = False, **voice: Any
) -> tuple[OpenAICompatibleTranscriber, Audit]:
    audit = Audit()
    config = VoiceConfig(
        stt_mode="hybrid", stt_cloud_endpoint="bulut", stt_cloud_model="whisper-x",
        allow_cloud_stt_audio=True, **voice,
    )
    endpoint = EndpointConfig(base_url=server.url, api_key=api_key)
    return OpenAICompatibleTranscriber(config, "bulut", endpoint, audit), audit  # type: ignore[arg-type]


async def test_request_shape_without_key(server: Server) -> None:
    transcriber, audit = _transcriber(server, stt_hotwords=("Artemis", "Brave"))
    assert await transcriber.transcribe(b"\x01\x00" * 160) == "bulut metni"
    path, headers, body = server.requests[0]
    assert path == "/v1/audio/transcriptions"
    assert not any(key.lower() == "authorization" for key in headers)
    assert headers["Content-Type"].startswith("multipart/form-data")
    for fragment in (
        b'name="model"\r\n\r\nwhisper-x', b'name="language"\r\n\r\ntr',
        b'name="response_format"\r\n\r\njson', b'name="prompt"\r\n\r\nArtemis Brave',
        b'name="file"; filename="command.wav"', b"RIFF",
    ):
        assert fragment in body, fragment
    # Denetim kaydı ses ya da metin taşımaz.
    assert audit.records == [
        ("cloud_stt_transfer",
         {"service": "bulut", "content_kind": "command_audio", "allowed": True})
    ]


async def test_wav_is_16k_mono_int16_and_language_omitted_unless_tr(server: Server) -> None:
    transcriber, _ = _transcriber(server, stt_language="auto")
    pcm = b"\x01\x00" * 160
    await transcriber.transcribe(pcm)
    body = server.requests[0][2]
    assert b'name="language"' not in body and b'name="prompt"' not in body
    wav_bytes = body[body.index(b"RIFF"):]
    with wave.open(io.BytesIO(wav_bytes[: 44 + len(pcm)])) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        assert wav.readframes(160) == pcm


async def test_key_sent_as_bearer_only_when_endpoint_needs_it(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapters, "stored_endpoint_key", lambda endpoint_id: "gizli-test")
    transcriber, _ = _transcriber(server, api_key=True)
    await transcriber.transcribe(b"\x00\x00")
    assert server.requests[0][1]["Authorization"] == "Bearer gizli-test"


async def test_missing_key_raises_without_sending(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapters, "stored_endpoint_key", lambda endpoint_id: None)
    transcriber, _ = _transcriber(server, api_key=True)
    with pytest.raises(RuntimeError):
        await transcriber.transcribe(b"\x00\x00")
    assert server.requests == []


async def test_redirect_is_not_followed(server: Server) -> None:
    server.status = 307
    server.headers = {"Location": server.url + "/baska"}
    transcriber, _ = _transcriber(server)
    with pytest.raises(RuntimeError, match="307"):
        await transcriber.transcribe(b"\x00\x00")
    assert len(server.requests) == 1


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_http_error_raises_without_body_or_key(
    server: Server, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapters, "stored_endpoint_key", lambda endpoint_id: "gizli-test")
    server.status = status
    server.body = b'{"error": "yanit-govdesi-sizmasin"}'
    transcriber, _ = _transcriber(server, api_key=True)
    with pytest.raises(RuntimeError) as caught:
        await transcriber.transcribe(b"\x00\x00")
    message = str(caught.value)
    assert str(status) in message and "gizli-test" not in message
    assert "sizmasin" not in message and server.url not in message


@pytest.mark.parametrize("body", [b"html", b"[]", b'{"text": 5}', b"{}"])
async def test_malformed_json_raises(server: Server, body: bytes) -> None:
    server.body = body
    transcriber, _ = _transcriber(server)
    with pytest.raises(RuntimeError):
        await transcriber.transcribe(b"\x00\x00")


async def test_timeout_raises(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapters, "CLOUD_STT_TIMEOUT_SECONDS", 0.3)
    server.hold = True
    transcriber, _ = _transcriber(server)
    with pytest.raises(RuntimeError):
        await transcriber.transcribe(b"\x00\x00")


async def test_ghost_cloud_text_is_dropped(server: Server) -> None:
    server.body = '{"text": "Abone olmayı unutmayın"}'.encode()
    transcriber, _ = _transcriber(server)
    assert await transcriber.transcribe(b"\x00\x00") == ""


def test_transcriber_refuses_without_consent() -> None:
    config = VoiceConfig(stt_mode="hybrid", stt_cloud_endpoint="b", stt_cloud_model="m")
    with pytest.raises(ValueError, match="allow_cloud_stt_audio"):
        OpenAICompatibleTranscriber(
            config, "b", EndpointConfig(base_url="https://x.com/v1"), Audit()  # type: ignore[arg-type]
        )


# ---- Denetleyici ----


class CloudFake:
    def __init__(self, text: str | Exception) -> None:
        self.text = text
        self.calls = 0

    async def transcribe(self, _pcm: bytes) -> str:
        self.calls += 1
        if isinstance(self.text, Exception):
            raise self.text
        return self.text

    async def transcribe_partial(self, _pcm: bytes) -> str:
        raise AssertionError("kısmi çözüm buluta gitmemeli")


def _controller(local: list[str], cloud: CloudFake, **config: Any):  # type: ignore[no-untyped-def]
    runner, events = FakeRunner(), []
    recognizer = FakeRecognizer(local)
    controller = VoiceController(
        runner, VoiceConfig(**config), FakeMic(), FakeVAD([]), recognizer, FakeTTS(),
        FakeSink(), clock=lambda: 100.0, on_event=events.append, cloud_recognizer=cloud,
    )
    return controller, runner, events


async def _say(controller: VoiceController) -> None:
    await controller._handle_utterance(AudioSegment(b"\x00\x00", 50.0, 51.0))
    await controller._settle_active()


async def test_wake_command_uses_cloud_text_and_strips_wake() -> None:
    cloud = CloudFake("Artemis, saat kaç?")
    controller, runner, _ = _controller(["artemis saat kac"], cloud)
    try:
        await _say(controller)
        assert cloud.calls == 1 and runner.commands == ["saat kaç?"]
    finally:
        await controller.close()


async def test_cloud_text_without_wake_is_used_whole() -> None:
    cloud = CloudFake("saat kaç")
    controller, runner, _ = _controller(["artemis saat kac"], cloud)
    try:
        await _say(controller)
        assert runner.commands == ["saat kaç"]
    finally:
        await controller.close()


async def test_wake_only_never_goes_to_cloud() -> None:
    cloud = CloudFake("Artemis")
    controller, runner, _ = _controller(["Artemis"], cloud)
    try:
        await _say(controller)
        assert cloud.calls == 0 and runner.commands == []
    finally:
        await controller.close()


async def test_followup_goes_to_cloud() -> None:
    cloud = CloudFake("hava nasıl")
    controller, runner, _ = _controller(["hava nasil"], cloud)
    controller._followup_until = 105.0
    try:
        await _say(controller)
        assert cloud.calls == 1 and runner.commands == ["hava nasıl"]
    finally:
        await controller.close()


async def test_continuous_mode_goes_to_cloud() -> None:
    cloud = CloudFake("hava nasıl")
    controller, runner, _ = _controller(["hava nasil"], cloud, continuous_mode=True)
    try:
        await _say(controller)
        assert cloud.calls == 1 and runner.commands == ["hava nasıl"]
    finally:
        await controller.close()


async def test_non_command_speech_never_goes_to_cloud() -> None:
    cloud = CloudFake("tv sesi")
    controller, runner, _ = _controller(["televizyon sesi"], cloud)
    try:
        await _say(controller)
        assert cloud.calls == 0 and runner.commands == []
    finally:
        await controller.close()


@pytest.mark.parametrize("failure", [
    RuntimeError("x"), OSError("x"), ValueError("x"), "",
])
async def test_cloud_failure_falls_back_to_local_text_and_logs_event(
    failure: Exception | str,
) -> None:
    cloud = CloudFake(failure)
    controller, runner, events = _controller(["artemis saat kac"], cloud)
    try:
        await _say(controller)
        assert runner.commands == ["saat kac"]
        fallback = [e for e in events if e.stage == "stt_cloud_fallback"]
        assert len(fallback) == 1 and fallback[0].reason == "stt_cloud_fallback"
    finally:
        await controller.close()


async def test_fallback_event_does_not_invalidate_latency_sample() -> None:
    from artemis.channels.voice.metrics import _sample

    cloud = CloudFake(RuntimeError("x"))
    controller, _, events = _controller(["artemis saat kac"], cloud)
    try:
        await _say(controller)
        items = [e for e in events if e.utterance_id == events[0].utterance_id]
        assert "stt_cloud_fallback" in {e.stage for e in items}
        rest = [e for e in items if e.stage != "stt_cloud_fallback"]
        assert _sample(1, items)["invalid_timing"] == _sample(1, rest)["invalid_timing"]
        assert _sample(1, items)["timings"] == _sample(1, rest)["timings"]
    finally:
        await controller.close()


async def test_partial_decode_never_calls_cloud() -> None:
    cloud = CloudFake("x")
    controller, _, _ = _controller(["artemis saat kac"], cloud)
    try:
        controller.segmenter._active.append(b"\x00\x00")
        controller._start_partial_decode()
        await controller._settle_partial()
        assert cloud.calls == 0
    finally:
        await controller.close()


# ---- Ayarlar ve fabrika ----


def _settings(**voice: Any) -> Settings:
    return Settings(
        endpoints={"bulut": EndpointConfig(base_url="https://api.example.com/v1")},
        voice=VoiceConfig(**voice),
    )


def test_defaults_are_local_without_consent() -> None:
    voice = VoiceConfig()
    assert (voice.stt_mode, voice.stt_cloud_endpoint, voice.stt_cloud_model) == ("local", "", "")
    assert voice.allow_cloud_stt_audio is False


def test_hybrid_requires_known_endpoint_and_model() -> None:
    ok = {"stt_mode": "hybrid", "stt_cloud_endpoint": "bulut", "stt_cloud_model": "m"}
    assert _settings(**ok).voice.stt_mode == "hybrid"
    for bad in (
        {**ok, "stt_cloud_endpoint": ""}, {**ok, "stt_cloud_endpoint": "yok"},
        {**ok, "stt_cloud_model": ""}, {**ok, "stt_cloud_model": "  "},
    ):
        with pytest.raises(ValidationError):
            _settings(**bad)


def _runtime(settings: Settings) -> SimpleNamespace:
    return SimpleNamespace(settings=settings, audit=Audit(), runner=object())


def _patch_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory, "FasterWhisperRecognizer",
                        lambda _config: SimpleNamespace(prepare=lambda: None))
    monkeypatch.setattr(factory, "PiperSynthesizer",
                        lambda _config: SimpleNamespace(prepare=lambda: None))
    monkeypatch.setattr(factory, "SoundDeviceMicrophone", lambda *_a, **_k: object())
    monkeypatch.setattr(factory, "SoundDeviceSink", lambda: object())
    monkeypatch.setattr(factory, "WebRTCVAD", lambda _config: object())


async def test_voice_channel_refuses_hybrid_without_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_factory(monkeypatch)
    settings = _settings(
        stt_mode="hybrid", stt_cloud_endpoint="bulut", stt_cloud_model="m"
    )
    with pytest.raises(ValueError, match="allow_cloud_stt_audio"):
        await factory.build_voice_controller(_runtime(settings), lambda _: None, lambda _: None)


async def test_factory_wires_cloud_recognizer_only_in_hybrid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_factory(monkeypatch)
    hybrid = _settings(
        stt_mode="hybrid", stt_cloud_endpoint="bulut", stt_cloud_model="m",
        allow_cloud_stt_audio=True,
    )
    controller = await factory.build_voice_controller(
        _runtime(hybrid), lambda _: None, lambda _: None
    )
    assert isinstance(controller.cloud_recognizer, OpenAICompatibleTranscriber)
    local = await factory.build_voice_controller(
        _runtime(_settings()), lambda _: None, lambda _: None
    )
    assert local.cloud_recognizer is None
