"""Takılabilir kod ajanı arka uçları (ADR 0049): argv, stdin istemi ve ek ortam adları.

Her tür küçük bir sınıftır; iş durumu, git doğrulaması ve süreç yaşam döngüsü
`code_agent_jobs`'ta ortak kalır. Başarıyı hiçbir arka uç belirlemez: sonucu git durumu
söyler (ajanın kendi beyanı başarı sayılmaz).

Resmî belge/`--help` ile doğrulanan bayraklar "doğrulandı"; diğerleri "doğrulanmadı"
işaretlidir ve profilin `args` alanıyla değiştirilebilir.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from artemis.config.settings import ENV_NAME_RE, AgentProfile
from artemis.tools.code_agent_process import NO_WINDOW, console_supported
from artemis.tools.platform import child_environment
from artemis.tools.project_spec import AUTHORITY_BOUNDARY, ProjectSpec, SegmentOutput

_COMMON_RULES = (
    "Work only inside this trusted repository. Commit locally with clear messages. "
    "NEVER push, change remotes, rewrite history or reset user changes. "
)
_CLAUDE_RULES = (
    "Delegate implementation to coder and mechanical checks to tester when useful. "
    "Do not call native AskUserQuestion in headless mode: if clarification is necessary, "
    "finish this segment with structured_output kind=question, question=your full question "
    "and summary=progress. Otherwise return kind=completed, question=null, summary=a brief "
    "Turkish result. A question is not completion. User answers arrive in a resumed session. "
)
RULES = _COMMON_RULES + _CLAUDE_RULES + AUTHORITY_BOUNDARY
# Görünür pencere kipi: kullanıcı konsolu görür ama Artemis soru-cevap yapamaz; ajan cevap
# beklememeli, takılırsa durup nedenini yazmalı. structured_output protokolü yoktur.
_VISIBLE_RULES = (
    _COMMON_RULES
    + "A console window is open for the user, but do not wait for answers: if you cannot "
    "proceed, stop and explain why in your final message. " + AUTHORITY_BOUNDARY
)
# Claude dışı ajanlarda soru/cevap ve structured_output protokolü yoktur; yanıltmamak için
# yalnız ortak kurallar verilir.
_PLAIN_RULES = (
    _COMMON_RULES
    + "There is no way to ask the user questions in this session: if you cannot proceed, "
    "stop and explain why in your final message. " + AUTHORITY_BOUNDARY
)

# Windows'ta Codex Microsoft Store paketindedir ve PATH'te yoktur; sürüm klasör adında
# değiştiği için paket konumu salt okunur sabit betikle sorulur (betik girdi almaz).
_CODEX_APPX_SCRIPT = (
    "(Get-AppxPackage OpenAI.Codex | Sort-Object Version -Descending "
    "| Select-Object -First 1).InstallLocation"
)
_CODEX_APPX_SUFFIX = Path("app") / "resources" / "codex.exe"
_LOCATE_CACHE: dict[str, str | None] = {}
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run_powershell(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=child_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=NO_WINDOW,
        check=False,
    )


def locate_codex(
    command: str = "codex",
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner = _run_powershell,
    environ: Mapping[str, str] | None = None,
    is_windows: bool | None = None,
    cache: dict[str, str | None] | None = None,
) -> str | None:
    """Sıra: açık mutlak komut, PATH, %APPDATA%\\npm\\codex.cmd, Appx paketi (yalnız Windows)."""
    cache = _LOCATE_CACHE if cache is None else cache
    if command in cache:
        return cache[command]
    environ = os.environ if environ is None else environ
    windows = os.name == "nt" if is_windows is None else is_windows
    found: str | None = None
    if Path(command).is_absolute():
        # Açık mutlak komut her zaman kazanır; yoksa başka yere düşülmez.
        found = command if Path(command).is_file() else None
    elif (resolved := which(command)) is not None:
        found = resolved
    elif command == "codex" and windows:
        found = _codex_from_npm(environ) or _codex_from_appx(runner)
    cache[command] = found
    return found


def _codex_from_npm(environ: Mapping[str, str]) -> str | None:
    appdata = environ.get("APPDATA")
    candidate = Path(appdata) / "npm" / "codex.cmd" if appdata else None
    return str(candidate) if candidate is not None and candidate.is_file() else None


def _codex_from_appx(runner: Runner) -> str | None:
    try:
        result = runner(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _CODEX_APPX_SCRIPT]
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    location = result.stdout.strip() if result.returncode == 0 else ""
    if not location or "\n" in location or not Path(location).is_absolute():
        return None
    candidate = Path(location) / _CODEX_APPX_SUFFIX
    return str(candidate) if candidate.is_file() else None


def resolve_executable(profile: AgentProfile) -> str | None:
    command = profile.command or profile.kind
    if profile.kind == "codex":
        return locate_codex(command)
    return shutil.which(command)


def run_environment(names: tuple[str, ...]) -> dict[str, str]:
    """child_environment() + yalnız profilin AD listesindeki, gerçekten var olan değişkenler."""
    env = child_environment()
    for name in names:
        if ENV_NAME_RE.fullmatch(name) and name in os.environ:
            env[name] = os.environ[name]
    return env


def visible_supported(profile: AgentProfile) -> bool:
    """Görünür pencere kipi: Windows konsolu + (claude/codex yerleşik biçimi veya visible_args)."""
    return console_supported() and (
        profile.kind in {"claude", "codex"} or bool(profile.visible_args)
    )


def role_profile(
    coder_model: str | None, tester_model: str | None, coordinator_rules: str = RULES
) -> dict[str, object]:
    profile: dict[str, object] = {}
    for name, description, prompt, tools, model in (
        (
            "coder",
            "Implementation agent",
            "Implement the approved scope; report changes honestly.",
            ["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
            coder_model,
        ),
        (
            "tester",
            "Mechanical verification agent",
            "Run focused checks and report actual failures.",
            ["Read", "Glob", "Grep", "Bash"],
            tester_model,
        ),
        (
            "coordinator",
            "Project manager",
            coordinator_rules,
            ["Read", "Glob", "Grep", "Bash", "Agent(coder)", "Agent(tester)"],
            None,
        ),
    ):
        entry: dict[str, object] = {"description": description, "prompt": prompt, "tools": tools}
        if model:
            entry["model"] = model
        profile[name] = entry
    return profile


@dataclass(frozen=True)
class BackendContext:
    exe: str
    profile: AgentProfile
    workdir: Path
    repo: Path
    workdir_is_repo: bool  # workdir bir git deposu mu (Codex --skip-git-repo-check kararı)
    spec: ProjectSpec
    manager_model: str | None = None
    coder_model: str | None = None
    tester_model: str | None = None
    # Görünür kipte tam istemin yazılacağı işe özel klasör (yoksa geçici klasör açılır).
    job_dir: Path | None = None

    @property
    def visible(self) -> bool:
        return self.spec.mode == "visible"

    @property
    def separate_workdir(self) -> bool:
        return self.workdir != self.repo


@dataclass
class AgentRun:
    """Tek koşunun hazır girdisi. Geçici dosyaları iş bitince `cleanup` siler."""

    argv: list[str]
    stdin_prompt: str
    kind: str
    extra_env_names: tuple[str, ...] = ()
    # "headless": boru + stdin istemi (arka plan). "visible": yeni konsol penceresi, boru yok,
    # istem dosyadadır; çıktı okunmaz ve soru-cevap protokolü yoktur (ADR 0049 eki).
    mode: Literal["headless", "visible"] = "headless"
    temp_files: list[Path] = field(default_factory=list)
    output_file: Path | None = None
    cleanup_dirs: list[Path] = field(default_factory=list)

    def cleanup(self) -> None:
        for path in self.temp_files:
            path.unlink(missing_ok=True)
        for directory in self.cleanup_dirs:
            with suppress(OSError):  # boş değilse (başka dosya varsa) dokunulmaz
                directory.rmdir()

    def last_message(self, limit: int = 600) -> str:
        """Ajanın son mesajı (yalnız bilgi; başarı kanıtı DEĞİLDİR)."""
        if self.output_file is None:
            return ""
        try:
            return self.output_file.read_text(encoding="utf-8", errors="replace").strip()[:limit]
        except OSError:
            return ""


def _target_lines(ctx: BackendContext) -> str:
    lines = (
        f"TARGET REPO (absolute path): {ctx.repo}\n"
        "Commit only inside that repo, local commits only, no git push, "
        "do not edit anything else unless the task says so.\n"
    )
    if ctx.separate_workdir:
        lines += (
            f"Your starting folder is {ctx.workdir} (it holds the user's own rules in "
            "AGENTS.md/CLAUDE.md - follow them); "
            f"the target repo is {ctx.repo}.\n"
        )
    return lines


def _plain_prompt(ctx: BackendContext) -> str:
    return (
        _PLAIN_RULES
        + "\n"
        + _target_lines(ctx)
        + "Approved project specification:\n"
        + ctx.spec.model_dump_json()
    )


def _temp_path(suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="artemis-agent-", suffix=suffix)
    os.close(descriptor)
    return Path(name)


def _visible_prompt(ctx: BackendContext) -> str:
    return (
        _VISIBLE_RULES
        + "\n"
        + _target_lines(ctx)
        + "Approved project specification:\n"
        + ctx.spec.model_dump_json()
    )


def _visible_run(
    ctx: BackendContext, kind: str, build_argv: Callable[[Path, str], list[str]]
) -> AgentRun:
    """Tam istemi işe özel klasöre yazar; argv'ye yalnız kısa yönerge girer (istem metni girmez)."""
    job_dir = ctx.job_dir or Path(tempfile.mkdtemp(prefix="artemis-agent-"))
    job_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = job_dir / "task.md"
    try:
        prompt_file.write_text(_visible_prompt(ctx), encoding="utf-8")
    except OSError:
        prompt_file.unlink(missing_ok=True)
        with suppress(OSError):
            job_dir.rmdir()
        raise
    instruction = f"Read {prompt_file} and carry out the task described there; stop when done."
    return AgentRun(
        build_argv(job_dir, instruction),
        "",
        kind,
        ctx.profile.env,
        mode="visible",
        temp_files=[prompt_file],
        cleanup_dirs=[job_dir],
    )


class Backend(Protocol):
    def build(self, ctx: BackendContext) -> AgentRun: ...


class ClaudeBackend:
    def _build_visible(self, ctx: BackendContext) -> AgentRun:
        def argv_for(job_dir: Path, instruction: str) -> list[str]:
            # Etkileşimli claude: aynı izin duruşu (acceptEdits + aynı araç listeleri), -p/json yok.
            # Yönerge ilk konumsal argümandır: çok değerli --allowedTools gibi bayraklardan sonra
            # gelseydi araç adı sanılırdı. Bayrak sırası canlı CLI ile doğrulanmadı.
            argv = [
                ctx.exe,
                instruction,
                "--permission-mode",
                "acceptEdits",
                "--allowedTools",
                "Read",
                "Edit",
                "Write",
                "Glob",
                "Grep",
                "Bash",
                "Agent(coder)",
                "Agent(tester)",
                "--disallowedTools",
                "Bash(git push:*)",
                "WebFetch",
                "WebSearch",
                "AskUserQuestion",
                "--append-system-prompt",
                _VISIBLE_RULES,
                "--agents",
                json.dumps(role_profile(ctx.coder_model, ctx.tester_model, _VISIBLE_RULES)),
                "--agent",
                "coordinator",
            ]
            if ctx.manager_model:
                argv += ["--model", ctx.manager_model]
            # Çok değerli --add-dir: istem dosyasının klasörü okunabilsin (izin sorusu çıkmasın).
            argv += ["--add-dir", str(job_dir)]
            if ctx.separate_workdir:
                argv.append(str(ctx.repo))
            return argv

        return _visible_run(ctx, "claude", argv_for)

    def build(self, ctx: BackendContext) -> AgentRun:
        if ctx.visible:
            return self._build_visible(ctx)
        argv = [
            ctx.exe,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(SegmentOutput.model_json_schema()),
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
            "Bash",
            "Agent(coder)",
            "Agent(tester)",
            "--disallowedTools",
            "Bash(git push:*)",
            "WebFetch",
            "WebSearch",
            "AskUserQuestion",
            "--append-system-prompt",
            RULES,
            "--agents",
            json.dumps(role_profile(ctx.coder_model, ctx.tester_model)),
            "--agent",
            "coordinator",
        ]
        if ctx.manager_model:
            argv += ["--model", ctx.manager_model]
        prompt = "Approved project specification:\n" + ctx.spec.model_dump_json()
        if ctx.separate_workdir:
            # Doğrulandı: --add-dir ek klasöre erişim verir; CLAUDE.md cwd ve üst
            # klasörlerinden yüklenir, --add-dir klasörlerinin CLAUDE.md'si varsayılan yüklenmez.
            argv += ["--add-dir", str(ctx.repo)]
            prompt = _target_lines(ctx) + prompt
        return AgentRun(argv, prompt, "claude", ctx.profile.env)


class CodexBackend:
    def _build_visible(self, ctx: BackendContext) -> AgentRun:
        def argv_for(_job_dir: Path, instruction: str) -> list[str]:
            # Doğrulandı (codex-cli 0.165 --help): `codex [OPTIONS] [PROMPT]`; -C, --add-dir,
            # -a on-request (model onay ister, kullanıcı pencerede cevaplar), -s workspace-write.
            # --dangerously-bypass-approvals-and-sandbox ve -a never ASLA eklenmez.
            argv = [ctx.exe, "-C", str(ctx.workdir)]
            if ctx.separate_workdir:
                argv += ["--add-dir", str(ctx.repo)]
            return [*argv, "-a", "on-request", "-s", "workspace-write", instruction]

        return _visible_run(ctx, "codex", argv_for)

    def build(self, ctx: BackendContext) -> AgentRun:
        if ctx.visible:
            return self._build_visible(ctx)
        output = _temp_path(".txt")
        # Doğrulandı (codex-cli 0.164 --help): exec seçenekleri; --add-dir exec'in kendi
        # seçeneğidir, istem stdin'den `-` ile gelir (konumsal istem verilmez). Ağ kapalı
        # varsayılanda kalır; --full-auto kaldırıldığı için kullanılmaz;
        # --dangerously-bypass-approvals-and-sandbox ASLA eklenmez.
        # workspace-write altında .git salt okunurdur: Codex kendisi commit atamayabilir;
        # iş yine de git durumundan yargılanır.
        argv = [ctx.exe, "exec", "--sandbox", "workspace-write", "-C", str(ctx.workdir)]
        if ctx.separate_workdir:
            argv += ["--add-dir", str(ctx.repo)]
        if not ctx.workdir_is_repo:
            argv.append("--skip-git-repo-check")
        argv += ["--json", "-o", str(output), "-"]
        return AgentRun(
            argv,
            _plain_prompt(ctx),
            "codex",
            ctx.profile.env,
            temp_files=[output],
            output_file=output,
        )


class GenericBackend:
    """Kullanıcı şablonlu CLI (doğrulanmadı: her CLI'nin bayrakları kullanıcıdan gelir)."""

    def _build_visible(self, ctx: BackendContext) -> AgentRun:
        profile = ctx.profile
        if not profile.visible_args:
            raise ValueError("Bu ajan görünür pencere kipini desteklemiyor (visible_args yok).")

        def argv_for(job_dir: Path, instruction: str) -> list[str]:
            values = {
                "{workdir}": str(ctx.workdir),
                "{repo}": str(ctx.repo),
                "{prompt_file}": str(job_dir / "task.md"),
                "{prompt}": instruction,  # tam istem değil, kısa yönerge
            }
            argv = [ctx.exe]
            for arg in profile.visible_args:
                for key, value in values.items():
                    arg = arg.replace(key, value)
                argv.append(arg)
            return argv

        return _visible_run(ctx, "generic", argv_for)

    def build(self, ctx: BackendContext) -> AgentRun:
        if ctx.visible:
            return self._build_visible(ctx)
        profile = ctx.profile
        prompt = _plain_prompt(ctx)
        via = profile.prompt_via or (
            "file" if any("{prompt_file}" in arg for arg in profile.args) else "stdin"
        )
        temp: list[Path] = []
        values = {"{workdir}": str(ctx.workdir), "{repo}": str(ctx.repo)}
        if via == "file":
            prompt_file = _temp_path(".md")
            temp.append(prompt_file)
            try:
                prompt_file.write_text(prompt, encoding="utf-8")
            except OSError:
                prompt_file.unlink(missing_ok=True)
                raise
            values["{prompt_file}"] = str(prompt_file)
        if via == "arg":
            values["{prompt}"] = prompt
        argv = [ctx.exe]
        for arg in profile.args:
            for key, value in values.items():
                arg = arg.replace(key, value)
            argv.append(arg)
        # Argv listesidir; kabuk yoktur. Yalnız "stdin" kipinde istem stdin'e yazılır.
        return AgentRun(
            argv, prompt if via == "stdin" else "", "generic", profile.env, temp_files=temp
        )


BACKENDS: dict[str, Backend] = {
    "claude": ClaudeBackend(),
    "codex": CodexBackend(),
    "generic": GenericBackend(),
}
