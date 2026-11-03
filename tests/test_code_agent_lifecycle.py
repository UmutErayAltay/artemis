"""Kapanış ve metaveri hataları da başarı veya serbest depo diye sunulmaz."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from test_code_agent import make_jobs, reply, wait_job
from test_code_agent import repo as _repo_fixture
from test_code_agent_windows import FakeKernel

from artemis.tools.base import Risk
from artemis.tools.code_agent_jobs import CodeAgentError
from artemis.tools.code_agent_windows import WindowsProcessTree

repo = _repo_fixture


def test_cancelled_windows_spawn_never_resumes_user_code() -> None:
    kernel = FakeKernel()
    tree = WindowsProcessTree(
        SimpleNamespace(pid=7, _handle=123), kernel=kernel, resume_allowed=lambda: False
    )
    assert "assign" in kernel.events and "resume" not in kernel.events
    assert tree.kill()


def test_store_write_failure_before_spawn_retains_uncertain_lock(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, cli = make_jobs(tmp_path, reply(), persistent=True)
    original = jobs._save

    def failed(key: str, kind: str, record: dict[str, Any]) -> None:
        if kind == "job" and record["status"] != "queued":
            raise CodeAgentError("simulated store failure")
        original(key, kind, record)

    monkeypatch.setattr(jobs, "_save", failed)
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    job = wait_job(jobs, job_id)
    assert job["status"] == "cleanup_pending" and job["cleanup_pending"]
    assert cli.calls == []
    with pytest.raises(CodeAgentError, match="belirsiz"):
        jobs.start("other", repo, "görev", command=sys.executable)
    jobs.shutdown()


def test_shutdown_bounds_all_workers_and_releases_store_owner(tmp_path: Path, repo: Path) -> None:
    jobs, cli = make_jobs(
        tmp_path, "import sys,time\nsys.stdin.read()\ntime.sleep(60)", persistent=True
    )
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    wait_job(jobs, job_id, {"running"})
    before = time.monotonic()
    jobs.shutdown()
    assert time.monotonic() - before < 3
    assert all(not thread.is_alive() for thread in jobs._all_threads)
    assert all(proc.poll() is not None for proc in cli.procs)
    restarted, _ = make_jobs(tmp_path, reply(), persistent=True)
    assert restarted.status()[0]["status"] == "cancelled"
    restarted.shutdown()


def test_incomplete_runtime_construction_releases_job_store(tmp_path: Path) -> None:
    from decimal import Decimal

    from artemis.channels.runtime import create_runtime
    from artemis.config.settings import ModelConfig, Settings
    from artemis.safety.audit import AuditLog
    from artemis.tools.code_agent_jobs import CodeAgentJobs

    settings = Settings(
        workspace=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        primary_model="fake",
        models={
            "fake": ModelConfig(
                provider="ollama",
                model="fake",
                input_per_million=Decimal(0),
                output_per_million=Decimal(0),
            )
        },
    )

    def failed_builder(settings: Any, audit: Any) -> Any:
        raise ValueError("simulated provider construction failure")

    with pytest.raises(ValueError, match="simulated provider"):
        create_runtime(
            tmp_path / "unused",
            lambda name, args: False,
            settings=settings,
            provider_builder=failed_builder,
        )
    jobs = CodeAgentJobs(
        AuditLog(settings.audit_path), store_path=settings.state_dir / "code_agent.sqlite3"
    )
    jobs.shutdown()


def test_job_store_connections_close_after_every_transaction_and_keep_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from artemis.safety.audit import AuditLog
    from artemis.tools.code_agent_jobs import CodeAgentJobs

    real_connect = sqlite3.connect
    opened: list[Any] = []
    exits: list[bool] = []
    failure = {"insert": False}

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def __exit__(self, error_type: Any, error: Any, traceback: Any) -> bool:
            exits.append(error_type is None)
            return super().__exit__(error_type, error, traceback)

        def execute(self, sql: str, *arguments: Any, **keywords: Any) -> Any:
            cursor = super().execute(sql, *arguments, **keywords)
            if failure["insert"] and sql.startswith("INSERT"):
                raise sqlite3.OperationalError("simulated failure after insert")
            return cursor

        def close(self) -> None:
            super().close()
            self.closed = True

    def tracked_connect(*arguments: Any, **keywords: Any) -> TrackedConnection:
        connection = real_connect(*arguments, factory=TrackedConnection, **keywords)
        opened.append(connection)  # Canlı referanslar GC'nin hatayı gizlemesini önler.
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    store = tmp_path / "state" / "jobs.sqlite3"
    audit = AuditLog(tmp_path / "audit.jsonl")
    jobs = CodeAgentJobs(audit, store_path=store)
    assert opened and all(connection.closed for connection in opened)
    spec = jobs.prepare("proj", "görev", ("kapsam",), ("ölçüt",))
    assert all(connection.closed for connection in opened)
    failure["insert"] = True
    with pytest.raises(CodeAgentError, match="metaverisi yazılamadı"):
        jobs._save(spec.spec_id, "draft", {"task": "must roll back"})
    assert all(connection.closed for connection in opened) and exits[-1] is False
    verification = real_connect(store)
    try:
        assert (
            '"task": "görev"'
            in verification.execute(
                "SELECT value FROM records WHERE key=?", (spec.spec_id,)
            ).fetchone()[0]
        )
    finally:
        verification.close()
    with pytest.raises(CodeAgentError, match="başka bir runtime"):
        CodeAgentJobs(audit, store_path=store)  # DB kapandı diye servis kilidi salınmaz.
    failure["insert"] = False
    jobs.shutdown()
    restarted = CodeAgentJobs(audit, store_path=store)
    assert restarted.drafts()[0]["spec_id"] == spec.spec_id
    assert all(connection.closed for connection in opened)
    restarted.shutdown()
    store.unlink()  # Windows'ta açık bağlantı varsa bu işlem PermissionError üretir.


@pytest.mark.parametrize("statement", ["CREATE TABLE", "SELECT"])
def test_job_store_initialization_failure_closes_connection_and_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    import sqlite3

    from artemis.safety.audit import AuditLog
    from artemis.tools.code_agent_jobs import CodeAgentJobs

    real_connect = sqlite3.connect
    opened: list[Any] = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def execute(self, sql: str, *arguments: Any, **keywords: Any) -> Any:
            if sql.startswith(statement):
                raise sqlite3.OperationalError("simulated initialization failure")
            return super().execute(sql, *arguments, **keywords)

        def close(self) -> None:
            super().close()
            self.closed = True

    def tracked_connect(*arguments: Any, **keywords: Any) -> TrackedConnection:
        connection = real_connect(*arguments, factory=TrackedConnection, **keywords)
        opened.append(connection)
        return connection

    store = tmp_path / "jobs.sqlite3"
    audit = AuditLog(tmp_path / "audit.jsonl")
    with monkeypatch.context() as tracking:
        tracking.setattr(sqlite3, "connect", tracked_connect)
        with pytest.raises(CodeAgentError, match="iş deposu okunamadı") as raised:
            CodeAgentJobs(audit, store_path=store)
        assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
        assert opened and all(connection.closed for connection in opened)
    restarted = CodeAgentJobs(audit, store_path=store)
    restarted.shutdown()
    store.unlink()


def test_corrupt_job_record_raises_code_agent_error_and_keeps_file(tmp_path: Path) -> None:
    import sqlite3

    from artemis.safety.audit import AuditLog
    from artemis.tools.code_agent_jobs import CodeAgentJobs

    store = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(store) as connection:
        connection.execute(
            "CREATE TABLE records (key TEXT PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO records VALUES ('x', 'job', '{bozuk json')")
    connection.close()
    before = store.read_bytes()
    with pytest.raises(CodeAgentError, match="JSONDecodeError"):
        CodeAgentJobs(AuditLog(tmp_path / "audit.jsonl"), store_path=store)
    assert store.read_bytes() == before  # dosya asla silinmez/değiştirilmez
    # Sahiplik kilidi salınmış olmalı: ikinci deneme "başka runtime" değil, yine okunamadı der.
    with pytest.raises(CodeAgentError, match="iş deposu okunamadı"):
        CodeAgentJobs(AuditLog(tmp_path / "audit.jsonl"), store_path=store)


def test_create_runtime_survives_corrupt_job_store(tmp_path: Path) -> None:
    import sqlite3

    from artemis.brain.fake import ScriptedProvider
    from artemis.channels.runtime import create_runtime
    from artemis.config.settings import Settings

    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "code_agent.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE records (key TEXT PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO records VALUES ('x', 'job', 'JSON-değil')")
    connection.close()
    settings = Settings(workspace=tmp_path, state_dir=state)
    runtime = create_runtime(
        tmp_path / "unused", lambda *_: False, settings=settings, provider=ScriptedProvider([])
    )
    try:
        assert "code_agent_jobs" not in runtime.context.services
        assert "okunamadı" in str(runtime.context.services["code_agent_unavailable"])
    finally:
        runtime.shutdown()


def _persist_status(tmp_path: Path, repo: Path, status: str) -> str:
    """Bitmiş bir işi kapatır ve kayıtlı durumunu değiştirir (çökmüş süreç taklidi)."""
    import json
    import sqlite3

    jobs, _ = make_jobs(tmp_path, reply(), persistent=True)
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    wait_job(jobs, job_id)
    jobs.shutdown()
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        record = json.loads(
            connection.execute("SELECT value FROM records WHERE key=?", (job_id,)).fetchone()[0]
        )
        record["status"] = status
        connection.execute(
            "UPDATE records SET value=? WHERE key=?", (json.dumps(record), job_id)
        )
    connection.close()
    return job_id


def test_restart_with_waiting_job_does_not_lock_repo(tmp_path: Path, repo: Path) -> None:
    job_id = _persist_status(tmp_path, repo, "waiting")
    restarted, _ = make_jobs(tmp_path, reply(), persistent=True)
    job = next(j for j in restarted.status() if j["job_id"] == job_id)
    assert job["status"] == "interrupted" and not job["cleanup_pending"]
    assert "kilidi" in job["summary"]
    second = restarted.start("proj", repo, "görev 2", command=sys.executable)
    assert wait_job(restarted, second)["status"] == "done"
    restarted.shutdown()


def test_release_unlocks_repo_after_crash_of_running_job(tmp_path: Path, repo: Path) -> None:
    job_id = _persist_status(tmp_path, repo, "running")
    restarted, _ = make_jobs(tmp_path, reply(), persistent=True)
    with pytest.raises(CodeAgentError, match="belirsiz"):
        restarted.start("proj", repo, "görev 2", command=sys.executable)
    released = restarted.release(job_id)
    assert released is not None and released["cleanup_pending"] is False
    assert released["leftover_changes"] is False
    assert restarted.release(job_id) is None  # artık bekleyen kilit yok
    second = restarted.start("proj", repo, "görev 2", command=sys.executable)
    assert wait_job(restarted, second)["status"] == "done"
    restarted.shutdown()
    again, _ = make_jobs(tmp_path, reply(), persistent=True)  # kalıcı: yeniden açılışta da serbest
    assert not any(j["cleanup_pending"] for j in again.status() if j["job_id"] == job_id)
    again.shutdown()


def test_release_refuses_unknown_active_and_non_pending_jobs(tmp_path: Path, repo: Path) -> None:
    jobs, _ = make_jobs(tmp_path, "import sys,time\nsys.stdin.read()\ntime.sleep(60)")
    assert jobs.release("yok") is None
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)
    wait_job(jobs, job_id, {"running"})
    assert jobs.release(job_id) is None  # aktif iş
    jobs.cancel(job_id)
    assert wait_job(jobs, job_id)["status"] == "cancelled"
    assert jobs.release(job_id) is None  # cleanup_pending değil
    jobs.shutdown()


def test_release_tool_is_confirm_required_and_reports_result(tmp_path: Path, repo: Path) -> None:
    import asyncio

    from test_code_agent import context

    from artemis.tools.plugins.code_agent import CodeAgentRelease, ReleaseArguments
    from artemis.tools.registry import discover_tools

    tool = discover_tools().get("code_agent_release")
    assert tool is not None and tool.risk is Risk.CONFIRM_REQUIRED
    assert "ajan penceresi" in CodeAgentRelease.description
    with pytest.raises(ValidationError):
        ReleaseArguments.model_validate({"job_id": "j", "x": 1})
    job_id = _persist_status(tmp_path, repo, "running")
    jobs, _ = make_jobs(tmp_path, reply(), persistent=True)
    ctx = context(tmp_path, repo, jobs)
    ok = asyncio.run(CodeAgentRelease().execute(ReleaseArguments(job_id=job_id), ctx))
    assert ok.success and ok.status == "ok" and "yok" in ok.message
    denied = asyncio.run(CodeAgentRelease().execute(ReleaseArguments(job_id=job_id), ctx))
    assert not denied.success
    jobs.shutdown()


def test_audit_failure_at_start_marks_job_error_and_frees_repo(tmp_path: Path, repo: Path) -> None:
    jobs, cli = make_jobs(tmp_path, reply())
    real_emit = jobs._audit.emit

    def failing(event: str, **fields: Any) -> Any:
        if event == "code_agent_started":
            raise OSError("disk dolu")
        return real_emit(event, **fields)

    jobs._audit.emit = failing  # type: ignore[method-assign]
    with pytest.raises(CodeAgentError, match="Denetim kaydı yazılamadı") as raised:
        jobs.start("proj", repo, "görev", command=sys.executable)
    assert isinstance(raised.value.__cause__, OSError)
    assert cli.calls == []
    assert [j["status"] for j in jobs.status()] == ["error"]
    jobs._audit.emit = real_emit  # type: ignore[method-assign]
    job_id = jobs.start("proj", repo, "görev", command=sys.executable)  # depo meşgul değil
    assert wait_job(jobs, job_id)["status"] == "done"
    jobs.shutdown()
