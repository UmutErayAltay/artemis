"""Kod ajanı (ADR 0023): gerçek git (tmp_path), sahte `claude` süreci."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from jarvis.config.settings import CodeAgentConfig, Settings
from jarvis.safety.audit import AuditLog
from jarvis.tools.base import Risk, ToolContext
from jarvis.tools.code_agent_jobs import CodeAgentError, CodeAgentJobs
from jarvis.tools.plugins.code_agent import (
    CodeAgentCancel,
    CodeAgentStart,
    CodeAgentStatus,
    StartArguments,
    StatusArguments,
)
from jarvis.tools.registry import discover_tools


def git(path: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=path, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    git(path, "init", "-q")
    (path / "a.txt").write_text("a\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "ilk")
    return path


class FakeProc:
    def __init__(self, behave: Any, kwargs: dict[str, Any]) -> None:
        self.behave = behave
        self.kwargs = kwargs
        self.returncode: int | None = 0
        self.killed = False

    def communicate(self, stdin: str | None = None, timeout: float | None = None) -> Any:
        return self.behave(self, stdin, timeout)

    def kill(self) -> None:
        self.killed = True


class Factory:
    """Popen yerine geçer; `behave(proc, stdin, timeout) -> (stdout, stderr)`."""

    def __init__(self, behave: Any) -> None:
        self.behave = behave
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.procs: list[FakeProc] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProc:
        self.calls.append((argv, kwargs))
        proc = FakeProc(self.behave, kwargs)
        self.procs.append(proc)
        return proc


def ok_json(**extra: Any) -> str:
    return json.dumps({"result": "Yapıldı.", "is_error": False, "total_cost_usd": 0.5,
                       "num_turns": 3, **extra})  # fmt: skip


def commit_and_reply(repo_path: Path, payload: str, code: int = 0) -> Any:
    def behave(proc: FakeProc, stdin: str | None, timeout: float | None) -> Any:
        (repo_path / "b.txt").write_text("b\n")
        git(repo_path, "add", ".")
        git(repo_path, "commit", "-q", "-m", "ajan işi")
        proc.returncode = code
        return payload, ""

    return behave


class Events(AuditLog):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.events: list[dict[str, Any]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})


def make_jobs(tmp_path: Path, behave: Any) -> tuple[CodeAgentJobs, Factory, Events]:
    audit = Events(tmp_path / "audit.jsonl")
    factory = Factory(behave)
    return CodeAgentJobs(audit, process_factory=factory), factory, audit


def run_job(jobs: CodeAgentJobs, repo_path: Path, **kwargs: Any) -> dict[str, Any]:
    job_id = jobs.start("proj", repo_path, "görev", command=sys.executable, **kwargs)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = next(j for j in jobs.status() if j["job_id"] == job_id)
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("iş bitmedi")


def done_event(audit: Events) -> dict[str, Any]:
    return next(e for e in audit.events if e["event"] == "code_agent_done")


def test_tools_discovered_with_schema_and_risk() -> None:
    registry = discover_tools()
    start = registry.get("code_agent_start")
    assert start is not None and start.risk is Risk.CONFIRM_REQUIRED
    schema = start.args_model.model_json_schema()
    assert set(schema["properties"]) == {"project", "task"}
    assert schema["properties"]["task"]["maxLength"] == 4000
    status = registry.get("code_agent_status")
    assert status is not None and status.risk is Risk.READ_ONLY and status.external
    cancel = registry.get("code_agent_cancel")
    assert cancel is not None and cancel.risk is Risk.SIDE_EFFECT


def context(tmp_path: Path, projects: dict[str, Path], jobs: CodeAgentJobs) -> ToolContext:
    settings = Settings(
        code_agent=CodeAgentConfig(projects=projects, command=sys.executable, model="m-1")
    )
    return ToolContext(
        workspace=tmp_path, settings=settings, audit=jobs._audit,
        services={"code_agent_jobs": jobs},
    )  # fmt: skip


async def test_feature_off_and_unknown_alias_error(tmp_path: Path, repo: Path) -> None:
    jobs, factory, _ = make_jobs(tmp_path, None)
    args = StartArguments(project="proj", task="x")
    off = await CodeAgentStart().execute(args, context(tmp_path, {}, jobs))
    assert off.status == "error" and "özellik kapalı" in off.message
    ctx = context(tmp_path, {"proj": repo}, jobs)
    unknown = await CodeAgentStart().execute(StartArguments(project="yok", task="x"), ctx)
    assert unknown.status == "error" and "proj" in unknown.message
    assert factory.calls == []


def test_alias_pattern_validated() -> None:
    with pytest.raises(ValueError):
        CodeAgentConfig(projects={"Kötü Ad": Path("x")})


async def test_start_tool_sent_not_success_and_argv(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    jobs, factory, audit = make_jobs(tmp_path, commit_and_reply(repo, ok_json()))
    base = git(repo, "rev-parse", "HEAD").strip()
    result = await CodeAgentStart().execute(
        StartArguments(project="proj", task="dosya ekle"), context(tmp_path, {"proj": repo}, jobs)
    )
    assert result.status == "sent" and result.success is False
    assert result.data["base"] == base
    deadline = time.monotonic() + 10
    while not any(e["event"] == "code_agent_done" for e in audit.events):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    argv, kwargs = factory.calls[0]
    assert "Bash(git push:*)" in argv and "Bash(git remote:*)" in argv
    assert argv[argv.index("--model") + 1] == "m-1"
    assert kwargs["cwd"] == repo and "OPENROUTER_API_KEY" not in kwargs["env"]
    assert factory.procs[0].kwargs["stdin"] == subprocess.PIPE
    listed = await CodeAgentStatus().execute(StatusArguments(), context(tmp_path, {}, jobs))
    assert listed.external and len(listed.data["jobs"]) == 1


def test_done_requires_verified_success(tmp_path: Path, repo: Path) -> None:
    jobs, _, audit = make_jobs(tmp_path, commit_and_reply(repo, ok_json()))
    base = git(repo, "rev-parse", "HEAD").strip()
    job = run_job(jobs, repo)
    assert job["status"] == "done" and job["base"] == base
    event = done_event(audit)
    assert event["status"] == "done" and len(event["commits"]) == 1
    assert event["commits"][0].endswith("ajan işi")
    assert event["leftover_changes"] is False and event["cost_usd"] == 0.5
    started = next(e for e in audit.events if e["event"] == "code_agent_started")
    assert started["base"] == base and started["project"] == "proj"


@pytest.mark.parametrize(
    ("payload", "code"),
    [(ok_json(is_error=True), 0), (ok_json(), 1), ("bu json değil", 0), ("[]", 0)],
)
def test_failures_are_error_not_done(tmp_path: Path, repo: Path, payload: str, code: int) -> None:
    jobs, _, audit = make_jobs(tmp_path, commit_and_reply(repo, payload, code))
    job = run_job(jobs, repo)
    assert job["status"] == "error"
    assert done_event(audit)["status"] == "error"
    assert len(done_event(audit)["commits"]) == 1  # commit'ler yine de raporlanır


def test_leftover_changes_reported(tmp_path: Path, repo: Path) -> None:
    def behave(proc: FakeProc, stdin: str | None, timeout: float | None) -> Any:
        (repo / "a.txt").write_text("kirli\n")
        return ok_json(), ""

    jobs, _, audit = make_jobs(tmp_path, behave)
    run_job(jobs, repo)
    assert done_event(audit)["leftover_changes"] is True


def test_timeout_kills(tmp_path: Path, repo: Path) -> None:
    def behave(proc: FakeProc, stdin: str | None, timeout: float | None) -> Any:
        if timeout is not None:
            assert timeout == 90  # 1.5 dk
            raise subprocess.TimeoutExpired("claude", timeout)
        proc.returncode = -9
        return "", ""

    jobs, factory, _ = make_jobs(tmp_path, behave)
    assert run_job(jobs, repo, max_minutes=1.5)["status"] == "timeout"
    assert factory.procs[0].killed


def test_cancel(tmp_path: Path, repo: Path) -> None:
    import threading

    started, release = threading.Event(), threading.Event()

    def behave(proc: FakeProc, stdin: str | None, timeout: float | None) -> Any:
        started.set()
        release.wait(5)
        proc.returncode = -9
        return "", ""

    jobs, factory, audit = make_jobs(tmp_path, behave)
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    assert started.wait(5)
    assert jobs.cancel(job_id) is True and factory.procs[0].killed
    release.set()
    deadline = time.monotonic() + 10
    while not any(e["event"] == "code_agent_done" for e in audit.events):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert done_event(audit)["status"] == "cancelled"
    assert jobs.cancel(job_id) is False
    assert jobs.cancel("yok") is False


def test_dirty_tree_and_non_repo_and_concurrent_refused(tmp_path: Path, repo: Path) -> None:
    import threading

    release = threading.Event()

    def behave(proc: FakeProc, stdin: str | None, timeout: float | None) -> Any:
        release.wait(5)
        return ok_json(), ""

    jobs, factory, _ = make_jobs(tmp_path, behave)
    (repo / "kirli.txt").write_text("x")
    with pytest.raises(CodeAgentError, match="kirli"):
        jobs.start("proj", repo, "t", command=sys.executable)
    (repo / "kirli.txt").unlink()
    plain = tmp_path / "duz"
    plain.mkdir()
    with pytest.raises(CodeAgentError, match="git deposu"):
        jobs.start("duz", plain, "t", command=sys.executable)
    jobs.start("proj", repo, "t", command=sys.executable)
    with pytest.raises(CodeAgentError, match="zaten çalışan"):
        jobs.start("proj", repo, "t", command=sys.executable)
    jobs.shutdown()
    release.set()
    assert len(factory.calls) <= 1


async def test_cancel_tool_honest_message(tmp_path: Path, repo: Path) -> None:
    jobs, _, _ = make_jobs(tmp_path, None)
    missing = await CodeAgentCancel().execute(
        CodeAgentCancel.args_model(job_id="yok"), context(tmp_path, {}, jobs)
    )
    assert missing.status == "error"
