"""F5 araçları: taslak hazırlama da dahil bütün kanallar aynı kapıyı kullanır."""

import asyncio
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from artemis.config.settings import OWN_REPO_ALIAS, CodeAgentConfig, ConfigChoiceError
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.code_agent_backends import resolve_executable, visible_supported
from artemis.tools.code_agent_jobs import CodeAgentError, CodeAgentJobs
from artemis.tools.github_repos import discover_projects
from artemis.tools.project_spec import AUTHORITY_BOUNDARY, ProjectSpec
from artemis.tools.registry import register_tool


class PrepareArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    task: str = Field(min_length=1, max_length=4000, repr=False)
    scope: tuple[str, ...] = Field(min_length=1, max_length=16, repr=False)
    acceptance: tuple[str, ...] = Field(min_length=1, max_length=16, repr=False)
    # Yalnız yapılandırılmış TAKMA AD; desen yol ayırıcıyı ve boşluğu dışlar (kural 6).
    agent: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,32}$")
    workdir: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,32}$")
    # Çalışma kipi: verilmezse projenin/genel varsayılanı (arka plan) kullanılır.
    mode: Literal["background", "visible"] | None = None


class StartArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Yeni onay tam snapshot taşır. Eski project/task istemcileri kapsamı görünür tutar.
    spec: ProjectSpec | None = Field(default=None, repr=False)
    project: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,32}$")
    task: str | None = Field(default=None, min_length=1, max_length=4000, repr=False)
    authority_boundary: str = AUTHORITY_BOUNDARY

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.authority_boundary != AUTHORITY_BOUNDARY:
            raise ValueError("Yetki sınırı değiştirilemez.")
        if self.spec is None and (self.project is None or self.task is None):
            raise ValueError("Tam spec veya eski project/task gerekli.")
        if self.spec is not None and (self.project is not None or self.task is not None):
            raise ValueError("Çelişen görev alanları kabul edilmez.")
        return self


class StatusArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str | None = Field(default=None, max_length=32)


class CancelArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=32)


class DiscardArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec_id: str = Field(min_length=1, max_length=64)


class AnswerArguments(CancelArguments):
    question_id: str = Field(min_length=1, max_length=64)
    answer: str = Field(min_length=1, max_length=4000, repr=False)


def _jobs(context: ToolContext) -> CodeAgentJobs | None:
    jobs = context.services.get("code_agent_jobs")
    return jobs if isinstance(jobs, CodeAgentJobs) else None


def available_projects(config: CodeAgentConfig) -> dict[str, Path]:
    """Yerel keşif her çağrıda yenilenir; açık ayarlar eşitlenen takma adları ezer."""
    found: dict[str, Path] = {}
    if config.github_owner and config.repos_dir:
        found = discover_projects(config.github_owner, config.repos_dir)
    return {**found, **config.projects}


def _resolve_workdir(context: ToolContext, alias: str, repo: Path) -> Path:
    return context.settings.code_agent.resolve_workdir(
        alias, repo, context.settings.memory.vault_path
    )


def _agent_status(config: CodeAgentConfig) -> dict[str, dict[str, bool]]:
    status: dict[str, dict[str, bool]] = {}
    for name in config.agent_names():
        _, profile = config.resolve_agent(name)
        status[name] = {
            "found": resolve_executable(profile) is not None,
            "visible": visible_supported(profile),
        }
    return status


@register_tool
class CodeAgentPrepare(BaseTool):
    name = "code_agent_prepare"
    label = "Proje taslağı hazırla"
    done_label = "Proje taslağı hazırlandı"
    description = (
        "Proje, görev, kapsam ve kabul ölçütlerini içeren değişmez yerel taslak hazırlar. "
        "İsteğe bağlı agent ve workdir yalnız yapılandırılmış takma adlardır (yol verme). "
        "Eksik ayrıntıları önce kullanıcıya sorun. Kodlayıcı veya subprocess başlatmaz. "
        "Sonraki code_agent_start çağrısı dönen spec'in tam kopyasını taşımalıdır."
    )
    args_model = PrepareArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(PrepareArguments, arguments)
        jobs = _jobs(context)
        # Hazırlama subprocess çalıştırmaz; keşif status turunun yerel snapshot'ıdır.
        known = {
            **context.services.get("code_agent_project_paths", {}),
            **context.settings.code_agent.projects,
        }
        if jobs is None or args.project not in known:
            return ToolResult(message="İzinli proje bulunamadı; taslak hazırlanmadı.")
        config = context.settings.code_agent
        agent = args.agent or config.project_agent(args.project)
        workdir = args.workdir or config.project_workdir(args.project)
        mode = args.mode or config.project_mode(args.project)
        repo = known[args.project]
        try:
            # Takma adlar yapılandırmaya karşı doğrulanır; çözülmüş yollar onay ekranı içindir.
            _, profile = config.resolve_agent(agent)
            workdir_path = _resolve_workdir(context, workdir, repo)
        except ConfigChoiceError as error:
            return ToolResult(message=f"{error} Taslak hazırlanmadı.")
        if mode == "visible" and not visible_supported(profile):
            return ToolResult(
                message=f"'{agent}' ajanı görünür pencere kipini desteklemiyor "
                "(Windows ve ajan desteği gerekir). Taslak hazırlanmadı."
            )
        try:
            spec = jobs.prepare(
                args.project,
                args.task,
                args.scope,
                args.acceptance,
                agent=agent,
                workdir=workdir,
                repo_path=str(repo.resolve()),
                workdir_path=str(workdir_path.resolve()),
                mode=mode,
            )
        except (CodeAgentError, ValueError):
            return ToolResult(
                message="Taslak kaydedilemedi; kapsam ve kabul ölçütlerini kontrol edin."
            )
        return ToolResult(
            success=True,
            status="ok",
            message="Taslak hazır; kod ajanı başlamadı.",
            data={"spec": spec.model_dump(mode="json")},
        )


@register_tool
class CodeAgentStart(BaseTool):
    name = "code_agent_start"
    label = "Onaylanan projeyi başlat"
    done_label = "Görev kod ajanına gönderildi"
    description = (
        "Tam proje taslağını onaydan sonra taslaktaki kipte (arka planda veya görünür pencerede) "
        "başlatır. " + AUTHORITY_BOUNDARY
    )
    args_model = StartArguments
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(StartArguments, arguments)
        config = context.settings.code_agent
        projects = available_projects(config)
        jobs = _jobs(context)
        alias = args.spec.project if args.spec else args.project
        task = args.spec.task if args.spec else args.task
        path = projects.get(alias or "")
        if path is None or jobs is None or alias is None or task is None:
            allowed = ", ".join(sorted(projects)) or "(hiçbiri; özellik kapalı)"
            return ToolResult(message=f"Bilinmeyen proje '{alias}'. İzinli projeler: {allowed}.")
        agent_alias = args.spec.agent if args.spec else "claude"
        workdir_alias = args.spec.workdir if args.spec else OWN_REPO_ALIAS
        try:
            _, profile = config.resolve_agent(agent_alias)
            workdir = _resolve_workdir(context, workdir_alias, path)
        except ConfigChoiceError as error:
            return ToolResult(message=str(error))
        if args.spec is not None and (
            args.spec.repo_path not in {"", str(path.resolve())}
            or (
                workdir_alias != OWN_REPO_ALIAS and args.spec.workdir_path != str(workdir.resolve())
            )
        ):
            # Onaylanan yollar ayar değişince eskir; kullanıcı eski yolu onaylamıştı.
            return ToolResult(
                message="Ayarlar değişti; onaylanan yollar geçersiz. Yeni taslak hazırlayın."
            )
        try:
            job_id = jobs.start(
                alias,
                path,
                task,
                profile=profile,
                workdir=None if workdir_alias == OWN_REPO_ALIAS else workdir,
                command=config.command,
                max_minutes=config.max_minutes,
                model=config.model,
                manager_model=config.manager_model,
                coder_model=config.coder_model,
                tester_model=config.tester_model,
                spec=args.spec,
            )
        except CodeAgentError as error:
            return ToolResult(message=str(error))
        base = next(j["base"] for j in jobs.status() if j["job_id"] == job_id)
        return ToolResult(
            status="sent",
            message=f"Görev gönderildi (iş {job_id}); sonuç bekleniyor.",
            data={"job_id": job_id, "base": base},
        )


@register_tool
class CodeAgentStatus(BaseTool):
    name = "code_agent_status"
    label = "Proje işleri durumu"
    done_label = "Proje işleri okundu"
    description = "İzinli projeler, taslaklar ve iş/soru/commit sonuçlarını listeler."
    args_model = StatusArguments
    risk = Risk.READ_ONLY
    external = True
    content_kind = "file"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(StatusArguments, arguments)
        jobs = _jobs(context)
        listed = [] if jobs is None else jobs.status()
        if args.job_id is not None:
            listed = [j for j in listed if j["job_id"] == args.job_id]
        paths = available_projects(context.settings.code_agent)
        context.services["code_agent_project_paths"] = paths
        # Komut arama (codex için PowerShell olabilir, önbellekli) işçi iş parçacığında yapılır,
        # arayüz iş parçacığında değil.
        agent_status = await asyncio.to_thread(_agent_status, context.settings.code_agent)
        projects = sorted(paths)
        return ToolResult(
            success=True,
            status="ok",
            message=(
                str(context.services.get("code_agent_unavailable"))
                if context.services.get("code_agent_unavailable")
                else f"{len(listed)} kod ajanı işi."
            ),
            data={
                "projects": projects,
                "agents": list(context.settings.code_agent.agent_names()),
                "agent_status": agent_status,
                "workdirs": sorted(
                    context.settings.code_agent.workdir_choices(context.settings.memory.vault_path)
                ),
                "jobs": listed,
                "drafts": [] if jobs is None else jobs.drafts(),
            },
            external=True,
            content_kind="file",
        )


@register_tool
class CodeAgentAnswer(BaseTool):
    name = "code_agent_answer"
    label = "Kod ajanı sorusunu cevapla"
    done_label = "Cevap kod ajanına gönderildi"
    description = (
        "İş ve tek kullanımlık soru kimliğiyle aynı Claude oturumuna devam eder (yalnız Claude). "
        + AUTHORITY_BOUNDARY
    )
    args_model = AnswerArguments
    risk = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(AnswerArguments, arguments)
        jobs = _jobs(context)
        if (
            jobs is None
            or not args.answer.strip()
            or not jobs.answer(args.job_id, args.question_id, args.answer)
        ):
            return ToolResult(message="Soru artık geçerli değil; cevap gönderilmedi.")
        return ToolResult(status="sent", message="Cevap aynı oturuma gönderildi; sonuç bekleniyor.")


@register_tool
class CodeAgentCancel(BaseTool):
    name = "code_agent_cancel"
    label = "Kod ajanını durdur"
    done_label = "Kod ajanı durdurma isteği gönderildi"
    description = "Bekleyen/çalışan işi iptal eder. Yarım değişiklikler diskte kalır."
    args_model = CancelArguments
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(CancelArguments, arguments)
        jobs = _jobs(context)
        if jobs is None or not jobs.cancel(args.job_id):
            return ToolResult(message=f"Çalışan iş bulunamadı: {args.job_id}.")
        return ToolResult(
            status="sent",
            message=f"İş {args.job_id} için durdurma gönderildi. "
            "Yarım değişiklikler diskte kalır; süreç temizliği doğrulanacak.",
        )


@register_tool
class CodeAgentDiscard(BaseTool):
    name = "code_agent_discard"
    label = "Taslağı sil"
    done_label = "Taslak silindi"
    description = (
        "Henüz başlatılmamış bir taslağı siler. İş kayıtlarını ve çalışan işleri silmez. "
        "Dosyalara ve repoya dokunmaz."
    )
    args_model = DiscardArguments
    # İptal ile aynı sınıf: yerel, başlamamış üstveri kaydı; içerik denetim günlüğünde
    # (`code_agent_draft`) kalır. Lekeli turda yine onay sorulur (SafetyGate).
    risk = Risk.SIDE_EFFECT

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = cast(DiscardArguments, arguments)
        jobs = _jobs(context)
        try:
            removed = jobs is not None and jobs.discard(args.spec_id)
        except CodeAgentError as error:
            return ToolResult(message=str(error))
        if not removed:
            return ToolResult(message=f"Silinebilir taslak bulunamadı: {args.spec_id}.")
        return ToolResult(
            success=True,
            status="ok",
            message="Taslak silindi.",
            data={"spec_id": args.spec_id},
        )
