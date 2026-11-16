"""Yerel dosya araması aracı (ADR 0065): kullanıcının seçtiği klasörlerde tam metin.

Sonuç yol, değişiklik tarihi ve en çok 200 karakterlik alıntıdır; bu kökler için dosyanın
tamamını döndüren araç yoktur. İçerik kişisel dosyadan geldiği için sonuç `external=True` ve
`content_kind="file"`'dır: tur lekelenir, `allow_cloud_file_content` kapalıyken gizli sayılır.
"""

from __future__ import annotations

import asyncio
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.file_index import FileIndex, FileIndexError, usable_roots
from artemis.tools.registry import register_tool

_DISABLED = "Dosya araması kapalı: config içinde file_search.enabled: true gerekir."
_NO_ROOTS = "Aranacak klasör yok: Ayarlar > Dosya araması'ndan bir klasör ekle."
_UNAVAILABLE = "Dosya araması kullanılamıyor: bu SQLite sürümünde FTS5 yok."


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=300, description="Dosya adında/içinde aranan.")
    limit: int = Field(default=5, ge=1, le=10)


@register_tool
class FileSearch(BaseTool):
    name = "file_search"
    label = "Dosya araması"
    done_label = "Dosyalarda arandı"
    description = (
        "Kullanıcının seçtiği klasörlerdeki dosyaların adında ve metninde arar (txt, md, kod, "
        "docx; PDF için yalnız ad). Yol, tarih ve kısa alıntı döndürür; dosyanın tamamını değil."
    )
    args_model = Arguments
    risk = Risk.READ_ONLY
    external = True
    content_kind = "file"

    def refuse(self, arguments: BaseModel, context: ToolContext) -> str | None:
        return None if context.settings.file_search.enabled else _DISABLED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(Arguments, arguments)
        index = context.services.get("file_index")
        if not isinstance(index, FileIndex):
            return ToolResult(message=_UNAVAILABLE)
        if not index.available:
            return ToolResult(message=_UNAVAILABLE)
        if not usable_roots(context.settings)[0]:
            return ToolResult(message=_NO_ROOTS)
        try:
            hits = await asyncio.to_thread(index.search, args.query, args.limit)
        except FileIndexError as error:
            return ToolResult(message=str(error))
        status = index.status()
        note = " İndeksleme sürüyor, sonuç eksik olabilir." if status["running"] else ""
        return ToolResult(
            success=True,
            status="ok",
            message=(f"{len(hits)} dosya bulundu." if hits else "Eşleşen dosya bulunamadı.") + note,
            data={"results": [
                {"path": h.path, "modified": h.modified, "snippet": h.snippet} for h in hits
            ]},
            external=True,
            content_kind="file",
        )
