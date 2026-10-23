"""MCP araçlarını Artemis araçlarına çeviren tek köprü (ADR 0028).

MCP araçları çalışma anında bilinir; `@register_tool` ile değil, burada üretilen `BaseTool`
alt sınıflarıyla runtime'a özel bir registry kopyasına eklenir. Global registry değişmez.
Kapı (SafetyGate) bu araçları diğerlerinden ayırt etmeden denetler; ayrı onay politikası
yoktur. Risk yalnız yükselir: sunucunun `readOnlyHint` beyanı dikkate alınmaz, yalnız
kullanıcının `read_only_tools` listesi riski düşürür.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, model_validator

from artemis.config.settings import MCPServerConfig, Settings
from artemis.mcp.client import MCPCallResult, MCPError, MCPToolSpec, StdioMCPClient
from artemis.safety.audit import AuditLog
from artemis.tools.base import BaseTool, ContentKind, Risk, ToolContext, ToolResult
from artemis.tools.platform import child_environment
from artemis.tools.registry import ToolRegistry

DESCRIPTION_LIMIT = 400
NAME_LIMIT = 64
TEXT_LIMIT = 3000
# Şema her model çağrısında sağlayıcıya (bulut dahil) gider ve talimat taşıyabilir:
# sınırı aşan araç kırpılmaz, kaydedilmez (yarım JSON şeması sağlayıcıyı bozardı).
SCHEMA_BYTES_LIMIT = 8 * 1024
SCHEMA_DEPTH_LIMIT = 8
SCHEMA_STRING_LIMIT = 200
TOOLS_PER_SERVER = 64
STRUCTURED_BYTES_LIMIT = 16 * 1024
ARGUMENTS_BYTES_LIMIT = 256 * 1024
_UNSAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]")
_WHITESPACE = re.compile(r"\s+")

ClientFactory = Callable[[MCPServerConfig], StdioMCPClient]


def server_environment(server: MCPServerConfig) -> dict[str, str]:
    """Yalnız OS değişkenleri ve kullanıcının bu sunucu için adlandırdığı değişkenler."""
    env = child_environment()
    for name in server.env_passthrough:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


PYTHON_PLACEHOLDER = "{python}"


def server_command(server: MCPServerConfig) -> list[str]:
    """ADR 0032: tam olarak `{python}` Artemis'in yorumlayıcısıdır; başka yer tutucu yoktur.

    Açılışta (ADR 0030) etkin sanal ortam olmadığından PATH'teki `python` başka bir ortam
    olabilir; Artemis'in kendi sunucuları aynı ortamda çalışmalıdır.
    """
    command = sys.executable if server.command == PYTHON_PLACEHOLDER else server.command
    return [command, *server.args]


def default_client(server: MCPServerConfig) -> StdioMCPClient:
    return StdioMCPClient(
        server_command(server),
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


def _clean(text: str, limit: int, *, single_line: bool = True) -> str:
    """Kontrol, biçim (bidi, sıfır genişlik, Unicode tag), vekil ve özel kullanım karakterleri
    atılır. Tek satır kipinde boşluklar birleştirilir: sunucu açıklaması onay penceresinde
    sahte bir "[MCP:...] güvenli" satırı üretemez."""
    kept = "".join(
        ch
        for ch in text
        if (not single_line and ch in "\n\t")
        or unicodedata.category(ch) not in {"Cc", "Cf", "Cs", "Co"}
    )
    cleaned = _WHITESPACE.sub(" ", kept).strip() if single_line else kept.strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


def _schema_depth(value: Any, depth: int = 0) -> int:
    if depth > SCHEMA_DEPTH_LIMIT:
        return depth
    if isinstance(value, dict):
        return max((_schema_depth(item, depth + 1) for item in value.values()), default=depth)
    if isinstance(value, list):
        return max((_schema_depth(item, depth + 1) for item in value), default=depth)
    return depth


def _clean_schema(value: Any) -> Any:
    """Şemadaki tüm metinler (description, title, enum, default) kırpılır ve temizlenir."""
    if isinstance(value, dict):
        return {str(key): _clean_schema(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_schema(item) for item in value]
    if isinstance(value, str):
        return _clean(value, SCHEMA_STRING_LIMIT)
    return value


def checked_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Kabul edilebilir şemanın temizlenmiş kopyası; değilse None (araç kaydedilmez)."""
    if _schema_depth(schema) > SCHEMA_DEPTH_LIMIT:
        return None
    cleaned = _clean_schema(schema)
    try:
        encoded = json.dumps(cleaned, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    # $ref/$defs çözümlemesi yapılmaz; dolaylı şema boyut ve içerik sınırını aşabilir.
    if '"$ref"' in encoded or '"$defs"' in encoded or '"definitions"' in encoded:
        return None
    if len(encoded.encode("utf-8")) > SCHEMA_BYTES_LIMIT:
        return None
    return cleaned if isinstance(cleaned, dict) else None


class MCPArguments(BaseModel):
    """Şema sunucudan gelir; burada yalnız zorunlu ve fazladan alanlar sınanır."""

    model_config = ConfigDict(extra="allow")
    input_schema: ClassVar[dict[str, Any]] = {}

    @model_validator(mode="before")
    @classmethod
    def check_schema(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueError("MCP argümanları bir nesne olmalı")
        try:
            size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as error:
            raise ValueError("MCP argümanları JSON'a çevrilemiyor") from error
        if size > ARGUMENTS_BYTES_LIMIT:
            raise ValueError("MCP argümanları çok büyük")
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
    text = _clean(result.text, TEXT_LIMIT, single_line=False)
    if result.is_error:
        # Hata metni de dosya içeriği taşıyabilir: gizli sunucuda etiket korunur.
        return ToolResult(
            message=f"MCP aracı hata döndürdü: {text or '(açıklama yok)'}",
            external=True,
            content_kind=private_kind,
        )
    data: dict[str, Any] = {"text": text}
    if result.structured is not None:
        # Denetim ve kalıcı hafıza modele giden 4000 karakter çerçevesinden bağımsızdır.
        encoded = json.dumps(result.structured, ensure_ascii=False)
        if len(encoded.encode("utf-8")) <= STRUCTURED_BYTES_LIMIT:
            data["structured"] = result.structured
        else:
            data["structured_omitted"] = "Yapılandırılmış sonuç sınırı aştığı için atlandı."
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
    checked = checked_schema(spec.input_schema)
    if checked is None:
        return None
    schema = checked
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
        # Ham uzak ad UI'ya gitmez (kaçış/bidi dizisi taşıyabilir); temizlenmiş ad kullanılır.
        label = f"MCP · {final_name}"
        done_label = f"MCP · {final_name} tamamlandı"
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
            # to_thread iptal edilemez; olay istemciye iptali iletir, o da süreç ağacını
            # kapatır. Böylece "iptal edildi" denen çağrı arkada yan etki üretmeye devam etmez.
            cancel = threading.Event()
            try:
                result = await asyncio.to_thread(
                    client.call_tool, remote_name, payload, timeout=remaining, cancel=cancel
                )
            except asyncio.CancelledError:
                cancel.set()
                raise
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

    def listing(server: MCPServerConfig) -> list[MCPToolSpec] | MCPError:
        try:
            return factory(server).list_tools()
        except MCPError as error:
            return error

    # Sunucular paralel keşfedilir: açılış en yavaş sunucunun süresi kadar gecikir, toplamı
    # kadar değil. Kayıt sırası config sırasıdır (çakışmada ilk gelen deterministiktir).
    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
        listings = list(pool.map(listing, servers))
    for server, found in zip(servers, listings, strict=True):
        if isinstance(found, MCPError):
            # Bir sunucunun arızası diğerlerini ve uygulama açılışını etkilemez.
            audit.emit("mcp_unavailable", server=server.name, error_type=found.error_type)
            continue
        registered: list[str] = []
        for index, spec in enumerate(found):
            if index >= TOOLS_PER_SERVER:
                audit.emit(
                    "mcp_tool_skipped", server=server.name, tool=spec.name, reason="limit"
                )
                continue
            if tool_name(server.name, spec.name) is None:
                audit.emit("mcp_tool_skipped", server=server.name, tool=spec.name, reason="name")
                continue
            cls = build_tool_class(server, spec, factory)
            if cls is None:
                audit.emit(
                    "mcp_tool_skipped", server=server.name, tool=spec.name, reason="schema"
                )
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
