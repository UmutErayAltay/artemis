"""İnceleme için yalnız geçici bellekte çalışan açık demo runtime'ı."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from artemis.brain.models import Completion, Message, ToolCall
from artemis.config.settings import ModelConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import ToolRegistry


class DemoWriteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=160)


class DemoReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=40)


class DemoReadTool(BaseTool):
    name = "demo_read_sample"
    label = "Demo örnek okuma"
    done_label = "Demo örneği okundu"
    description = "Sabit örnek veriyi demo belleğinden okur; gerçek dosya veya servis kullanmaz."
    args_model = DemoReadArguments
    risk = Risk.READ_ONLY
    external = True

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        records: dict[str, str] = context.services["demo_records"]
        return ToolResult(
            success=True,
            status="ok",
            message="Demo örnek verisi okundu.",
            data={"sample": records.get("sample", "Yerel örnek bilgi")},
        )


class DemoWriteTool(BaseTool):
    name = "demo_write_memory"
    label = "Demo belleğe yazma"
    done_label = "Demo belleğe yazıldı"
    description = "Yalnız geçici demo belleğini günceller; gerçek dosya veya servis kullanmaz."
    args_model = DemoWriteArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        values = DemoWriteArguments.model_validate(arguments)
        records: dict[str, str] = context.services["demo_records"]
        records[values.key] = values.value
        return ToolResult(
            success=True,
            status="ok",
            message="Demo belleği güncellendi.",
            data={"key": values.key, "value": values.value},
        )


class DemoProvider:
    """İki sabit senaryoyu oynatır; serbest metin işlem iddiası üretmez."""

    async def complete(self, messages: list[Message], _tools: Any, *, timeout: float) -> Completion:
        del timeout
        # Önceki turlardan gelen araç mesajları bu turun yanıtını belirlememeli.
        last_user = max(
            (index for index, message in enumerate(messages) if message.role == "user"),
            default=-1,
        )
        tool_messages = [
            message for message in messages[last_user + 1 :] if message.role == "tool"
        ]
        if tool_messages:
            joined = "\n".join(message.content for message in tool_messages)
            if '"status":"denied"' in joined or "onaylamadı" in joined:
                text = "DEMO · Onay verilmedi; geçici demo belleği değiştirilmedi."
            elif "demo_write_memory" in joined or "Demo belleği güncellendi" in joined:
                text = "DEMO · Örnek veri işlendi ve yalnız geçici demo belleği güncellendi."
            else:
                text = "DEMO · Örnek veri okundu; dışarıda hiçbir işlem yapılmadı."
            return Completion(content=text, provider="demo", model="demo")

        user_text = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        normalized = user_text.casefold()
        if "örnek akış" in normalized or "ornek akis" in normalized:
            return Completion(
                tool_calls=[
                    ToolCall(
                        id="demo-read",
                        name="demo_read_sample",
                        arguments={"key": "sample"},
                    ),
                    ToolCall(
                        id="demo-write",
                        name="demo_write_memory",
                        arguments={"key": "son_örnek", "value": "geçici demo kaydı"},
                    ),
                ],
                provider="demo",
                model="demo",
            )
        return Completion(
            content=(
                "DEMO · Bu girdi yalnız çevrimdışı demo sağlayıcısına ulaştı; "
                "gerçek bir iş yapılmadı. Güvenli onay örneği için ‘örnek akış’ yazın."
            ),
            provider="demo",
            model="demo",
        )


def create_demo_parts(
    root: Path, on_record: Any = None
) -> tuple[Settings, DemoProvider, ToolRegistry, AuditLog]:
    """Gerçek config/anahtar/araç keşfi olmadan demo bağımlılıklarını kurar."""
    workspace = root / "workspace"
    state_dir = root / "state"
    audit_path = root / "audit.jsonl"
    settings = Settings(
        workspace=workspace,
        state_dir=state_dir,
        audit_path=audit_path,
        primary_model="demo",
        models={
            "demo": ModelConfig(
                provider="ollama",
                model="offline-demo",
                input_per_million=Decimal(0),
                output_per_million=Decimal(0),
            )
        },
        allow_paid=False,
        daily_budget_usd=Decimal(0),
    )
    registry = ToolRegistry()
    registry.register(DemoReadTool)
    registry.register(DemoWriteTool)
    audit = AuditLog(audit_path, on_record=on_record)
    return settings, DemoProvider(), registry, audit
