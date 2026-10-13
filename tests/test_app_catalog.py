"""Kurulu uygulama erişimi yalnız OS kataloğundan gelir; isim shell olmaz."""

import json
import subprocess
from pathlib import Path

import pytest

from jarvis.channels.runtime import create_runtime
from jarvis.config.settings import Settings
from jarvis.tools.app_catalog import installed_applications
from jarvis.tools.plugins.desktop import application_executable


def test_all_applications_merge_without_overriding_explicit_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "jarvis.tools.app_catalog.installed_applications",
        lambda: {"Notepad++": "startapp:id|notepad++.exe", "brave": "catalog-value"},
    )
    settings = Settings(workspace=tmp_path, allow_all_applications=True,
                        applications={"brave": "brave.exe", "notepad": "notepad.exe"})
    from jarvis.brain.fake import ScriptedProvider

    runtime = create_runtime(tmp_path / "unused.yaml", lambda *_: False, settings=settings,
                             provider=ScriptedProvider([]))
    assert application_executable(runtime.context, "NOTEPAD++") == "startapp:id|notepad++.exe"
    assert application_executable(runtime.context, "Notepad") == "notepad.exe"
    assert application_executable(runtime.context, "Brave") == "brave.exe"
    assert application_executable(runtime.context, "cmd /c arbitrary") is None


def test_catalog_process_receives_no_provider_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    installed_applications.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-secret")
    monkeypatch.setattr("jarvis.tools.app_catalog.platform.system", lambda: "Windows")

    def run(command, **kwargs):
        assert "OPENROUTER_API_KEY" not in kwargs["env"]
        assert "shell" not in kwargs
        return subprocess.CompletedProcess(command, 0, json.dumps([
            {"name": "Editor", "id": "editor.app", "process": "editor.exe"}
        ]).encode(), b"")

    monkeypatch.setattr("jarvis.tools.app_catalog.subprocess.run", run)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    try:
        assert installed_applications() == {"Editor": "startapp:editor.app|editor.exe"}
    finally:
        installed_applications.cache_clear()
