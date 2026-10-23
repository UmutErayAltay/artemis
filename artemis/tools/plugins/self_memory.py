"""Artemis'in kendi kalıcı hafızası: öz notlar ve geçmiş konuşma araması (ADR 0025).

Vault araçlarından (memory.py) ayrıdır: bu depo `state_dir/memory.sqlite3` içindedir.
`memory_remember`/`memory_forget` SIDE_EFFECT'tir; kapı temiz turda sormadan çalıştırır,
dış içerikle lekeli turda onay ister. Böylece web/dosya metni kullanıcı adına kalıcı
"anı" yazamaz (anti-injection). Kapıya özel durum eklenmez.
"""

import asyncio
import sqlite3
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from artemis.memory.store import MemoryStore
from artemis.memory.vault import make_excerpt
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool

MAX_NOTE_CHARS = 300
MAX_NOTES = 200
_DISABLED = "Kalıcı hafıza kapalı (persist_memory=false veya depo açılamadı)."
_PRIVATE_ANSWER = "[gizli içerik; yanıt gösterilmedi]"


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RememberArguments(Arguments):
    text: str = Field(max_length=MAX_NOTE_CHARS)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Not boş olamaz.")
        return value


class ForgetArguments(Arguments):
    note_id: int = Field(ge=1)


class ListArguments(Arguments):
    pass


class ConversationSearchArguments(Arguments):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)


def _store(context: ToolContext) -> MemoryStore | None:
    return cast(MemoryStore | None, context.services.get("memory_store"))


def _error(error: Exception) -> ToolResult:
    return ToolResult(status="error", message=f"Hafıza deposu hatası: {error}")


@register_tool
class MemoryRemember(BaseTool):
    name = "memory_remember"
    label = "Hafızaya yazma"
    done_label = "Hafızaya yazıldı"
    description = (
        "Kullanıcının kalıcı tercihini, kendisi hakkındaki bilgiyi veya kararını kısa bir "
        f"cümleyle (en çok {MAX_NOTE_CHARS} karakter) kalıcı hafızaya yazar."
    )
    args_model = RememberArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(RememberArguments, arguments)
        store = _store(context)
        if store is None:
            return ToolResult(status="error", message=_DISABLED)
        try:
            outcome, note_id = await asyncio.to_thread(
                store.add_note, args.text, context.turn_id, MAX_NOTES
            )
        except (OSError, sqlite3.Error) as error:
            return _error(error)
        if outcome == "full":
            return ToolResult(
                status="error",
                message=f"Hafıza dolu ({MAX_NOTES} not); önce memory_list ile bakıp "
                "memory_forget ile bir not sil.",
            )
        message = "Bu not zaten kayıtlı." if outcome == "duplicate" else "Hafızaya yazıldı."
        return ToolResult(
            success=True, status="ok", message=message, data={"id": note_id, "result": outcome}
        )


@register_tool
class MemoryForget(BaseTool):
    name = "memory_forget"
    label = "Hafızadan silme"
    done_label = "Hafızadan silindi"
    description = "memory_list'teki numarasıyla bir hafıza notunu siler."
    args_model = ForgetArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ForgetArguments, arguments)
        store = _store(context)
        if store is None:
            return ToolResult(status="error", message=_DISABLED)
        try:
            deleted = await asyncio.to_thread(store.delete_note, args.note_id)
        except (OSError, sqlite3.Error) as error:
            return _error(error)
        if not deleted:
            return ToolResult(status="error", message=f"{args.note_id} numaralı not yok.")
        return ToolResult(success=True, status="ok", message="Not silindi.")


@register_tool
class MemoryList(BaseTool):
    name = "memory_list"
    label = "Hafıza listesi"
    done_label = "Hafıza listelendi"
    description = "Kalıcı hafızadaki notları numaralarıyla listeler (yeniden eskiye)."
    args_model = ListArguments
    risk = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        store = _store(context)
        if store is None:
            return ToolResult(status="error", message=_DISABLED)
        try:
            notes = await asyncio.to_thread(store.list_notes)
        except (OSError, sqlite3.Error) as error:
            return _error(error)
        items = [{"id": n.id, "text": n.text, "created_at": n.created_at} for n in notes]
        return ToolResult(
            success=True, status="ok", message=f"{len(notes)} not var.", data={"notes": items}
        )


@register_tool
class ConversationSearch(BaseTool):
    name = "conversation_search"
    label = "Geçmiş konuşmalarda arama"
    done_label = "Geçmiş konuşmalarda arandı"
    description = (
        "Daha önceki konuşmalarda (sıfırlananlar dahil) sözcük araması yapar; tarih, kullanıcı "
        "mesajı ve yanıt alıntısı döndürür. 'Geçen sefer ne konuşmuştuk' türü sorular için."
    )
    args_model = ConversationSearchArguments
    risk = Risk.READ_ONLY

    # ponytail: sözcük (tf-idf) sıralaması; eş anlamlı/çekimli kaçırmalar ölçülür düzeyde
    # artarsa artemis/memory/embedding.py ile embedding araması eklenir.
    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ConversationSearchArguments, arguments)
        store = _store(context)
        if store is None:
            return ToolResult(status="error", message=_DISABLED)
        try:
            hits = await asyncio.to_thread(store.search_turns, args.query, args.limit)
        except (OSError, sqlite3.Error) as error:
            return _error(error)
        results = [
            {
                "date": hit.created_at,
                "user": make_excerpt(hit.user_text),
                "answer": _PRIVATE_ANSWER if hit.answer_private else make_excerpt(hit.answer),
            }
            for hit in hits
        ]
        return ToolResult(
            success=True,
            status="ok",
            message=f"{len(hits)} eşleşen tur bulundu." if hits else "Eşleşen tur bulunamadı.",
            data={"results": results},
            # Lekeli turun alıntısı dış içerik taşıyabilir: taint bu turda da sürer.
            external=any(hit.tainted for hit in hits),
        )
