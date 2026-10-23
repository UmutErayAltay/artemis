"""F2 güvenlik sınırlarının gerçek SQLite ve izole dosyalarla entegrasyon kontrolleri."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from artemis.brain.agent_loop import AgentLoop
from artemis.brain.models import Completion, ToolCall
from artemis.brain.router import ProviderRouter
from artemis.channels.runner import CommandRunner
from artemis.config.settings import ModelConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.safety.undo import UndoJournal
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.plugins.filesystem import DeleteFile, ReadFile
from artemis.tools.registry import ToolRegistry


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateEffect(BaseTool):
    name = "create_effect"
    description = "Test kökünde yan etki oluşturur."
    args_model = EmptyArgs
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        (context.workspace / "effect.txt").write_text("oluştu", encoding="utf-8")
        return ToolResult(success=True, status="ok", message="Yan etki oluşturuldu.")


def context_for(root: Path, settings: Settings | None = None) -> ToolContext:
    return ToolContext(
        workspace=root,
        settings=settings or Settings(workspace=root, state_dir=root / "state"),
        audit=AuditLog(root / "audit.jsonl"),
    )


def read_audit(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_cancel_active_turn_blocks_tool_and_next_turn_succeeds(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(CreateEffect)
    context = context_for(tmp_path)
    started = asyncio.Event()

    class Provider:
        def __init__(self) -> None:
            self.requests = 0

        async def complete(self, messages, tools, *, timeout):
            self.requests += 1
            if self.requests == 1:
                started.set()
                await asyncio.Future()
            return Completion(content="Yeni tur çalıştı.")

    provider = Provider()
    loop = AgentLoop(
        provider, SafetyGate(registry), context, lambda _name, _args: True
    )
    runner = CommandRunner(loop)
    active = asyncio.create_task(runner.run("bekleyen tur"))
    await started.wait()

    assert runner.cancel_current() is True
    cancelled = await active
    assert cancelled.stop_reason == "cancelled"
    assert not (tmp_path / "effect.txt").exists()

    next_turn = await runner.run("yeni tur")
    assert next_turn.stop_reason == "completed"
    assert next_turn.text == "Yeni tur çalıştı."
    assert provider.requests == 2
    records = read_audit(context.audit.path)
    assert [item["event"] for item in records].count("turn_end") == 2


async def test_delete_is_approved_and_journaled_before_real_tmp_effect(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("geçici içerik", encoding="utf-8")
    context = context_for(tmp_path)
    journal = UndoJournal(context.settings.state_dir)
    context.services["undo_journal"] = journal
    observed: list[str] = []

    def trash(path: Path) -> None:
        records = journal.list_records(root=tmp_path)
        observed.append(records[-1].status)
        path.unlink()

    context.services["trash"] = trash
    registry = ToolRegistry()
    registry.register(DeleteFile)
    approvals: list[tuple[str, dict[str, object]]] = []
    result = await SafetyGate(registry).dispatch(
        ToolCall(id="delete", name="delete_file", arguments={"path": "note.txt"}),
        context,
        TurnState(turn_id="turn-delete"),
        lambda name, args: approvals.append((name, args)) or True,
    )

    assert approvals == [("delete_file", {"path": "note.txt"})]
    assert observed == ["prepared"]
    assert result.success is True and result.status == "ok"
    assert not target.exists()
    records = journal.list_records(root=tmp_path)
    assert len(records) == 1
    assert records[0].status == "completed"
    events = [record["event"] for record in read_audit(context.audit.path)]
    assert events.index("approval") < events.index("tool_dispatch")


async def test_delete_failures_report_file_and_journal_state_truthfully(tmp_path: Path) -> None:
    for name, remove_first, expected_status, expected_exists in (
        ("before", False, "failed", True),
        ("after", True, "uncertain", False),
    ):
        root = tmp_path / name
        root.mkdir()
        target = root / "note.txt"
        target.write_text("izole", encoding="utf-8")
        context = context_for(root)
        journal = UndoJournal(context.settings.state_dir)
        context.services["undo_journal"] = journal

        def failing_trash(path: Path, *, delete: bool = remove_first) -> None:
            if delete:
                path.unlink()
            raise OSError("simulated isolated trash failure")

        context.services["trash"] = failing_trash
        registry = ToolRegistry()
        registry.register(DeleteFile)
        result = await SafetyGate(registry).dispatch(
            ToolCall(id=f"delete-{name}", name="delete_file", arguments={"path": "note.txt"}),
            context,
            TurnState(turn_id=f"turn-{name}"),
            lambda _tool, _args: True,
        )

        record = journal.list_records(root=root)[0]
        assert record.status == expected_status
        assert target.exists() is expected_exists
        assert result.success is False and result.status == "error"
        if remove_first:
            assert "belirsiz" in result.message
        else:
            assert "başarısız" in result.message


async def test_provider_timeout_falls_back_with_remaining_turn_budget(
    tmp_path: Path, monkeypatch
) -> None:
    # Router anahtarı çağrıdan önce ister; test sağlayıcısı HTTP kullanmaz.
    monkeypatch.setenv("OPENROUTER_API_KEY", f"test-sentinel-{uuid4().hex}")
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        primary_model="cloud",
        fallback_model="local",
        max_seconds=1.0,
        provider_timeout_seconds=0.05,
        max_output_tokens=100,
        allow_paid=True,
        daily_budget_usd=Decimal("0.01"),
        models={
            "cloud": ModelConfig(
                provider="openrouter",
                model="fixture-cloud",
                input_per_million=Decimal("1"),
                output_per_million=Decimal("2"),
                context_window=1024,
            ),
            "local": ModelConfig(
                provider="ollama",
                model="fixture-local",
                input_per_million=Decimal("0"),
                output_per_million=Decimal("0"),
            ),
        },
    )
    cloud_cancelled = asyncio.Event()
    local_timeouts: list[float] = []

    class SlowCloud:
        async def complete(self, messages, tools, *, timeout):
            try:
                await asyncio.sleep(5)
            finally:
                cloud_cancelled.set()

    class LocalFallback:
        async def complete(self, messages, tools, *, timeout):
            local_timeouts.append(timeout)
            return Completion(content="Yerel yedek yanıtı.")

    router = ProviderRouter(
        settings,
        AuditLog(settings.audit_path),
        providers={"cloud": SlowCloud(), "local": LocalFallback()},
    )
    context = context_for(tmp_path, settings)
    loop = AgentLoop(router, SafetyGate(ToolRegistry()), context, lambda _name, _args: False)

    result = await CommandRunner(loop).run("kısa yanıt ver")

    assert result.stop_reason == "completed"
    assert result.text == "Yerel yedek yanıtı."
    assert cloud_cancelled.is_set()
    assert len(local_timeouts) == 1 and 0 < local_timeouts[0] < settings.max_seconds
    assert router.ledger.totals() == (Decimal("0"), Decimal("0.001224"))
    audit_text = settings.audit_path.read_text(encoding="utf-8")
    assert '"event": "provider_fallback"' in audit_text
    assert '"retained_reservation_usd": "0.001224"' in audit_text


async def test_file_taint_survives_provider_boundary_and_requires_approval(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("dış içerik", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(ReadFile)
    registry.register(CreateEffect)
    context = context_for(tmp_path)

    class Provider:
        def __init__(self) -> None:
            self.responses = [
                Completion(
                    tool_calls=[
                        ToolCall(id="read", name="read_file", arguments={"path": "source.txt"})
                    ]
                ),
                Completion(
                    tool_calls=[ToolCall(id="write", name="create_effect", arguments={})]
                ),
                Completion(content="Tamam.")
            ]
            self.requests = []

        async def complete(self, messages, tools, *, timeout):
            self.requests.append([message.model_copy(deep=True) for message in messages])
            return self.responses.pop(0)

    provider = Provider()
    approvals: list[str] = []
    loop = AgentLoop(
        provider,
        SafetyGate(registry),
        context,
        lambda name, _args: approvals.append(loop.gate.pending_notice) or True,
    )

    result = await CommandRunner(loop).run("oku ve oluştur")

    assert result.stop_reason == "completed"
    assert (tmp_path / "effect.txt").exists()
    assert len(approvals) == 1
    assert "dış bir içerik" in approvals[0]
    assert all(message.private for message in provider.requests[2][-2:])
