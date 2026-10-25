"""Tek kullanıcıdan eşzamanlı gelen turların onay bağlamını karıştırmaz."""

import asyncio
from time import monotonic
from typing import Any

from pydantic import BaseModel

from artemis.brain.agent_loop import AgentLoop, RunResult
from artemis.brain.conversation import ConversationContext
from artemis.brain.models import ToolCall
from artemis.brain.streaming import ResponseStream
from artemis.safety.gate import TurnState
from artemis.tools.base import ToolResult


class CommandRunner:
    def __init__(self, loop: AgentLoop, context: ConversationContext | None = None) -> None:
        self.loop = loop
        # Geçmişin tek sahibi runner'dır: CLI, panel ve ses aynı geçmişi görür.
        if context is None:
            settings = loop.context.settings
            context = ConversationContext(settings.context_turns, settings.context_token_budget)
        self.conversation = context
        self._lock = asyncio.Lock()
        self._active_task: asyncio.Task[Any] | None = None

    def cancel_current(self) -> bool:
        """Yalnız şu anda yürüyen turu iptal eder; sıradaki turu etkilemez."""
        task = self._active_task
        if task is None or task.done():
            return False
        self.loop.context.cancelled.set()
        return True

    def reset_context(self) -> bool:
        """Aktif tur sürerken sıfırlamaz: turun kaydı sıfırlamadan sonra geri yazılırdı."""
        if self._lock.locked():
            return False
        removed = self.conversation.reset()
        self.loop.context.audit.emit("context_reset", removed_turns=removed)
        return True

    async def _run_locked(
        self, text: str, unattended: bool, stream: ResponseStream | None = None,
    ) -> RunResult:
        window = self.conversation.render()
        if stream is None:
            result = await self.loop.run(text, window, unattended=unattended)
        else:
            result = await self.loop.run(text, window, unattended=unattended, stream=stream)
        if result.record is not None:
            self.conversation.record(result.record)
        return result

    async def run(
        self, text: str, *, unattended: bool = False, stream: ResponseStream | None = None,
    ) -> RunResult:
        async with self._lock:
            if not self.loop.cleanup_pending:
                self.loop.context.cancelled.clear()
            task = asyncio.current_task()
            if task is None:
                return await self._run_locked(text, unattended, stream)
            self._active_task = task
            try:
                return await self._run_locked(text, unattended, stream)
            finally:
                if self._active_task is task:
                    self._active_task = None
                if not self.loop.cleanup_pending:
                    self.loop.context.cancelled.clear()

    async def dispatch_tool(self, name: str, arguments: BaseModel) -> ToolResult:
        """UI'nin tipli çağrısı: LLM olmadan, aynı kilit ve SafetyGate ile yeni tur."""
        if self._lock.locked() or self.loop.cleanup_pending:
            return ToolResult(message="Başka bir işlem sürüyor; araç çalıştırılmadı.")
        async with self._lock:
            context = self.loop.context
            context.cancelled.clear()
            self._active_task = asyncio.current_task()
            state = TurnState(
                deadline=monotonic() + context.settings.max_seconds,
                history_external=self.conversation.render().external,
            )
            context.audit.emit("turn_start", turn_id=state.turn_id, channel="typed_tool")
            try:
                return await self.loop.gate.dispatch(
                    ToolCall(
                        id=state.turn_id, name=name, arguments=arguments.model_dump(mode="json")
                    ),
                    context,
                    state,
                    self.loop.approve,
                )
            finally:
                self._active_task = None
                if not self.loop.cleanup_pending:
                    context.cancelled.clear()
