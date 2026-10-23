"""Kesintisiz ses kipini (uyandırma sözcüğü olmadan dinleme) modelin açıp kapatmasını sağlar."""

from typing import Any

from pydantic import BaseModel, ConfigDict

from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool


class VoiceContinuousArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


@register_tool
class VoiceContinuousTool(BaseTool):
    name = "voice_continuous"
    label = "Kesintisiz dinleme"
    done_label = "Dinleme kipi değişti"
    description = (
        "Sesli kesintisiz dinleme kipini açar veya kapatır. enabled=true: asistan uyandırma "
        "sözcüğü beklemeden her konuşmayı komut sayar ('beni sürekli dinle', 'kesintisiz moda "
        "geç', 'artık uyandırma sözcüğü olmadan dinle'). enabled=false: normal kip, yalnız "
        "uyandırma sözcüğüyle dinler ('normal moda dön', 'kesintisiz modu kapat'). Yalnız ses "
        "açıkken çalışır; ses kapalıysa bunu kullanıcıya söyle."
    )
    args_model = VoiceContinuousArguments
    # Yerel mikrofon kipi; onay politikası yalnız SafetyGate'tedir (lekeli turda kapı sorar).
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = VoiceContinuousArguments.model_validate(arguments)
        # Denetleyiciyi masaüstü köprüsü yalnız ses çalışırken kaydeder (rule 10: global yok).
        controller: Any = context.services.get("voice_controller")
        if controller is None:
            return ToolResult(message="Ses kapalı; önce Ses düğmesiyle açın.")
        controller.set_continuous(args.enabled)
        # Başarı yalnız durum gerçekten değiştiyse söylenir (kural 5).
        if bool(controller.continuous) != args.enabled:
            return ToolResult(status="error", message="Dinleme kipi değiştirilemedi.")
        state = "açıldı" if args.enabled else "kapatıldı"
        return ToolResult(
            success=True,
            status="ok",
            message=f"Kesintisiz dinleme {state}.",
            data={"continuous": args.enabled},
        )
