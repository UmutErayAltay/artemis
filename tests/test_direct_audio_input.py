"""Doğrudan sesli komut girdisi (opt-in WAV → beyin modeli + otomatik düşüş) testleri."""

from __future__ import annotations

import base64
import io
import json
import time
import types
import wave
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from artemis.brain.agent_loop import AgentLoop, RunResult
from artemis.brain.conversation import ConversationContext, TurnRecord
from artemis.brain.errors import ProviderError
from artemis.brain.models import Completion, Message
from artemis.brain.providers import (
    AnthropicProvider,
    ClaudeCliProvider,
    OpenAICompatibleProvider,
    OpenRouterProvider,
    _chat_messages,
    _cloud_messages,
)
from artemis.brain.router import ProviderRouter
from artemis.channels.runner import CommandRunner
from artemis.channels.telegram.api import TelegramClient
from artemis.channels.telegram.channel import TelegramChannel
from artemis.channels.voice.core import (
    AudioFrame,
    AudioSegment,
    PCMChunk,
    VoiceController,
    pcm_to_wav,
)
from artemis.config.settings import EndpointConfig, ModelConfig, Settings, VoiceConfig
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate
from artemis.tools.base import ToolContext, ToolDefinition
from artemis.tools.registry import ToolRegistry

PCM = b"\x01\x00\x02\x00\x03\x00\x04\x00" * 10
WAV = pcm_to_wav(PCM, 16000)
WAV_B64 = base64.b64encode(WAV).decode("ascii")
PNG = b"\x89PNG\r\n\x1a\n-sahte"
PNG_B64 = base64.b64encode(PNG).decode("ascii")


class _CaptureProvider:
    def __init__(self, responses: list[Completion | ProviderError]) -> None:
        self.responses = list(responses)
        self.seen_messages: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        timeout: float,
        stream: Any = None,
    ) -> Completion:
        self.seen_messages.append(list(messages))
        item = self.responses.pop(0)
        if isinstance(item, ProviderError):
            raise item
        return item


class _FakeMic:
    async def frames(self) -> AsyncGenerator[AudioFrame]:
        if False:
            yield AudioFrame(b"", 0.0)

    def discard_pending(self) -> None:
        pass

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    async def close(self) -> None:
        pass


class _FakeVAD:
    def is_speech(self, _pcm: bytes) -> bool:
        return False


class _FakeRecognizer:
    def __init__(self, texts: list[str]) -> None:
        self.texts = iter(texts)
        self.calls: list[bytes] = []

    async def transcribe(self, pcm: bytes) -> str:
        self.calls.append(pcm)
        return next(self.texts, "")


class _FakeTTS:
    async def stream(self, _text: str, _cancelled: Any) -> AsyncGenerator[PCMChunk]:
        yield PCMChunk(b"\x01\x00")

    async def close(self) -> None:
        pass


class _FakeSink:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    async def play(self, chunks: Any, cancelled: Any, on_first_pcm: Any) -> None:
        async for chunk in chunks:
            if cancelled.is_set():
                return
            self.chunks.append(chunk.pcm)
            on_first_pcm()

    async def close(self) -> None:
        pass


def _settings(
    tmp_path: Path,
    *,
    primary_provider: str = "openai_compatible",
    direct_audio: bool = False,
) -> Settings:
    models: dict[str, ModelConfig] = {
        "gemini": ModelConfig(
            provider="openai_compatible",
            endpoint="gem",
            model="gemini-3.1-flash-lite",
            input_per_million=Decimal("0"),
            output_per_million=Decimal("0"),
        ),
        "or": ModelConfig(
            provider="openrouter",
            model="google/gemini-2.5-flash:free",
            input_per_million=Decimal("0"),
            output_per_million=Decimal("0"),
        ),
        "claude": ModelConfig(
            provider="anthropic",
            model="claude-sonnet",
            input_per_million=Decimal("0"),
            output_per_million=Decimal("0"),
        ),
        "local": ModelConfig(
            provider="ollama",
            model="qwen3:8b",
            input_per_million=Decimal("0"),
            output_per_million=Decimal("0"),
        ),
    }
    primary = {
        "openai_compatible": "gemini",
        "openrouter": "or",
        "anthropic": "claude",
        "ollama": "local",
    }[primary_provider]
    return Settings(
        workspace=tmp_path / "ws",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        primary_model=primary,
        endpoints={"gem": EndpointConfig(base_url="http://127.0.0.1:9999/v1", api_key=False)},
        voice=VoiceConfig(direct_audio_input=direct_audio),
        models=models,
    )


def test_message_excludes_audio_wav_from_repr_dump_and_turn_record() -> None:
    msg = Message(role="user", content="Deadlock aç", audio_wav=WAV)
    assert "RIFF" not in repr(msg) and WAV_B64 not in repr(msg)
    assert "audio_wav" not in msg.model_dump()
    assert WAV_B64 not in msg.model_dump_json()

    record = TurnRecord(
        turn_id="t1",
        user_text="Deadlock aç",
        steps=(msg,),
        answer="Açtım.",
        answer_private=False,
        tainted=False,
    )
    assert WAV_B64 not in record.model_dump_json()
    restored = TurnRecord.model_validate_json(record.model_dump_json())
    assert restored.steps[0].audio_wav is None  # type: ignore[union-attr]


def test_cloud_messages_strips_private_audio_and_chat_messages_formats_input_audio() -> None:
    msg = Message(role="user", content="Notu oku", audio_wav=WAV, image_png=PNG)
    [item] = _chat_messages([msg])
    assert item["content"] == [
        {"type": "text", "text": "Notu oku"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}},
        {"type": "input_audio", "input_audio": {"data": WAV_B64, "format": "wav"}},
    ]

    [ollama_item] = _chat_messages([msg], ollama=True)
    assert ollama_item == {"role": "user", "content": "Notu oku", "images": [PNG_B64]}

    [masked] = _cloud_messages([Message(role="user", content="gizli", audio_wav=WAV, private=True)])
    assert masked.audio_wav is None


async def test_openai_compatible_and_openrouter_serialize_input_audio_and_others_ignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    settings = _settings(tmp_path)
    sent_payloads: list[tuple[str, dict[str, Any]]] = []

    async def fake_post(
        url: str, payload: dict[str, Any], headers: dict[str, str], *, timeout: float
    ) -> dict[str, Any]:
        sent_payloads.append((url, payload))
        if url.endswith("/messages"):
            return {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr("artemis.brain.providers._post", fake_post)
    msg = Message(role="user", content="Deadlock aç", audio_wav=WAV)

    await OpenAICompatibleProvider(settings, settings.models["gemini"]).complete(
        [msg], [], timeout=5
    )
    await OpenRouterProvider(settings, settings.models["or"]).complete([msg], [], timeout=5)
    await AnthropicProvider(settings, settings.models["claude"]).complete([msg], [], timeout=5)

    assert len(sent_payloads) == 3
    expected_audio = {"type": "input_audio", "input_audio": {"data": WAV_B64, "format": "wav"}}
    for idx in (0, 1):
        parts = sent_payloads[idx][1]["messages"][0]["content"]
        assert parts[1] == expected_audio
    # Anthropic yalnızca metin görür, audio_wav yüzünden çökmez
    assert sent_payloads[2][1]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Deadlock aç"}]}
    ]

    cli = ClaudeCliProvider(
        settings,
        ModelConfig(
            provider="claude_cli",
            model="sonnet",
            input_per_million=Decimal("0"),
            output_per_million=Decimal("0"),
        ),
    )
    class _FakeProc:
        returncode = 0
        pid = 0

        async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
            payload = json.dumps(
                {
                    "structured_output": {"content": "cli ok", "tool_calls": []},
                    "total_cost_usd": 0,
                    "usage": {},
                }
            ).encode()
            return payload, b""

        async def wait(self) -> int:
            return 0

    async def fake_spawn(*_a: Any, **_kw: Any) -> _FakeProc:
        return _FakeProc()

    monkeypatch.setattr("artemis.brain.providers.shutil.which", lambda _n: "claude")
    monkeypatch.setattr("artemis.brain.providers.own_process_tree", lambda _p: None)
    monkeypatch.setattr("artemis.brain.providers.asyncio.create_subprocess_exec", fake_spawn)
    res = await cli.complete([msg], [], timeout=5)
    assert res.content == "cli ok"


async def test_router_retries_once_without_audio_on_http_400_and_skips_for_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    settings = _settings(tmp_path)
    fake = _CaptureProvider(
        [
            ProviderError("Sağlayıcı isteği kabul etmedi (HTTP 400)."),
            Completion(content="Metinle devam edildi", provider="openai_compatible", model="m"),
        ]
    )
    router = ProviderRouter(settings, AuditLog(settings.audit_path), providers={"gemini": fake})
    msg = Message(role="user", content="saat kaç", audio_wav=WAV)

    comp = await router.complete([msg], [], timeout=10)
    assert comp.content == "Metinle devam edildi"
    assert len(fake.seen_messages) == 2
    assert fake.seen_messages[0][0].audio_wav == WAV
    assert fake.seen_messages[1][0].audio_wav is None

    audits = [
        json.loads(line) for line in settings.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    cloud_ev = next(e for e in audits if e["event"] == "cloud_transfer")
    assert cloud_ev["has_audio"] is True
    fb_ev = next(e for e in audits if e["event"] == "audio_input_fallback")
    assert fb_ev["reason"] == "rejected_audio_input"

    # Desteklemeyen sağlayıcıda (Anthropic) HTTP isteğinden önce sesi temizler
    claude_settings = _settings(tmp_path / "cl", primary_provider="anthropic")
    claude_fake = _CaptureProvider([Completion(content="claude ok")])
    claude_router = ProviderRouter(
        claude_settings,
        AuditLog(claude_settings.audit_path),
        providers={"claude": claude_fake},
    )
    await claude_router.complete([msg], [], timeout=10)
    assert claude_fake.seen_messages[0][0].audio_wav is None
    cl_audits = [
        json.loads(line)
        for line in claude_settings.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        e["event"] == "audio_input_fallback" and e["reason"] == "unsupported_provider"
        for e in cl_audits
    )


async def test_router_does_not_strip_audio_on_auth_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = _CaptureProvider(
        [ProviderError("Sağlayıcı API anahtarını reddetti (HTTP 401).")]
    )
    router = ProviderRouter(settings, AuditLog(settings.audit_path), providers={"gemini": fake})
    with pytest.raises(ProviderError, match="HTTP 401"):
        await router.complete([Message(role="user", content="x", audio_wav=WAV)], [], timeout=10)
    assert len(fake.seen_messages) == 1


async def test_agent_loop_and_runner_attach_audio_only_on_current_turn(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake = _CaptureProvider(
        [
            Completion(content="Tamam.", provider="openai_compatible", model="gemini"),
            Completion(content="İkinci tur.", provider="openai_compatible", model="gemini"),
        ]
    )
    audit = AuditLog(settings.audit_path)
    context = ToolContext(settings.workspace, settings, audit)
    loop = AgentLoop(fake, SafetyGate(ToolRegistry()), context, lambda *_: True)
    runner = CommandRunner(loop)

    res1 = await runner.run("dedlok aç", audio_wav=WAV)
    assert res1.text == "Tamam."
    assert loop.context.user_text == "dedlok aç"
    turn1_msgs = fake.seen_messages[0]
    assert turn1_msgs[-1].audio_wav == WAV
    assert "Doğrudan ses kaydı eklendi" in turn1_msgs[-1].content

    # İkinci turda önceki turun ses baytları tekrar gönderilmez
    await runner.run("nasıl gitti")
    turn2_msgs = fake.seen_messages[1]
    assert all(m.audio_wav is None for m in turn2_msgs)


async def test_voice_controller_sends_wav_when_enabled_and_skips_cloud_stt() -> None:
    calls: list[tuple[str, bytes | None]] = []

    async def run_cmd(text: str, *, audio_wav: bytes | None = None, **_kw: Any) -> RunResult:
        calls.append((text, audio_wav))
        return RunResult(text="oldu", stop_reason="completed", turn_id="t1")

    local = _FakeRecognizer(["Artemis deadlock aç", "Artemis"])
    cloud = _FakeRecognizer(["buluttan"])
    ctrl = VoiceController(
        types.SimpleNamespace(run=run_cmd, cancel_current=lambda: True),  # type: ignore[arg-type]
        VoiceConfig(direct_audio_input=True),
        _FakeMic(),
        _FakeVAD(),
        local,
        _FakeTTS(),
        _FakeSink(),
        run_command=run_cmd,
        cloud_recognizer=cloud,
    )
    try:
        await ctrl._handle_utterance(AudioSegment(PCM, 0.0, 1.0))
        await ctrl._settle_active()
        assert len(calls) == 1
        assert calls[0][0] == "deadlock aç"
        sent_wav = calls[0][1]
        assert sent_wav is not None and sent_wav.startswith(b"RIFF")
        with wave.open(io.BytesIO(sent_wav), "rb") as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
            assert w.readframes(w.getnframes()) == PCM
        assert cloud.calls == []  # ikinci bulut STT atlandı

        # Yalnız uyandırma ("Artemis"): komut yok → run_command çağrılmaz
        await ctrl._handle_utterance(AudioSegment(PCM, 1.0, 2.0))
        await ctrl._settle_active()
        assert len(calls) == 1
    finally:
        await ctrl.close()

    # Eski imza (audio_wav almayan run_command): TypeError çıkarmadan çalışır
    legacy_calls: list[str] = []

    async def legacy_cmd(text: str) -> RunResult:
        legacy_calls.append(text)
        return RunResult(text="eski", stop_reason="completed", turn_id="t2")

    ctrl2 = VoiceController(
        types.SimpleNamespace(run=legacy_cmd, cancel_current=lambda: True),  # type: ignore[arg-type]
        VoiceConfig(direct_audio_input=True),
        _FakeMic(),
        _FakeVAD(),
        _FakeRecognizer(["Artemis saat kaç"]),
        _FakeTTS(),
        _FakeSink(),
        run_command=legacy_cmd,
    )
    try:
        await ctrl2._handle_utterance(AudioSegment(PCM, 0.0, 1.0))
        await ctrl2._settle_active()
        assert legacy_calls == ["saat kaç"]
    finally:
        await ctrl2.close()


@pytest.mark.parametrize("enabled", [False, True])
async def test_telegram_voice_attaches_audio_wav_only_when_direct_audio_enabled(
    tmp_path: Path, enabled: bool
) -> None:
    base = tmp_path / f"tg-{enabled}"
    settings = _settings(base, direct_audio=enabled)
    audit = AuditLog(settings.audit_path)
    fake_provider = _CaptureProvider(
        [Completion(content="cevap", provider="openai_compatible", model="gemini")]
    )
    registry = ToolRegistry()
    context = ToolContext(settings.workspace, settings, audit)
    loop = AgentLoop(fake_provider, SafetyGate(registry), context, lambda *_: True)
    runner = CommandRunner(loop, ConversationContext(10, 4096))
    runtime = types.SimpleNamespace(
        context=context,
        runner=runner,
        registry=registry,
    )
    client = TelegramClient("test-token", "http://127.0.0.1:9", audit=audit)
    try:
        client.get_file = lambda _fid: ("f/v1", 12)  # type: ignore[method-assign]
        client.download_file = lambda _p, _m: b"OGGDATA"  # type: ignore[method-assign]
        sent_msgs: list[str] = []
        client.send_message = (  # type: ignore[method-assign]
            lambda _cid, text, **_kw: sent_msgs.append(text)
        )
        rec = _FakeRecognizer(["Steam aç"])
        channel = TelegramChannel(
            client,
            1,
            audit=audit,
            approval_timeout=5.0,
            max_message_age=60,
            poll_timeout=1,
        )
        channel.attach(runtime)  # type: ignore[arg-type]
        channel.audio_decoder = lambda _data, _sec: PCM
        channel.recognizer_factory = lambda _rt: _async_val(rec)  # type: ignore[assignment]
        msg = {
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1},
            "date": int(time.time()),
            "voice": {"file_id": "v1", "duration": 2, "file_size": 7},
        }
        await channel._handle_voice(runtime, msg)  # type: ignore[arg-type]
    finally:
        client.close()
    sent = fake_provider.seen_messages[0][-1]
    if enabled:
        assert sent.audio_wav is not None and sent.audio_wav.startswith(b"RIFF")
    else:
        assert sent.audio_wav is None


async def _async_val(val: Any) -> Any:
    return val


def test_settings_panel_direct_audio_input_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    from artemis.channels.desktop.settings_io import save_choices
    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.config.settings import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text("workspace: .\nvoice:\n  input_device: null\n", encoding="utf-8")
    panel = SettingsPanel(cfg)
    try:
        assert not panel.direct_audio_input.isChecked()
        panel.direct_audio_input.setChecked(True)
        assert panel._dirty and panel.has_unsaved
        assert panel.values()[("voice", "direct_audio_input")] is True
        assert save_choices(cfg, None, values=panel.values())
        assert load_settings(cfg).voice.direct_audio_input is True
        panel.refresh()
        assert panel.direct_audio_input.isChecked()
    finally:
        panel.deleteLater()
        app.processEvents()
