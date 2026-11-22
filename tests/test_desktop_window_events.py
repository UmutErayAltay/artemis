"""Masaüstü pencere olay ve yeniden başlatma kenar durumları (ağsız, Qt döngüsüz)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from artemis.channels.desktop import window_events, window_restart


def test_notify_job_ignores_runtime_error_when_window_destroyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SyncThread:
        def __init__(self, *, target: Any, **_kwargs: Any) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    class _DeletedSignal:
        def emit(self, _message: str) -> None:
            raise RuntimeError("Internal C++ object already deleted.")

    monkeypatch.setattr(window_events.threading, "Thread", _SyncThread)
    window = SimpleNamespace(
        _notified_jobs=set(),
        _append_chat=lambda *_args: None,
        _toast=lambda *_args: SimpleNamespace(status="failed", message="bildirim kapalı"),
        _toast_failed=_DeletedSignal(),
    )
    fields = {
        "status": "done",
        "job_id": "job-1",
        "project": "artemis",
        "summary": "tamamlandı",
    }

    window_events.notify_job(window, fields)  # type: ignore[arg-type]
    assert ("job-1", "done", "") in window._notified_jobs


def test_apply_settings_handles_missing_hotkey_keys_and_calls_save_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "workspace": "desktop",
            "state_dir": "state",
            "audit_path": "audit.jsonl",
            "primary_model": "first",
            "models": {
                "first": {
                    "provider": "openrouter",
                    "model": "first-model",
                    "input_per_million": "0",
                    "output_per_million": "0",
                }
            },
        }),
        encoding="utf-8",
    )
    saved_calls: list[tuple[Path, Any, dict[Any, Any] | None]] = []

    def fake_save_choices(path: Path, device: Any, **kwargs: Any) -> bool:
        saved_calls.append((path, device, kwargs.get("values")))
        return True

    monkeypatch.setattr(window_restart, "save_choices", fake_save_choices)
    marked: list[bool] = []
    failed: list[str] = []
    panel = SimpleNamespace(
        values=lambda: {},
        applications={},
        code_agent_values=lambda: {},
        stt_model_path=lambda: None,
        mark_saved=lambda written: marked.append(written),
        save_failed=lambda text: failed.append(text),
    )
    reloaded: list[Any] = []
    window = SimpleNamespace(
        settings_panel=panel,
        bridge=SimpleNamespace(reload_dictation_settings=lambda cfg: reloaded.append(cfg) or True),
        _demo=False,
        _exiting=False,
        _config_path=config_path,
    )

    window_restart.apply_settings(window, None)  # type: ignore[arg-type]
    assert failed == []
    assert saved_calls == [(config_path, None, {})]
    assert marked == [True]
    assert len(reloaded) == 1 and reloaded[0].max_seconds == 120.0

    # bridge olmadan sahte/basit pencerede de güvenle tamamlanır.
    bare = SimpleNamespace(
        settings_panel=panel,
        _demo=False,
        _exiting=False,
        _config_path=config_path,
    )
    window_restart.apply_settings(bare, None)  # type: ignore[arg-type]
