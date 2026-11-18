"""Rutin araçları (ADR 0058): listele, çalıştır, kaydet, sil.

Rutin sabit araç adımlarıdır; çalışırken model yoktur. `routine_run` her adımı çağrıldığı
turun kapısından (`context.gate_handle`) geçirir: leke, onay, gözetimsiz kuralı ve denetim
tek tek uygulanır. Rutin kaydetmek onay atlatmanın yolu olmasın diye `routine_save` ve
`routine_delete` her zaman onaylıdır, `routine_save` dış içerik okunmuş turda hiç sorulmadan
reddedilir. Zamanlama bu araçlarda YOKTUR; yalnız Ayarlar panelinden kurulur.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from artemis.brain.models import ToolCall
from artemis.proactive.routines import (
    MAX_ROUTINES,
    MAX_STEPS,
    Routine,
    RoutinesError,
    RoutineStep,
    RoutineStore,
    name_key,
    validate_steps,
)
from artemis.tools.base import BaseTool, ContentKind, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool

_NAME = r"^\w[\w -]{0,39}$"
_DISABLED = "Rutinler kapalı: config içinde routines.enabled: true gerekir."
_CONTENT_KIND_RANK: dict[ContentKind, int] = {
    "text": 0,
    "web": 1,
    "file": 2,
    "clipboard": 3,
    "screen": 4,
}


class RoutineNameArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME, description="Rutin adı, örn. 'işe başla'.")


class RoutineListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoutineSaveArguments(RoutineNameArguments):
    steps: list[RoutineStep] = Field(
        min_length=1,
        max_length=MAX_STEPS,
        description="Sırayla çalışacak sabit araç çağrıları: {tool, arguments}.",
    )


def _store(context: ToolContext) -> RoutineStore:
    return RoutineStore(context.settings.state_dir / "routines.json")


def _disabled(context: ToolContext) -> str | None:
    return None if context.settings.routines.enabled else _DISABLED


def _summary(routine: Routine) -> dict[str, Any]:
    schedule = routine.schedule
    return {
        "name": routine.name,
        "steps": [step.tool for step in routine.steps],
        "schedule": f"{schedule.at} {list(schedule.days)}" if schedule else None,
    }


@register_tool
class RoutineList(BaseTool):
    name = "routine_list"
    label = "Rutinleri listele"
    done_label = "Rutinler listelendi"
    description = "Kayıtlı rutinleri adları, adım araçları ve zamanlarıyla listeler."
    args_model = RoutineListArguments
    risk = Risk.READ_ONLY

    def refuse(self, arguments: BaseModel, context: ToolContext) -> str | None:
        return _disabled(context)

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        try:
            routines = _store(context).load()
        except RoutinesError as error:
            return ToolResult(message=str(error))
        return ToolResult(
            success=True,
            status="ok",
            message=f"{len(routines)} rutin var.",
            data={"routines": [_summary(item) for item in routines]},
        )


@register_tool
class RoutineRun(BaseTool):
    name = "routine_run"
    label = "Rutin çalıştır"
    done_label = "Rutin çalıştırıldı"
    description = (
        "Kayıtlı bir rutini adıyla çalıştırır: adımlar sırayla ve her biri kendi onay kurallarıyla "
        "yürür; onaylanmayan ya da başarısız ilk adımda durur."
    )
    args_model = RoutineNameArguments
    risk = Risk.SIDE_EFFECT

    def refuse(self, arguments: BaseModel, context: ToolContext) -> str | None:
        return _disabled(context)

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(RoutineNameArguments, arguments)
        handle = context.gate_handle
        if handle is None:
            # Kapıdan gelmeyen çağrıda adımları onaysız çalıştırmak yerine hiç çalıştırılmaz.
            return ToolResult(message="Rutin yalnız güvenlik kapısı üzerinden çalıştırılır.")
        try:
            routine = _store(context).find(args.name)
        except RoutinesError as error:
            return ToolResult(message=str(error))
        if routine is None:
            return ToolResult(message=f"«{args.name}» adında bir rutin yok.")
        try:
            steps = validate_steps(routine.steps, handle.registry)
        except ValueError as error:
            return ToolResult(message=f"Rutin çalıştırılmadı: {error}")
        audit = context.audit
        audit.emit(
            "routine_run_start", turn_id=context.turn_id, name=routine.name, steps=len(steps)
        )
        results: list[ToolResult] = []
        for number, step in enumerate(steps, start=1):
            call = ToolCall(
                id=f"{context.turn_id}:routine:{number}", name=step.tool, arguments=step.arguments
            )
            results.append(await handle.dispatch(call))
            # "sent" (ör. uygulama açma: komut gitti, açıldığı doğrulanmadı) rutini durdurmaz;
            # yoksa "işe başla" ilk uygulamada biterdi. Hata ve ret durdurur.
            if not (results[-1].success or results[-1].status == "sent"):
                break
        done = sum(1 for item in results if item.success or item.status == "sent")
        unverified = sum(1 for item in results if item.status == "sent")
        complete = done == len(steps)
        audit.emit(
            "routine_run_end",
            turn_id=context.turn_id,
            name=routine.name,
            steps=len(steps),
            steps_completed=done,
            completed=complete,
        )
        data: dict[str, Any] = {
            "steps": [
                {"tool": step.tool, "status": item.status, "success": item.success}
                for step, item in zip(steps, results, strict=False)
            ],
            "steps_total": len(steps),
            "steps_completed": done,
        }
        # Adım sonuçları dış ya da gizli içerik taşıyabilir (ör. dosya okuma);
        # en kısıtlayıcı tür kazanır.
        external = any(item.external for item in results)
        private = any(item.private for item in results)
        kind: ContentKind = (
            max(
                (item.content_kind for item in results),
                key=lambda item_kind: _CONTENT_KIND_RANK.get(item_kind, 0),
            )
            if results
            else "text"
        )
        if complete:
            # Doğrulanmamış adım varsa başarı denmez (kural 5): rutin de "sent" döner.
            return ToolResult(
                success=unverified == 0,
                status="ok" if unverified == 0 else "sent",
                message=(
                    f"«{routine.name}» tamamlandı: {done}/{len(steps)} adım."
                    if unverified == 0
                    else f"«{routine.name}»: {done}/{len(steps)} adım gönderildi; "
                    f"{unverified} adımın sonucu doğrulanmadı."
                ),
                data=data,
                external=external,
                private=private,
                content_kind=kind,
            )
        last = results[-1]
        reason = "onaylanmadı" if last.status == "denied" else f"başarısız ({last.message})"
        return ToolResult(
            status="denied" if last.status == "denied" else "error",
            message=(
                f"«{routine.name}» durdu: {done}/{len(steps)} adım tamamlandı; "
                f"{len(results)}. adım ({steps[len(results) - 1].tool}) {reason}."
            ),
            data=data,
            external=external,
            private=private,
            content_kind=kind,
        )


@register_tool
class RoutineSave(BaseTool):
    name = "routine_save"
    label = "Rutin kaydet"
    done_label = "Rutin kaydedildi"
    description = (
        "Adlandırılmış bir rutin kaydeder: sabit, sırayla çalışacak araç çağrıları. Zaman "
        "ayarlayamaz, rutin içinden rutin çağıramaz. Her zaman onay ister; onayda tüm adımlar "
        "gösterilir."
    )
    args_model = RoutineSaveArguments
    risk = Risk.CONFIRM_REQUIRED

    def refuse(self, arguments: BaseModel, context: ToolContext) -> str | None:
        if (reason := _disabled(context)) is not None:
            return reason
        # memory_rule_add ile aynı kural (ADR 0054): dış içerik okunmuş turda kalıcı iş
        # tarifi yazılmaz; enjekte edilmiş metin sonradan "kendi rutinim" diye çalışmasın.
        if context.turn_read_external:
            return (
                "Rutin kaydedilmedi: bu turda dış içerik okundu; "
                "rutinler yalnız temiz turda kaydedilir."
            )
        return None

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(RoutineSaveArguments, arguments)
        refusal = self.refuse(args, context)
        if refusal is not None:
            return ToolResult(message=refusal)
        handle = context.gate_handle
        if handle is None:
            return ToolResult(message="Rutin yalnız güvenlik kapısı üzerinden kaydedilir.")
        name = " ".join(args.name.split())
        try:
            steps = validate_steps(args.steps, handle.registry)
        except ValueError as error:
            return ToolResult(message=f"Rutin kaydedilmedi: {error}")
        store = _store(context)
        try:
            routines = store.load()
            key = name_key(name)
            old = next((item for item in routines if name_key(item.name) == key), None)
            if old is None and len(routines) >= MAX_ROUTINES:
                return ToolResult(message=f"En çok {MAX_ROUTINES} rutin kaydedilebilir.")
            # Var olan rutinin kullanıcı tarafından Ayarlar'da kurulan zamanı korunur.
            new = Routine(name=name, steps=steps, schedule=old.schedule if old else None)
            kept = [new if item is old else item for item in routines]
            store.save(kept if old is not None else [*kept, new])
            stored = store.find(name)
        except RoutinesError as error:
            return ToolResult(message=str(error))
        if stored is None or stored.steps != steps:
            return ToolResult(message="Rutin kaydı doğrulanamadı.")
        return ToolResult(
            success=True,
            status="ok",
            message=f"«{name}» rutini kaydedildi ({len(steps)} adım).",
            data={"name": name, "steps": len(steps)},
        )


@register_tool
class RoutineDelete(BaseTool):
    name = "routine_delete"
    label = "Rutin sil"
    done_label = "Rutin silindi"
    description = "Kayıtlı bir rutini adıyla siler. Her zaman onay ister."
    args_model = RoutineNameArguments
    risk = Risk.CONFIRM_REQUIRED

    def refuse(self, arguments: BaseModel, context: ToolContext) -> str | None:
        return _disabled(context)

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(RoutineNameArguments, arguments)
        store = _store(context)
        key = name_key(args.name)
        try:
            routines = store.load()
            kept = [item for item in routines if name_key(item.name) != key]
            if len(kept) == len(routines):
                return ToolResult(message=f"«{args.name}» adında bir rutin yok.")
            store.save(kept)
            gone = store.find(args.name) is None
        except RoutinesError as error:
            return ToolResult(message=str(error))
        if not gone:
            return ToolResult(message="Rutin silinemedi; kayıt hâlâ duruyor.")
        return ToolResult(success=True, status="ok", message=f"«{args.name}» rutini silindi.")
