"""Her kanaldan gelen çağrı için tek onay ve denetim noktası."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import ValidationError

from jarvis.tools.base import Risk, ToolContext, ToolResult, normalize_risk
from jarvis.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from jarvis.brain.models import ToolCall

ApprovalCallback = Callable[[str, dict[str, Any]], bool]
TAINT_NOTICE = "Bu adım dış bir içerik okunduktan sonra istendi"
# Taint bu turda değil, bağlamdaki önceki bir turdan geliyorsa kullanıcıya bu söylenir.
HISTORY_TAINT_NOTICE = TAINT_NOTICE + " (önceki bir turda)"


@dataclass
class TurnState:
    turn_id: str = field(default_factory=lambda: str(uuid4()))
    tainted: bool = False
    # Modele verilen geçmişte dış içerikli bir tur var (ADR 0013). `tainted` gibi tur
    # içinde değişmez; yalnız `outbound` araçlar için onay nedenidir.
    history_external: bool = False
    deadline: float | None = None


class SafetyGate:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.pending_notice = ""

    async def dispatch(
        self,
        call: ToolCall,
        context: ToolContext,
        state: TurnState,
        approve: ApprovalCallback,
    ) -> ToolResult:
        started = monotonic()
        context.turn_id = state.turn_id
        context.deadline = state.deadline
        self.pending_notice = ""
        context.audit.emit(
            "tool_requested", turn_id=state.turn_id, tool=call.name, arguments=call.arguments
        )
        tool = self.registry.get(call.name)
        if tool is None:
            return self._finish(
                ToolResult(message=f"Kayıtlı olmayan araç çalıştırılmadı: {call.name}"),
                call,
                context,
                state,
                started,
            )
        if context.cancelled.is_set():
            return self._finish(
                ToolResult(message="İşlem iptal edildi."), call, context, state, started
            )
        if state.deadline is not None and monotonic() >= state.deadline:
            return self._finish(
                ToolResult(message="Süre sınırı dolduğu için işlem yapılmadı."),
                call,
                context,
                state,
                started,
            )
        try:
            arguments = tool.args_model.model_validate(deepcopy(call.arguments))
        except ValidationError:
            # Pydantic ham girdiyi hata metnine ekleyebilir; sırları taşımıyoruz.
            return self._finish(
                ToolResult(message="Araç argümanları şemaya uymuyor."),
                call,
                context,
                state,
                started,
            )
        risk = normalize_risk(tool.risk)
        history_outbound = state.history_external and tool.outbound
        requires_approval = (
            risk is Risk.CONFIRM_REQUIRED
            or (state.tainted and risk is not Risk.READ_ONLY)
            or (history_outbound and risk is not Risk.READ_ONLY)
        )
        normalized = arguments.model_dump(mode="json")
        approved: bool | None = None
        if requires_approval:
            if state.tainted:
                notice = TAINT_NOTICE
            elif history_outbound:
                notice = HISTORY_TAINT_NOTICE
            else:
                notice = "Bu işlem onay gerektirir."
            # Onay ekranı tool açıklamasını da taşır; kullanıcı etkilenecek kapsamı
            # (ör. uygulamanın tüm süreçlerinin zorla kapatılmasını) görebilir.
            self.pending_notice = f"{notice}. {tool.description}"
            approved = approve(call.name, deepcopy(normalized)) is True
            context.audit.emit(
                "approval",
                turn_id=state.turn_id,
                tool=call.name,
                arguments=normalized,
                approved=approved,
                notice=self.pending_notice,
            )
            if not approved:
                return self._finish(
                    ToolResult(status="denied", message="Kullanıcı işlemi onaylamadı."),
                    call,
                    context,
                    state,
                    started,
                )
        # Onay sırasında iptal geldiyse araç çalıştırılmaz.
        if context.cancelled.is_set():
            return self._finish(
                ToolResult(message="İşlem iptal edildi."), call, context, state, started
            )
        if state.deadline is not None and monotonic() >= state.deadline:
            return self._finish(
                ToolResult(message="Süre sınırı dolduğu için işlem yapılmadı."),
                call,
                context,
                state,
                started,
            )
        context.audit.emit(
            "tool_dispatch",
            turn_id=state.turn_id,
            tool=call.name,
            arguments=normalized,
            risk=risk.value,
            confirmation_required=requires_approval,
            approved=approved,
            tainted=state.tainted,
            outbound=tool.outbound,
            history_external=state.history_external,
        )
        try:
            result = await tool.execute(arguments, context)
        except asyncio.CancelledError:
            context.audit.emit(
                "tool_interrupted",
                turn_id=state.turn_id,
                tool=call.name,
                completion="unknown",
                reason="Araç görevi iptal edildi; alt işlemin tamamlanması doğrulanamadı.",
            )
            self._finish(
                ToolResult(message="Araç iptal edildi; işlemin tamamlanması doğrulanamadı."),
                call,
                context,
                state,
                started,
            )
            raise
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            context.audit.emit(
                "tool_exception",
                turn_id=state.turn_id,
                tool=call.name,
                error_type=type(error).__name__,
                reason="Araç yürütmesi başarısız oldu.",
            )
            result = ToolResult(message=f"Araç işlemi başarısız oldu ({type(error).__name__}).")
        if tool.external or result.external:
            state.tainted = True
            result = result.model_copy(update={"external": True})
        if tool.content_kind != "text":
            result = result.model_copy(update={"content_kind": tool.content_kind})
        return self._finish(result, call, context, state, started)

    @staticmethod
    def _finish(
        result: ToolResult,
        call: ToolCall,
        context: ToolContext,
        state: TurnState,
        started: float,
    ) -> ToolResult:
        context.audit.emit(
            "tool_result",
            turn_id=state.turn_id,
            tool=call.name,
            result=result.model_dump(mode="json"),
            tainted=state.tainted,
            elapsed_seconds=monotonic() - started,
        )
        return result
