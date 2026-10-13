"""Araç sözleşmeleri güvenlik politikasını uygulama ayrıntılarından ayırır."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from jarvis.config.settings import Settings
    from jarvis.safety.audit import AuditLog


class Risk(str, Enum):
    READ_ONLY = "READ_ONLY"
    SIDE_EFFECT = "SIDE_EFFECT"
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"


ContentKind = Literal["text", "file", "clipboard", "web"]


def normalize_risk(value: object) -> Risk:
    """Yeni veya hatalı risk etiketleri güvenli tarafta yan etkili sayılır."""
    try:
        return Risk(value)
    except (ValueError, TypeError):
        return Risk.SIDE_EFFECT


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = False
    status: Literal["ok", "error", "denied", "sent"] = "error"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    external: bool = False
    content_kind: ContentKind = "text"

    @model_validator(mode="after")
    def validate_status(self) -> ToolResult:
        """Gönderilmiş fakat doğrulanmamış komut başarı diye sunulamaz."""
        if self.success != (self.status == "ok"):
            raise ValueError("Başarı yalnız doğrulanmış ok durumuyla birlikte kullanılabilir.")
        return self


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$", min_length=1, max_length=64)
    description: str
    parameters: dict[str, Any]
    risk: Risk = Risk.SIDE_EFFECT

    @field_validator("risk", mode="before")
    @classmethod
    def validate_risk(cls, value: object) -> Risk:
        return normalize_risk(value)


@dataclass
class ToolContext:
    """Ortak bağımlılıklar kalıtım ve global durum gerektirmeden enjekte edilir."""

    workspace: Path
    settings: Settings
    audit: AuditLog
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    turn_id: str = ""
    deadline: float | None = None
    services: dict[str, Any] = field(default_factory=dict, repr=False)


class BaseTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[BaseModel]]
    risk: ClassVar[Risk] = Risk.SIDE_EFFECT
    external: ClassVar[bool] = False
    # Araç argümanını makinenin dışına (ağ, tarayıcı) taşıyor. Geçmişte dış içerik
    # kalmışsa kapı bu araçlara yeni turda da onay sorar (ADR 0013): injection metni
    # URL veya sorgu üzerinden dışarı sızdırılamasın. Kapıda araç adı listesi tutulmaz.
    outbound: ClassVar[bool] = False
    # Kullanıcıya gösterilen okunur ad (onay başlığı) ve başarı satırı (ADR 0014).
    # Boşsa arayüz ham adı gösterir; ayrı bir ad tablosu tutulmaz.
    label: ClassVar[str] = ""
    done_label: ClassVar[str] = ""
    content_kind: ClassVar[ContentKind] = "text"

    def risk_for(self, arguments: BaseModel, context: ToolContext) -> Risk:
        """Doğrulanmış argümanlar için riski bildirir; onay kararını kapı verir."""
        return normalize_risk(self.risk)

    @abstractmethod
    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        """Alt işlemin sonucu doğrulanmadan başarı bildirilmemelidir."""
        raise NotImplementedError
