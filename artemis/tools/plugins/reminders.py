"""Hatırlatma araçları (ADR 0027): kurma, listeleme, iptal.

Hatırlatma sabit bir metindir; zamanı gelince model çağrılmadan teslim edilir. Kurma yalnız
yerel SQLite'a yazar (SIDE_EFFECT, ağa çıkmaz): temiz turda onaysız, lekeli turda onaylı.
Kurulduğu turun lekeli/gizli durumu kayda yazılır; teslimde gösterilir ve gizli kayıt
Telegram'a gitmez. Brifing istemi kuran bir araç bilerek YOKTUR.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from artemis.proactive.schedule import format_local, parse_local
from artemis.proactive.store import ProactiveStore, ProactiveStoreError
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool

MAX_AHEAD = timedelta(days=365)
_DISABLED = "Hatırlatmalar kapalı: config içinde proactive.enabled: true gerekir."


class ReminderCreateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    at: str | None = Field(
        default=None, description="Yerel saat, 'YYYY-MM-DD HH:MM' biçiminde."
    )
    in_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 365)

    @model_validator(mode="after")
    def exactly_one_time(self) -> ReminderCreateArguments:
        if (self.at is None) == (self.in_minutes is None):
            raise ValueError("at veya in_minutes alanlarından tam olarak biri verilmeli")
        return self


class ReminderListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReminderCancelArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reminder_id: int = Field(ge=1)


def _store(context: ToolContext) -> ProactiveStore | None:
    store = context.services.get("proactive_store")
    return store if isinstance(store, ProactiveStore) else None


def _now(context: ToolContext) -> datetime:
    # Testler sahte saat verir; gerçek çalışmada UTC şimdi.
    clock = cast(Callable[[], datetime] | None, context.services.get("proactive_clock"))
    return clock() if clock is not None else datetime.now(UTC)


@register_tool
class ReminderCreate(BaseTool):
    name = "reminder_create"
    label = "Hatırlatma kur"
    done_label = "Hatırlatma kuruldu"
    description = (
        "Belirtilen yerel saatte (at: 'YYYY-MM-DD HH:MM') ya da şu andan dakika sonra "
        "(in_minutes) kullanıcıya sabit bir hatırlatma metni gösterir. Artemis açık olmalıdır."
    )
    args_model = ReminderCreateArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ReminderCreateArguments, arguments)
        store = _store(context)
        if store is None:
            return ToolResult(message=_DISABLED)
        tz = ZoneInfo(context.settings.proactive.timezone)
        now = _now(context)
        if args.at is not None:
            try:
                due = parse_local(args.at, tz)
            except ValueError:
                return ToolResult(message="Saat 'YYYY-MM-DD HH:MM' biçiminde olmalı.")
        else:
            due = now + timedelta(minutes=args.in_minutes or 0)
        if due <= now:
            return ToolResult(message="Hatırlatma zamanı geçmişte; kurulmadı.")
        if due - now > MAX_AHEAD:
            return ToolResult(message="Hatırlatma en çok bir yıl sonrasına kurulabilir.")
        try:
            created = store.add_reminder(
                args.text,
                due,
                tainted=context.turn_tainted,
                private=context.turn_private,
                now=now,
            )
            stored = store.get_reminder(created.id)
        except ProactiveStoreError:
            return ToolResult(message="Hatırlatma kaydedilemedi (yerel depo hatası).")
        if stored is None or stored.cancelled or stored.text != args.text:
            return ToolResult(message="Hatırlatma kaydı doğrulanamadı.")
        local = format_local(stored.due_at, tz)
        return ToolResult(
            success=True,
            status="ok",
            message=f"Hatırlatma {local} için kuruldu.",
            data={"reminder_id": stored.id, "due_local": local},
        )


@register_tool
class ReminderList(BaseTool):
    name = "reminder_list"
    label = "Hatırlatmaları listele"
    done_label = "Hatırlatmalar listelendi"
    description = "Henüz zamanı gelmemiş veya teslim edilmemiş hatırlatmaları listeler."
    args_model = ReminderListArguments
    risk = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        store = _store(context)
        if store is None:
            return ToolResult(message=_DISABLED)
        tz = ZoneInfo(context.settings.proactive.timezone)
        try:
            pending = store.pending_reminders()
        except ProactiveStoreError:
            return ToolResult(message="Hatırlatmalar okunamadı (yerel depo hatası).")
        items: list[dict[str, Any]] = [
            {
                "reminder_id": item.id,
                "text": item.text,
                "due_local": format_local(item.due_at, tz),
                "created_after_external_content": item.created_tainted,
            }
            for item in pending
        ]
        # Dış içerikten kurulan metin yine dış içeriktir; gizli kayıt buluta gitmez.
        return ToolResult(
            success=True,
            status="ok",
            message=f"{len(items)} bekleyen hatırlatma var.",
            data={"reminders": items},
            external=any(item.created_tainted for item in pending),
            content_kind="file" if any(item.created_private for item in pending) else "text",
        )


@register_tool
class ReminderCancel(BaseTool):
    name = "reminder_cancel"
    label = "Hatırlatmayı iptal et"
    done_label = "Hatırlatma iptal edildi"
    description = "Henüz teslim edilmemiş bir hatırlatmayı numarasıyla iptal eder."
    args_model = ReminderCancelArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ReminderCancelArguments, arguments)
        store = _store(context)
        if store is None:
            return ToolResult(message=_DISABLED)
        try:
            cancelled = store.cancel_reminder(args.reminder_id, _now(context))
        except ProactiveStoreError:
            return ToolResult(message="Hatırlatma iptal edilemedi (yerel depo hatası).")
        if not cancelled:
            return ToolResult(
                message="Hatırlatma bulunamadı ya da zaten teslim edildi veya iptal edildi."
            )
        return ToolResult(success=True, status="ok", message="Hatırlatma iptal edildi.")
