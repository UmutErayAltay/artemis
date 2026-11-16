"""Bulut konuşma sesi (ADR 0074): OpenAI uyumlu /audio/speech adaptörü ve ayar doğrulaması.

Ağ yalnız loopback sahte sunucudur; gerçek sır kullanılmaz.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from typing import Any

import pytest
from pydantic import ValidationError

from artemis.channels.voice import cloud_tts
from artemis.channels.voice.cloud_tts import CloudSpeechSynthesizer
from artemis.config.settings import EndpointConfig, Settings, VoiceConfig


@dataclass
class Server:
    url: str = ""
    status: int = 200
    body_chunks: list[bytes] = field(default_factory=lambda: [b"\x01\x00\x02\x00"])
    headers: dict[str, str] = field(default_factory=lambda: {"Content-Type": "audio/pcm"})
    chunk_delay: float = 0.0
    hold_after_first: bool = False
    first_sent: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    requests: list[tuple[str, dict[str, str], bytes]] = field(default_factory=list)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    state = Server()

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, format: str, *args: Any) -> None:
            """Erişim günlüğü anahtar veya gövde yazmaz."""

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            state.requests.append(
                (self.path, dict(self.headers.items()), self.rfile.read(length))
            )
            total = sum(len(part) for part in state.body_chunks)
            self.send_response(state.status)
            self.send_header("Content-Length", str(total))
            for key, value in state.headers.items():
                self.send_header(key, value)
            self.end_headers()
            for index, part in enumerate(state.body_chunks):
                try:
                    self.wfile.write(part)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                if state.chunk_delay > 0 and index + 1 < len(state.body_chunks):
                    time.sleep(state.chunk_delay)
                if index == 0 and state.hold_after_first:
                    state.first_sent.set()
                    state.release.wait(timeout=5)

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


def _synthesizer(
    server: Server, *, api_key: bool = False, **voice: Any
) -> tuple[CloudSpeechSynthesizer, Audit]:
    audit = Audit()
    defaults: dict[str, Any] = {
        "tts_backend": "cloud",
        "tts_cloud_endpoint": "bulut",
        "tts_cloud_model": "heygen/heygen-voice-1-turbo:free",
        "allow_cloud_tts_text": True,
    }
    defaults.update(voice)
    config = VoiceConfig(**defaults)
    endpoint = EndpointConfig(base_url=server.url, api_key=api_key)
    return CloudSpeechSynthesizer(config, "bulut", endpoint, audit), audit  # type: ignore[arg-type]


async def test_request_json_omits_empty_voice_and_includes_set_voice(server: Server) -> None:
    synth, _ = _synthesizer(server)
    chunks = [chunk async for chunk in synth.stream("Merhaba", asyncio.Event())]
    assert len(chunks) == 1
    path, _, raw_body = server.requests[0]
    assert path == "/v1/audio/speech"
    body = json.loads(raw_body.decode("utf-8"))
    assert body == {
        "model": "heygen/heygen-voice-1-turbo:free",
        "input": "Merhaba",
        "response_format": "pcm",
    }
    assert "voice" not in body

    synth_with_voice, _ = _synthesizer(server, tts_cloud_voice="tr-emel")
    _ = [chunk async for chunk in synth_with_voice.stream("Selam", asyncio.Event())]
    body_with_voice = json.loads(server.requests[1][2].decode("utf-8"))
    assert body_with_voice["voice"] == "tr-emel"


async def test_authorization_header_only_when_api_key_set(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    synth_no_key, _ = _synthesizer(server, api_key=False)
    _ = [chunk async for chunk in synth_no_key.stream("Merhaba", asyncio.Event())]
    assert not any(k.lower() == "authorization" for k in server.requests[0][1])

    monkeypatch.setattr(cloud_tts, "stored_endpoint_key", lambda _id: "gizli-test")
    synth_with_key, _ = _synthesizer(server, api_key=True)
    _ = [chunk async for chunk in synth_with_key.stream("Merhaba", asyncio.Event())]
    assert server.requests[1][1]["Authorization"] == "Bearer gizli-test"


async def test_missing_key_raises_without_sending(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cloud_tts, "stored_endpoint_key", lambda _id: None)
    synth, audit = _synthesizer(server, api_key=True)
    with pytest.raises(RuntimeError):
        _ = [chunk async for chunk in synth.stream("Merhaba", asyncio.Event())]
    assert server.requests == []
    assert audit.records == []


async def test_audit_emitted_before_request_and_without_text(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = Audit()
    seen_audit_during_post: list[tuple[str, dict[str, Any]]] = []
    config = VoiceConfig(
        tts_backend="cloud",
        tts_cloud_endpoint="bulut",
        tts_cloud_model="heygen/heygen-voice-1-turbo:free",
        allow_cloud_tts_text=True,
    )
    endpoint = EndpointConfig(base_url=server.url, api_key=False)
    synth = CloudSpeechSynthesizer(config, "bulut", endpoint, audit)  # type: ignore[arg-type]

    orig_stream = cloud_tts.httpx.AsyncClient.stream

    def spy_stream(self_client: Any, method: str, url: str, **kwargs: Any) -> Any:
        seen_audit_during_post.extend(audit.records)
        return orig_stream(self_client, method, url, **kwargs)

    monkeypatch.setattr(cloud_tts.httpx.AsyncClient, "stream", spy_stream)
    secret_text = "Gizli asistan yanıtı burada"
    _ = [chunk async for chunk in synth.stream(secret_text, asyncio.Event())]

    expected_event = (
        "cloud_tts_transfer",
        {
            "service": "bulut",
            "model": "heygen/heygen-voice-1-turbo:free",
            "content_kind": "assistant_response_text",
            "allowed": True,
        },
    )
    assert seen_audit_during_post == [expected_event]
    assert audit.records == [expected_event]
    assert secret_text not in str(audit.records)


async def test_chunk_byte_alignment_and_trailing_odd_byte_dropped(server: Server) -> None:
    server.body_chunks = [b"\x01", b"\x02\x03", b"\x04", b"\x05"]
    server.chunk_delay = 0.02
    synth, _ = _synthesizer(server, tts_cloud_sample_rate=16000)
    chunks = [chunk async for chunk in synth.stream("Test", asyncio.Event())]
    assert len(chunks) > 1
    assert all(len(chunk.pcm) % 2 == 0 for chunk in chunks)
    assert all(
        (chunk.sample_rate, chunk.channels, chunk.sample_width) == (16000, 1, 2)
        for chunk in chunks
    )
    assert b"".join(chunk.pcm for chunk in chunks) == b"\x01\x02\x03\x04"


async def test_empty_or_single_byte_audio_raises(server: Server) -> None:
    for body in ([], [b"\x01"]):
        server.body_chunks = body
        synth, _ = _synthesizer(server)
        with pytest.raises(RuntimeError):
            _ = [chunk async for chunk in synth.stream("Test", asyncio.Event())]


async def test_cancel_stops_streaming(server: Server) -> None:
    server.body_chunks = [b"\x01\x02", b"\x03\x04"]
    server.hold_after_first = True
    synth, _ = _synthesizer(server)
    cancelled = asyncio.Event()
    received = []
    async for chunk in synth.stream("Uzun metin", cancelled):
        received.append(chunk.pcm)
        cancelled.set()
        server.release.set()
    assert received == [b"\x01\x02"]


@pytest.mark.parametrize("status", [307, 402, 429, 500])
async def test_http_errors_raise_without_url_key_or_body(
    server: Server, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cloud_tts, "stored_endpoint_key", lambda _id: "gizli-test")
    server.status = status
    server.headers = {"Content-Type": "application/json", "Location": server.url + "/baska"}
    server.body_chunks = [b'{"error": "yanit-govdesi-sizmasin"}']
    synth, _ = _synthesizer(server, api_key=True)
    with pytest.raises(RuntimeError) as caught:
        _ = [chunk async for chunk in synth.stream("Test", asyncio.Event())]
    message = str(caught.value)
    assert str(status) in message
    if status == 429:
        assert "hız/kota sınırı" in message
    assert "gizli-test" not in message
    assert "sizmasin" not in message
    assert server.url not in message


async def test_non_audio_content_type_raises_without_url(server: Server) -> None:
    server.headers = {"Content-Type": "application/json"}
    server.body_chunks = [b'{"ok": true, "secret": "govde-sizmasin"}']
    synth, _ = _synthesizer(server)
    with pytest.raises(RuntimeError) as caught:
        _ = [chunk async for chunk in synth.stream("Test", asyncio.Event())]
    assert server.url not in str(caught.value)
    assert "govde-sizmasin" not in str(caught.value)


def test_constructor_refusals_and_paid_guard() -> None:
    audit = Audit()
    paid_ep = EndpointConfig(base_url="https://api.example.com/v1", api_key=True)
    local_ep = EndpointConfig(base_url="http://127.0.0.1:8000/v1", api_key=False)

    with pytest.raises(ValueError, match="allow_cloud_tts_text"):
        CloudSpeechSynthesizer(
            VoiceConfig(tts_cloud_model="m:free", allow_cloud_tts_text=False),
            "bulut", paid_ep, audit,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError):
        CloudSpeechSynthesizer(
            VoiceConfig(tts_cloud_model="", allow_cloud_tts_text=True),
            "bulut", paid_ep, audit,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="tts_cloud_allow_paid"):
        CloudSpeechSynthesizer(
            VoiceConfig(
                tts_cloud_model="openai/tts-1",
                allow_cloud_tts_text=True,
                tts_cloud_allow_paid=False,
            ),
            "bulut", paid_ep, audit,  # type: ignore[arg-type]
        )

    # Ücretli model açık izinle kabul edilir.
    CloudSpeechSynthesizer(
        VoiceConfig(
            tts_cloud_model="openai/tts-1",
            allow_cloud_tts_text=True,
            tts_cloud_allow_paid=True,
        ),
        "bulut", paid_ep, audit,  # type: ignore[arg-type]
    )

    # api_key=False olan yerel uç ücretli sayılmaz.
    CloudSpeechSynthesizer(
        VoiceConfig(
            tts_cloud_model="kokoro",
            allow_cloud_tts_text=True,
            tts_cloud_allow_paid=False,
        ),
        "yerel", local_ep, audit,  # type: ignore[arg-type]
    )

    # Gemini protokolü + api_key=True: tts_cloud_allow_paid olmadan reddedilir.
    with pytest.raises(ValueError, match="Google"):
        CloudSpeechSynthesizer(
            VoiceConfig(
                tts_cloud_protocol="gemini",
                tts_cloud_model="gemini-2.5-flash-preview-tts",
                allow_cloud_tts_text=True,
                tts_cloud_allow_paid=False,
            ),
            "bulut", paid_ep, audit,  # type: ignore[arg-type]
        )


def _sse_event(pcm: bytes, mime_type: str = "audio/l16; rate=24000; channels=1") -> bytes:
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": base64.b64encode(pcm).decode("ascii"),
                            }
                        }
                    ]
                }
            }
        ]
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


async def test_gemini_request_path_headers_and_voice_body(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cloud_tts, "stored_endpoint_key", lambda _id: "gemini-gizli-anahtar")
    server.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
    server.body_chunks = [_sse_event(b"\x01\x00\x02\x00")]

    synth_no_voice, _ = _synthesizer(
        server,
        api_key=True,
        tts_cloud_protocol="gemini",
        tts_cloud_model="gemini-2.5-flash-preview-tts",
        tts_cloud_allow_paid=True,
        tts_cloud_voice="",
    )
    chunks = [chunk async for chunk in synth_no_voice.stream("Merhaba", asyncio.Event())]
    assert len(chunks) == 1
    path, req_headers, raw_body = server.requests[0]
    assert path == "/v1/models/gemini-2.5-flash-preview-tts:streamGenerateContent?alt=sse"
    lower_headers = {k.lower(): v for k, v in req_headers.items()}
    assert lower_headers["x-goog-api-key"] == "gemini-gizli-anahtar"
    assert "authorization" not in lower_headers
    body_no_voice = json.loads(raw_body.decode("utf-8"))
    assert body_no_voice["contents"] == [{"parts": [{"text": "Merhaba"}]}]
    assert body_no_voice["generationConfig"] == {"responseModalities": ["AUDIO"]}
    assert "speechConfig" not in body_no_voice["generationConfig"]

    synth_with_voice, _ = _synthesizer(
        server,
        api_key=True,
        tts_cloud_protocol="gemini",
        tts_cloud_model="gemini-2.5-flash-preview-tts",
        tts_cloud_allow_paid=True,
        tts_cloud_voice="Kore",
    )
    _ = [chunk async for chunk in synth_with_voice.stream("Selam", asyncio.Event())]
    body_with_voice = json.loads(server.requests[1][2].decode("utf-8"))
    assert body_with_voice["generationConfig"]["speechConfig"] == {
        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}
    }


async def test_gemini_parses_mimetype_rate_and_aligns_odd_parts(server: Server) -> None:
    server.headers = {"Content-Type": "text/event-stream"}
    server.body_chunks = [
        _sse_event(b"\x01\x02\x03", mime_type="audio/l16; rate=22050; channels=1"),
        _sse_event(b"\x04\x05\x06", mime_type="audio/l16; rate=22050; channels=1"),
    ]
    synth, _ = _synthesizer(
        server,
        api_key=False,
        tts_cloud_protocol="gemini",
        tts_cloud_model="gemini-2.5-flash-preview-tts",
        tts_cloud_sample_rate=16000,
    )
    chunks = [chunk async for chunk in synth.stream("Test", asyncio.Event())]
    assert len(chunks) == 2
    assert all(len(chunk.pcm) % 2 == 0 for chunk in chunks)
    assert all(chunk.sample_rate == 22050 for chunk in chunks)
    assert b"".join(chunk.pcm for chunk in chunks) == b"\x01\x02\x03\x04\x05\x06"


async def test_gemini_malformed_data_line_raises_turkish_error(server: Server) -> None:
    server.headers = {"Content-Type": "text/event-stream"}
    server.body_chunks = [b"data: {bozuk-json\n\n"]
    synth, _ = _synthesizer(
        server,
        api_key=False,
        tts_cloud_protocol="gemini",
        tts_cloud_model="gemini-2.5-flash-preview-tts",
    )
    with pytest.raises(RuntimeError) as caught:
        _ = [chunk async for chunk in synth.stream("Test", asyncio.Event())]
    assert str(caught.value) == "Bulut konuşma yanıtı çözülemedi."
    endpoints = {"bulut": EndpointConfig(base_url="https://api.example.com/v1")}
    for bad in ("", "yok"):
        with pytest.raises(ValidationError):
            Settings(
                endpoints=endpoints,
                voice=VoiceConfig(tts_backend="cloud", tts_cloud_endpoint=bad),
            )
    # Yarım doldurulmuş form (model/izin boş) Settings düzeyinde kaydedilebilir.
    saved = Settings(
        endpoints=endpoints,
        voice=VoiceConfig(tts_backend="cloud", tts_cloud_endpoint="bulut"),
    )
    assert saved.voice.tts_backend == "cloud"
