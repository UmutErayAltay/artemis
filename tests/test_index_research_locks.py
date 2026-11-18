"""Tur 2 eşzamanlılık düzeltmeleri: FileIndex.clear başlatma kilidi ve ResearchJobs._run kilidi."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from artemis.brain.fake import ScriptedProvider
from artemis.config.settings import FileSearchConfig, ResearchConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.tools.base import ToolContext
from artemis.tools.file_index import FileIndex
from artemis.tools.registry import ToolRegistry
from artemis.tools.research_jobs import ResearchJobs


class _SpyLock:
    """Kilit edinimini ve o anda tutulup tutulmadığını deterministik olarak kaydeder."""

    def __init__(self) -> None:
        self.held = False
        self.enters = 0
        self.exits = 0

    def __enter__(self) -> _SpyLock:
        assert not self.held, "Aynı kilit iki kez alınmaya çalışıldı (deadlock riski)"
        self.held = True
        self.enters += 1
        return self

    def __exit__(self, *_exc: object) -> None:
        self.held = False
        self.exits += 1


class _FakeThread:
    """Gerçek iş parçacığı açmadan join sırasındaki kilit durumunu gözler."""

    def __init__(self, spy: _SpyLock, *, still_alive: bool = False) -> None:
        self._spy = spy
        self._still_alive = still_alive
        self.joined_while_held: list[bool] = []

    def join(self, _timeout: float | None = None) -> None:
        self.joined_while_held.append(self._spy.held)

    def is_alive(self) -> bool:
        return self._still_alive


def _make_index(tmp_path: Path) -> FileIndex:
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        file_search=FileSearchConfig(enabled=True, roots=[str(docs)]),
    )
    index = FileIndex(settings, AuditLog(settings.audit_path))
    with closing(index._connect()):
        pass
    return index


def _make_jobs(tmp_path: Path) -> tuple[ResearchJobs, Settings, AuditLog]:
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        research=ResearchConfig(enabled=True),
    )
    audit = AuditLog(settings.audit_path)
    jobs = ResearchJobs(
        settings,
        audit,
        ScriptedProvider([]),
        ToolRegistry(),
        services={"toast": lambda _t, _b: None},
    )
    return jobs, settings, audit


def test_clear_acquires_start_lock_and_unlinks_while_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = _make_index(tmp_path)
    spy = _SpyLock()
    fake_thread = _FakeThread(spy, still_alive=False)
    index._start_lock = spy  # type: ignore[assignment]
    index._thread = fake_thread  # type: ignore[assignment]

    unlink_held_states: list[bool] = []
    real_unlink = Path.unlink

    def spy_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == index.path:
            unlink_held_states.append(spy.held)
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", spy_unlink)

    message = index.clear()

    assert message == "İndeks silindi."
    assert spy.enters == 1 and spy.exits == 1 and not spy.held
    assert fake_thread.joined_while_held == [True]
    assert unlink_held_states == [True]
    assert index._cancel.is_set()
    assert not index.path.exists()


def test_clear_releases_start_lock_when_thread_still_alive(tmp_path: Path) -> None:
    index = _make_index(tmp_path)
    spy = _SpyLock()
    index._start_lock = spy  # type: ignore[assignment]
    index._thread = _FakeThread(spy, still_alive=True)  # type: ignore[assignment]

    message = index.clear()

    assert "silinmedi" in message
    assert spy.enters == 1 and spy.exits == 1 and not spy.held
    assert index._cancel.is_set()
    assert index.path.exists()


def test_run_error_updates_status_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, settings, audit = _make_jobs(tmp_path)
    job_id = "job123456789"
    with closing(jobs._connect()) as db, db:
        db.execute(
            "INSERT INTO research_jobs (id, topic, status, max_searches, created) "
            "VALUES (?, ?, 'running', 3, 0.0)",
            (job_id, "test konusu"),
        )

    spy = _SpyLock()
    jobs._lock = spy  # type: ignore[assignment]

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("beklenmeyen hata")

    monkeypatch.setattr(jobs, "_work", boom)

    update_calls: list[tuple[bool, str, dict[str, Any]]] = []
    real_update = jobs._update

    def spy_update(target_id: str, **fields: Any) -> None:
        update_calls.append((spy.held, target_id, fields))
        real_update(target_id, **fields)

    monkeypatch.setattr(jobs, "_update", spy_update)

    context = ToolContext(workspace=tmp_path, settings=settings, audit=audit, services={})
    jobs._run(job_id, "test konusu", 3, context)

    assert len(update_calls) == 1
    assert update_calls[0][0] is True
    assert update_calls[0][1] == job_id
    assert spy.enters == 1 and spy.exits == 1 and not spy.held

    job = jobs.get(job_id)
    assert job is not None
    assert job["status"] == "error"
    assert "RuntimeError" in job["summary"]


def test_run_error_suppresses_sqlite_error_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, settings, audit = _make_jobs(tmp_path)
    spy = _SpyLock()
    jobs._lock = spy  # type: ignore[assignment]

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("ilk hata")

    def db_fail(*_args: Any, **_kwargs: Any) -> None:
        assert spy.held
        raise sqlite3.OperationalError("kilitli")

    monkeypatch.setattr(jobs, "_work", boom)
    monkeypatch.setattr(jobs, "_update", db_fail)

    context = ToolContext(workspace=tmp_path, settings=settings, audit=audit, services={})
    jobs._run("missing_job", "konu", 3, context)

    assert spy.enters == 1 and spy.exits == 1 and not spy.held
