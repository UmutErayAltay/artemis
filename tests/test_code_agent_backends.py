"""ADR 0049: ajan arka uçları ve başlangıç klasörü; gerçek CLI yok, sahte süreç + gerçek git."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_code_agent import git, make_jobs, wait_job
from test_code_agent import repo as _repo_fixture

from artemis.config.settings import (
    AgentProfile,
    CodeAgentConfig,
    ConfigChoiceError,
    MemoryConfig,
    Settings,
)
from artemis.safety.audit import AuditLog
from artemis.tools.base import ToolContext
from artemis.tools.code_agent_backends import RULES, locate_codex, role_profile
from artemis.tools.code_agent_jobs import CodeAgentError, CodeAgentJobs
from artemis.tools.plugins.code_agent import (
    CodeAgentPrepare,
    CodeAgentStart,
    PrepareArguments,
    StartArguments,
)
from artemis.tools.project_spec import ProjectSpec

repo = _repo_fixture

CODEX = AgentProfile(kind="codex", command=sys.executable)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


def plain_script(out: Path, *, code: int = 0, before: str = "") -> str:
    """Claude dışı sahte ajan: istemi ve cwd'yi dosyaya yazar, isteğe bağlı git işi yapar."""
    return (
        "import sys, os, pathlib, subprocess\n"
        f"out = pathlib.Path({str(out)!r})\n"
        "out.with_suffix('.prompt').write_text(sys.stdin.read(), encoding='utf-8')\n"
        "out.with_suffix('.cwd').write_text(os.getcwd(), encoding='utf-8')\n"
        "out.with_suffix('.argv').write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
        + before
        + f"\nsys.exit({code})\n"
    )


def commit_in(repo_path: Path, name: str = "b.txt") -> str:
    return (
        f"r = {str(repo_path)!r}\n"
        f"pathlib.Path(r, {name!r}).write_text('b')\n"
        "subprocess.run(['git','-C',r,'add','.'], check=True)\n"
        "subprocess.run(['git','-C',r,'-c','user.name=t','-c','user.email=t@t',"
        "'commit','-q','-m','ajan'], check=True)\n"
    )


def spec_for(jobs: CodeAgentJobs, repo_path: Path, **kwargs: Any) -> ProjectSpec:
    return jobs.prepare("proj", "görev", ("kapsam",), ("ölçüt",), **kwargs)


def run_plain(
    jobs: CodeAgentJobs, repo_path: Path, profile: AgentProfile, workdir: Path | None = None
) -> dict[str, Any]:
    spec = spec_for(jobs, repo_path, agent="x")
    job_id = jobs.start(
        "proj", repo_path, "görev", spec=spec, profile=profile, workdir=workdir, command="unused"
    )
    return wait_job(jobs, job_id)


def test_claude_default_argv_snapshot_unchanged(tmp_path: Path, repo: Path) -> None:
    jobs, factory = make_jobs(tmp_path, "import sys\nsys.stdin.read()\nprint('{}')\n")
    job_id = jobs.start(
        "proj", repo, "görev", command=sys.executable, manager_model="m", coder_model="c"
    )
    wait_job(jobs, job_id)
    argv, kwargs = factory.calls[0]
    schema = argv[argv.index("--json-schema") + 1]
    expected = [
        sys.executable, "-p", "--output-format", "json", "--json-schema", schema,
        "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Edit", "Write", "Glob",
        "Grep", "Bash", "Agent(coder)", "Agent(tester)", "--disallowedTools",
        "Bash(git push:*)", "WebFetch", "WebSearch", "AskUserQuestion",
        "--append-system-prompt", RULES, "--agents", json.dumps(role_profile("c", None)),
        "--agent", "coordinator", "--model", "m",
    ]  # fmt: skip
    assert argv == expected and "--add-dir" not in argv
    assert Path(kwargs["cwd"]).resolve() == repo.resolve()
    jobs.shutdown()


def test_claude_with_other_workdir_adds_add_dir_and_cwd(
    tmp_path: Path, repo: Path, vault: Path
) -> None:
    out = tmp_path / "o"
    jobs, factory = make_jobs(tmp_path, plain_script(out))
    spec = spec_for(jobs, repo, workdir="ikinci-beyin")
    job_id = jobs.start("proj", repo, "görev", spec=spec, command=sys.executable, workdir=vault)
    job = wait_job(jobs, job_id)
    argv, kwargs = factory.calls[0]
    assert argv[-2:] == ["--add-dir", str(repo.resolve())]
    assert Path(kwargs["cwd"]).resolve() == vault.resolve()
    prompt = out.with_suffix(".prompt").read_text(encoding="utf-8")
    assert "Your starting folder is" in prompt and str(repo.resolve()) in prompt
    assert job["workdir"] == "ikinci-beyin" and job["workdir_path"] == str(vault.resolve())
    jobs.shutdown()


def test_codex_argv_same_folder_and_separate_folder(
    tmp_path: Path, repo: Path, vault: Path
) -> None:
    jobs, factory = make_jobs(tmp_path, plain_script(tmp_path / "o"))
    job = run_plain(jobs, repo, CODEX)
    argv, kwargs = factory.calls[0]
    flags = argv[1:]
    assert flags[:6] == [
        "exec",
        "--sandbox",
        "workspace-write",
        "-C",
        str(repo.resolve()),
        "--json",
    ]
    assert "--add-dir" not in flags and "--skip-git-repo-check" not in flags
    assert flags[-3] == "-o" and flags[-1] == "-" and not Path(flags[-2]).exists()
    assert "--full-auto" not in flags and "--dangerously-bypass-approvals-and-sandbox" not in flags
    assert job["status"] == "done" and Path(kwargs["cwd"]).resolve() == repo.resolve()
    run_plain(jobs, repo, CODEX, workdir=vault)
    flags = factory.calls[1][0][1:]
    assert flags[flags.index("--add-dir") + 1] == str(repo.resolve())
    assert "--skip-git-repo-check" in flags  # vault git deposu değil
    assert Path(factory.calls[1][1]["cwd"]).resolve() == vault.resolve()
    jobs.shutdown()


def test_plain_prompt_has_target_repo_and_starting_folder(
    tmp_path: Path, repo: Path, vault: Path
) -> None:
    out = tmp_path / "o"
    jobs, _ = make_jobs(tmp_path, plain_script(out))
    run_plain(jobs, repo, CODEX, workdir=vault)
    prompt = out.with_suffix(".prompt").read_text(encoding="utf-8")
    assert f"TARGET REPO (absolute path): {repo.resolve()}" in prompt
    assert "no git push" in prompt and "Approved project specification" in prompt
    assert f"Your starting folder is {vault.resolve()}" in prompt
    assert "AskUserQuestion" not in prompt  # claude'a özgü protokol sızmaz
    jobs.shutdown()


def test_cwd_is_workdir_but_git_checks_run_on_repo(tmp_path: Path, repo: Path, vault: Path) -> None:
    out = tmp_path / "o"
    jobs, _ = make_jobs(tmp_path, plain_script(out, before=commit_in(repo.resolve())))
    job = run_plain(jobs, repo, CODEX, workdir=vault)
    assert Path(out.with_suffix(".cwd").read_text()).resolve() == vault.resolve()
    assert job["status"] == "done" and len(job["commits"]) == 1
    assert job["repo"] == str(repo.resolve())
    jobs.shutdown()


def test_generic_prompt_file_placeholders_and_cleanup(tmp_path: Path, repo: Path) -> None:
    keep = tmp_path / "copy.txt"
    script = (
        "import sys, pathlib\n"
        "a = sys.argv[1:]\n"
        f"pathlib.Path({str(keep)!r}).write_text("
        "pathlib.Path(a[a.index('--message-file') + 1]).read_text(encoding='utf-8')"
        " + '|' + '|'.join(a) + '|' + a[a.index('--message-file') + 1], encoding='utf-8')\n"
        "sys.stdin.read()\n"
    )
    jobs, factory = make_jobs(tmp_path, script)
    profile = AgentProfile(
        kind="generic",
        command=sys.executable,
        args=("--message-file", "{prompt_file}", "--yes-always", "--cwd={workdir}", "{repo}"),
    )
    assert run_plain(jobs, repo, profile)["status"] == "done"
    text = keep.read_text(encoding="utf-8")
    assert "TARGET REPO" in text and f"--cwd={repo.resolve()}" in text
    prompt_file = Path(text.rsplit("|", 1)[1])
    assert not prompt_file.exists()  # iş bitince silinir
    assert factory.calls[0][0][0] == sys.executable
    jobs.shutdown()


def test_generic_stdin_default_and_arg_mode(tmp_path: Path, repo: Path) -> None:
    out = tmp_path / "o"
    jobs, factory = make_jobs(tmp_path, plain_script(out))
    stdin_profile = AgentProfile(kind="generic", command=sys.executable, args=("--go",))
    run_plain(jobs, repo, stdin_profile)
    assert "TARGET REPO" in out.with_suffix(".prompt").read_text(encoding="utf-8")
    arg_profile = AgentProfile(
        kind="generic", command=sys.executable, args=("--p", "{prompt}"), prompt_via="arg"
    )
    run_plain(jobs, repo, arg_profile)
    assert "TARGET REPO" in factory.calls[1][0][2]
    assert out.with_suffix(".prompt").read_text(encoding="utf-8") == ""
    jobs.shutdown()


@pytest.mark.parametrize(
    ("before", "code", "status", "needles"),
    [
        (commit_in, 0, "done", ["1 yeni commit", "kalan commitlenmemiş değişiklik yok"]),
        (
            lambda r: "pathlib.Path(" + repr(str(r)) + ", 'a.txt').write_text('kirli')\n",
            0,
            "done",
            ["0 yeni commit", "değişiklik var"],
        ),
        (lambda r: "", 0, "done", ["git'te değişiklik görülmedi"]),
        (commit_in, 3, "error", ["3 koduyla çıktı", "başarısız sayıldı", "1 yeni commit"]),
    ],
)
def test_plain_agent_summary_is_git_based(
    tmp_path: Path, repo: Path, before: Any, code: int, status: str, needles: list[str]
) -> None:
    jobs, _ = make_jobs(
        tmp_path, plain_script(tmp_path / "o", code=code, before=before(repo.resolve()))
    )
    job = run_plain(jobs, repo, CODEX)
    assert job["status"] == status
    for needle in needles:
        assert needle in job["summary"]
    jobs.shutdown()


def test_plain_agent_unreadable_git_is_not_done(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, _ = make_jobs(tmp_path, plain_script(tmp_path / "o"))
    import artemis.tools.code_agent_jobs as module

    original = module._checked_git
    calls = {"n": 0}

    def flaky(path: Path, *args: str) -> str:
        if args[0] == "log":
            raise CodeAgentError("x")
        calls["n"] += 1
        return original(path, *args)

    monkeypatch.setattr(module, "_checked_git", flaky)
    assert run_plain(jobs, repo, CODEX)["status"] == "error"
    jobs.shutdown()


def test_dirty_tree_refused_for_every_agent(tmp_path: Path, repo: Path) -> None:
    (repo / "a.txt").write_text("kirli")
    jobs, factory = make_jobs(tmp_path, plain_script(tmp_path / "o"))
    with pytest.raises(CodeAgentError, match="kirli"):
        run_plain(jobs, repo, CODEX)
    assert factory.calls == []
    jobs.shutdown()


def test_missing_workdir_launches_nothing(tmp_path: Path, repo: Path) -> None:
    jobs, factory = make_jobs(tmp_path, plain_script(tmp_path / "o"))
    for bad in (tmp_path / "yok", Path("relative")):
        with pytest.raises(CodeAgentError, match="Başlangıç klasörü"):
            run_plain(jobs, repo, CODEX, workdir=bad)
    assert factory.calls == []
    jobs.shutdown()


def test_env_passthrough_names_only(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_AGENT_KEY", "gizli-deger-yok")
    monkeypatch.setenv("OTHER_SECRET_KEY", "baska-sir")
    jobs, factory = make_jobs(tmp_path, plain_script(tmp_path / "o"))
    profile = AgentProfile(kind="codex", command=sys.executable, env=("MY_AGENT_KEY", "UNSET_ONE"))
    job = run_plain(jobs, repo, profile)
    argv, kwargs = factory.calls[0]
    assert kwargs["env"]["MY_AGENT_KEY"] == "gizli-deger-yok"
    assert "OTHER_SECRET_KEY" not in kwargs["env"] and "UNSET_ONE" not in kwargs["env"]
    blob = json.dumps(argv) + repr(profile) + json.dumps(job, default=str)
    blob += (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "gizli-deger-yok" not in blob and "baska-sir" not in blob
    jobs.shutdown()


def test_concurrency_guard_repo_and_workdir_that_is_active_repo(tmp_path: Path, repo: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    (other / "x").write_text("x")
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", "ilk")
    jobs, _ = make_jobs(tmp_path, "import sys,time\nsys.stdin.read()\ntime.sleep(30)\n")
    first = jobs.start("proj", repo, "görev", command=sys.executable)
    wait_job(jobs, first, {"running"})
    with pytest.raises(CodeAgentError, match="zaten çalışan"):
        run_plain(jobs, repo, CODEX)
    with pytest.raises(CodeAgentError, match="Başlangıç klasöründe"):
        spec = jobs.prepare("other", "görev", ("k",), ("o",))
        jobs.start("other", other, "görev", spec=spec, profile=CODEX, workdir=repo)
    jobs.cancel(first)
    wait_job(jobs, first)
    jobs.shutdown()


def test_old_sqlite_rows_load_with_defaults(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, "import sys, json\nsys.stdin.read()\n", persistent=True)
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    wait_job(jobs, job_id)
    jobs.shutdown()
    store = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(store) as connection:
        for key, _kind, value in list(connection.execute("SELECT key, kind, value FROM records")):
            record = json.loads(value)
            for field in ("agent", "workdir", "workdir_path"):
                record.pop(field, None)
            for field in ("agent", "workdir", "repo_path", "workdir_path", "approval_text"):
                (record.get("spec") or record).pop(field, None)
            connection.execute("UPDATE records SET value=? WHERE key=?", (json.dumps(record), key))
    restarted = CodeAgentJobs(AuditLog(tmp_path / "audit.jsonl"), store_path=store)
    job = restarted.status()[0]
    assert (job["agent"], job["workdir"], job["workdir_path"]) == ("claude", "proje", job["repo"])
    assert restarted.drafts() == [] or restarted.drafts()[0]["agent"] == "claude"
    restarted.shutdown()


# --- yapılandırma ---


def test_old_and_new_project_shapes_load(tmp_path: Path) -> None:
    old = CodeAgentConfig.model_validate({"projects": {"a": str(tmp_path)}})
    assert old.projects == {"a": tmp_path}
    assert old.project_agent("a") == "claude" and old.project_workdir("a") == "proje"
    new = CodeAgentConfig.model_validate(
        {
            "agents": {
                "codex": {"kind": "codex", "command": "codex", "env": ["OPENAI_API_KEY"]},
                "aider": {"kind": "generic", "command": "aider", "args": ["--yes-always"]},
            },
            "default_agent": "claude",
            "workdirs": {"notlar": str(tmp_path)},
            "projects": {
                "a": str(tmp_path),
                "kule": {"path": str(tmp_path), "agent": "codex", "workdir": "ikinci-beyin"},
            },
        }
    )
    assert new.projects["kule"] == tmp_path and new.project_path("kule") == tmp_path
    assert new.project_agent("kule") == "codex" and new.project_workdir("kule") == "ikinci-beyin"
    assert new.resolve_agent(None)[1].kind == "claude"
    assert new.resolve_agent("codex")[1].env == ("OPENAI_API_KEY",)


@pytest.mark.parametrize(
    "raw",
    [
        {"projects": {"a": {"path": "/x", "agent": "yok"}}},
        {"projects": {"a": {"path": "/x", "workdir": "yok"}}},
        {"default_agent": "yok"},
        {"agents": {"claude": {"kind": "codex"}}},
        {"agents": {"Bad Name": {"kind": "codex"}}},
        {"agents": {"c": {"kind": "codex", "env": ["lower_case"]}}},
        {"agents": {"c": {"kind": "generic"}}},
        {"agents": {"c": {"kind": "generic", "command": "x", "prompt_via": "file"}}},
        {"workdirs": {"proje": "/x"}},
        {"workdirs": {"Bad/Alias": "/x"}},
        {"projects": {"A B": "/x"}},
    ],
)
def test_invalid_config_rejected(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CodeAgentConfig.model_validate(raw)


def test_unknown_reference_error_is_turkish() -> None:
    with pytest.raises(ValidationError, match="tanımsız ajan"):
        CodeAgentConfig.model_validate({"projects": {"a": {"path": "/x", "agent": "yok"}}})


def test_implicit_vault_workdir_and_explicit_override(tmp_path: Path) -> None:
    config = CodeAgentConfig()
    assert config.resolve_workdir("ikinci-beyin", tmp_path / "r", tmp_path / "v") == tmp_path / "v"
    with pytest.raises(ConfigChoiceError, match="Bilinmeyen başlangıç klasörü"):
        config.resolve_workdir("ikinci-beyin", tmp_path / "r", None)
    explicit = CodeAgentConfig(workdirs={"ikinci-beyin": tmp_path / "e"})
    assert explicit.resolve_workdir("ikinci-beyin", tmp_path, tmp_path / "v") == tmp_path / "e"
    assert config.resolve_workdir(None, tmp_path / "r") == tmp_path / "r"
    with pytest.raises(ConfigChoiceError):
        config.resolve_agent("yok")


def test_relative_paths_resolved_at_load(tmp_path: Path) -> None:
    from artemis.config.settings import load_settings

    path = tmp_path / "settings.yaml"
    path.write_text(
        "code_agent:\n  workdirs: {notlar: n}\n  projects:\n    a: {path: p, workdir: notlar}\n",
        encoding="utf-8",
    )
    loaded = load_settings(path).code_agent
    assert loaded.projects["a"] == (tmp_path / "p").resolve()
    assert loaded.workdirs["notlar"] == (tmp_path / "n").resolve()


# --- araçlar / onay ---


def tool_context(
    tmp_path: Path, repo_path: Path, vault_path: Path | None, jobs: CodeAgentJobs
) -> ToolContext:
    config = CodeAgentConfig(
        projects={"proj": repo_path},
        command=sys.executable,
        agents={"codex": CODEX},
    )
    return ToolContext(
        workspace=tmp_path,
        settings=Settings(code_agent=config, memory=MemoryConfig(vault_path=vault_path)),
        audit=jobs._audit,
        services={"code_agent_jobs": jobs},
    )


def prepare_args(**extra: Any) -> PrepareArguments:
    return PrepareArguments(project="proj", task="Görev", scope=("k",), acceptance=("o",), **extra)


@pytest.mark.parametrize("bad", ["../x", "C:\\x", "a/b", "a b", "", "x" * 33, "A"])
def test_alias_with_path_rejected_by_schema(bad: str) -> None:
    with pytest.raises(ValidationError):
        prepare_args(workdir=bad)
    with pytest.raises(ValidationError):
        prepare_args(agent=bad)


async def test_unknown_aliases_rejected_by_prepare(tmp_path: Path, repo: Path, vault: Path) -> None:
    jobs, factory = make_jobs(tmp_path, plain_script(tmp_path / "o"))
    ctx = tool_context(tmp_path, repo, vault, jobs)
    bad_agent = await CodeAgentPrepare().execute(prepare_args(agent="gemini"), ctx)
    assert not bad_agent.success and "Bilinmeyen ajan 'gemini'" in bad_agent.message
    bad_dir = await CodeAgentPrepare().execute(prepare_args(workdir="masaustu"), ctx)
    assert not bad_dir.success and "Bilinmeyen başlangıç klasörü" in bad_dir.message
    no_vault = tool_context(tmp_path, repo, None, jobs)
    missing = await CodeAgentPrepare().execute(prepare_args(workdir="ikinci-beyin"), no_vault)
    assert not missing.success and jobs.drafts() == [] and factory.calls == []
    jobs.shutdown()


async def test_prepare_and_start_with_codex_and_vault_show_everything(
    tmp_path: Path, repo: Path, vault: Path
) -> None:
    jobs, factory = make_jobs(tmp_path, plain_script(tmp_path / "o"))
    ctx = tool_context(tmp_path, repo, vault, jobs)
    result = await CodeAgentPrepare().execute(
        prepare_args(agent="codex", workdir="ikinci-beyin"), ctx
    )
    assert result.success and factory.calls == []
    spec = ProjectSpec.model_validate(result.data["spec"])
    text = spec.approval_text
    assert "Ajan: codex" in text
    assert f"Başlangıç klasörü: ikinci-beyin -> {vault.resolve()}" in text
    assert f"Hedef repo: {repo.resolve()}" in text
    assert "Ajan bu klasörde başlar ve oradaki kuralları yükler" in text
    assert "Bu klasördeki dosyaları değiştirebilir" in text
    # Model onay metnini sahtelese bile yeniden türetilir.
    forged = ProjectSpec.model_validate({**result.data["spec"], "approval_text": "zararsız"})
    assert forged.approval_text == text and forged == spec
    sent = await CodeAgentStart().execute(StartArguments(spec=spec), ctx)
    assert sent.status == "sent"
    job = wait_job(jobs, sent.data["job_id"])
    assert job["agent"] == "codex" and job["workdir"] == "ikinci-beyin"
    assert Path(factory.calls[0][1]["cwd"]).resolve() == vault.resolve()
    # Aynı repo, ayar değişti: onaylanan yol eskidi, hiçbir şey başlamaz.
    again = await CodeAgentPrepare().execute(
        prepare_args(agent="codex", workdir="ikinci-beyin"), ctx
    )
    moved = tool_context(tmp_path, repo, tmp_path, jobs)
    stale = await CodeAgentStart().execute(
        StartArguments(spec=ProjectSpec.model_validate(again.data["spec"])), moved
    )
    assert "Ayarlar değişti" in stale.message and len(factory.calls) == 1
    jobs.shutdown()


async def test_prepare_uses_project_defaults(tmp_path: Path, repo: Path, vault: Path) -> None:
    jobs, _ = make_jobs(tmp_path, plain_script(tmp_path / "o"))
    config = CodeAgentConfig(
        projects={"proj": {"path": repo, "agent": "codex", "workdir": "ikinci-beyin"}},
        agents={"codex": CODEX},
    )
    ctx = ToolContext(
        workspace=tmp_path,
        settings=Settings(code_agent=config, memory=MemoryConfig(vault_path=vault)),
        audit=jobs._audit,
        services={"code_agent_jobs": jobs},
    )
    spec = (await CodeAgentPrepare().execute(prepare_args(), ctx)).data["spec"]
    assert spec["agent"] == "codex" and spec["workdir"] == "ikinci-beyin"
    override = (
        await CodeAgentPrepare().execute(prepare_args(agent="claude", workdir="proje"), ctx)
    ).data["spec"]
    assert override["agent"] == "claude" and "Bu klasördeki" not in override["approval_text"]
    jobs.shutdown()


def test_default_approval_text_has_no_folder_warning() -> None:
    spec = ProjectSpec(
        spec_id="00000000-0000-4000-8000-000000000000",
        project="p",
        task="t",
        scope=("s",),
        acceptance=("a",),
        repo_path="/r",
        workdir_path="/r",
    )
    assert "Ajan: claude" in spec.approval_text and "değiştirebilir" not in spec.approval_text


# --- codex konumlandırma ---


class Runner:
    def __init__(self, output: str = "", code: int = 0) -> None:
        self.output, self.code, self.calls = output, code, 0

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        assert argv[0] == "powershell" and "Get-AppxPackage OpenAI.Codex" in argv[-1]
        return subprocess.CompletedProcess(argv, self.code, self.output, "")


def appx(tmp_path: Path) -> tuple[Path, Path]:
    location = tmp_path / "OpenAI.Codex_1.2.3_x64__abc"
    exe = location / "app" / "resources" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    return location, exe


def test_locate_codex_order(tmp_path: Path) -> None:
    location, exe = appx(tmp_path)
    runner = Runner(str(location) + "\n")
    explicit = tmp_path / "mine.exe"
    explicit.write_text("")
    kwargs: dict[str, Any] = {"runner": runner, "is_windows": True, "environ": {}}
    # 1) açık mutlak komut her zaman kazanır, PATH/PowerShell sorulmaz
    assert locate_codex(str(explicit), which=lambda c: "PATH", cache={}, **kwargs) == str(explicit)
    assert (
        locate_codex(str(tmp_path / "yok.exe"), which=lambda c: "PATH", cache={}, **kwargs) is None
    )
    # 2) PATH
    assert (
        locate_codex("codex", which=lambda c: "C:/path/codex", cache={}, **kwargs)
        == "C:/path/codex"
    )
    assert runner.calls == 0
    # 3) %APPDATA%\npm\codex.cmd
    npm = tmp_path / "npm" / "codex.cmd"
    npm.parent.mkdir()
    npm.write_text("")
    env = {"APPDATA": str(tmp_path)}
    assert locate_codex(
        "codex", which=lambda c: None, runner=runner, is_windows=True, environ=env, cache={}
    ) == str(npm)
    assert runner.calls == 0
    # 4) Appx paketi (sabit PowerShell betiği, sahte runner)
    cache: dict[str, str | None] = {}
    assert locate_codex("codex", which=lambda c: None, cache=cache, **kwargs) == str(exe)
    assert locate_codex("codex", which=lambda c: None, cache=cache, **kwargs) == str(exe)
    assert runner.calls == 1  # süreç başına önbellek


def test_locate_codex_failures_and_non_windows(tmp_path: Path) -> None:
    location, _ = appx(tmp_path)
    base: dict[str, Any] = {"which": lambda c: None, "environ": {}}
    for bad in (
        Runner("", 1),
        Runner("", 0),
        Runner("relative\\path"),
        Runner(str(tmp_path / "bos")),
    ):
        assert locate_codex("codex", runner=bad, is_windows=True, cache={}, **base) is None
    boom = Runner(str(location))
    assert locate_codex("codex", runner=boom, is_windows=False, cache={}, **base) is None
    assert boom.calls == 0  # Windows dışında PowerShell çalıştırılmaz
    assert locate_codex("baska", runner=boom, is_windows=True, cache={}, **base) is None
    assert boom.calls == 0  # yalnız "codex" adı için Appx aranır
