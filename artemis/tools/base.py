"""Araç sözleşmeleri güvenlik politikasını uygulama ayrıntılarından ayırır."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, ParamSpec, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_P = ParamSpec("_P")
_T = TypeVar("_T")

if TYPE_CHECKING:
    from artemis.config.settings import Settings
    from artemis.safety.audit import AuditLog


class Risk(str, Enum):
    READ_ONLY = "READ_ONLY"
    SIDE_EFFECT = "SIDE_EFFECT"
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"


ContentKind = Literal["text", "file", "clipboard", "web", "screen"]


def normalize_risk(value: object) -> Risk:
    """Yeni veya hatalı risk etiketleri güvenli tarafta yan etkili sayılır."""
    try:
        return Risk(value)
    except (ValueError, TypeError):
        return Risk.SIDE_EFFECT


async def run_blocking(func: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """`asyncio.to_thread` gibi, ama iptal edilince thread bitene dek görevi bitirmez.

    İptal yalnız beklemeyi keser; thread'deki PowerShell/OS çağrısı sürer. Görev hemen
    "iptal edildi" diye biterse tur temizlenmiş sayılır ve yan etki sürerken yeni tur başlar.
    Görev canlı kaldıkça AgentLoop onu temizliği süren iş olarak tutar (`cleanup_pending`).
    Yan etkili araçlar thread'e bu yardımcıyla çıkar.
    """
    future = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.wait((future,))
            except asyncio.CancelledError:
                continue  # Tekrarlanan iptal de thread'i durduramaz; bitişini beklemeye devam.
        if not future.cancelled():
            future.exception()  # Sonuç okunmazsa "never retrieved" uyarısı kalır.
        raise


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool = False
    status: Literal["ok", "error", "denied", "sent"] = "error"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    external: bool = False
    content_kind: ContentKind = "text"
    # İçeriğin gizlilik kökeni (ADR 0056): gizli bir turda yazılmış hafıza gibi, türü ne olursa
    # olsun buluta yalnız maskeli gidecek sonuç. content_kind'e göre verilen karara eklenir.
    private: bool = False

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
    # Çağrı anında turun lekeli olup olmadığı; kalıcı kayıt kuran araç (hatırlatma) bunu
    # kayda yazar ki dış içerikten doğan kayıt sonradan da öyle gösterilsin (ADR 0027).
    turn_tainted: bool = False
    # Yalnız bu turda dış içerik okundu mu (geçmiş hariç). Kural ekleme bunu kullanır: önceki
    # turda vault okunması, kullanıcının bu turda kendi yazdığı talimatı engellememeli.
    turn_read_external: bool = False
    # Turda veya geçmişte buluta verilmeyecek (dosya/pano) içerik görüldü; kalıcı kayıt
    # bunu taşır ki sonradan Telegram'a gönderilmesin (ADR 0016/0027). Döngü doldurur.
    turn_private: bool = False
    services: dict[str, Any] = field(default_factory=dict, repr=False)
    # Yalnız runner.dispatch_tool süresince True; paylaşılan servis sözlüğünde tutulmaz.
    control_request: bool = False


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
    # Bilgisayar kullanımı otomatik kipindeyken (`computer_use.mode: auto`) bu aracın tur
    # içi/dış içerik kaynaklı onayı atlanır. Yalnız kullanıcının bilerek açtığı tek araçta
    # True (ADR 0039 ek karar); diğer araçların kural 4 davranışı değişmez.
    auto_mode_ok: ClassVar[bool] = False
    # Kullanıcıya gösterilen okunur ad (onay başlığı) ve başarı satırı (ADR 0014).
    # Boşsa arayüz ham adı gösterir; ayrı bir ad tablosu tutulmaz.
    label: ClassVar[str] = ""
    done_label: ClassVar[str] = ""
    content_kind: ClassVar[ContentKind] = "text"
    # Şeması çalışma anında bilinen araçlar (MCP köprüsü, ADR 0028) için modele gösterilen
    # JSON şeması; None ise pydantic modelinden üretilir.
    parameters_schema: ClassVar[dict[str, Any] | None] = None

    def risk_for(self, arguments: BaseModel, context: ToolContext) -> Risk:
        """Doğrulanmış argümanlar için riski bildirir; onay kararını kapı verir."""
        return normalize_risk(self.risk)

    @abstractmethod
    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        """Alt işlemin sonucu doğrulanmadan başarı bildirilmemelidir."""
        raise NotImplementedError
