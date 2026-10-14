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
