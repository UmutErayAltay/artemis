"""Qt'siz kanalların paylaştığı güvenli runtime fabrikası."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from artemis.brain.agent_loop import AgentLoop
from artemis.brain.conversation import ConversationContext
from artemis.brain.providers import Provider, build_provider
from artemis.channels.runner import CommandRunner
from artemis.config.settings import Settings, load_settings
from artemis.memory.store import MemoryStore
from artemis.proactive.store import ProactiveStore, ProactiveStoreError
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate
from artemis.tools.base import ToolContext
from artemis.tools.code_agent_jobs import CodeAgentError, CodeAgentJobs
from artemis.tools.registry import ToolRegistry, discover_tools

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
        from artemis.tools.app_catalog import installed_applications

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
    if loaded.mcp.servers:
        # Lazy: MCP yapılandırılmamışsa köprü ve istemci hiç yüklenmez (ADR 0028).
        from artemis.mcp.bridge import discover_mcp_tools

        tools = discover_mcp_tools(loaded, log, tools)
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
        if loaded.proactive.enabled:
            # Hatırlatma deposu (ADR 0027); kurulamazsa araçlar dürüstçe "kapalı" der.
            try:
                context.services["proactive_store"] = ProactiveStore(
                    loaded.state_dir / "proactive.sqlite3"
                )
            except ProactiveStoreError as error:
                log.emit("proactive_unavailable", error_type=type(error).__name__)
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
