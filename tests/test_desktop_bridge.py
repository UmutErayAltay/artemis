"""Qt'siz masaüstü köprüsünün onay ve tek tur sözleşmesi."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from jarvis.brain.fake import ScriptedProvider
from jarvis.brain.models import Completion, ToolCall
from jarvis.channels.desktop.bridge import (
    Activity,
    ActivityGap,
    Approval,
    DesktopBridge,
    Failed,
    Finished,
    State,
    VoiceCommand,
)
from jarvis.channels.runtime import AuditObserver, Runtime, create_runtime
from jarvis.config.settings import ModelConfig, Settings
from jarvis.safety.audit import AuditLog
from jarvis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from jarvis.tools.registry import ToolRegistry


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class ExternalRead(BaseTool):
    name = "external_read"
    description = "Geçici dosyadan örnek veri okur."
    args_model = EmptyArgs
    risk = Risk.READ_ONLY
    external = True
    label = "Harici veri okuma"
    done_label = "Harici veri okundu"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(success=True, status="ok", message="Okundu.", data={"value": "örnek"})


class ConfirmWrite(BaseTool):
    name = "confirm_write"
    description = "İzole test dosyasını yazar."
    args_model = WriteArgs
    risk = Risk.SIDE_EFFECT
    label = "Dosya yazma"
    done_label = "Dosya yazıldı"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = WriteArgs.model_validate(arguments)
        (context.workspace / args.path).write_text("yazıldı", encoding="utf-8")
        return ToolResult(success=True, status="ok", message="Yazıldı.")


_event_cache: dict[DesktopBridge, list[Any]] = {}


def _wait_for(bridge: DesktopBridge, kind: type[Any], timeout: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout
    cached = _event_cache.setdefault(bridge, [])
    for index, event in enumerate(cached):
        if isinstance(event, kind):
            return cached.pop(index)
    while time.monotonic() < deadline:
        for event in bridge.poll_events():
            if isinstance(event, kind):
                return event
            cached.append(event)
        time.sleep(0.005)
    raise AssertionError(f"{kind.__name__} olayı gelmedi")


def _fixture(
    tmp_path: Path,
    responses: list[Completion],
    *,
    max_seconds: float = 1.0,
    provider_override: Any = None,
    voice_factory: Any = None,
) -> DesktopBridge:
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        primary_model="fake",
        models={
            "fake": ModelConfig(
                provider="ollama",
                model="fake",
                input_per_million=Decimal(0),
                output_per_million=Decimal(0),
            )
        },
        max_seconds=max_seconds,
    )
    registry = ToolRegistry()
    registry.register(ExternalRead)
    registry.register(ConfirmWrite)
    provider = ScriptedProvider(responses)

    def factory(
        _config: Path, approve: Any, on_record: AuditObserver
    ) -> Runtime:
        return create_runtime(
            _config,
            approve,
            settings=settings,
            provider=provider_override if provider_override is not None else provider,
            registry=registry,
            audit=AuditLog(settings.audit_path, on_record),
        )

    bridge = DesktopBridge(tmp_path / "unused.yaml", factory, voice_factory=voice_factory)
    assert _wait_for(bridge, State).state == "ready"
    return bridge


def _collect_until(bridge: DesktopBridge, done: Any, timeout: float = 3.0) -> list[Any]:
    """Olayları sırayla toplar; ``done(olaylar)`` doğru olunca döner."""
    cached = _event_cache.setdefault(bridge, [])
    collected = list(cached)
    cached.clear()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in bridge.poll_events():
            if done(collected):
                # Koşul sağlandıktan sonra aynı okumada gelen olaylar (ör. hemen ardından
                # gelen Approval) kaybolmasın; sonraki _wait_for önbellekten bulur.
                cached.append(event)
            else:
                collected.append(event)
        if done(collected):
            return collected
        time.sleep(0.005)
    raise AssertionError("beklenen olay zamanında gelmedi")


def _has_state(events: list[Any], state: str, request_id: str | None = None) -> bool:
    return any(
        isinstance(event, State)
        and event.state == state
        and (request_id is None or event.request_id == request_id)
        for event in events
    )


class FakeVoice:
    """Mikrofonsuz sahte denetleyici; komutları köprünün run_command'ına yollar."""

    def __init__(self, run_command: Any, on_state: Any, first_command: str | None) -> None:
        self._run_command = run_command
        self._on_state = on_state
        self._first_command = first_command
        self._pending: deque[str] = deque()
        self._stopped = threading.Event()
        self.results: list[Any] = []
        # Köprünün duraklatma ve devam çağrılarının sırası; onay bu sırayı doğrular.
        self.log: list[str] = []
        self.closed = False

    def send(self, text: str) -> None:
        self._pending.append(text)

    async def run(self) -> None:
        self._on_state("listening")
        if self._first_command is not None:
            await self._handle(self._first_command)
        while not self._stopped.is_set():
            if self._pending:
                await self._handle(self._pending.popleft())
            else:
                await asyncio.sleep(0.005)

    async def _handle(self, text: str) -> None:
        self._on_state("thinking")
        self.results.append(await self._run_command(text))
        self._on_state("listening")

    def request_stop(self) -> None:
        self._stopped.set()

    def pause_for_approval(self) -> None:
        self.log.append("pause")

    def resume_after_approval(self) -> None:
        self.log.append("resume")

    async def close(self) -> None:
        self.closed = True


def _voice_factory(created: list[FakeVoice], first_command: str | None = None) -> Any:
    async def factory(runtime: Any, run_command: Any, on_state: Any) -> FakeVoice:
        fake = FakeVoice(run_command, on_state, first_command)
        created.append(fake)
        return fake

    return factory


def _tool_scenario() -> list[Completion]:
    return [
        Completion(
            tool_calls=[
                ToolCall(id="read", name="external_read", arguments={}),
                ToolCall(
                    id="write",
                    name="confirm_write",
                    arguments={"path": "target.txt"},
                ),
            ]
        ),
        Completion(content="Tamamlandı."),
    ]


@pytest.mark.parametrize("approved", [False, True])
def test_bridge_uses_gate_taint_and_exact_validated_arguments(tmp_path, approved):
    bridge = _fixture(tmp_path, _tool_scenario())
    try:
        request_id = bridge.submit("oku ve yaz")
        assert request_id is not None
        assert bridge.submit("ikinci tur") is None
        prompt = _wait_for(bridge, Approval)
        assert prompt.request_id == request_id
        assert prompt.tool_name == "confirm_write"
        assert prompt.arguments == {"path": "target.txt"}
        assert "dış bir içerik okunduktan sonra" in prompt.notice
        assert bridge.decide(
            prompt.request_id, prompt.turn_id, prompt.approval_id, approved
        )
        result = _wait_for(bridge, Finished).result
        assert (tmp_path / "target.txt").exists() is approved
        if approved:
            assert result.stop_reason == "completed"
        else:
            assert result.stop_reason in {"completed", "denied_repeat"}
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_cancel_during_approval_invalidates_future_and_stale_decision(tmp_path):
    bridge = _fixture(tmp_path, _tool_scenario())
    try:
        request_id = bridge.submit("oku ve yaz")
        assert request_id is not None
        prompt = _wait_for(bridge, Approval)
        assert bridge.cancel(request_id)
        assert not bridge.decide(
            prompt.request_id, prompt.turn_id, prompt.approval_id, True
        )
        assert _wait_for(bridge, Finished).result.stop_reason == "cancelled"
        assert not (tmp_path / "target.txt").exists()
        assert bridge.decide(
            prompt.request_id, prompt.turn_id, prompt.approval_id, True
        ) is False
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_immediate_cancel_before_worker_starts_rejects_turn(tmp_path):
    bridge = _fixture(tmp_path, _tool_scenario())
    entered = threading.Event()
    release = threading.Event()

    def block_worker() -> None:
        entered.set()
        release.wait(2)

    try:
        assert bridge._loop is not None
        bridge._loop.call_soon_threadsafe(block_worker)
        assert entered.wait(2)
        request_id = bridge.submit("oku ve yaz")
        assert request_id is not None
        assert bridge.cancel(request_id)
        release.set()
        deadline = time.monotonic() + 2
        cancelled = False
        while time.monotonic() < deadline and not cancelled:
            cancelled = any(
                isinstance(event, State)
                and event.state == "cancelled"
                and event.request_id == request_id
                for event in bridge.poll_events()
            )
            time.sleep(0.005)
        assert cancelled
        assert not (tmp_path / "target.txt").exists()
    finally:
        release.set()
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_shutdown_before_queued_start_still_stops_worker(tmp_path):
    bridge = _fixture(tmp_path, _tool_scenario())
    entered = threading.Event()
    release = threading.Event()

    def block_worker() -> None:
        entered.set()
        release.wait(2)

    try:
        assert bridge._loop is not None
        bridge._loop.call_soon_threadsafe(block_worker)
        assert entered.wait(2)
        assert bridge.submit("oku ve yaz") is not None
        bridge.shutdown()
        release.set()
        assert bridge._stopped.wait(2)
        assert not (tmp_path / "target.txt").exists()
    finally:
        release.set()
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_shutdown_waits_for_cancelled_provider_cleanup(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class IgnoresTaskCancellation:
        async def complete(self, *_args: Any, **_kwargs: Any) -> Completion:
            started.set()
            try:
                await asyncio.to_thread(release.wait)
            except asyncio.CancelledError:
                await asyncio.to_thread(release.wait)
            return Completion(content="Temizlik tamamlandı.")

    bridge = _fixture(
        tmp_path,
        [],
        provider_override=IgnoresTaskCancellation(),
    )
    try:
        request_id = bridge.submit("bekle")
        assert request_id is not None
        assert started.wait(2)
        assert bridge.cancel(request_id)
        state = _wait_for(bridge, State, timeout=2)
        while state.state != "cleanup_pending":
            state = _wait_for(bridge, State, timeout=2)
        assert bridge.submit("temizlik bitmeden çalışma") is None

        bridge.shutdown()
        assert not bridge._stopped.wait(0.1)
        release.set()
        assert bridge._stopped.wait(2)
        assert bridge.submit("işçi durdu") is None
    finally:
        release.set()
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_approval_timeout_fails_closed_and_cleanup_completes(tmp_path):
    bridge = _fixture(tmp_path, _tool_scenario(), max_seconds=0.5)
    try:
        request_id = bridge.submit("oku ve yaz")
        assert request_id is not None
        prompt = _wait_for(bridge, Approval)
        result = _wait_for(bridge, Finished, timeout=2.0).result
        assert result.stop_reason in {"timeout", "denied_repeat"}
        assert not (tmp_path / "target.txt").exists()
        assert not bridge.decide(
            prompt.request_id, prompt.turn_id, prompt.approval_id, True
        )
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_shutdown_rejects_pending_approval_before_worker_stops(tmp_path):
    bridge = _fixture(tmp_path, _tool_scenario())
    request_id = bridge.submit("oku ve yaz")
    assert request_id is not None
    prompt = _wait_for(bridge, Approval)
    bridge.shutdown()
    assert bridge._stopped.wait(2)
    assert not bridge.decide(prompt.request_id, prompt.turn_id, prompt.approval_id, True)
    assert not (tmp_path / "target.txt").exists()


def test_factory_failure_is_sanitized_and_worker_stops(tmp_path):
    def broken_factory(_config: Path, _approve: Any, _observer: AuditObserver) -> Runtime:
        raise ValueError("synthetic-secret must not escape")

    bridge = DesktopBridge(tmp_path / "bad.yaml", broken_factory)
    failure = _wait_for(bridge, Failed)
    assert failure.error_type == "ValueError"
    assert "synthetic-secret" not in failure.message
    assert bridge._stopped.wait(2)
    assert bridge.submit("yaz") is None
    bridge.shutdown()


def test_runtime_failure_is_sanitized_and_worker_recovers(tmp_path):
    class BrokenProvider:
        async def complete(self, *_args: Any, **_kwargs: Any) -> Completion:
            raise KeyError("synthetic-secret")

    bridge = _fixture(tmp_path, [], provider_override=BrokenProvider())
    try:
        request_id = bridge.submit("sor")
        assert request_id is not None
        failure = _wait_for(bridge, Failed)
        assert failure.request_id == request_id
        assert failure.error_type == "KeyError"
        assert "synthetic-secret" not in failure.message
        assert not bridge.stopped
        second_id = bridge.submit("yeniden dene")
        assert second_id is not None
        second_failure = _wait_for(bridge, Failed)
        assert second_failure.request_id == second_id
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_audit_view_is_redacted_bounded_and_reports_gaps(tmp_path, monkeypatch):
    bridge = _fixture(tmp_path, [Completion(content="bitti")])
    try:
        monkeypatch.setenv("JARVIS_TEST_SECRET", "synthetic-secret")
        assert bridge._runtime is not None
        for index in range(300):
            bridge._runtime.audit.emit("inspection", value=f"synthetic-secret-{index}")
        events = bridge.poll_events(limit=512)
        activities = [event for event in events if isinstance(event, Activity)]
        gaps = [event for event in events if isinstance(event, ActivityGap)]
        assert len(activities) <= 256
        assert gaps and gaps[0].dropped_count > 0
        stored = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert "synthetic-secret" not in json.dumps(stored)
        assert all("synthetic-secret" not in json.dumps(event.fields) for event in activities)
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_large_audit_summary_preserves_routing_and_tool_result_metadata(tmp_path):
    bridge = _fixture(tmp_path, [Completion(content="ready")])
    try:
        bridge._on_record(
            {
                "event": "tool_result",
                "turn_id": "old-turn-id",
                "tool": "demo_write_memory",
                "result": {
                    "status": "sent",
                    "success": False,
                    "message": "Komut gönderildi; sonuç doğrulanmadı.",
                    "data": {"large": "x" * 20_000},
                },
            }
        )
        events = bridge.poll_events()
        activity = next(event for event in events if isinstance(event, Activity))
        assert activity.request_id is None
        assert activity.turn_id == "old-turn-id"
        assert activity.fields["tool"] == "demo_write_memory"
        assert activity.fields["result"] == {
            "status": "sent",
            "success": False,
            "message": "Komut gönderildi; sonuç doğrulanmadı.",
        }
        assert activity.fields["summary"]
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_reset_conversation_refused_while_active_and_announced_when_idle(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingProvider:
        async def complete(self, *_args: Any, **_kwargs: Any) -> Completion:
            entered.set()
            await asyncio.to_thread(release.wait, 2)
            return Completion(content="bitti")

    bridge = _fixture(tmp_path, [], provider_override=BlockingProvider())
    try:
        request_id = bridge.submit("bekle")
        assert request_id is not None
        assert entered.wait(2)
        assert bridge.reset_conversation() is False
        release.set()
        # Bitiş "ready" durumuyla duyurulur; sıfırlama yalnız boşta kabul edilir.
        state = _wait_for(bridge, State, timeout=2)
        while not (state.state == "ready" and state.request_id == request_id):
            state = _wait_for(bridge, State, timeout=2)
        assert bridge.reset_conversation() is True
        state = _wait_for(bridge, State, timeout=2)
        while state.state != "context_reset":
            state = _wait_for(bridge, State, timeout=2)
    finally:
        release.set()
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_voice_turn_shares_request_id_and_forwards_states(tmp_path):
    created: list[FakeVoice] = []
    bridge = _fixture(
        tmp_path,
        [Completion(content="Dosyalar listelendi.")],
        voice_factory=_voice_factory(created, first_command="dosyaları listele"),
    )
    try:
        assert bridge.start_voice()
        events = _collect_until(bridge, lambda items: _has_state(items, "ready") and any(
            isinstance(item, Finished) for item in items
        ))
        voice_states = [
            event.message for event in events if isinstance(event, State) and event.state == "voice"
        ]
        assert voice_states[:2] == ["preparing", "listening"]
        command = next(event for event in events if isinstance(event, VoiceCommand))
        assert command.text == "dosyaları listele"
        finished = next(event for event in events if isinstance(event, Finished))
        assert finished.request_id == command.request_id
        assert finished.result.text == "Dosyalar listelendi."
        assert created[0].results[0].text == "Dosyalar listelendi."
        assert bridge.voice_active

        assert bridge.stop_voice()
        _collect_until(bridge, lambda items: _has_state_message(items, "voice", "off"))
        assert not bridge.voice_active
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def _has_state_message(events: list[Any], state: str, message: str) -> bool:
    return any(
        isinstance(event, State) and event.state == state and event.message == message
        for event in events
    )


@pytest.mark.parametrize("approved", [False, True])
def test_voice_approval_goes_through_broker_and_pauses_the_microphone(tmp_path, approved):
    created: list[FakeVoice] = []
    bridge = _fixture(
        tmp_path,
        _tool_scenario(),
        voice_factory=_voice_factory(created, first_command="dosyayı yaz"),
    )
    try:
        assert bridge.start_voice()
        events = _collect_until(bridge, lambda items: any(
            isinstance(item, VoiceCommand) for item in items
        ))
        command = next(event for event in events if isinstance(event, VoiceCommand))
        fake = created[0]
        prompt = _wait_for(bridge, Approval)
        assert prompt.request_id == command.request_id
        assert prompt.tool_name == "confirm_write"
        # Onay penceresi açılmadan mikrofon duraklatılmış olmalı; karar gelene dek devam yok.
        assert fake.log == ["pause"]
        assert bridge.decide(prompt.request_id, prompt.turn_id, prompt.approval_id, approved)
        finished = _wait_for(bridge, Finished).result
        assert fake.log == ["pause", "resume"]
        assert (tmp_path / "target.txt").exists() is approved
        if not approved:
            assert finished.stop_reason in {"completed", "denied_repeat"}
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_voice_command_while_typed_request_is_busy_and_skips_runner(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingProvider:
        async def complete(self, *_args: Any, **_kwargs: Any) -> Completion:
            calls.append("provider")
            entered.set()
            await asyncio.to_thread(release.wait, 2)
            return Completion(content="bitti")

    created: list[FakeVoice] = []
    bridge = _fixture(
        tmp_path, [], provider_override=BlockingProvider(), voice_factory=_voice_factory(created)
    )
    try:
        assert bridge.start_voice()
        _collect_until(bridge, lambda items: _has_state_message(items, "voice", "listening"))
        typed_id = bridge.submit("yazılı istek")
        assert typed_id is not None
        assert entered.wait(2)
        created[0].send("dosyaları listele")
        deadline = time.monotonic() + 2
        while not created[0].results and time.monotonic() < deadline:
            time.sleep(0.005)
        assert created[0].results[0].stop_reason == "busy"
        # Meşgul sesli komut çalıştırıcıya hiç gitmez ve kendi isteğini açmaz.
        assert calls == ["provider"]
        seen = _collect_until(bridge, lambda items: _has_state(items, "ready", typed_id))
        assert not any(isinstance(event, VoiceCommand) for event in seen)
    finally:
        release.set()
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_voice_factory_failure_reports_error_and_typed_requests_still_work(tmp_path):
    async def broken(_runtime: Any, _run: Any, _on_state: Any) -> None:
        raise OSError("synthetic-device-missing")

    bridge = _fixture(
        tmp_path, [Completion(content="Tamam.")], voice_factory=broken
    )
    try:
        assert bridge.start_voice()
        events = _collect_until(bridge, lambda items: _has_state_message(items, "voice", "off"))
        errors = [
            event for event in events if isinstance(event, State) and event.state == "voice_error"
        ]
        assert len(errors) == 1
        assert "Ses açılamadı" in errors[0].message
        assert "synthetic-device-missing" not in errors[0].message
        # Hata iletisi "off"tan önce gelir; ekran kapanışı hatadan sonra görür.
        assert events.index(errors[0]) < next(
            index
            for index, event in enumerate(events)
            if isinstance(event, State) and event.state == "voice" and event.message == "off"
        )
        assert not bridge.voice_active
        assert bridge.submit("merhaba") is not None
        result = _wait_for(bridge, Finished, timeout=2).result
        assert result.text == "Tamam."
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_approval_carries_tool_label_and_labels_are_a_copy(tmp_path):
    bridge = _fixture(tmp_path, _tool_scenario())
    try:
        assert bridge.labels() == {
            "external_read": ("Harici veri okuma", "Harici veri okundu"),
            "confirm_write": ("Dosya yazma", "Dosya yazıldı"),
        }
        request_id = bridge.submit("oku ve yaz")
        assert request_id is not None
        prompt = _wait_for(bridge, Approval)
        assert prompt.tool_label == "Dosya yazma"
        assert prompt.tool_name == "confirm_write"
        assert bridge.decide(prompt.request_id, prompt.turn_id, prompt.approval_id, False)
        _wait_for(bridge, Finished)
        copied = bridge.labels()
        copied.clear()
        assert bridge.labels()["confirm_write"] == ("Dosya yazma", "Dosya yazıldı")
    finally:
        bridge.shutdown()
        assert bridge._stopped.wait(2)


def test_ignore_reset_on_close_only_swallows_proactor_reset() -> None:
    from unittest.mock import MagicMock

    from jarvis.channels.desktop.bridge import _ignore_reset_on_close

    loop = MagicMock()
    handle = "<Handle _ProactorBasePipeTransport._call_connection_lost(SSLError(1))>"
    _ignore_reset_on_close(loop, {"exception": ConnectionResetError(), "handle": handle})
    loop.default_exception_handler.assert_not_called()
    _ignore_reset_on_close(loop, {"exception": ConnectionResetError(), "handle": "other"})
    _ignore_reset_on_close(loop, {"exception": ValueError(), "handle": handle})
    assert loop.default_exception_handler.call_count == 2
