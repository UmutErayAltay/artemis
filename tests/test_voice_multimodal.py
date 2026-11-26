"""Ses + görüntü çok kipli akış (ADR 0085) uçtan uca ve güvenlik kapısı testleri."""

from __future__ import annotations

import asyncio
import json
import time
import types
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from artemis.brain.agent_loop import AgentLoop, RunResult
from artemis.brain.errors import MediaRejectedError, ProviderError
from artemis.brain.models import Completion, Message, ToolCall
from artemis.brain.router import ProviderRouter
from artemis.brain.streaming import TextStream
from artemis.channels.desktop.bridge import Approval, DesktopBridge, State
from artemis.channels.runner import CommandRunner
from artemis.channels.runtime import Runtime, create_runtime
from artemis.channels.voice.core import (
    AudioFrame,
    AudioSegment,
    PCMChunk,
    VoiceController,
    pcm_to_wav,
)
from artemis.channels.voice.factory import build_voice_controller
from artemis.channels.voice.multimodal import (
    capture_active_screen_png,
    is_voice_screen_context_allowed,
)
from artemis.config.settings import (
    EndpointConfig,
    ModelConfig,
    PerceptionConfig,
    Settings,
    VoiceConfig,
)
from artemis.config.work_profile import effective_settings_for_profile
from artemis.safety.audit import AuditLog
from artemis.safety.gate import TAINT_NOTICE, SafetyGate
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import ToolRegistry

PCM = b"\x01\x00\x02\x00\x03\x00\x04\x00" * 10
WAV = pcm_to_wav(PCM, 16000)
PNG = b"\x89PNG\r\n\x1a\n-sahte-ekran"


class _FakeCaptureBackend:
    def __init__(self, width: int = 8, height: int = 4, *, fail: bool = False) -> None:
        self.width = width
        self.height = height
        self.fail = fail
        self.calls: list[tuple[int, int, int, int]] = []

    def monitors(self) -> list[tuple[int, int, int, int]]:
        if self.fail:
            raise OSError("ekran yok")
        return [(0, 0, self.width, self.height)]

    def capture(self, left: int, top: int, width: int, height: int) -> tuple[bytes, int, int]:
        self.calls.append((left, top, width, height))
        # BGRA piksel tamponu (yalnız bellek içi)
        pixel = b"\x10\x20\x30\xff"
        return pixel * (width * height), width, height


class _CaptureProvider:
    def __init__(self, responses: list[Completion | ProviderError]) -> None:
        self.responses = list(responses)
        self.seen_messages: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[Any],
        *,
        timeout: float,
        stream: TextStream | None = None,
    ) -> Completion:
        self.seen_messages.append(list(messages))
        item = self.responses.pop(0)
        if isinstance(item, ProviderError):
            if stream is not None and "kısmi" in str(item):
                stream.text("yarım metin")
            raise item
        if stream is not None and item.content:
            stream.text(item.content)
        return item


class _SideEffectArgs(BaseModel):
    target: str = "not.txt"


class _SideEffectTool(BaseTool):
    name = "demo_write"
    description = "Demo yan etkili araç"
    risk = Risk.SIDE_EFFECT
    args_model = _SideEffectArgs
    executed: list[str]

    def __init__(self) -> None:
        self.executed = []

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = _SideEffectArgs.model_validate(arguments)
        self.executed.append(args.target)
        return ToolResult(success=True, status="ok", message=f"yazıldı: {args.target}")


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

    def prepare(self) -> None:
        pass

    async def transcribe(self, _pcm: bytes) -> str:
        return next(self.texts, "")


class _FakeTTS:
    async def stream(self, _text: str, _cancelled: Any) -> AsyncGenerator[PCMChunk]:
        yield PCMChunk(b"\x01\x00")

    async def close(self) -> None:
        pass


class _FakeSink:
    async def play(self, chunks: Any, cancelled: Any, on_first_pcm: Any) -> None:
        async for _chunk in chunks:
            if cancelled.is_set():
                return
            on_first_pcm()

    async def close(self) -> None:
        pass


def _settings(
    tmp_path: Path,
    *,
    multimodal: bool = True,
    direct_audio: bool = True,
    allow_cloud_screen: bool = True,
    work_profile: str = "work",
    primary_provider: str = "openai_compatible",
) -> Settings:
    models: dict[str, ModelConfig] = {
        "gemini": ModelConfig(
            provider="openai_compatible",
            endpoint="gem",
            model="gemini-3.1-flash-lite",
            input_per_million=Decimal("0"),
            output_per_million=Decimal("0"),
        ),
        "cli": ModelConfig(
            provider="claude_cli",
            model="sonnet",
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
        "claude_cli": "cli",
        "ollama": "local",
    }[primary_provider]
    return Settings(
        workspace=tmp_path / "ws",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        work_profile=work_profile,  # type: ignore[arg-type]
        primary_model=primary,
        endpoints={"gem": EndpointConfig(base_url="http://127.0.0.1:9999/v1", api_key=False)},
        voice=VoiceConfig(
            direct_audio_input=direct_audio,
            multimodal_screen_context=multimodal,
        ),
        perception=PerceptionConfig(allow_cloud_screen=allow_cloud_screen),
        models=models,
    )


def test_multimodal_config_defaults_gates_and_private_profile(tmp_path: Path) -> None:
    assert VoiceConfig().multimodal_screen_context is False

    open_cfg = _settings(tmp_path, multimodal=True, direct_audio=True, allow_cloud_screen=True)
    assert is_voice_screen_context_allowed(open_cfg) is True

    for kwargs in (
        {"multimodal": False},
        {"direct_audio": False},
        {"allow_cloud_screen": False},
        {"work_profile": "private"},
    ):
        gated = _settings(tmp_path, **kwargs)  # type: ignore[arg-type]
        assert is_voice_screen_context_allowed(gated) is False

    private_effective = effective_settings_for_profile(
        _settings(tmp_path, work_profile="private")
    )
    assert private_effective.voice.multimodal_screen_context is False
    assert private_effective.voice.direct_audio_input is False
    assert private_effective.perception.allow_cloud_screen is False


def test_capture_active_screen_png_in_memory_zero_disk_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Diske herhangi bir yazma denemesini yakala
    disk_writes: list[str] = []
    orig_write_bytes = Path.write_bytes
    orig_write_text = Path.write_text

    def spy_write_bytes(self: Path, data: bytes) -> int:
        disk_writes.append(str(self))
        return orig_write_bytes(self, data)

    def spy_write_text(self: Path, data: str, *a: Any, **kw: Any) -> int:
        disk_writes.append(str(self))
        return orig_write_text(self, data, *a, **kw)

    monkeypatch.setattr(Path, "write_bytes", spy_write_bytes)
    monkeypatch.setattr(Path, "write_text", spy_write_text)

    backend = _FakeCaptureBackend(width=8, height=4)
    png_bytes = capture_active_screen_png(max_width=4, backend=backend)
    assert png_bytes is not None
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert backend.calls == [(0, 0, 8, 4)]
    assert disk_writes == []
    assert list(tmp_path.iterdir()) == []

    # Windows dışı platformda veya yakalama hatasında None döner
    assert capture_active_screen_png(max_width=4, backend=backend, platform="linux") is None
    assert capture_active_screen_png(max_width=4, backend=None, platform="linux") is None
    failing = _FakeCaptureBackend(fail=True)
    assert capture_active_screen_png(max_width=4, backend=failing) is None


async def test_voice_controller_captures_screen_only_when_gated_and_legacy_safe() -> None:
    calls: list[tuple[str, bytes | None, bytes | None]] = []
    captures = 0

    def fake_capture() -> bytes | None:
        nonlocal captures
        captures += 1
        return PNG

    async def run_cmd(
        text: str,
        *,
        audio_wav: bytes | None = None,
        image_png: bytes | None = None,
        **_kw: Any,
    ) -> RunResult:
        calls.append((text, audio_wav, image_png))
        return RunResult(text="oldu", stop_reason="completed", turn_id="t1")

    profile = ["work"]
    ctrl = VoiceController(
        types.SimpleNamespace(run=run_cmd, cancel_current=lambda: True),  # type: ignore[arg-type]
        VoiceConfig(direct_audio_input=True, multimodal_screen_context=True),
        _FakeMic(),
        _FakeVAD(),
        _FakeRecognizer(["Artemis bunu düzelt", "Artemis bunu da düzelt"]),
        _FakeTTS(),
        _FakeSink(),
        run_command=run_cmd,
        profile_getter=lambda: profile[0],
        capture_screen=fake_capture,
    )
    try:
        await ctrl._handle_utterance(AudioSegment(PCM, 0.0, 1.0))
        await ctrl._settle_active()
        assert len(calls) == 1
        assert calls[0][0] == "bunu düzelt"
        assert calls[0][1] is not None and calls[0][1].startswith(b"RIFF")
        assert calls[0][2] == PNG
        assert captures == 1

        # Canlı private profile geçişinde ekran yakalanmaz ve doğrudan ses kapanır
        profile[0] = "private"
        await ctrl._handle_utterance(AudioSegment(PCM, 1.0, 2.0))
        await ctrl._settle_active()
        assert len(calls) == 2
        assert calls[1][1] is None
        assert calls[1][2] is None
        assert captures == 1
    finally:
        await ctrl.close()

    # Eski imza (image_png/audio_wav kabul etmeyen run_command): TypeError vermez
    legacy_calls: list[str] = []

    async def legacy_cmd(text: str) -> RunResult:
        legacy_calls.append(text)
        return RunResult(text="tamam", stop_reason="completed", turn_id="t2")

    ctrl2 = VoiceController(
        types.SimpleNamespace(run=legacy_cmd, cancel_current=lambda: True),  # type: ignore[arg-type]
        VoiceConfig(direct_audio_input=True, multimodal_screen_context=True),
        _FakeMic(),
        _FakeVAD(),
        _FakeRecognizer(["Artemis saat kaç"]),
        _FakeTTS(),
        _FakeSink(),
        run_command=legacy_cmd,
        capture_screen=fake_capture,
    )
    try:
        await ctrl2._handle_utterance(AudioSegment(PCM, 0.0, 1.0))
        await ctrl2._settle_active()
        assert legacy_calls == ["saat kaç"]
    finally:
        await ctrl2.close()


async def test_factory_capture_screen_closure_respects_live_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, multimodal=True, direct_audio=True, allow_cloud_screen=True)
    provider = _CaptureProvider([Completion(content="tamam")])
    audit = AuditLog(settings.audit_path)
    runtime = create_runtime(
        tmp_path / "cfg.yaml",
        lambda *_: True,
        settings=settings,
        provider=provider,
        registry=ToolRegistry(),
        audit=audit,
    )

    captured_calls: list[int] = []
    monkeypatch.setattr("artemis.channels.voice.factory._build_denoiser", _async_none)
    monkeypatch.setattr(
        "artemis.channels.voice.factory.build_vad", lambda *_a, **_k: _FakeVAD()
    )
    monkeypatch.setattr(
        "artemis.channels.voice.factory.build_turn_detector", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "artemis.channels.voice.factory.build_wake_spotter", _async_none
    )
    monkeypatch.setattr(
        "artemis.channels.voice.factory._build_live_transcriber", _async_none
    )
    monkeypatch.setattr(
        "artemis.channels.voice.factory.FasterWhisperRecognizer",
        lambda *_a, **_k: _FakeRecognizer(["Artemis ekranı oku"]),
    )
    monkeypatch.setattr(
        "artemis.channels.voice.factory._build_synthesizer",
        _async_synth,
    )
    monkeypatch.setattr(
        "artemis.channels.voice.factory.capture_active_screen_png",
        lambda max_width=1280: captured_calls.append(max_width) or PNG,
    )

    controller = await build_voice_controller(
        runtime,
        runtime.runner.run,
        lambda _s: None,
        microphone=_FakeMic(),
        sink=_FakeSink(),
    )
    try:
        assert controller._capture_screen is not None
        assert controller._capture_screen() == PNG
        assert len(captured_calls) == 1

        # Canlı allow_cloud_screen kapanınca yakalama yapılmaz
        runtime.context.settings = settings.model_copy(
            update={"perception": PerceptionConfig(allow_cloud_screen=False)}
        )
        assert controller._capture_screen() is None
        assert len(captured_calls) == 1
    finally:
        await controller.close()


async def _async_none(*_a: Any, **_k: Any) -> None:
    return None


async def _async_synth(*_a: Any, **_k: Any) -> tuple[_FakeTTS, None]:
    return _FakeTTS(), None


async def test_agent_loop_privacy_gate_taint_framing_and_safety_gate(tmp_path: Path) -> None:
    # 1) İzin kapalıyken image_png düşer, privacy_decision(cloud_allowed=False) yazılır
    blocked_settings = _settings(tmp_path / "blocked", allow_cloud_screen=False)
    blocked_provider = _CaptureProvider([Completion(content="yalnız ses")])
    blocked_audit = AuditLog(blocked_settings.audit_path)
    blocked_ctx = ToolContext(blocked_settings.workspace, blocked_settings, blocked_audit)
    blocked_loop = AgentLoop(
        blocked_provider, SafetyGate(ToolRegistry()), blocked_ctx, lambda *_: True
    )
    res_blocked = await blocked_loop.run("bunu oku", audio_wav=WAV, image_png=PNG)
    assert res_blocked.text == "yalnız ses"
    assert blocked_provider.seen_messages[0][-1].image_png is None
    assert blocked_provider.seen_messages[0][-1].audio_wav == WAV
    assert res_blocked.record is not None and res_blocked.record.tainted is False
    blocked_audits = [
        json.loads(line)
        for line in blocked_settings.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        ev["event"] == "privacy_decision"
        and ev["tool"] == "voice_screen_context"
        and ev["content_kind"] == "screen"
        and ev["cloud_allowed"] is False
        for ev in blocked_audits
    )

    # 2) İzinler açıkken image_png + audio_wav taşınır, VERİDİR eklenir, tur lekelenir ve
    # SafetyGate yan etkili araçta ekrandan onay ister
    open_settings = _settings(tmp_path / "open", allow_cloud_screen=True)
    registry = ToolRegistry()
    registry.register(_SideEffectTool)
    tool = registry.get("demo_write")
    assert isinstance(tool, _SideEffectTool)
    approvals: list[tuple[str, dict[str, Any]]] = []

    def approve(name: str, args: dict[str, Any]) -> bool:
        approvals.append((name, args))
        return False  # Reddedilirse araç çalışmamalı

    open_provider = _CaptureProvider(
        [
            Completion(
                content="",
                tool_calls=[ToolCall(id="c1", name="demo_write", arguments={"target": "a.txt"})],
            ),
            Completion(content="Onay verilmediği için yazmadım."),
            Completion(content="Temiz ikinci tur."),
        ]
    )
    open_audit = AuditLog(open_settings.audit_path)
    open_ctx = ToolContext(open_settings.workspace, open_settings, open_audit)
    open_loop = AgentLoop(open_provider, SafetyGate(registry), open_ctx, approve)
    runner = CommandRunner(open_loop)

    res1 = await runner.run("dosyaya yaz", audio_wav=WAV, image_png=PNG)
    assert approvals == [("demo_write", {"target": "a.txt"})]
    assert tool.executed == []
    assert res1.record is not None and res1.record.tainted is True
    first_user_msg = open_provider.seen_messages[0][-1]
    assert first_user_msg.audio_wav == WAV
    assert first_user_msg.image_png == PNG
    assert "VERİDİR" in first_user_msg.content
    assert "Doğrudan ses kaydı eklendi" in first_user_msg.content

    open_audits = [
        json.loads(line)
        for line in open_settings.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        ev["event"] == "privacy_decision"
        and ev["tool"] == "voice_screen_context"
        and ev["cloud_allowed"] is True
        for ev in open_audits
    )
    assert "sahte-ekran" not in open_settings.audit_path.read_text(encoding="utf-8")

    # Sonraki tura image_png/audio_wav taşınmaz
    await runner.run("ikinci soru")
    assert all(m.image_png is None and m.audio_wav is None for m in open_provider.seen_messages[2])


def test_desktop_bridge_end_to_end_voice_multimodal_and_screen_approval(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    registry = ToolRegistry()
    registry.register(_SideEffectTool)
    tool = registry.get("demo_write")
    assert isinstance(tool, _SideEffectTool)
    provider = _CaptureProvider(
        [
            Completion(
                content="",
                tool_calls=[ToolCall(id="c1", name="demo_write", arguments={"target": "b.txt"})],
            ),
            Completion(content="Ekrandaki onayla yazıldı."),
        ]
    )

    def runtime_factory(path: Path, approve: Any, on_record: Any) -> Runtime:
        return create_runtime(
            path,
            approve,
            settings=settings,
            provider=provider,
            registry=registry,
            audit=AuditLog(settings.audit_path, on_record),
        )

    voice_ready = asyncio.Event()
    controller_box: list[Any] = []

    class _VoiceDouble:
        def __init__(self, rt: Runtime, run_cmd: Any, on_state: Any) -> None:
            self.rt = rt
            self.run_cmd = run_cmd
            self.on_state = on_state
            self.on_event: Any = None
            self.sink = types.SimpleNamespace(output_info=None)
            self._stop = asyncio.Event()
            self.paused = 0
            self.resumed = 0

        async def run(self) -> None:
            self.on_state("listening")
            voice_ready.set()
            await self._stop.wait()

        def pause_for_approval(self) -> None:
            self.paused += 1

        def resume_after_approval(self) -> None:
            self.resumed += 1

        def request_stop(self) -> None:
            self._stop.set()

        async def close(self) -> None:
            self._stop.set()

    async def voice_factory(rt: Runtime, run_cmd: Any, on_state: Any) -> Any:
        double = _VoiceDouble(rt, run_cmd, on_state)
        controller_box.append(double)
        return double

    bridge = DesktopBridge(tmp_path / "unused.yaml", runtime_factory, voice_factory=voice_factory)
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if any(isinstance(e, State) and e.state == "ready" for e in bridge.poll_events()):
                break
            time.sleep(0.01)

        bridge.start_voice()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not controller_box:
            bridge.poll_events()
            time.sleep(0.01)
        assert controller_box
        double = controller_box[0]

        # Ses denetleyicisi üzerinden image_png + audio_wav ile komut gönder
        future = asyncio.run_coroutine_threadsafe(
            double.run_cmd("ekrandakini kaydet", audio_wav=WAV, image_png=PNG),
            bridge._loop,
        )
        approval_ev: Approval | None = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and approval_ev is None:
            for ev in bridge.poll_events():
                if isinstance(ev, Approval):
                    approval_ev = ev
                    break
            time.sleep(0.01)
        assert approval_ev is not None
        assert approval_ev.tool_name == "demo_write"
        assert TAINT_NOTICE in approval_ev.notice
        assert double.paused >= 1
        assert bridge.decide(
            approval_ev.request_id, approval_ev.turn_id, approval_ev.approval_id, True
        )
        res = future.result(timeout=10.0)
        assert res.text == "Ekrandaki onayla yazıldı."
        assert tool.executed == ["b.txt"]
        assert double.resumed >= 1
        assert provider.seen_messages[0][-1].image_png == PNG
        assert provider.seen_messages[0][-1].audio_wav == WAV
    finally:
        bridge.shutdown()


async def test_router_audits_has_image_and_falls_back_on_claude_cli_or_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1) cloud_transfer has_image=True ve HTTP 400 image rejection -> image_input_fallback
    settings = _settings(tmp_path / "r1")
    fake = _CaptureProvider(
        [
            MediaRejectedError(
                "Sağlayıcı medya girdisini kabul etmedi (HTTP 400).", media_kind="image"
            ),
            Completion(content="Görüntüsüz devam edildi", provider="openai_compatible", model="m"),
        ]
    )
    router = ProviderRouter(settings, AuditLog(settings.audit_path), providers={"gemini": fake})
    msg = Message(role="user", content="ekranda ne var", image_png=PNG)

    comp = await router.complete([msg], [], timeout=10)
    assert comp.content == "Görüntüsüz devam edildi"
    assert len(fake.seen_messages) == 2
    assert fake.seen_messages[0][0].image_png == PNG
    assert fake.seen_messages[1][0].image_png is None

    audits = [
        json.loads(line) for line in settings.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    cloud_ev = next(e for e in audits if e["event"] == "cloud_transfer")
    assert cloud_ev["has_image"] is True
    fb_ev = next(e for e in audits if e["event"] == "image_input_fallback")
    assert fb_ev["reason"] == "rejected_image_input"

    # 2) claude_cli sağlayıcısı image_png'yi düşürüp image_input_fallback yazar
    cli_settings = _settings(tmp_path / "r2", primary_provider="claude_cli")
    cli_fake = _CaptureProvider([Completion(content="cli metinle devam")])
    cli_router = ProviderRouter(
        cli_settings, AuditLog(cli_settings.audit_path), providers={"cli": cli_fake}
    )
    res_cli = await cli_router.complete([msg], [], timeout=10)
    assert res_cli.content == "cli metinle devam"
    assert cli_fake.seen_messages[0][0].image_png is None
    cli_audits = [
        json.loads(line)
        for line in cli_settings.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        e["event"] == "image_input_fallback" and e["reason"] == "unsupported_provider"
        for e in cli_audits
    )

    # 3) Kısmi akış yayınlandıktan sonra hata olursa tekrar denenmez (ADR 0043)
    stream_settings = _settings(tmp_path / "r3")
    stream_fake = _CaptureProvider(
        [ProviderError("kısmi akış sonrası HTTP 400")]
    )
    stream_router = ProviderRouter(
        stream_settings, AuditLog(stream_settings.audit_path), providers={"gemini": stream_fake}
    )

    class _RecStream:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        def text(self, delta: str) -> None:
            self.chunks.append(delta)

        def tool_call(self) -> None:
            pass

    rec_stream = _RecStream()
    with pytest.raises(ProviderError, match="HTTP 400"):
        await stream_router.complete([msg], [], timeout=10, stream=rec_stream)  # type: ignore[arg-type]
    assert rec_stream.chunks == ["yarım metin"]
    assert len(stream_fake.seen_messages) == 1


def test_settings_panel_multimodal_screen_context_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    from artemis.channels.desktop.settings_io import save_choices
    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.channels.desktop.widgets import HelpTip
    from artemis.config.settings import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text("workspace: .\nvoice:\n  input_device: null\n", encoding="utf-8")
    panel = SettingsPanel(cfg)
    try:
        assert not panel.multimodal_screen_context.isChecked()
        panel.multimodal_screen_context.setChecked(True)
        assert panel._dirty and panel.has_unsaved
        assert panel.values()[("voice", "multimodal_screen_context")] is True
        assert save_choices(cfg, None, values=panel.values())
        assert load_settings(cfg).voice.multimodal_screen_context is True
        panel.refresh()
        assert panel.multimodal_screen_context.isChecked()
        ses_page = panel.stack.widget(panel.section_names().index("Ses"))
        assert ses_page is not None
        assert len(ses_page.findChildren(HelpTip)) <= 6
    finally:
        panel.deleteLater()
        app.processEvents()
