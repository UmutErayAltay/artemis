"""Tek kullanıcıdan eşzamanlı gelen turların onay bağlamını karıştırmaz."""

import asyncio

from jarvis.brain.agent_loop import AgentLoop, RunResult


class CommandRunner:
    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop
        self._lock = asyncio.Lock()

    async def run(self, text: str) -> RunResult:
        async with self._lock:
            return await self.loop.run(text)
