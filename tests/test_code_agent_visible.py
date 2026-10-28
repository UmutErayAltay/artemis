"""ADR 0049 eki: yerleşik codex, taslak silme ve görünür pencere kipi.

Gerçek CLI ve gerçek konsol penceresi yok: sahte süreç + gerçek git. Görünür kipin yeni konsol
bayrağı fabrikaya giden argümanlardan okunur; sahte fabrika testte pencere açmamak için o bayrağı
düşürür.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_code_agent import make_jobs, reply, wait_job
from test_code_agent import repo as _repo_fixture

from artemis.brain.fake import ScriptedProvider
from artemis.channels.runtime import create_runtime
from artemis.config.settings import (
    AgentProfile,
    CodeAgentConfig,
    ConfigChoiceError,
    MemoryConfig,
    Settings,
)
from artemis.safety.audit import AuditLog
from artemis.tools import code_agent_backends, code_agent_jobs
from artemis.tools.base import Risk, ToolContext
from artemis.tools.code_agent_jobs import CodeAgentError, CodeAgentJobs
from artemis.tools.code_agent_process import NEW_CONSOLE, NO_WINDOW
from artemis.tools.plugins import code_agent as plugin
from artemis.tools.plugins.code_agent import (
    CodeAgentCancel,
    CodeAgentDiscard,
    CodeAgentPrepare,
    CodeAgentStart,
    CodeAgentStatus,
    DiscardArguments,
    PrepareArguments,
    StartArguments,
    StatusArguments,
)
from artemis.tools.project_spec import VISIBLE_NOTICE, ProjectSpec, approval_text

repo = _repo_fixture

TASK = "ÖZEL-GÖREV-METNİ-98765"
CODEX = AgentProfile(kind="codex", command=sys.executable)
CLAUDE = AgentProfile(kind="claude", command=sys.executable)


class VisibleCLI:
    """Görünür kip fabrikası: argümanları kaydeder, gerçek pencere yerine arka plan süreci açar."""

    def __init__(self, script: str) -> None:
        self.script = script
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.procs: list[subprocess.Popen[bytes]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append((list(argv), dict(kwargs)))
        real = dict(kwargs)
        if os.name == "nt":
            # Gerçek konsol penceresi açılmasın; askıya alınmış başlatma korunur.
            real["creationflags"] = (real.get("creationflags", 0) & ~NEW_CONSOLE) | NO_WINDOW
        proc = subprocess.Popen(
            [sys.executable, "-c", self.script, *argv[1:]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **real,
        )
        self.procs.append(proc)
        return proc


def agent_script(out: Path, *, code: int = 0, before: str = "", sleep: float = 0.0) -> str:
    """Sahte etkileşimli ajan: yönergedeki istem dosyasını kopyalar, argv'yi yazar, çıkar."""
    return (
        "import sys, re, pathlib, subprocess, time\n"
        f"out = pathlib.Path({str(out)!r})\n"
        "out.with_suffix('.argv').write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
        "m = re.search(r'Read (.*?) and carry out', ' '.join(sys.argv[1:]))\n"
        "out.with_suffix('.prompt').write_text(\n"
        "    pathlib.Path(m.group(1)).read_text(encoding='utf-8') if m else '', encoding='utf-8')\n"
        "print('TAMAMLANDI BAŞARIYLA kind=completed')\n"
        + before
        + f"\ntime.sleep({sleep})\nsys.exit({code})\n"
    )


def commit_in(repo_path: Path) -> str:
    return (
        f"r = {str(repo_path)!r}\n"
        "pathlib.Path(r, 'b.txt').write_text('b')\n"
        "subprocess.run(['git','-C',r,'add','.'], check=True)\n"
        "subprocess.run(['git','-C',r,'-c','user.name=t','-c','user.email=t@t',"
        "'commit','-q','-m','ajan'], check=True)\n"
    )


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Testler Windows dışında da görünür kipi çalıştırabilsin (fabrika pencere açmaz)."""
    monkeypatch.setattr(code_agent_backends, "console_supported", lambda: True)
    monkeypatch.setattr(code_agent_jobs, "console_supported", lambda: True)


def make_visible(
    tmp_path: Path, script: str, *, persistent: bool = False
) -> tuple[CodeAgentJobs, VisibleCLI]:
    factory = VisibleCLI(script)
    jobs = CodeAgentJobs(
        AuditLog(tmp_path / "audit.jsonl"),
        process_factory=factory,
        store_path=tmp_path / "jobs.sqlite3" if persistent else None,
    )
    return jobs, factory


def start_visible(
    jobs: CodeAgentJobs,
    repo_path: Path,
    profile: AgentProfile = CODEX,
    *,
    workdir: Path | None = None,
    **kwargs: Any,
) -> str:
    spec = jobs.prepare("proj", TASK, ("kapsam",), ("ölçüt",), agent="x", mode="visible")
    return jobs.start(
        "proj", repo_path, TASK, spec=spec, profile=profile, workdir=workdir, command="u", **kwargs
    )


# --- yerleşik codex ve yapılandırma --------------------------------------------------------------


def test_codex_is_builtin_like_claude(tmp_path: Path) -> None:
    config = CodeAgentConfig()
    assert config.agent_names() == ("claude", "codex")
    name, profile = config.resolve_agent("codex")
    assert (name, profile.kind, profile.command) == ("codex", "codex", None)
    # Kullanıcı tanımı (örn. açık mutlak yol) geriye dönük uyumlu olarak kazanır.
    explicit = AgentProfile(kind="codex", command=str(tmp_path / "codex.exe"))
    custom = CodeAgentConfig(agents={"codex": explicit, "aider": CODEX}, default_agent="codex")
    assert custom.resolve_agent("codex")[1] == explicit
    assert custom.agent_names() == ("claude", "codex", "aider")  # tekrar yok, özel adlar sıralı
    # Doğrulayıcılar yerleşik codex'i tanır.
    assert CodeAgentConfig(default_agent="codex").default_agent == "codex"
    entry = CodeAgentConfig(projects={"p": {"path": str(tmp_path), "agent": "codex"}})
    assert entry.project_agent("p") == "codex"
    with pytest.raises(ValidationError):
        CodeAgentConfig(agents={"claude": CODEX})  # claude hâlâ yeniden tanımlanamaz
    with pytest.raises(ConfigChoiceError, match="claude, codex"):
        config.resolve_agent("yok")


def test_mode_config_defaults_and_validation(tmp_path: Path) -> None:
    config = CodeAgentConfig(
        projects={"a": str(tmp_path), "b": {"path": str(tmp_path), "mode": "visible"}},
        default_mode="background",
    )
    assert config.project_mode("a") == "background" and config.project_mode("b") == "visible"
    assert CodeAgentConfig(default_mode="visible").project_mode("yok") == "visible"
    for raw in (
        {"default_mode": "headless"},
        {"projects": {"a": {"path": "/x", "mode": "gizli"}}},
    ):
        with pytest.raises(ValidationError):
            CodeAgentConfig.model_validate(raw)


def test_visible_args_only_for_generic_and_need_a_prompt_placeholder() -> None:
    ok = AgentProfile(kind="generic", command="x", visible_args=("--file", "{prompt_file}"))
    assert ok.visible_args
    for bad in (
        {"kind": "generic", "command": "x", "visible_args": ["--yolo"]},
        {"kind": "codex", "visible_args": ["{prompt}"]},
    ):
        with pytest.raises(ValidationError):
            AgentProfile.model_validate(bad)


async def test_status_reports_found_and_visible_per_agent_off_the_calling_thread(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[str] = []

    def fake_resolve(profile: AgentProfile) -> str | None:
        threads.append(threading.current_thread().name)
        return None if profile.kind == "codex" else "X"

    monkeypatch.setattr(plugin, "resolve_executable", fake_resolve)
    jobs, _ = make_jobs(tmp_path, reply())
    config = CodeAgentConfig(
        projects={"proj": repo},
        agents={"aider": AgentProfile(kind="generic", command="aider")},
    )
    ctx = ToolContext(
        workspace=tmp_path,
        settings=Settings(code_agent=config),
        audit=jobs._audit,
        services={"code_agent_jobs": jobs},
    )
    result = await CodeAgentStatus().execute(StatusArguments(), ctx)
    status = result.data["agent_status"]
    assert result.data["agents"] == ["claude", "codex", "aider"]
    assert set(status) == {"claude", "codex", "aider"}
    assert status["claude"]["found"] and not status["codex"]["found"]
    assert status["claude"]["visible"] == (os.name == "nt")
    assert not status["aider"]["visible"]  # generic + visible_args yok
    assert threading.main_thread().name not in threads  # çağıran (olay döngüsü) iş parçacığı değil
    jobs.shutdown()


# --- onay metni ----------------------------------------------------------------------------------


def _spec_values(**extra: Any) -> dict[str, Any]:
    return {
        "spec_id": "00000000-0000-4000-8000-000000000000",
        "project": "p",
        "task": "t",
        "scope": ["s"],
        "acceptance": ["a"],
        "repo_path": "/r",
        "workdir_path": "/r",
        **extra,
    }


def test_approval_text_states_mode_and_cannot_be_forged() -> None:
    background = ProjectSpec.model_validate(_spec_values())
    assert background.mode == "background"
    assert background.approval_text.splitlines()[3] == "Çalışma kipi: Arka planda"
    assert VISIBLE_NOTICE not in background.approval_text
    visible = ProjectSpec.model_validate(_spec_values(mode="visible"))
    lines = visible.approval_text.splitlines()
    assert lines[3] == "Çalışma kipi: Görünür pencerede" and lines[4] == VISIBLE_NOTICE
    assert VISIBLE_NOTICE == (
        "Görünür pencerede çalışır; pencereyi kapatırsan iş kesilir. "
        "Artemis bu kipte ajana soru-cevap yapamaz."
    )
    # Model "arka planda" yazıp kipi görünür bırakamaz: metin her zaman yeniden türetilir.
    forged = ProjectSpec.model_validate(
        _spec_values(mode="visible", approval_text="Çalışma kipi: Arka planda")
    )
    assert forged.approval_text == visible.approval_text and forged == visible
    assert approval_text(_spec_values(mode="visible")) == visible.approval_text
    with pytest.raises(ValidationError):
        ProjectSpec.model_validate(_spec_values(mode="gizli"))


# --- hazırlama aracı -----------------------------------------------------------------------------


def tool_ctx(tmp_path: Path, repo_path: Path, jobs: CodeAgentJobs, **config: Any) -> ToolContext:
    base: dict[str, Any] = {"projects": {"proj": repo_path}, "command": sys.executable}
    return ToolContext(
        workspace=tmp_path,
        settings=Settings(
            code_agent=CodeAgentConfig(**{**base, **config}),
            memory=MemoryConfig(vault_path=None),
        ),
        audit=jobs._audit,
        services={"code_agent_jobs": jobs},
    )


def prep(**extra: Any) -> PrepareArguments:
    return PrepareArguments(project="proj", task=TASK, scope=("k",), acceptance=("o",), **extra)


async def test_prepare_mode_defaults_override_and_unsupported_agent(
    tmp_path: Path, repo: Path, console: None
) -> None:
    jobs, _ = make_visible(tmp_path, "pass")
    aider = AgentProfile(kind="generic", command="aider")
    ctx = tool_ctx(
        tmp_path,
        repo,
        jobs,
        projects={"proj": {"path": repo, "mode": "visible"}},
        agents={"aider": aider},
    )
    saved = (await CodeAgentPrepare().execute(prep(), ctx)).data["spec"]
    # Proje varsayılanı görünür kip: onay metni uyarıyı taşır.
    assert saved["mode"] == "visible" and VISIBLE_NOTICE in saved["approval_text"]
    over = (await CodeAgentPrepare().execute(prep(mode="background"), ctx)).data["spec"]
    assert over["mode"] == "background"
    refused = await CodeAgentPrepare().execute(prep(agent="aider", mode="visible"), ctx)
    assert not refused.success and "desteklemiyor" in refused.message
    count = len(jobs.drafts())
    assert count == 2  # reddedilen taslak yazılmadı
    with pytest.raises(ValidationError):
        prep(mode="headless")
    jobs.shutdown()


async def test_prepare_visible_refused_when_console_unsupported(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(code_agent_backends, "console_supported", lambda: False)
    jobs, _ = make_visible(tmp_path, "pass")
    result = await CodeAgentPrepare().execute(prep(mode="visible"), tool_ctx(tmp_path, repo, jobs))
    assert not result.success and jobs.drafts() == []
    jobs.shutdown()


# --- görünür kip: argv ve istem dosyası ----------------------------------------------------------


def test_codex_visible_argv_flags_prompt_file_and_no_prompt_in_argv(
    tmp_path: Path, repo: Path, console: None
) -> None:
    out = tmp_path / "o"
    jobs, factory = make_visible(tmp_path, agent_script(out), persistent=True)
    job_id = start_visible(jobs, repo)
    job = wait_job(jobs, job_id)
    argv, kwargs = factory.calls[0]
    flags = argv[1:]
    assert flags[:2] == ["-C", str(repo.resolve())]
    assert flags[flags.index("-a") + 1] == "on-request"
    assert flags[flags.index("-s") + 1] == "workspace-write"
    assert "exec" not in flags and "-a" in flags and "never" not in flags
    assert "--dangerously-bypass-approvals-and-sandbox" not in flags and "--full-auto" not in flags
    assert "--add-dir" not in flags  # başlangıç klasörü = repo
    instruction = flags[-1]
    assert instruction.startswith("Read ") and instruction.endswith("stop when done.")
    assert TASK not in "\n".join(argv)  # istem metni argv'ye girmez
    # Tam istem işin kendi state klasöründeki dosyadan okundu.
    prompt_file = tmp_path / "code_agent_jobs" / job_id / "task.md"
    assert str(prompt_file) in instruction
    prompt = out.with_suffix(".prompt").read_text(encoding="utf-8")
    assert TASK in prompt and "NEVER push" in prompt and f"{repo.resolve()}" in prompt
    assert "AskUserQuestion" not in prompt and "structured_output" not in prompt
    # Boru yok, metin kipi yok; pencere ve süreç ağacı bayrakları var.
    assert not {"stdin", "stdout", "stderr", "text"} & set(kwargs)
    if os.name == "nt":
        assert kwargs["creationflags"] & NEW_CONSOLE and kwargs["creationflags"] & 0x4
    assert job["mode"] == "visible" and job["spec"]["mode"] == "visible"
    assert not prompt_file.exists() and not prompt_file.parent.exists()  # iş bitince temizlendi
    jobs.shutdown()


def test_codex_visible_adds_add_dir_for_separate_workdir(
    tmp_path: Path, repo: Path, console: None
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    jobs, factory = make_visible(tmp_path, agent_script(tmp_path / "o"))
    wait_job(jobs, start_visible(jobs, repo, workdir=other))
    flags = factory.calls[0][0][1:]
    assert flags[:2] == ["-C", str(other.resolve())]
    assert flags[flags.index("--add-dir") + 1] == str(repo.resolve())
    jobs.shutdown()


def test_claude_visible_keeps_permission_posture_and_never_skips_permissions(
    tmp_path: Path, repo: Path, console: None
) -> None:
    out = tmp_path / "o"
    jobs, factory = make_visible(tmp_path, agent_script(out), persistent=True)
    job_id = start_visible(jobs, repo, CLAUDE)
    wait_job(jobs, job_id)
    argv = factory.calls[0][0]
    for banned in (
        "-p",
        "--output-format",
        "--json-schema",
        "--dangerously-skip-permissions",
        "--resume",
    ):
        assert banned not in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    denied = argv[argv.index("--disallowedTools") + 1 :]
    assert "Bash(git push:*)" in denied[:4] and "AskUserQuestion" in denied[:4]
    assert argv[1].startswith("Read ") and TASK not in "\n".join(argv)
    job_dir = str(tmp_path / "code_agent_jobs" / job_id)
    # İstem dosyasının klasörü izin sorusu çıkarmadan okunur.
    assert argv[argv.index("--add-dir") + 1] == job_dir
    assert "structured_output" not in json.dumps(argv)  # soru-cevap protokolü yok
    jobs.shutdown()


def test_generic_visible_uses_visible_args_and_refuses_without_them(
    tmp_path: Path, repo: Path, console: None
) -> None:
    out = tmp_path / "o"
    jobs, factory = make_visible(tmp_path, agent_script(out))
    profile = AgentProfile(
        kind="generic",
        command=sys.executable,
        args=("--headless-only",),
        visible_args=(
            "--message", "{prompt}", "--read", "{prompt_file}", "--cwd={workdir}", "{repo}",
        ),
    )
    wait_job(jobs, start_visible(jobs, repo, profile))
    argv = factory.calls[0][0]
    assert "--headless-only" not in argv  # görünür kip headless args kullanmaz
    assert argv[argv.index("--message") + 1].startswith("Read ") and TASK not in "\n".join(argv)
    assert Path(argv[argv.index("--read") + 1]).name == "task.md"
    assert f"--cwd={repo.resolve()}" in argv and str(repo.resolve()) in argv
    bare = AgentProfile(kind="generic", command=sys.executable, args=("--go",))
    with pytest.raises(CodeAgentError, match="desteklemiyor"):
        start_visible(jobs, repo, bare)
    assert len(factory.calls) == 1
    jobs.shutdown()


def test_visible_refused_where_console_is_unsupported(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(code_agent_jobs, "console_supported", lambda: False)
    jobs, factory = make_visible(tmp_path, agent_script(tmp_path / "o"))
    with pytest.raises(CodeAgentError, match="Windows"):
        start_visible(jobs, repo)
    assert factory.calls == [] and jobs.status() == []
    jobs.shutdown()


def test_visible_env_passes_only_named_variables(
    tmp_path: Path, repo: Path, console: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_AGENT_KEY", "gizli-deger-yok")
    monkeypatch.setenv("OTHER_SECRET_KEY", "baska-sir")
    jobs, factory = make_visible(tmp_path, agent_script(tmp_path / "o"))
    profile = AgentProfile(kind="codex", command=sys.executable, env=("MY_AGENT_KEY",))
    wait_job(jobs, start_visible(jobs, repo, profile))
    argv, kwargs = factory.calls[0]
    assert kwargs["env"]["MY_AGENT_KEY"] == "gizli-deger-yok"
    assert "OTHER_SECRET_KEY" not in kwargs["env"]
    blob = json.dumps(argv) + (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "gizli-deger-yok" not in blob
    jobs.shutdown()


# --- görünür kip: süreç yaşam döngüsü ------------------------------------------------------------


@pytest.mark.parametrize(
    ("with_commit", "code", "status", "needles"),
    [
        (True, 0, "done", ["1 yeni commit", "kalan commitlenmemiş değişiklik yok"]),
        (False, 0, "done", ["git'te değişiklik görülmedi"]),
        # Pencere kapatılınca süreç sıfır dışı çıkar: commit olsa da başarı değildir.
        (True, 1, "interrupted", ["Pencere kapandı", "tamamlanmış sayılmadı", "1 yeni commit"]),
        (False, 3, "interrupted", ["tamamlanmış sayılmadı"]),
    ],
)
def test_visible_success_comes_from_exit_and_git_never_from_agent_text(
    tmp_path: Path, repo: Path, console: None, with_commit: bool, code: int, status: str,
    needles: list[str],
) -> None:
    before = commit_in(repo.resolve()) if with_commit else ""
    # Ajan "BAŞARIYLA tamamlandı" yazar ama bu metin hiçbir yerde okunmaz.
    jobs, _ = make_visible(tmp_path, agent_script(tmp_path / "o", code=code, before=before))
    job = wait_job(jobs, start_visible(jobs, repo))
    assert job["status"] == status
    for needle in needles:
        assert needle in job["summary"]
    assert "BAŞARIYLA" not in job["summary"] and "TAMAMLANDI" not in json.dumps(job)
    jobs.shutdown()


def test_visible_window_closed_from_outside_is_interrupted_not_done(
    tmp_path: Path, repo: Path, console: None
) -> None:
    out = tmp_path / "o"
    jobs, factory = make_visible(tmp_path, agent_script(out, sleep=30))
    job_id = start_visible(jobs, repo)
    wait_job(jobs, job_id, {"running"})
    deadline = time.monotonic() + 5
    # Ajan gerçekten başladıktan (süreç ağacı kurulduktan) sonra pencere kapanır.
    while not out.with_suffix(".argv").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    factory.procs[0].kill()  # kullanıcı pencereyi kapattı
    job = wait_job(jobs, job_id)
    assert job["status"] == "interrupted" and job["status"] != "done"
    assert not job["cleanup_pending"]
    jobs.shutdown()


def test_visible_cancel_kills_the_window_process_and_marks_cancelled(
    tmp_path: Path, repo: Path, console: None
) -> None:
    jobs, factory = make_visible(tmp_path, agent_script(tmp_path / "o", sleep=30))
    job_id = start_visible(jobs, repo)
    wait_job(jobs, job_id, {"running"})
    assert jobs.cancel(job_id)
    job = wait_job(jobs, job_id)
    assert job["status"] == "cancelled" and not job["cleanup_pending"]
    assert all(proc.poll() is not None for proc in factory.procs)
    jobs.shutdown()


def test_visible_timeout_kills_process_and_reports_timeout(
    tmp_path: Path, repo: Path, console: None
) -> None:
    jobs, factory = make_visible(tmp_path, agent_script(tmp_path / "o", sleep=30))
    job = wait_job(jobs, start_visible(jobs, repo, max_minutes=0.03))
    assert job["status"] == "timeout" and not job["cleanup_pending"]
    assert all(proc.poll() is not None for proc in factory.procs)
    jobs.shutdown()


def test_visible_job_audit_records_the_mode(
    tmp_path: Path, repo: Path, console: None
) -> None:
    jobs, _ = make_visible(tmp_path, agent_script(tmp_path / "o"))
    wait_job(jobs, start_visible(jobs, repo))
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"mode": "visible"' in audit or '"mode":"visible"' in audit
    jobs.shutdown()


# --- taslak silme --------------------------------------------------------------------------------


def make_store(tmp_path: Path) -> CodeAgentJobs:
    return CodeAgentJobs(AuditLog(tmp_path / "audit.jsonl"), store_path=tmp_path / "jobs.sqlite3")


def store_keys(tmp_path: Path) -> dict[str, str]:
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        return dict(connection.execute("SELECT key, kind FROM records"))


def test_discard_removes_only_an_unstarted_draft_and_survives_restart(tmp_path: Path) -> None:
    jobs = make_store(tmp_path)
    keep = jobs.prepare("proj", "kalsın", ("k",), ("o",))
    gone = jobs.prepare("proj", "silinsin", ("k",), ("o",))
    assert jobs.discard(gone.spec_id)
    assert [d["spec_id"] for d in jobs.drafts()] == [keep.spec_id]
    assert store_keys(tmp_path) == {keep.spec_id: "draft"}
    assert not jobs.discard(gone.spec_id)  # ikinci silme: dürüst başarısızlık
    assert not jobs.discard("yok-boyle-bir-id")
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "code_agent_draft_discarded" in audit and gone.spec_id in audit
    jobs.shutdown()
    again = make_store(tmp_path)
    assert [d["spec_id"] for d in again.drafts()] == [keep.spec_id]  # silinen geri gelmedi
    again.shutdown()


def test_discard_cannot_remove_a_job_or_a_started_draft(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, reply(), persistent=True)
    spec = jobs.prepare("proj", "görev", ("k",), ("o",))
    job_id = jobs.start("proj", repo, "görev", spec=spec, command=sys.executable)
    job = wait_job(jobs, job_id)
    assert not jobs.discard(job_id)  # iş kimliği silinemez
    assert not jobs.discard(spec.spec_id)  # başlamış taslak da silinemez
    assert [j["job_id"] for j in jobs.status()] == [job_id]
    assert job["status"] == "done" and store_keys(tmp_path)[job_id] == "job"
    # Anahtar iş kaydı olsa bile SQL koşulu (kind='draft') satırı korur.
    jobs._delete_draft(job_id)
    assert store_keys(tmp_path)[job_id] == "job"
    jobs.shutdown()


def test_discard_never_touches_a_running_job(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, "import sys,time\nsys.stdin.read()\ntime.sleep(30)\n")
    spec = jobs.prepare("proj", "görev", ("k",), ("o",))
    job_id = jobs.start("proj", repo, "görev", spec=spec, command=sys.executable)
    wait_job(jobs, job_id, {"running"})
    assert not jobs.discard(job_id) and not jobs.discard(spec.spec_id)
    assert jobs.status()[0]["status"] == "running"
    jobs.cancel(job_id)
    wait_job(jobs, job_id)
    jobs.shutdown()


def test_discard_store_failure_keeps_the_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = make_store(tmp_path)
    spec = jobs.prepare("proj", "görev", ("k",), ("o",))
    real_connect = sqlite3.connect

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("disk dolu")

    monkeypatch.setattr(code_agent_jobs.sqlite3, "connect", broken)
    with pytest.raises(CodeAgentError, match="silinemedi"):
        jobs.discard(spec.spec_id)
    monkeypatch.setattr(code_agent_jobs.sqlite3, "connect", real_connect)
    assert [d["spec_id"] for d in jobs.drafts()] == [spec.spec_id]
    jobs.shutdown()


def test_discard_tool_schema_and_risk_matches_cancel() -> None:
    assert CodeAgentDiscard.risk is CodeAgentCancel.risk is Risk.SIDE_EFFECT
    assert not CodeAgentDiscard.external
    with pytest.raises(ValidationError):
        DiscardArguments.model_validate({"spec_id": "x", "job_id": "y"})
    with pytest.raises(ValidationError):
        DiscardArguments(spec_id="")


async def test_discard_goes_through_the_gate_and_refuses_job_ids(
    tmp_path: Path, repo: Path
) -> None:
    jobs, _ = make_jobs(tmp_path, reply())
    approvals: list[Any] = []
    runtime = create_runtime(
        tmp_path / "unused",
        lambda name, values: approvals.append((name, values)) or False,
        settings=Settings(workspace=tmp_path, state_dir=tmp_path / "state"),
        provider=ScriptedProvider([]),
    )
    runtime.context.services["code_agent_jobs"].shutdown()
    runtime.context.services["code_agent_jobs"] = jobs
    spec = jobs.prepare("proj", "görev", ("k",), ("o",))
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    wait_job(jobs, job_id)
    refused = await runtime.runner.dispatch_tool(
        "code_agent_discard", DiscardArguments(spec_id=job_id)
    )
    assert not refused.success and "bulunamadı" in refused.message and len(jobs.status()) == 1
    done = await runtime.runner.dispatch_tool(
        "code_agent_discard", DiscardArguments(spec_id=spec.spec_id)
    )
    assert done.success and done.data == {"spec_id": spec.spec_id}
    assert all(d["spec_id"] != spec.spec_id for d in jobs.drafts())
    audit = (runtime.settings.audit_path).read_text(encoding="utf-8")
    assert '"code_agent_discard"' in audit  # kapıdan geçti: araç çağrısı denetimde
    jobs.shutdown()


async def test_discard_tool_without_service_fails_honestly(tmp_path: Path) -> None:
    ctx = ToolContext(
        workspace=tmp_path, settings=Settings(), audit=AuditLog(tmp_path / "a.jsonl"), services={}
    )
    result = await CodeAgentDiscard().execute(DiscardArguments(spec_id="x"), ctx)
    assert not result.success and result.status != "ok"


async def test_start_tool_carries_visible_mode_from_the_approved_spec(
    tmp_path: Path, repo: Path, console: None
) -> None:
    jobs, factory = make_visible(tmp_path, agent_script(tmp_path / "o"))
    ctx = tool_ctx(tmp_path, repo, jobs, agents={"codex": CODEX})
    spec = ProjectSpec.model_validate(
        (await CodeAgentPrepare().execute(prep(agent="codex", mode="visible"), ctx)).data["spec"]
    )
    sent = await CodeAgentStart().execute(StartArguments(spec=spec), ctx)
    assert sent.status == "sent"
    job = wait_job(jobs, sent.data["job_id"])
    assert job["mode"] == "visible" and factory.calls[0][0][1] == "-C"
    jobs.shutdown()
