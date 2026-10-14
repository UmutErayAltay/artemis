"""MCP araçlarını Jarvis araçlarına çeviren tek köprü (ADR 0028).

MCP araçları çalışma anında bilinir; `@register_tool` ile değil, burada üretilen `BaseTool`
alt sınıflarıyla runtime'a özel bir registry kopyasına eklenir. Global registry değişmez.
Kapı (SafetyGate) bu araçları diğerlerinden ayırt etmeden denetler; ayrı onay politikası
yoktur. Risk yalnız yükselir: sunucunun `readOnlyHint` beyanı dikkate alınmaz, yalnız
kullanıcının `read_only_tools` listesi riski düşürür.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from time import monotonic
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, model_validator

from jarvis.config.settings import MCPServerConfig, Settings
from jarvis.mcp.client import MCPCallResult, MCPError, MCPToolSpec, StdioMCPClient
from jarvis.safety.audit import AuditLog
from jarvis.tools.base import BaseTool, ContentKind, Risk, ToolContext, ToolResult
from jarvis.tools.platform import child_environment
from jarvis.tools.registry import ToolRegistry

DESCRIPTION_LIMIT = 400
NAME_LIMIT = 64
TEXT_LIMIT = 3000
_UNSAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

ClientFactory = Callable[[MCPServerConfig], StdioMCPClient]


def server_environment(server: MCPServerConfig) -> dict[str, str]:
    """Yalnız OS değişkenleri ve kullanıcının bu sunucu için adlandırdığı değişkenler."""
    env = child_environment()
    for name in server.env_passthrough:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def default_client(server: MCPServerConfig) -> StdioMCPClient:
    return StdioMCPClient(
        [server.command, *server.args],
        env=server_environment(server),
        cwd=server.cwd,
        timeout=server.timeout_seconds,
    )


def tool_name(server: str, tool: str) -> str | None:
    """`mcp_<sunucu>_<araç>`; sınıra sığmayan ad kaydedilmez (kısaltma çakışma doğurur)."""
    name = _UNSAFE_NAME.sub("_", f"mcp_{server}_{tool}")
    return name if 1 <= len(name) <= NAME_LIMIT else None


def resolve_risk(server: MCPServerConfig, spec: MCPToolSpec) -> Risk:
    if spec.name in server.confirm_tools:
        return Risk.CONFIRM_REQUIRED
    if spec.name in server.read_only_tools:
        return Risk.READ_ONLY
    # Sunucu beyanı yalnız yükseltebilir; readOnlyHint üçüncü tarafın iddiasıdır.
    if spec.annotations.get("destructiveHint") is True:
        return Risk.CONFIRM_REQUIRED
    return Risk.SIDE_EFFECT


def _clean(text: str, limit: int) -> str:
    cleaned = _CONTROL.sub(" ", text).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


class MCPArguments(BaseModel):
    """Şema sunucudan gelir; burada yalnız zorunlu ve fazladan alanlar sınanır."""

    model_config = ConfigDict(extra="allow")
    input_schema: ClassVar[dict[str, Any]] = {}

    @model_validator(mode="before")
    @classmethod
    def check_schema(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueError("MCP argümanları bir nesne olmalı")
        required = cls.input_schema.get("required", [])
        if isinstance(required, list):
            missing = [key for key in required if isinstance(key, str) and key not in data]
            if missing:
                raise ValueError("Zorunlu MCP argümanı eksik")
        properties = cls.input_schema.get("properties", {})
        closed = cls.input_schema.get("additionalProperties") is False
        if closed and isinstance(properties, dict) and any(key not in properties for key in data):
            raise ValueError("MCP şemasında olmayan argüman")
        return data


def _result(server: MCPServerConfig, result: MCPCallResult) -> ToolResult:
    private_kind: ContentKind = "file" if server.content == "file" else "text"
    text = _clean(result.text, TEXT_LIMIT)
    if result.is_error:
        # Hata metni de dosya içeriği taşıyabilir: gizli sunucuda etiket korunur.
        return ToolResult(
            message=f"MCP aracı hata döndürdü: {text or '(açıklama yok)'}",
            external=True,
            content_kind=private_kind,
        )
    data: dict[str, Any] = {"text": text}
    if result.structured is not None:
        data["structured"] = result.structured
    if result.unsupported_blocks:
        data["unsupported_blocks"] = result.unsupported_blocks
    return ToolResult(
        success=True,
        status="ok",
        message="MCP aracı tamamlandı.",
        data=data,
        external=True,
        content_kind=private_kind,
    )


def build_tool_class(
    server: MCPServerConfig, spec: MCPToolSpec, factory: ClientFactory
) -> type[BaseTool] | None:
    resolved_name = tool_name(server.name, spec.name)
    if resolved_name is None:
        return None
    schema = dict(spec.input_schema)
    arguments_model = type(
        f"MCPArguments_{resolved_name}",
        (MCPArguments,),
        {"input_schema": schema, "__module__": __name__},
    )
    description = _clean(
        f"[MCP:{server.name}] {spec.description or spec.name}", DESCRIPTION_LIMIT
    )
    remote_name = spec.name

    final_name: str = resolved_name

    class MCPTool(BaseTool):
        name = final_name
        label = f"MCP · {server.name}/{remote_name}"
        done_label = f"MCP · {server.name}/{remote_name} tamamlandı"
        args_model = arguments_model
        risk = resolve_risk(server, spec)
        external = True
        # Argümanlar üçüncü taraf sürece gider; oradan ağa çıkabilir (ADR 0013).
        outbound = True
        content_kind = "file" if server.content == "file" else "text"
        parameters_schema = schema

        async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
            payload = arguments.model_dump(mode="json")
            remaining: float | None = None
            if context.deadline is not None:
                remaining = context.deadline - monotonic()
                if remaining <= 0:
                    return ToolResult(message="Süre sınırı dolduğu için MCP çağrılmadı.")
            client = factory(server)
            try:
                result = await asyncio.to_thread(
                    client.call_tool, remote_name, payload, timeout=remaining
                )
            except MCPError as error:
                return ToolResult(
                    message=f"MCP «{server.name}» çağrısı başarısız ({error.error_type}).",
                    external=True,
                )
            return _result(server, result)

    MCPTool.description = description
    MCPTool.__name__ = MCPTool.__qualname__ = f"MCPTool_{resolved_name}"
    return MCPTool


def discover_mcp_tools(
    settings: Settings,
    audit: AuditLog,
    registry: ToolRegistry,
    factory: ClientFactory = default_client,
) -> ToolRegistry:
    """Sunucu yoksa registry'yi aynen döndürür; varsa MCP araçlarıyla bir kopya üretir."""
    servers = settings.mcp.servers
    if not servers:
        return registry
    extended = registry.copy()
    for server in servers:
        try:
            specs = factory(server).list_tools()
        except MCPError as error:
            # Bir sunucunun arızası diğerlerini ve uygulama açılışını etkilemez.
            audit.emit("mcp_unavailable", server=server.name, error_type=error.error_type)
            continue
        registered: list[str] = []
        for spec in specs:
            cls = build_tool_class(server, spec, factory)
            if cls is None:
                audit.emit("mcp_tool_skipped", server=server.name, tool=spec.name, reason="name")
                continue
            if cls.name in extended:
                # Üzerine yazılmaz: yerel araç ya da başka MCP aracı korunur.
                audit.emit(
                    "mcp_tool_skipped", server=server.name, tool=spec.name, reason="collision"
                )
                continue
            extended.register(cls)
            registered.append(cls.name)
        audit.emit("mcp_ready", server=server.name, tools=registered)
    return extended
