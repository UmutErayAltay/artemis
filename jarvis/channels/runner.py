"""Tek kullanıcıdan eşzamanlı gelen turların onay bağlamını karıştırmaz."""

import asyncio
from typing import Any

from jarvis.brain.agent_loop import AgentLoop, RunResult


class CommandRunner:
    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop
        self._lock = asyncio.Lock()
        self._active_task: asyncio.Task[Any] | None = None

    def cancel_current(self) -> bool:
        """Yalnız şu anda yürüyen turu iptal eder; sıradaki turu etkilemez."""
        task = self._active_task
        if task is None or task.done():
            return False
        self.loop.context.cancelled.set()
        return True

    async def run(self, text: str) -> RunResult:
        async with self._lock:
            if not self.loop.cleanup_pending:
                self.loop.context.cancelled.clear()
            task = asyncio.current_task()
            if task is None:
                return await self.loop.run(text)
            self._active_task = task
            try:
                return await self.loop.run(text)
            finally:
                if self._active_task is task:
                    self._active_task = None
                if not self.loop.cleanup_pending:
                    self.loop.context.cancelled.clear()
