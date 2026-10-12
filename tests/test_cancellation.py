"""Tur iptali bekleyen işleri temizler ve sonraki turu kullanılabilir tutar."""

import asyncio
import json

from pydantic import BaseModel, ConfigDict

from jarvis.brain.agent_loop import AgentLoop
from jarvis.brain.models import Completion, ToolCall
from jarvis.channels.runner import CommandRunner
from jarvis.config.settings import Settings
from jarvis.safety.audit import AuditLog
from jarvis.safety.gate import SafetyGate
from jarvis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from jarvis.tools.registry import ToolRegistry


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WaitingTool(BaseTool):
    name = "wait"
    description = "Bekleyen işi taklit eder."
    args_model = EmptyArgs
    risk = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        context.services["tool_started"].set()
        try:
            await asyncio.Event().wait()
        finally:
            context.services["tool_cleaned"].set()
        return ToolResult(success=True, status="ok", message="Bitti.")


class WaitingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.calls = 0

    async def complete(self, messages, tools, *, timeout):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cleaned.set()
        return Completion(content="Sonraki tur çalıştı.")


def make_runner(tmp_path, provider, *, tool=None):
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    audit = AuditLog(tmp_path / "audit.jsonl")
    context = ToolContext(tmp_path, Settings(), audit)
    if tool is not None:
        context.services["tool_started"] = asyncio.Event()
        context.services["tool_cleaned"] = asyncio.Event()
    runner = CommandRunner(AgentLoop(provider, SafetyGate(registry), context, lambda *_: True))
    return runner, context


async def test_cancel_current_interrupts_provider_and_next_turn_runs(tmp_path):
    provider = WaitingProvider()
    runner, context = make_runner(tmp_path, provider)
    active = asyncio.create_task(runner.run("birinci"))
    await provider.started.wait()

    assert runner.cancel_current() is True
    result = await active
    assert result.stop_reason == "cancelled"
    assert provider.cleaned.is_set()
    assert provider.calls == 1
    assert runner.cancel_current() is False

    following = await runner.run("ikinci")
    assert following.text == "Sonraki tur çalıştı."
    assert provider.calls == 2
    assert context.cancelled.is_set() is False


async def test_cancel_current_interrupts_tool_and_audits_unknown_completion(tmp_path):
    class ToolProvider:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools, *, timeout):
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    tool_calls=[ToolCall(id="wait-1", name="wait", arguments={})]
                )
            return Completion(content="Sonraki tur çalıştı.")

    provider = ToolProvider()
    runner, context = make_runner(tmp_path, provider, tool=WaitingTool)
    active = asyncio.create_task(runner.run("bekle"))
    await context.services["tool_started"].wait()
    assert runner.cancel_current() is True
    result = await active

    assert result.stop_reason == "cancelled"
    assert provider.calls == 1
    records = [json.loads(line) for line in context.audit.path.read_text().splitlines()]
    interrupted = next(row for row in records if row["event"] == "tool_interrupted")
    assert interrupted["completion"] == "unknown"
    tool_result = next(row for row in records if row["event"] == "tool_result")
    assert tool_result["result"]["success"] is False
    assert tool_result["result"]["status"] == "error"
    assert (await runner.run("devam")).text == "Sonraki tur çalıştı."


async def test_queued_turn_is_not_cancelled_with_active_turn(tmp_path):
    provider = WaitingProvider()
    runner, _context = make_runner(tmp_path, provider)
    active = asyncio.create_task(runner.run("birinci"))
    await provider.started.wait()
    queued = asyncio.create_task(runner.run("kuyrukta"))
    await asyncio.sleep(0)

    assert runner.cancel_current() is True
    assert (await active).stop_reason == "cancelled"
    assert (await queued).text == "Sonraki tur çalıştı."
    assert provider.calls == 2


async def test_outer_task_cancellation_quarantines_pending_child_cleanup(tmp_path):
    class CancellationResistantProvider:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cleaned = asyncio.Event()
            self.calls = 0

        async def complete(self, messages, tools, *, timeout):
            self.calls += 1
            if self.calls > 1:
                return Completion(content="Yeni tur tamamlandı")
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # İptali yutan sağlayıcıyı temizlik süresi sınırında bırakırız.
                await self.release.wait()
                self.cleaned.set()
                return Completion(content="Tamamlandı")

    provider = CancellationResistantProvider()
    runner, context = make_runner(tmp_path, provider)
    active = asyncio.create_task(runner.run("iptal et"))
    await provider.started.wait()
    active.cancel()

    try:
        await active
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("Dış task iptali çağırana aktarılmalı")

    records = [
        json.loads(line)
        for line in context.audit.path.read_text(encoding="utf-8").splitlines()
    ]
    cancelled = next(row for row in records if row["event"] == "turn_cancelled")
    ended = next(row for row in records if row["event"] == "turn_end")
    assert cancelled["cleanup_pending"] is True
    assert ended["stop_reason"] == "cancelled"
    assert "temizleme süresi içinde sonlanmadı" in ended["text"]
    cancelled_turn_id = context.turn_id
    cancelled_deadline = context.deadline
    assert context.cancelled.is_set() is True

    blocked = await runner.run("sıradaki tur")
    assert blocked.stop_reason == "cleanup_pending"
    assert provider.cleaned.is_set() is False
    assert context.cancelled.is_set() is True
    assert context.turn_id == cancelled_turn_id
    assert context.deadline == cancelled_deadline

    provider.release.set()
    await asyncio.wait_for(provider.cleaned.wait(), timeout=1)
    await asyncio.sleep(0)
    following = await runner.run("sonraki tur")
    assert following.text == "Yeni tur tamamlandı"
    assert context.cancelled.is_set() is False
    assert context.turn_id == following.turn_id
