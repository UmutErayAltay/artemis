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
