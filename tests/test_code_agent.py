"""F5 kabulü: tmp_path gerçek git ve ücretli CLI yerine yerel sahte süreç."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jarvis.brain.fake import ScriptedProvider
from jarvis.channels.runtime import create_runtime
from jarvis.config.settings import CodeAgentConfig, Settings
from jarvis.safety.audit import AuditLog
from jarvis.tools.base import Risk, ToolContext
from jarvis.tools.code_agent_jobs import CodeAgentError, CodeAgentJobs
from jarvis.tools.plugins.code_agent import (
    CodeAgentAnswer,
    CodeAgentCancel,
    CodeAgentPrepare,
    CodeAgentStatus,
    PrepareArguments,
    StartArguments,
    StatusArguments,
)
from jarvis.tools.project_spec import AUTHORITY_BOUNDARY, ProjectSpec
from jarvis.tools.registry import discover_tools

SESSION = str(uuid.uuid4())
TERMINAL = {"done", "error", "cancelled", "timeout", "cleanup_pending", "interrupted"}


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    git(path, "init", "-q")
    (path / "a.txt").write_text("a\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "ilk")
    return path


def payload(kind: str = "completed", **extra: Any) -> dict[str, Any]:
    return {
        "is_error": False,
        "session_id": SESSION,
        "total_cost_usd": 0.5,
        "structured_output": {
            "kind": kind,
            "summary": "Yapıldı.",
            "question": "Hangi renk?" if kind == "question" else None,
        },
        **extra,
    }


class FakeCLI:
    """Gerçek boru, process group, exit ve timeout; yalnız CLI programı sahtedir."""

    def __init__(self, script: str) -> None:
        self.script = script
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.procs: list[Any] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append((argv, kwargs))
        proc = subprocess.Popen([sys.executable, "-c", self.script, *argv[1:]], **kwargs)
        self.procs.append(proc)
        return proc


def reply(data: Any = None, *, code: int = 0, before: str = "") -> str:
    data = payload() if data is None else data
    return (
        "import sys, json, time, subprocess, pathlib\n"
        "prompt = sys.stdin.read()\n" + before + "\n"
        f"print({json.dumps(data)!r})\nsys.exit({code})\n"
    )


def make_jobs(
    tmp_path: Path, script: str, *, persistent: bool = False
) -> tuple[CodeAgentJobs, FakeCLI]:
    factory = FakeCLI(script)
    jobs = CodeAgentJobs(
        AuditLog(tmp_path / "audit.jsonl"),
        process_factory=factory,
        store_path=tmp_path / "jobs.sqlite3" if persistent else None,
    )
    return jobs, factory


def wait_job(jobs: CodeAgentJobs, job_id: str, statuses: set[str] = TERMINAL) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = next(job for job in jobs.status() if job["job_id"] == job_id)
        if job["status"] in statuses:
            return job
        time.sleep(0.01)
    raise AssertionError(f"İş bitmedi: {jobs.status()}")


def run_job(jobs: CodeAgentJobs, repo: Path, **kwargs: Any) -> dict[str, Any]:
    job_id = jobs.start("proj", repo, "görev", command=sys.executable, **kwargs)
    return wait_job(jobs, job_id)


def context(tmp_path: Path, repo: Path, jobs: CodeAgentJobs) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        settings=Settings(
            code_agent=CodeAgentConfig(projects={"proj": repo}, command=sys.executable)
        ),
        audit=jobs._audit,
        services={"code_agent_jobs": jobs},
    )


def test_tools_discovered_with_schema_and_risk() -> None:
    registry = discover_tools()
    start = registry.get("code_agent_start")
    assert start is not None and start.risk is Risk.CONFIRM_REQUIRED
    assert "spec" in start.args_model.model_json_schema()["properties"]
    assert "authority_boundary" in start.args_model.model_json_schema()["properties"]
    assert registry.get("code_agent_prepare") is not None
    assert CodeAgentAnswer.risk is Risk.CONFIRM_REQUIRED
    assert CodeAgentStatus.risk is Risk.READ_ONLY
    assert CodeAgentStatus.external and CodeAgentStatus.content_kind == "file"
    assert CodeAgentCancel.risk is Risk.SIDE_EFFECT


async def test_prepare_never_spawns_and_start_approves_entire_immutable_spec(
    tmp_path: Path, repo: Path
) -> None:
    jobs, factory = make_jobs(tmp_path, reply())
    args = PrepareArguments(
        project="proj", task="Görev", scope=("Dosya ekle",), acceptance=("Test geçer",)
    )
    result = await CodeAgentPrepare().execute(args, context(tmp_path, repo, jobs))
    assert result.success and factory.calls == []
    spec = ProjectSpec.model_validate(result.data["spec"])
    with pytest.raises(ValidationError):
        spec.task = "Değiştir"
    approvals: list[dict[str, Any]] = []
    runtime = create_runtime(
        tmp_path / "unused",
        lambda name, values: approvals.append(values) or False,
        settings=Settings(workspace=tmp_path, state_dir=tmp_path / "state"),
        provider=ScriptedProvider([]),
    )
    runtime.context.services["code_agent_jobs"].shutdown()
    runtime.context.services["code_agent_jobs"] = jobs
    runtime.context.settings = runtime.settings.model_copy(
        update={"code_agent": CodeAgentConfig(projects={"proj": repo}, command=sys.executable)}
    )
    denied = await runtime.runner.dispatch_tool("code_agent_start", StartArguments(spec=spec))
    assert denied.status == "denied" and factory.calls == []
    assert approvals[0]["spec"] == spec.model_dump(mode="json")
    assert approvals[0]["authority_boundary"] == AUTHORITY_BOUNDARY
    runtime.agent.approve = lambda name, values: True
    sent = await runtime.runner.dispatch_tool("code_agent_start", StartArguments(spec=spec))
    assert sent.status == "sent" and not sent.success
    assert wait_job(jobs, sent.data["job_id"])["status"] == "done"
    replay = await runtime.runner.dispatch_tool("code_agent_start", StartArguments(spec=spec))
    assert replay.status == "error" and len(factory.calls) == 1
    altered = spec.model_copy(update={"task": "Yanlış"})
    with pytest.raises(CodeAgentError, match="değişmiş"):
        jobs.start("proj", repo, altered.task, command=sys.executable, spec=altered)
    jobs.shutdown()


def test_argv_models_schema_stdin_and_secret_boundary(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("TEST_TOKEN", "token-test-not-real")
    jobs, factory = make_jobs(tmp_path, reply())
    job = run_job(jobs, repo, manager_model="manager", coder_model="coder", tester_model="tester")
    assert job["status"] == "done" and job["commits"] == []
    argv, kwargs = factory.calls[0]
    assert "--json-schema" in argv and "--bare" not in argv
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == [
        "kind",
        "summary",
        "question",
    ]
    assert argv[argv.index("--model") + 1] == "manager"
    profile = json.loads(argv[argv.index("--agents") + 1])
    assert profile["coder"]["model"] == "coder" and profile["tester"]["model"] == "tester"
    assert "Agent(coder)" in profile["coordinator"]["tools"]
    assert "Bash(git push:*)" in argv and "AskUserQuestion" in argv
    assert "OPENROUTER_API_KEY" not in kwargs["env"] and "TEST_TOKEN" not in kwargs["env"]
    assert kwargs["stdin"] == subprocess.PIPE and "görev" not in argv
    jobs.shutdown()


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (payload(is_error=True), 0),
        (payload(), 1),
        ("not a json object", 0),
        ({"is_error": False, "result": "Yaptım"}, 0),
        (payload(session_id="not-a-uuid"), 0),
        (payload(structured_output={"kind": "completed", "summary": "x", "question": "Soru"}), 0),
    ],
)
def test_failures_never_done(tmp_path: Path, repo: Path, data: Any, code: int) -> None:
    jobs, _ = make_jobs(tmp_path, reply(data, code=code))
    assert run_job(jobs, repo)["status"] == "error"
    jobs.shutdown()


def test_real_git_result_and_leftovers(tmp_path: Path, repo: Path) -> None:
    before = (
        "pathlib.Path('b.txt').write_text('b')\n"
        "subprocess.run(['git','add','.'], check=True)\n"
        "subprocess.run(['git','-c','user.name=t','-c','user.email=t@t',"
        "'commit','-q','-m','ajan'], check=True)\n"
        "pathlib.Path('a.txt').write_text('kalan')"
    )
    jobs, _ = make_jobs(tmp_path, reply(before=before))
    job = run_job(jobs, repo)
    assert job["status"] == "done" and job["leftover_changes"] is True
    assert len(job["commits"]) == 1 and job["commits"][0].endswith("ajan")
    jobs.shutdown()


def test_question_exits_and_answer_resumes_exact_uuid_once(tmp_path: Path, repo: Path) -> None:
    script = (
        "import sys,json,pathlib\nprompt=sys.stdin.read()\n"
        f"result={payload('question')!r}\n"
        f"if '--resume' in sys.argv: result={payload()!r}; "
        "pathlib.Path('answer.txt').write_text(prompt)\n"
        "print(json.dumps(result))\n"
    )
    jobs, factory = make_jobs(tmp_path, script)
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    waiting = wait_job(jobs, job_id, {"waiting"})
    assert factory.procs[0].poll() == 0
    assert waiting["session_id"] == SESSION and waiting["question"] == "Hangi renk?"
    assert not jobs.answer(job_id, "stale", "Mavi")
    assert jobs.answer(job_id, waiting["question_id"], "Mavi")
    assert not jobs.answer(job_id, waiting["question_id"], "Tekrar")
    done = wait_job(jobs, job_id)
    assert done["status"] == "done" and done["deadline"] == waiting["deadline"]
    argv = factory.calls[1][0]
    assert argv[argv.index("--resume") + 1] == SESSION
    assert (repo / "answer.txt").read_text() == "Mavi"
    jobs.shutdown()


def test_resume_other_uuid_rejected(tmp_path: Path, repo: Path) -> None:
    script = (
        "import sys,json\nsys.stdin.read()\n"
        f"result={payload('question')!r}\n"
        f"if '--resume' in sys.argv: result={payload(session_id=str(uuid.uuid4()))!r}\n"
        "print(json.dumps(result))\n"
    )
    jobs, _ = make_jobs(tmp_path, script)
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    waiting = wait_job(jobs, job_id, {"waiting"})
    assert jobs.answer(job_id, waiting["question_id"], "Mavi")
    assert wait_job(jobs, job_id)["status"] == "error"
    jobs.shutdown()


def test_waiting_deadline_and_cancel_reject_answers(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, reply(payload("question")))
    job_id = jobs.start("proj", repo, "görev", command=sys.executable, max_minutes=0.006)
    waiting = wait_job(jobs, job_id, {"waiting"})
    assert wait_job(jobs, job_id)["status"] == "timeout"
    assert not jobs.answer(job_id, waiting["question_id"], "geç")
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    waiting = wait_job(jobs, job_id, {"waiting"})
    assert jobs.cancel(job_id)
    assert wait_job(jobs, job_id)["status"] == "cancelled"
    assert not jobs.answer(job_id, waiting["question_id"], "iptal sonrası")
    jobs.shutdown()


def test_monotonic_total_deadline_survives_wall_clock_rollback(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, _ = make_jobs(tmp_path, reply(payload("question")))
    job_id = jobs.start("proj", repo, "görev", command=sys.executable, max_minutes=0.006)
    wait_job(jobs, job_id, {"waiting"})
    monkeypatch.setattr("jarvis.tools.code_agent_jobs.time.time", lambda: 0)
    assert wait_job(jobs, job_id)["status"] == "timeout"
    jobs.shutdown()


def test_running_timeout_and_cancel_bounded(tmp_path: Path, repo: Path) -> None:
    jobs, factory = make_jobs(tmp_path, "import sys,time\nsys.stdin.read()\ntime.sleep(60)\n")
    before = time.monotonic()
    assert run_job(jobs, repo, max_minutes=0.002)["status"] == "timeout"
    assert time.monotonic() - before < 3
    assert factory.procs[0].poll() is not None
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    wait_job(jobs, job_id, {"running"})
    assert jobs.cancel(job_id)
    assert wait_job(jobs, job_id)["status"] == "cancelled"
    assert not jobs.cancel(job_id)
    jobs.shutdown()


def test_cancel_before_and_during_spawn(tmp_path: Path, repo: Path) -> None:
    jobs, factory = make_jobs(tmp_path, "import sys,time\nsys.stdin.read()\ntime.sleep(60)\n")
    entered, release = threading.Event(), threading.Event()
    actual_factory = jobs._factory

    def paused(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(3)
        return actual_factory(*args, **kwargs)

    jobs._factory = paused
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    assert entered.wait(2)
    assert jobs.cancel(job_id)  # Fabrika kilidi tutup iptali engellemez.
    release.set()
    assert wait_job(jobs, job_id)["status"] == "cancelled"
    assert factory.procs[0].poll() is not None
    jobs.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="POSIX grup/DEVNULL regresyonu")
def test_normal_exit_cleans_child_with_closed_stdio(tmp_path: Path, repo: Path) -> None:
    child = "import time,pathlib; time.sleep(.6); pathlib.Path('escaped.txt').write_text('bad')"
    before = (
        f"subprocess.Popen([sys.executable,'-c',{child!r}], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    )
    jobs, _ = make_jobs(tmp_path, reply(before=before))
    assert run_job(jobs, repo)["status"] == "done"
    time.sleep(0.75)
    assert not (repo / "escaped.txt").exists()
    jobs.shutdown()


def test_cleanup_failure_retains_repo_lock(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.tools.code_agent_process import ProcessCapture

    jobs, factory = make_jobs(tmp_path, reply())
    original_finish = ProcessCapture.finish

    def unverified_cleanup(capture: ProcessCapture, kill: bool) -> bool:
        # Gerçek çocukları temizler; yalnız doğrulama sonucu her platformda başarısızdır.
        original_finish(capture, kill=kill)
        return False

    monkeypatch.setattr(ProcessCapture, "finish", unverified_cleanup)
    job = run_job(jobs, repo)
    assert factory.procs[0].poll() is not None
    assert job["status"] == "cleanup_pending" and job["cleanup_pending"]
    with pytest.raises(CodeAgentError, match="belirsiz"):
        jobs.start("alias", repo, "görev", command=sys.executable)
    jobs.shutdown()


def test_git_error_never_clean_or_done(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jarvis.tools.code_agent_jobs as module

    jobs, factory = make_jobs(tmp_path, reply())
    original = module._git

    def failing(path: Path, *args: str) -> Any:
        if factory.calls and args[0] == "status":
            return subprocess.CompletedProcess([], 1, "", "failure")
        return original(path, *args)

    monkeypatch.setattr(module, "_git", failing)
    job = run_job(jobs, repo)
    assert job["status"] == "error" and job["leftover_changes"] is None
    jobs.shutdown()


def test_git_failure_at_start_and_dirty_tree_rejected(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jarvis.tools.code_agent_jobs as module

    jobs, factory = make_jobs(tmp_path, reply())
    (repo / "dirty").write_text("x")
    with pytest.raises(CodeAgentError, match="kirli"):
        jobs.start("proj", repo, "görev", command=sys.executable)
    (repo / "dirty").unlink()
    original = module._git
    monkeypatch.setattr(
        module,
        "_git",
        lambda path, *args: subprocess.CompletedProcess([], 1, "", "error")
        if args[0] == "status"
        else original(path, *args),
    )
    with pytest.raises(CodeAgentError, match="temiz kabul edilmedi"):
        jobs.start("proj", repo, "görev", command=sys.executable)
    assert factory.calls == []
    jobs.shutdown()


def test_aliases_share_physical_repo_lock(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, "import sys,time\nsys.stdin.read()\ntime.sleep(60)")
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    with pytest.raises(CodeAgentError, match="zaten çalışan"):
        jobs.start("other", repo / ".", "görev", command=sys.executable)
    jobs.cancel(job_id)
    assert wait_job(jobs, job_id)["status"] == "cancelled"
    jobs.shutdown()


def test_store_has_single_owner_restart_preserves_drafts_and_results(
    tmp_path: Path, repo: Path
) -> None:
    jobs, _ = make_jobs(tmp_path, reply(), persistent=True)
    spec = jobs.prepare("proj", "taslak", ("kapsam",), ("ölçüt",))
    with pytest.raises(CodeAgentError, match="başka bir runtime"):
        make_jobs(tmp_path, reply(), persistent=True)
    job = run_job(jobs, repo)
    jobs.shutdown()
    restarted, factory = make_jobs(tmp_path, reply(), persistent=True)
    assert restarted.status()[0]["status"] == "done"
    assert restarted.status()[0]["job_id"] == job["job_id"]
    assert restarted.drafts()[0]["spec_id"] == spec.spec_id
    assert factory.calls == []
    restarted.shutdown()


def test_restart_active_interrupted_no_auto_spawn_and_uncertain_lock(
    tmp_path: Path, repo: Path
) -> None:
    jobs, _ = make_jobs(tmp_path, reply(), persistent=True)
    job = run_job(jobs, repo)
    jobs.shutdown()
    store = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(store) as connection:
        record = json.loads(
            connection.execute(
                "SELECT value FROM records WHERE key=?", (job["job_id"],)
            ).fetchone()[0]
        )
        record["status"] = "waiting"
        record["pid"] = 1  # Kullanılmayan eski PID hiçbir öldürme işlemi yaratmaz.
        connection.execute(
            "UPDATE records SET value=? WHERE key=?", (json.dumps(record), job["job_id"])
        )
    restarted, factory = make_jobs(tmp_path, reply(), persistent=True)
    assert restarted.status()[0]["status"] == "interrupted" and factory.calls == []
    with pytest.raises(CodeAgentError, match="belirsiz"):
        restarted.start("other", repo, "görev", command=sys.executable)
    restarted.shutdown()


def test_output_limit_and_safe_audit_repr(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TOKEN", "not-real-sensitive-token")
    jobs, _ = make_jobs(tmp_path, reply(before="print('x'*300000)"))
    assert run_job(jobs, repo)["status"] == "error"
    spec = jobs.prepare("proj", "not-real-sensitive-token", ("not-real-sensitive-token",), ("ok",))
    assert "not-real-sensitive-token" not in repr(spec)
    assert "not-real-sensitive-token" not in (tmp_path / "audit.jsonl").read_text()
    jobs.shutdown()


async def test_status_file_privacy_and_second_runtime_keeps_chat(
    tmp_path: Path, repo: Path
) -> None:
    settings = Settings(
        workspace=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
    )
    runtime = create_runtime(
        tmp_path / "unused",
        lambda name, args: False,
        settings=settings,
        provider=ScriptedProvider([]),
    )
    other = create_runtime(
        tmp_path / "unused",
        lambda name, args: False,
        settings=settings,
        provider=ScriptedProvider([]),
    )
    assert "code_agent_unavailable" in other.context.services
    result = await other.runner.dispatch_tool("code_agent_status", StatusArguments())
    assert result.success and result.content_kind == "file" and result.external
    runtime.context.services["code_agent_jobs"].shutdown()


def test_start_schema_rejects_id_only_or_hidden_authority() -> None:
    with pytest.raises(ValidationError):
        StartArguments.model_validate({"spec_id": str(uuid.uuid4())})
    with pytest.raises(ValidationError):
        StartArguments(project="proj", task="task", authority_boundary="safe")
