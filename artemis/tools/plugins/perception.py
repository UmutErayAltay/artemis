"""Ekran okuma aracı (ADR 0029): onaylı, yerel OCR, görüntü saklanmaz.

Varsayılan kapalıdır (`perception.screen_enabled`). Açıkken her çağrı CONFIRM_REQUIRED'dır:
kullanıcı hangi kapsamın okunacağını onay penceresinde görür; gözetimsiz turda (ADR 0027)
kapı bunu zaten reddeder. Ekran metni dış içeriktir (turu lekeler) ve `screen` içerik
türüyle bulut için gizli sayılır.
"""

from __future__ import annotations

import asyncio
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from artemis.perception.screen import read_screen
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool


class ScreenReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["active_window", "screen"] = "active_window"


@register_tool
class ScreenRead(BaseTool):
    name = "screen_read"
    label = "Ekranı oku"
    done_label = "Ekran okundu"
    description = (
        "Etkin pencerenin (varsayılan) ya da tüm ekranın görünen metnini Windows'un yerel "
        "OCR'ıyla okur. Görüntü saklanmaz; ekranda görünen her şey (parola, mesaj) okunabilir."
    )
    args_model = ScreenReadArguments
    risk = Risk.CONFIRM_REQUIRED
    external = True
    content_kind = "screen"
    # Oto kipte computer_use ekran görüntüsünü zaten sormadan modele verir; yerel OCR daha
    # azını açar (ADR 0039 ek karar). Gözetimsiz tur yine okuyamaz: o kural kapıda önce gelir.
    auto_mode_ok = True

    def risk_for(self, arguments: BaseModel, context: ToolContext) -> Risk:
        # Kapalıyken araç hiçbir şey okumaz ve yalnız "kapalı" der; kullanıcıya boşuna onay
        # sorulmaz. Açıkken her okuma onaylıdır.
        if not context.settings.perception.screen_enabled:
            return Risk.READ_ONLY
        return Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ScreenReadArguments, arguments)
        config = context.settings.perception
        if not config.screen_enabled:
            return ToolResult(
                message="Ekran okuma kapalı: config içinde perception.screen_enabled: true gerekir."
            )
        reader = context.services.get("screen_reader", read_screen)
        outcome = await asyncio.to_thread(reader, args.scope, timeout=config.timeout_seconds)
        context.audit.emit(
            "screen_read",
            turn_id=context.turn_id,
            scope=args.scope,
            status=outcome.status,
            characters=len(outcome.text),
            language=outcome.language,
        )
        if outcome.status != "ok":
            return ToolResult(message=outcome.message)
        data: dict[str, object] = {
            "scope": args.scope,
            "text": outcome.text,
            "language": outcome.language,
            "truncated": outcome.truncated,
        }
        if outcome.window_title:
            data["window_title"] = outcome.window_title
        return ToolResult(
            success=True,
            status="ok",
            message="Ekran metni okundu.",
            data=data,
            external=True,
            content_kind="screen",
        )
