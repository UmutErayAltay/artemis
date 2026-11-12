"""`daily_summary_read`: saklı gün özetini okur (ADR 0066), "dün ne yaptık" için.

Dosya Artemis'in kendi klasöründedir (`state_dir/gunluk`) ve kullanıcının etkinliğini listeler:
`content_kind="file"` ve `private=True` olduğu için buluta yalnız maskeli gider. Gün katı
`YYYY-MM-DD` olarak ayrıştırılır; dosya adı yalnız ayrıştırılan tarihten türetilir. Özeti
üreten ve vault'a ekleyen yol bu araç değildir (zamanlayıcı ve onaylı `vault_append`).
"""

from __future__ import annotations

from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from artemis.proactive.daily_summary import parse_day, summary_path
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool

_DISABLED = "Gün özeti kapalı: config içinde daily_summary.enabled: true gerekir."
_READ_LIMIT = 40_000


class ReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str | None = Field(
        default=None, max_length=10, description="YYYY-MM-DD; boşsa bugün (proactive.timezone)."
    )


@register_tool
class DailySummaryRead(BaseTool):
    name = "daily_summary_read"
    label = "Gün özetini okuma"
    done_label = "Gün özeti okundu"
    description = (
        "Artemis'in yazdığı gün sonu özetini döndürür (sayılar, araçlar, dosya işlemleri). "
        "day: YYYY-MM-DD, boşsa bugün. Konuşma metni içermez."
    )
    args_model = ReadArguments
    risk = Risk.READ_ONLY
    content_kind = "file"

    def refuse(self, arguments: BaseModel, context: ToolContext) -> str | None:
        return None if context.settings.daily_summary.enabled else _DISABLED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ReadArguments, arguments)
        settings = context.settings
        if args.day is None:
            day = datetime.now(ZoneInfo(settings.proactive.timezone)).date()
        else:
            parsed = parse_day(args.day)
            if parsed is None:
                return ToolResult(message="Gün YYYY-MM-DD biçiminde olmalı.")
            day = parsed
        path = summary_path(settings.state_dir, day)
        try:
            if path.is_symlink() or not path.is_file():
                return ToolResult(message=f"{day.isoformat()} için kayıtlı özet yok.")
            with path.open(encoding="utf-8") as stream:
                text = stream.read(_READ_LIMIT)
        except (OSError, UnicodeError):
            return ToolResult(message="Özet dosyası okunamadı.")
        return ToolResult(
            success=True,
            status="ok",
            message=f"{day.isoformat()} özeti okundu.",
            data={"day": day.isoformat(), "summary": text},
            content_kind="file",
            private=True,
        )
