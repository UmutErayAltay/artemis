"""`artemis voice` ve `artemis desktop --ses` masaüstünü ses açık başlatır (ADR 0014)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from artemis.channels import cli  # noqa: E402


@pytest.fixture
def desktop_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    # Gerçek pencere açılmaz; yalnız run_desktop'a giden argümanlar yakalanır.
    app_module = importlib.import_module("artemis.channels.desktop.app")
    calls: list[dict[str, Any]] = []

    def fake_run_desktop(
        config_path: Path, *, demo: bool = False, voice: bool = False, background: bool = False
    ) -> int:
        calls.append(
            {"config_path": config_path, "demo": demo, "voice": voice, "background": background}
        )
        return 0

    monkeypatch.setattr(app_module, "run_desktop", fake_run_desktop)
    return calls


def _run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int | None:
    monkeypatch.setattr(sys, "argv", ["artemis", *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


def test_voice_command_opens_desktop_with_voice(monkeypatch, desktop_calls):
    assert _run_cli(monkeypatch, "voice") == 0
    assert [call["voice"] for call in desktop_calls] == [True]
    assert desktop_calls[0]["demo"] is False


def test_desktop_ses_flag_opens_desktop_with_voice(monkeypatch, desktop_calls):
    assert _run_cli(monkeypatch, "desktop", "--ses") == 0
    assert [call["voice"] for call in desktop_calls] == [True]
    assert desktop_calls[0]["demo"] is False


def test_plain_desktop_keeps_voice_closed(monkeypatch, desktop_calls):
    assert _run_cli(monkeypatch, "desktop") == 0
    assert [call["voice"] for call in desktop_calls] == [False]


def test_demo_and_ses_are_both_passed_and_demo_ownership_stays_in_the_window(
    monkeypatch, desktop_calls
):
    # CLI bayrakları olduğu gibi iletir; demoda sesi kapatan karar pencerededir (GUI testi).
    assert _run_cli(monkeypatch, "desktop", "--demo", "--ses") == 0
    assert desktop_calls == [
        {
            "config_path": Path("config/config.yaml"),
            "demo": True,
            "voice": True,
            "background": False,
        }
    ]


def test_background_flag_starts_desktop_in_tray_without_voice(monkeypatch, desktop_calls):
    assert _run_cli(monkeypatch, "desktop", "--arka-plan") == 0
    assert desktop_calls[0]["background"] is True
    assert desktop_calls[0]["voice"] is False
