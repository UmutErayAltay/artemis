"""Pencere ve sistem kontrolü araçları (ADR 0031).

Windows işleri `SystemControl` servisindedir (sabit PowerShell betikleri); bu dosya yalnız
argüman doğrulaması, risk sınıfı ve dürüst sonuç eşlemesini yapar. Servis
`context.services["system_control"]` ile değiştirilebilir; testler sahte servis kullanır.
"""

from __future__ import annotations

import asyncio
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jarvis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from jarvis.tools.registry import register_tool
from jarvis.tools.system_control import SystemControl, SystemOutcome


def _service(context: ToolContext) -> SystemControl:
    service = context.services.get("system_control")
    if service is None:
        service = SystemControl(timeout=context.settings.system_control.timeout_seconds)
        context.services["system_control"] = service
    return cast(SystemControl, service)


def _result(outcome: SystemOutcome, data: dict[str, object] | None = None) -> ToolResult:
    """`sent` başarı değildir: komut verildi ama gerçekleştiği doğrulanamaz (CLAUDE.md 5)."""
    if outcome.status == "ok":
        return ToolResult(success=True, status="ok", message=outcome.message, data=data or {})
    if outcome.status == "sent":
        return ToolResult(success=False, status="sent", message=outcome.message)
    return ToolResult(message=outcome.message)


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


@register_tool
class WindowList(BaseTool):
    name = "window_list"
    label = "Pencereleri listele"
    done_label = "Pencereler listelendi"
    description = (
        "Açık uygulama pencerelerini (kimlik, başlık, süreç adı, etkin mi) listeler. "
        "Pencereyi öne getirmek veya yerleştirmek için önce bunu kullanın."
    )
    args_model = NoArguments
    risk = Risk.READ_ONLY
    # Başlıklar e-posta konusu, belge adı, web sayfası başlığı taşır: dış ve gizli içerik.
    external = True
    content_kind = "screen"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        outcome = await asyncio.to_thread(_service(context).list_windows)
        if outcome.status != "ok":
            return _result(outcome)
        windows = [
            {"id": item.id, "title": item.title, "process": item.process, "active": item.active}
            for item in outcome.windows
        ]
        return ToolResult(
            success=True,
            status="ok",
            message=outcome.message,
            data={"windows": windows, "truncated": outcome.truncated},
            external=True,
            content_kind="screen",
        )


class WindowControlArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_id: int = Field(gt=0, description="window_list'ten gelen pencere kimliği")
    action: Literal[
        "focus",
        "maximize",
        "minimize",
        "restore",
        "left",
        "right",
        "move_to_screen",
    ]
    # Neden: Pydantic lax modunda True değerini 1'e dönüştürebilir; yanlışlıkla birincil
    # ekranın hedeflenmesini engellemek için ekran numarası gerçek bir tam sayı olmalıdır.
    screen: int | None = Field(
        default=None,
        ge=1,
        le=8,
        strict=True,
        description="move_to_screen için hedef ekran (1 birincil)",
    )

    # Neden: Yanlış ekran seçimini yok saymak yerine şemayı reddetmek, hedef monitörün
    # belirsizleşmesini ve istenmeyen taşımayı engeller.
    @model_validator(mode="after")
    def _screen_matches_action(self) -> WindowControlArguments:
        if self.action == "move_to_screen":
            if self.screen is None:
                raise ValueError("move_to_screen için screen verilmeli")
        elif self.screen is not None:
            raise ValueError("screen yalnız move_to_screen işleminde kullanılabilir")
        return self


@register_tool
class WindowControl(BaseTool):
    name = "window_control"
    label = "Pencereyi düzenle"
    done_label = "Pencere düzenlendi"
    description = (
        "Bir pencereyi öne getirir (focus), büyütür, simge durumuna küçültür, eski boyutuna "
        "döndürür, ekranın sol/sağ yarısına yerleştirir veya pencereyi başka ekrana taşır "
        "(move_to_screen + screen). window_id window_list'ten gelir."
    )
    args_model = WindowControlArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(WindowControlArguments, arguments)
        outcome = await asyncio.to_thread(
            _service(context).control_window,
            args.window_id,
            args.action,
            screen=args.screen,
        )
        data: dict[str, object] = {"window_id": args.window_id, "action": args.action}
        if args.screen is not None:
            data["screen"] = args.screen
        return _result(outcome, data)


class VolumeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: int | None = Field(default=None, ge=0, le=100, description="Ses düzeyi yüzde")
    muted: bool | None = Field(default=None, description="true sessiz, false sesi aç")

    @model_validator(mode="after")
    def _needs_one(self) -> VolumeArguments:
        if self.level is None and self.muted is None:
            raise ValueError("level veya muted verilmeli")
        return self


@register_tool
class VolumeSet(BaseTool):
    name = "volume_set"
    label = "Sesi ayarla"
    done_label = "Ses ayarlandı"
    description = "Varsayılan hoparlörün ses düzeyini (0-100) ayarlar ya da sesi kapatıp açar."
    args_model = VolumeArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(VolumeArguments, arguments)
        outcome = await asyncio.to_thread(_service(context).set_volume, args.level, args.muted)
        return _result(outcome, {"level": outcome.level, "muted": outcome.muted})


class BrightnessArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: int = Field(ge=0, le=100, description="Parlaklık yüzde")


@register_tool
class BrightnessSet(BaseTool):
    name = "brightness_set"
    label = "Parlaklığı ayarla"
    done_label = "Parlaklık ayarlandı"
    description = "Dizüstü dahili ekranın parlaklığını (0-100) ayarlar; harici monitörde çalışmaz."
    args_model = BrightnessArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(BrightnessArguments, arguments)
        outcome = await asyncio.to_thread(_service(context).set_brightness, args.level)
        return _result(outcome, {"level": outcome.level})


class PowerArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["lock", "sleep", "shutdown", "restart", "cancel_shutdown"]


# Kilit ve iptal iş kaybettirmez; diğerleri kaydedilmemiş işi ve çalışan işleri etkiler.
_CONFIRMED_POWER_ACTIONS = frozenset({"sleep", "shutdown", "restart"})


@register_tool
class Power(BaseTool):
    name = "power"
    label = "Güç işlemi"
    done_label = "Güç komutu verildi"
    description = (
        "Bilgisayarı kilitler (lock), uyku moduna alır (sleep), kapatır (shutdown) ya da yeniden "
        "başlatır (restart); kapatma/yeniden başlatma gecikmelidir ve cancel_shutdown ile "
        "iptal edilebilir. Uyku, kapatma ve yeniden başlatma her zaman onay ister."
    )
    args_model = PowerArguments
    risk = Risk.CONFIRM_REQUIRED

    def risk_for(self, arguments: BaseModel, context: ToolContext) -> Risk:
        args = cast(PowerArguments, arguments)
        if args.action in _CONFIRMED_POWER_ACTIONS:
            return Risk.CONFIRM_REQUIRED
        return Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(PowerArguments, arguments)
        delay = context.settings.system_control.shutdown_delay_seconds
        service = _service(context)
        outcome = await asyncio.to_thread(lambda: service.power(args.action, delay_seconds=delay))
        context.audit.emit(
            "power",
            turn_id=context.turn_id,
            action=args.action,
            delay_seconds=delay if args.action in ("shutdown", "restart") else None,
            status=outcome.status,
        )
        return _result(outcome, {"action": args.action})
