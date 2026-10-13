"""Tek kullanıcıdan eşzamanlı gelen turların onay bağlamını karıştırmaz."""

import asyncio
from typing import Any

from jarvis.brain.agent_loop import AgentLoop, RunResult
from jarvis.brain.conversation import ConversationContext


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

    async def _run_locked(self, text: str) -> RunResult:
        window = self.conversation.render()
        result = await self.loop.run(text, window)
        if result.record is not None:
            self.conversation.record(result.record)
        return result

    async def run(self, text: str) -> RunResult:
        async with self._lock:
            if not self.loop.cleanup_pending:
                self.loop.context.cancelled.clear()
            task = asyncio.current_task()
            if task is None:
                return await self._run_locked(text)
            self._active_task = task
            try:
                return await self._run_locked(text)
            finally:
                if self._active_task is task:
                    self._active_task = None
                if not self.loop.cleanup_pending:
                    self.loop.context.cancelled.clear()
