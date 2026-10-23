"""Önceki dosya taşıma kayıtlarını çalışma alanına göre listeler."""

from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from artemis.safety.undo import UndoJournal
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool


class ListUndoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=100)


@register_tool
class ListUndoRecords(BaseTool):
    name = "list_undo_records"
    label = "Silme günlüğü"
    done_label = "Silme günlüğü okundu"
    description = "Çalışma alanındaki dosya geri dönüşüm kayıtlarını listeler."
    args_model = ListUndoArguments
    risk = Risk.READ_ONLY
    external = True
    content_kind = "file"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ListUndoArguments, arguments)
        journal = context.services.get("undo_journal")
        if journal is None:
            journal = UndoJournal(context.settings.state_dir)
        try:
            records = journal.list_records(root=Path(context.workspace).resolve(), limit=args.limit)
        except (OSError, ValueError):
            return ToolResult(
                status="error",
                message="Geri alma kayıtları okunamadı.",
                external=True,
                content_kind="file",
            )
        return ToolResult(
            success=True,
            status="ok",
            message=(
                "Kayıtlar yalnızca işletim sisteminin geri dönüşüm kutusuyla ilgili "
                "durum bilgisidir. 'prepared' kayıtlar çözümlenmemiştir ve silme "
                "başarıyla tamamlandı anlamına gelmez; tamamlanmış kayıtlar da "
                "öğenin hâlâ orada olduğunu garanti etmez. Geri yükleme yalnızca "
                "öğe işletim sistemi geri dönüşüm kutusunda duruyorsa mümkündür."
            ),
            data={"records": [record.as_dict() for record in records]},
            external=True,
            content_kind="file",
        )
