"""Yerel geçmiş yönetimi; silme ve saklama politikası yalnız açık UI/CLI isteğiyle."""

import sqlite3
from collections.abc import Callable
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from artemis.memory.store import MemoryStore
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool


class HistoryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["status", "list", "delete", "retention"] = "status"
    retention_days: int = Field(default=0, ge=0, le=36500)


@register_tool
class HistoryTool(BaseTool):
    name = "conversation_history"
    label = "Konuşma geçmişi yönetimi"
    done_label = "Geçmiş işlemi tamamlandı"
    description = (
        "Yerel konuşma geçmişi yönetimi; yalnız panel/CLI kontrolü içindir. "
        "delete memory.sqlite3'teki tüm konuşma arşivini siler ve dosyayı yeniden yazar; öz "
        "notlar, denetim günlüğü (audit*.jsonl, kullanıcı girdilerini de içerir) ve proje işi "
        "kayıtları SİLİNMEZ. retention 0 otomatik silmeyi kapatır, pozitif gün sayısı eski "
        "turları hemen siler. Güvenli disk silme değildir (SSD/yedek kopyaları kapsamaz)."
    )
    args_model = HistoryArguments
    risk = Risk.CONFIRM_REQUIRED
    content_kind = "file"

    def risk_for(self, arguments: BaseModel, context: ToolContext) -> Risk:
        args = cast(HistoryArguments, arguments)
        return Risk.READ_ONLY if args.action in {"status", "list"} else self.risk

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        if not context.control_request:
            return ToolResult(message="Geçmiş yönetimini panelden veya /hafıza komutuyla başlatın.")
        store = context.services.get("memory_store")
        if not isinstance(store, MemoryStore):
            return ToolResult(message="Kalıcı hafıza kapalı veya depo açılamadı.")
        args = cast(HistoryArguments, arguments)
        try:
            removed = 0
            if args.action == "delete":
                removed = store.delete_history()
            elif args.action == "retention":
                removed = store.set_retention(args.retention_days)
            refresh = cast(Callable[[], None], context.services["memory_refresh"])
            refresh()
            # `action` masaüstünün silme sonrası sohbet ekranını temizlemesi içindir.
            data: dict[str, object] = {
                **store.history_status(),
                "removed": removed,
                "action": args.action,
            }
            if args.action == "list":
                data["recent_turns"] = store.history_page()
        except (OSError, sqlite3.Error) as error:
            return ToolResult(message=f"Geçmiş işlemi tamamlanamadı ({type(error).__name__}).")
        if args.action == "status":
            message = "Geçmiş durumu okundu."
        elif args.action == "list":
            message = f"{len(cast(list[object], data['recent_turns']))} tur listelendi."
        else:
            message = (
                f"Geçmiş işlemi doğrulandı; {removed} tur silindi. Kalıcı öz notlar ve "
                "denetim günlüğü korundu."
            )
        return ToolResult(success=True, status="ok", data=data, message=message)
