"""Runtime sahiplik kilidi, başarısız kurulum ve tekrarlı kapanış."""

from pathlib import Path

import pytest

from artemis.brain.fake import ScriptedProvider
from artemis.channels.runtime import create_runtime
from artemis.config.settings import ModelConfig, Settings


def test_repeated_shutdown_releases_job_ownership(tmp_path: Path):
    settings = Settings(workspace=tmp_path, state_dir=tmp_path / "state")
    runtime = create_runtime(
        tmp_path / "unused", lambda *_: False, settings=settings, provider=ScriptedProvider([])
    )
    runtime.shutdown()
    runtime.shutdown()
    second = create_runtime(
        tmp_path / "unused", lambda *_: False, settings=settings, provider=ScriptedProvider([])
    )
    try:
        assert "code_agent_jobs" in second.context.services
    finally:
        second.shutdown()


def test_provider_setup_failure_releases_ownership(tmp_path: Path):
    settings = Settings(workspace=tmp_path, state_dir=tmp_path / "state", primary_model="m",
                        models={"m": ModelConfig(provider="ollama", model="fake",
                                                input_per_million=0, output_per_million=0)})

    def broken(*_):
        raise RuntimeError("kurulum")

    with pytest.raises(RuntimeError, match="kurulum"):
        create_runtime(
            tmp_path / "unused", lambda *_: False, settings=settings, provider_builder=broken
        )
    runtime = create_runtime(
        tmp_path / "unused", lambda *_: False, settings=settings, provider=ScriptedProvider([])
    )
    try:
        assert "code_agent_jobs" in runtime.context.services
    finally:
        runtime.shutdown()


def test_startup_reconciles_stale_prepared_once_and_tolerates_broken_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from artemis.safety import undo

    calls: list[int] = []
    real = undo.UndoJournal.reconcile_prepared

    def counting(self: undo.UndoJournal, **kwargs: float) -> int:
        calls.append(1)
        return real(self, **kwargs)

    monkeypatch.setattr(undo.UndoJournal, "reconcile_prepared", counting)
    settings = Settings(workspace=tmp_path, state_dir=tmp_path / "state")
    create_runtime(
        tmp_path / "unused", lambda *_: False, settings=settings, provider=ScriptedProvider([])
    ).shutdown()
    assert calls == [1]

    def broken(self: undo.UndoJournal, **kwargs: float) -> int:
        raise undo.UndoJournalError("bozuk")

    monkeypatch.setattr(undo.UndoJournal, "reconcile_prepared", broken)
    create_runtime(
        tmp_path / "unused", lambda *_: False, settings=settings, provider=ScriptedProvider([])
    ).shutdown()
