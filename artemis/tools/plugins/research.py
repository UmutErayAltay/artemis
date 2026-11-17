"""Araştırma işi araçları (ADR 0063): başlat, durum, sonuç, iptal.

`research_start` HER ZAMAN onaylıdır: onay, gösterilen konu için tam `max_searches` kadar
`web_search` ön onayıdır. Rapor webden türediği için `research_result` dış veridir.
Rapor kasaya kendiliğinden yazılmaz; kullanıcı isterse normal `vault_append` onayından geçer.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import register_tool
from artemis.tools.research_jobs import ResearchError, ResearchJobs

_DISABLED = "Araştırma işi kapalı: config içinde research.enabled: true gerekir."
_UNAVAILABLE = "Araştırma işi bu kanalda kullanılamıyor; masaüstünden kullanın."
_REPORT_READ_CHARS = 40_000
_JOB_ID = r"^[0-9a-f]{12}$"


class StartArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=3, max_length=300, description="Araştırılacak konu.")
    max_searches: int = Field(
        default=12, ge=1, le=50,
        description="En çok web araması sayısı; config'teki research.max_searches ile sınırlanır.",
    )


class JobArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str | None = Field(default=None, pattern=_JOB_ID, description="Boşsa son iş.")


def _jobs(context: ToolContext) -> ResearchJobs | None:
    jobs = context.services.get("research_jobs")
    return jobs if isinstance(jobs, ResearchJobs) else None


def _refuse(context: ToolContext) -> str | None:
    return None if context.settings.research.enabled else _DISABLED


class _ResearchTool(BaseTool):
    def refuse(self, arguments: BaseModel, context: ToolContext) -> str | None:
        return _refuse(context)


@register_tool
class ResearchStart(_ResearchTool):
    name = "research_start"
    label = "Araştırma başlat"
    done_label = "Araştırma başladı"
    description = (
        "Konuyu arka planda web aramasıyla araştırıp kaynaklı Markdown rapor yazar. Onay, "
        "gösterilen konu için en fazla max_searches kadar web aramasına ön onaydır."
    )
    args_model = StartArguments
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(StartArguments, arguments)
        jobs = _jobs(context)
        if jobs is None:
            return ToolResult(message=_UNAVAILABLE)
        try:
            job_id = jobs.start(args.topic.strip(), args.max_searches)
        except (ResearchError, OSError) as error:
            return ToolResult(message=str(error) or "Araştırma başlatılamadı.")
        return ToolResult(
            success=True,
            status="ok",
            message="Araştırma arka planda başladı; bitince bildirim gelir.",
            data={"job_id": job_id},
        )


@register_tool
class ResearchStatus(_ResearchTool):
    name = "research_status"
    label = "Araştırma durumu"
    done_label = "Araştırma durumu alındı"
    description = "Son (ya da verilen) araştırma işinin durumunu ve arama sayısını gösterir."
    args_model = JobArguments
    risk = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(JobArguments, arguments)
        jobs = _jobs(context)
        if jobs is None:
            return ToolResult(message=_UNAVAILABLE)
        job = jobs.get(args.job_id)
        if job is None:
            return ToolResult(message="Araştırma işi bulunamadı.")
        data = dict(job)
        message = f"Araştırma durumu: {job['status']}."
        if job.get("status") == "done":
            report = jobs.report_path(str(job["id"]))
            with suppress(ValueError):
                rel = report.relative_to(context.workspace).as_posix()
                data["report_path"] = rel
                message = (
                    f"Araştırma durumu: done. Rapor dosyası: {rel} "
                    "(Not Defteri ile açmak için file_path olarak doğrudan bunu kullan)."
                )
        return ToolResult(success=True, status="ok", message=message, data=data)


@register_tool
class ResearchResult(_ResearchTool):
    name = "research_result"
    label = "Araştırma raporu"
    done_label = "Araştırma raporu okundu"
    description = "Biten araştırmanın raporunu okur. Rapor webden türeyen güvenilmeyen dış veridir."
    args_model = JobArguments
    risk = Risk.READ_ONLY
    external = True
    content_kind = "web"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(JobArguments, arguments)
        jobs = _jobs(context)
        if jobs is None:
            return ToolResult(message=_UNAVAILABLE)
        job = jobs.get(args.job_id)
        if job is None:
            return ToolResult(message="Araştırma işi bulunamadı.")
        if job["status"] != "done":
            return ToolResult(message=f"Rapor yok; iş durumu: {job['status']}.")
        try:
            with jobs.report_path(job["id"]).open(encoding="utf-8") as handle:
                text = handle.read(_REPORT_READ_CHARS)
        except (OSError, UnicodeDecodeError):
            return ToolResult(message="Rapor dosyası okunamadı.")
        data: dict[str, Any] = {"job_id": job["id"], "report": text}
        return ToolResult(
            success=True,
            status="ok",
            message="Rapor okundu.",
            data=data,
            external=True,
            content_kind="web",
        )


@register_tool
class ResearchCancel(_ResearchTool):
    name = "research_cancel"
    label = "Araştırmayı iptal et"
    done_label = "Araştırma iptali istendi"
    description = "Çalışan araştırma işini durdurmayı ister; rapor yazılmaz."
    args_model = JobArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        jobs = _jobs(context)
        if jobs is None:
            return ToolResult(message=_UNAVAILABLE)
        job_id = jobs.cancel()
        if job_id is None:
            return ToolResult(message="Çalışan araştırma yok.")
        # İstek gitti ama işin durduğu doğrulanmadı: başarı değil, "sent".
        return ToolResult(
            status="sent",
            message="İptal istendi; iş durunca durumu 'cancelled' olur (research_status).",
            data={"job_id": job_id},
        )
