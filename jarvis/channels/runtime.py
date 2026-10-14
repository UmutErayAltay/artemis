"""Qt'siz kanalların paylaştığı güvenli runtime fabrikası."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.brain.agent_loop import AgentLoop
from jarvis.brain.conversation import ConversationContext
from jarvis.brain.providers import Provider, build_provider
from jarvis.channels.runner import CommandRunner
from jarvis.config.settings import Settings, load_settings
from jarvis.memory.store import MemoryStore
from jarvis.safety.audit import AuditLog
from jarvis.safety.gate import SafetyGate
from jarvis.tools.base import ToolContext
from jarvis.tools.code_agent_jobs import CodeAgentError, CodeAgentJobs
from jarvis.tools.registry import ToolRegistry, discover_tools

Approval = Callable[[str, dict[str, Any]], bool]
AuditObserver = Callable[[dict[str, Any]], None]
ProviderBuilder = Callable[[Settings, AuditLog], Provider]
RegistryBuilder = Callable[[], ToolRegistry]


@dataclass(frozen=True)
class Runtime:
    """Bir kanalın kullandığı ortak bileşenler; sır taşıyanlar repr'e girmez."""

    settings: Settings = field(repr=False)
    audit: AuditLog = field(repr=False)
    registry: ToolRegistry = field(repr=False)
    gate: SafetyGate = field(repr=False)
    context: ToolContext = field(repr=False)
    provider: Provider = field(repr=False)
    agent: AgentLoop = field(repr=False)
    runner: CommandRunner = field(repr=False)

    def shutdown(self) -> None:
        """Kanal kapanışı çalışan işleri sınırlı biçimde temizler."""
        jobs = self.context.services.get("code_agent_jobs")
        if isinstance(jobs, CodeAgentJobs):
            jobs.shutdown()


def create_runtime(
    config_path: Path,
    approve: Approval,
    *,
    provider: Provider | None = None,
    registry: ToolRegistry | None = None,
    audit: AuditLog | None = None,
    on_record: AuditObserver | None = None,
    settings: Settings | None = None,
    provider_builder: ProviderBuilder | None = None,
    registry_builder: RegistryBuilder | None = None,
) -> Runtime:
    """CLI, ses ve masaüstü için aynı kapı ve sağlayıcı zincirini kurar."""
    loaded = settings if settings is not None else load_settings(config_path)
    if loaded.allow_all_applications:
        from jarvis.tools.app_catalog import installed_applications

        loaded = loaded.model_copy(
            update={"applications": {**installed_applications(), **loaded.applications}}
        )
    if not loaded.primary_model and provider is None:
        raise ValueError("Config içinde resmi katalogdan doğrulanmış bir primary_model tanımlayın.")
    loaded.workspace.mkdir(parents=True, exist_ok=True)
    log = audit if audit is not None else AuditLog(loaded.audit_path, on_record=on_record)
    tools = (
        registry
        if registry is not None
        else (registry_builder() if registry_builder is not None else discover_tools())
    )
    gate = SafetyGate(tools)
    context = ToolContext(workspace=loaded.workspace, settings=loaded, audit=log)
    # Runtime kurulamazsa sahibi dışarıya dönmez; callback kilidi kapatır.
    with ExitStack() as cleanup:
        # Kod işleri turdan uzun yaşar; servis runtime ömrünce tutulur.
        try:
            jobs = CodeAgentJobs(log, store_path=loaded.state_dir / "code_agent.sqlite3")
            cleanup.callback(jobs.shutdown)
            context.services["code_agent_jobs"] = jobs
        except CodeAgentError as error:
            log.emit("code_agent_unavailable", reason=str(error))
            context.services["code_agent_unavailable"] = str(error)
        builder = provider_builder if provider_builder is not None else build_provider
        selected_provider = provider if provider is not None else builder(loaded, log)
        agent = AgentLoop(selected_provider, gate, context, approve)
        budget = loaded.context_token_budget
        primary = loaded.models.get(loaded.primary_model)
        if primary is not None:
            # Geçmiş modelin penceresinin yarısını aşmamalı; yanıt ve araç şeması de sığmalı.
            budget = min(budget, primary.context_window // 2)
        store: MemoryStore | None = None
        if loaded.persist_memory:
            try:
                store = MemoryStore(loaded.state_dir / "memory.sqlite3")
            except (OSError, sqlite3.Error) as error:
                log.emit("memory_error", error=str(error))
            else:
                context.services["memory_store"] = store

        def memory_failed(error: Exception) -> None:
            log.emit("memory_error", error=str(error))

        conversation = ConversationContext(loaded.context_turns, budget, store, memory_failed)
        runner = CommandRunner(agent, conversation)
        runtime = Runtime(loaded, log, tools, gate, context, selected_provider, agent, runner)
        cleanup.pop_all()
        return runtime
