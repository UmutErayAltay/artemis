"""Bilgisayar kullanımı aracı (ADR 0039): tek onayla ekranı görüp fare/klavyeyle iş yapar.

Varsayılan kapalıdır (`computer_use.enabled`). Açıkken çağrı CONFIRM_REQUIRED'dır; oturum
yalnız masaüstü uygulamasında çalışır çünkü Esc ile durdurma ve hassas adım onayı orada
vardır (`services["computer_host"]`). Sonuç `sent`'tir: modelin "bitti" demesi işin
yapıldığını kanıtlamaz (kural 5).
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from jarvis.brain.providers import Provider
from jarvis.brain.router import ProviderRouter
from jarvis.computer.session import ComputerSession, SessionResult
from jarvis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from jarvis.tools.registry import register_tool

_REASON_TEXT = {
    "done": "Model işi bitirdiğini bildirdi (doğrulanmadı)",
    "ask_user": "Model size bir soru soruyor",
    "limit": "Adım sınırına ulaşıldı",
    "timeout": "Süre doldu",
    "rejected": "Hassas adım reddedildi, oturum bitti",
    "stopped": "Oturum durduruldu",
    # Engel kullanıcı reddi değildir: model bunu "reddedildi, tekrarlama" diye okuyup kullanıcı
    # açıkça isteyince bile yeniden denemiyordu.
    "blocked": "Kod kuralı adımı engelledi (kullanıcı reddi değil; pencere değişince yeniden "
    "denenebilir)",
    "invalid": "Model geçerli bir eylem döndürmedi",
    "error": "Oturum hata ile bitti",
}


class ComputerUseArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=500)


@register_tool
class ComputerUse(BaseTool):
    name = "computer_use"
    label = "Bilgisayarı kullan"
    done_label = "Bilgisayar oturumu bitti"
    description = (
        "Ekranı görüp fareyle ve klavyeyle verilen hedefi yapar (yalnız masaüstü uygulaması). "
        "Her adımda ekran görüntüsü bir görsel modele gider; hassas adımlar ayrıca sorulur, "
        "Esc durdurur."
    )
    args_model = ComputerUseArguments
    risk = Risk.CONFIRM_REQUIRED
    external = True
    content_kind = "screen"
    outbound = True

    def risk_for(self, arguments: BaseModel, context: ToolContext) -> Risk:
        # Kapalıyken araç hiçbir şey yapmaz, yalnız "kapalı" der (screen_read ile aynı).
        if not context.settings.computer_use.enabled:
            return Risk.READ_ONLY
        # Otomatik kip (ADR 0039 ek karar): temiz turda başlangıç sorulmaz. Kapı kuralları aynen
        # işler: dış içerikten sonra ve `outbound` olduğu için önceki turlarda dış içerik
        # varken (ADR 0013) yine onay istenir.
        if context.settings.computer_use.mode == "auto":
            return Risk.SIDE_EFFECT
        return Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(ComputerUseArguments, arguments)
        settings = context.settings
        config = settings.computer_use
        if not config.enabled:
            return ToolResult(
                message="Bilgisayar kullanımı kapalı: computer_use.enabled: true gerekir."
            )
        host = context.services.get("computer_host")
        if host is None:
            return ToolResult(
                message="Bilgisayar kullanımı yalnız masaüstü uygulamasında çalışır; "
                "Esc ile durdurma ve adım onayı burada yok."
            )
        model = settings.models[config.model]
        # Görüntüyü yakalamadan ÖNCE reddedilir: ekran buluta yalnız açık izinle gider.
        if model.provider != "ollama" and not settings.perception.allow_cloud_screen:
            return ToolResult(
                message="Ekran görüntüsü bulut modele gönderilemez: yerel (ollama) model seçin "
                "ya da perception.allow_cloud_screen: true yapın."
            )
        provider = context.services.get("computer_provider")
        if provider is None:
            provider = ProviderRouter(
                settings.model_copy(
                    update={"primary_model": config.model, "fallback_model": None}
                ),
                context.audit,
            )
        backend = context.services.get("computer_backend")
        if backend is None:
            from jarvis.computer import windows  # yalnız Windows'ta işe yarar; geç yüklenir

            backend = windows

        with host.session() as (stop, on_step, approve):
            session = ComputerSession(
                provider=cast(Provider, provider),
                backend=backend,
                approve=approve,
                audit=context.audit,
                config=config,
                own_pid=os.getpid(),
                stop=stop,
                on_step=on_step,
                provider_timeout=settings.provider_timeout_seconds,
            )
            # İptal asyncio.Event'tir, durdurma threading.Event: küçük bir görev köprüler.
            watcher = asyncio.create_task(_relay_cancel(context.cancelled, stop))
            try:
                result = await session.run(args.goal, context.turn_id, context.deadline)
            finally:
                watcher.cancel()
        return _to_tool_result(result)


async def _relay_cancel(cancelled: asyncio.Event, stop: threading.Event) -> None:
    await cancelled.wait()
    stop.set()


def _to_tool_result(result: SessionResult) -> ToolResult:
    detail = f" {result.summary}" if result.summary else ""
    return ToolResult(
        success=False,
        status="sent",
        message=f"{_REASON_TEXT[result.reason]} ({result.steps} adım).{detail}",
        data={"reason": result.reason, "steps": result.steps},
        external=True,
        content_kind="screen",
    )
