"""Bildirim araçları: sabit alıcıya Telegram mesajı ve yerel masaüstü toast'ı (ADR 0016)."""

import asyncio
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from artemis.channels.telegram.api import TelegramClient, TelegramError, build_telegram_client
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool
from artemis.tools.toast import TOAST_BODY_LIMIT, TOAST_TITLE_LIMIT, show_toast


class TelegramArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Alıcı argümanı bilerek YOK: model mesajın kime gideceğini seçemez (ADR 0016).
    text: str = Field(min_length=1, max_length=4000)


class DesktopNotifyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=TOAST_TITLE_LIMIT)
    body: str = Field(min_length=1, max_length=TOAST_BODY_LIMIT)


@register_tool
class NotifyTelegram(BaseTool):
    name = "notify_telegram"
    label = "Telegram mesajı"
    done_label = "Telegram'a gönderildi"
    description = (
        "Kullanıcının kendi Telegram sohbetine kısa bir bildirim mesajı gönderir. "
        "Alıcı sabittir, değiştirilemez."
    )
    args_model = TelegramArguments
    risk = Risk.SIDE_EFFECT
    # Metin makine dışına çıkar: dış içerik bağlamda kalmışsa yeni turda da onay sorulur.
    outbound = True
    external = False

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(TelegramArguments, arguments)
        chat_id = context.settings.telegram.chat_id
        injected = context.services.get("telegram_client")
        client: TelegramClient | None = injected
        built = False
        if client is None:
            client = build_telegram_client(context.settings, context.audit)
            built = True
        if client is None or chat_id is None:
            if built and client is not None:
                client.close()
            return ToolResult(
                status="error",
                message="Telegram yapılandırılmamış: telegram.chat_id ve token ortam "
                "değişkeni gerekli.",
            )
        try:
            sent = await asyncio.to_thread(
                client.send_message, chat_id, args.text, kind="notification"
            )
        except TelegramError as error:
            return ToolResult(
                status="error",
                message=f"Telegram mesajı gönderilemedi ({error.error_type}).",
            )
        except ValueError:
            return ToolResult(status="error", message="Telegram mesajı gönderilemedi (metin).")
        finally:
            if built:
                client.close()
        return ToolResult(
            success=True,
            status="ok",
            message="Telegram mesajı gönderildi.",
            data={"message_id": sent.message_id},
        )


@register_tool
class NotifyDesktop(BaseTool):
    name = "notify_desktop"
    label = "Bildirim"
    done_label = "Bildirim gösterildi"
    description = "Windows masaüstünde kısa bir bildirim (toast) gösterir."
    args_model = DesktopNotifyArguments
    risk = Risk.SIDE_EFFECT
    # Yalnız yerel ekrana yazar; ağa çıkmaz, bu yüzden outbound değildir.

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(DesktopNotifyArguments, arguments)
        toast = context.services.get("toast", show_toast)
        outcome = await asyncio.to_thread(toast, args.title, args.body)
        context.audit.emit(
            "desktop_notification", title=args.title, body=args.body, status=outcome.status
        )
        if outcome.status == "ok":
            return ToolResult(success=True, status="ok", message=outcome.message)
        return ToolResult(status="error", message=outcome.message)
