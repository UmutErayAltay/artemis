"""Kapanış ve metaveri hataları da başarı veya serbest depo diye sunulmaz."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_code_agent import make_jobs, reply, wait_job
from test_code_agent import repo as _repo_fixture
from test_code_agent_windows import FakeKernel

from jarvis.tools.code_agent_jobs import CodeAgentError
from jarvis.tools.code_agent_windows import WindowsProcessTree

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

    from jarvis.channels.runtime import create_runtime
    from jarvis.config.settings import ModelConfig, Settings
    from jarvis.safety.audit import AuditLog
    from jarvis.tools.code_agent_jobs import CodeAgentJobs

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

    from jarvis.safety.audit import AuditLog
    from jarvis.tools.code_agent_jobs import CodeAgentJobs

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

    from jarvis.safety.audit import AuditLog
    from jarvis.tools.code_agent_jobs import CodeAgentJobs

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
        with pytest.raises(sqlite3.OperationalError, match="simulated initialization"):
            CodeAgentJobs(audit, store_path=store)
        assert opened and all(connection.closed for connection in opened)
    restarted = CodeAgentJobs(audit, store_path=store)
    restarted.shutdown()
    store.unlink()
