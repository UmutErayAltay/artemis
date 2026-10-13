"""Kod ajanı araçları (ADR 0023): izinli projede arka planda headless Claude Code."""

from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from jarvis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from jarvis.tools.code_agent_jobs import CodeAgentError, CodeAgentJobs
from jarvis.tools.registry import register_tool


class StartArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Yol değil takma ad: model dosya yolu seçemez, yalnız config'teki projeleri.
    project: str = Field(min_length=1, max_length=32)
    task: str = Field(min_length=1, max_length=4000)


class StatusArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str | None = Field(default=None, max_length=32)


class CancelArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=32)


def _jobs(context: ToolContext) -> CodeAgentJobs | None:
    jobs = context.services.get("code_agent_jobs")
    return jobs if isinstance(jobs, CodeAgentJobs) else None


@register_tool
class CodeAgentStart(BaseTool):
    name = "code_agent_start"
    label = "Kod ajanına görev ver"
    done_label = "Görev kod ajanına gönderildi"
    description = (
        "İzinli bir projede (takma ad ile) kodlama görevini arka planda çalışan kod ajanına "
        "devreder. Ajan dosya düzenler ve yerel commit atar; push yapamaz. Bitince bildirim "
        "gelir. Çalışma ağacı temiz olmalıdır."
    )
    args_model = StartArguments
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(StartArguments, arguments)
        config = context.settings.code_agent
        jobs = _jobs(context)
        path = config.projects.get(args.project)
        if path is None or jobs is None:
            allowed = ", ".join(sorted(config.projects)) or "(hiçbiri; özellik kapalı)"
            return ToolResult(
                status="error",
                message=f"Bilinmeyen proje '{args.project}'. İzinli projeler: {allowed}.",
            )
        try:
            job_id = jobs.start(
                args.project,
                path,
                args.task,
                command=config.command,
                max_minutes=config.max_minutes,
                model=config.model,
            )
        except CodeAgentError as error:
            return ToolResult(status="error", message=str(error))
        base = next(j["base"] for j in jobs.status() if j["job_id"] == job_id)
        return ToolResult(
            status="sent",
            message=f"Görev kod ajanına gönderildi (iş {job_id}, taban {base[:10]}). "
            "Bitince bildirim gelecek.",
            data={"job_id": job_id, "base": base},
        )


@register_tool
class CodeAgentStatus(BaseTool):
    name = "code_agent_status"
    label = "Kod ajanı durumu"
    done_label = "Kod ajanı durumu okundu"
    description = "Kod ajanı işlerinin durumunu, commit'lerini ve özetini listeler."
    args_model = StatusArguments
    risk = Risk.READ_ONLY
    # Özet repo içeriğinden üretilir: dış veridir, turu lekeler.
    external = True

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(StatusArguments, arguments)
        jobs = _jobs(context)
        listed = [] if jobs is None else jobs.status()
        if args.job_id is not None:
            listed = [j for j in listed if j["job_id"] == args.job_id]
        return ToolResult(
            success=True,
            status="ok",
            message=f"{len(listed)} kod ajanı işi.",
            data={"jobs": listed},
            external=True,
        )


@register_tool
class CodeAgentCancel(BaseTool):
    name = "code_agent_cancel"
    label = "Kod ajanını durdur"
    done_label = "Kod ajanı durdurma isteği gönderildi"
    description = "Çalışan bir kod ajanı işini sonlandırır."
    args_model = CancelArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(CancelArguments, arguments)
        jobs = _jobs(context)
        if jobs is None or not jobs.cancel(args.job_id):
            return ToolResult(
                status="error", message=f"Çalışan iş bulunamadı: {args.job_id}."
            )
        # Süreç öldürme isteği iletildi; bitiş thread'in raporuyla (code_agent_done) doğrulanır.
        return ToolResult(
            status="sent",
            message=f"İş {args.job_id} için durdurma gönderildi. Ajanın yarım bıraktığı "
            "değişiklikler diskte kalır; geri almak sizde.",
        )
