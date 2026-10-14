"""Ekran OCR çalıştırıcısı: PowerShell ÇALIŞMAZ; sahte runner sözleşmeyi yakalar."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from typing import Any

import pytest

from jarvis.perception.screen import (
    SCREEN_SCRIPT,
    SCREEN_TEXT_LIMIT,
    ScreenOutcome,
    read_screen,
)
from jarvis.tools.platform import child_environment


class FakeRunner:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        error: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), dict(kwargs)))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


def ok_stdout(text: str = "Merhaba dünya", title: str = "Not Defteri") -> str:
    payload = {"text": text, "language": "tr-TR", "title": title}
    return "uyarı satırı\n" + json.dumps(payload, ensure_ascii=False) + "\n\n"


def test_non_windows_is_unavailable_and_runner_not_called() -> None:
    runner = FakeRunner()
    outcome = read_screen("screen", runner=runner, platform="linux")
    assert outcome.status == "unavailable"
    assert runner.calls == []


def test_invalid_scope_raises_value_error() -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        read_screen("desktop", runner=runner, platform="win32")  # type: ignore[arg-type]
    assert runner.calls == []


def test_argv_has_encoded_command_and_no_scope_or_text() -> None:
    runner = FakeRunner(stdout=ok_stdout())
    read_screen("active_window", runner=runner, platform="win32")
    args, kwargs = runner.calls[0]
    assert args[:5] == [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
    ]
    assert args[5] == "-EncodedCommand"
    assert base64.b64decode(args[6]).decode("utf-16-le") == SCREEN_SCRIPT
    assert "active_window" not in " ".join(args[:6])
    assert kwargs["timeout"] == 20.0
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True


@pytest.mark.parametrize("scope", ["active_window", "screen"])
def test_env_carries_scope_and_no_secrets(
    scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_TELEGRAM_BOT_TOKEN", "123:fake-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("JARVIS_UNLISTED_SETTING", "1")
    runner = FakeRunner(stdout=ok_stdout())
    read_screen(scope, runner=runner, platform="win32")  # type: ignore[arg-type]
    env = runner.calls[0][1]["env"]
    assert env["JARVIS_SCREEN_SCOPE"] == scope
    for key in ("JARVIS_TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY", "JARVIS_UNLISTED_SETTING"):
        assert key not in env
    assert set(env) <= set(child_environment()) | {"JARVIS_SCREEN_SCOPE"}


def test_script_contract_and_nothing_written_to_disk() -> None:
    for needle in (
        "CopyFromScreen",
        "OcrEngine",
        "SetProcessDPIAware",
        "JARVIS_SCREEN_SCOPE",
        "MemoryStream",
        "TryCreateFromLanguage",
        "TryCreateFromUserProfileLanguages",
        "MaxImageDimension",
        "ConvertTo-Json -Compress",
        "$ErrorActionPreference = 'Stop'",
    ):
        assert needle in SCREEN_SCRIPT
    # Görüntü yalnız MemoryStream'e kaydedilir; dosya yoluna yazan hiçbir şey olmamalı.
    assert not re.search(r"\.Save\(\s*[\"']", SCREEN_SCRIPT)
    assert ".Save($stream" in SCREEN_SCRIPT
    for forbidden in ("Out-File", "Set-Content", "Add-Content", "WriteAllBytes", "[IO.File]"):
        assert forbidden not in SCREEN_SCRIPT
    assert "System.IO.File" not in SCREEN_SCRIPT
    # Sabit betik: kullanıcı verisi gömülmez, yalnız ortamdan okunur.
    assert "$env:JARVIS_SCREEN_SCOPE" in SCREEN_SCRIPT


def test_ok_path_returns_turkish_text_language_and_title() -> None:
    runner = FakeRunner(stdout=ok_stdout("Şğüİıç öÖ\nİkinci satır", "Belge – Not Defteri"))
    outcome = read_screen("active_window", runner=runner, platform="win32")
    assert outcome.status == "ok"
    assert outcome.text == "Şğüİıç öÖ\nİkinci satır"
    assert outcome.language == "tr-TR"
    assert outcome.window_title == "Belge – Not Defteri"
    assert outcome.truncated is False


@pytest.mark.parametrize("text", ["", "   \n\t ", "\x00\x07"])
def test_blank_text_is_empty_never_ok(text: str) -> None:
    runner = FakeRunner(stdout=ok_stdout(text))
    outcome = read_screen("screen", runner=runner, platform="win32")
    assert outcome.status == "empty"
    assert outcome.text == ""


def test_control_characters_are_removed_but_tabs_and_newlines_kept() -> None:
    runner = FakeRunner(stdout=ok_stdout("  a\x00b\x1b[31m\tc\nd\x7f  ", "ti\ntle\x07"))
    outcome = read_screen("screen", runner=runner, platform="win32")
    assert outcome.text == "ab[31m\tc\nd"
    assert outcome.window_title == "ti tle"


def test_long_text_and_title_are_truncated() -> None:
    runner = FakeRunner(stdout=ok_stdout("x" * (SCREEN_TEXT_LIMIT + 500), "t" * 500))
    outcome = read_screen("screen", runner=runner, platform="win32")
    assert outcome.status == "ok"
    assert len(outcome.text) == SCREEN_TEXT_LIMIT
    assert outcome.truncated is True
    assert len(outcome.window_title) == 200


@pytest.mark.parametrize(
    ("code", "status", "fragment"),
    [
        (2, "error", "kapsam"),
        (3, "unavailable", "dil paketi"),
        (4, "error", "boyutu"),
        (1, "error", "çıkış kodu 1"),
        (77, "error", "çıkış kodu 77"),
    ],
)
def test_exit_codes(code: int, status: str, fragment: str) -> None:
    runner = FakeRunner(returncode=code)
    outcome = read_screen("screen", runner=runner, platform="win32")
    assert outcome.status == status
    assert fragment in outcome.message
    assert outcome.text == ""


@pytest.mark.parametrize(
    "stdout",
    ["", "\n\n", "json değil", "[1, 2]", '{"language": "tr-TR"}', '{"text": 5}'],
)
def test_invalid_output_is_error(stdout: str) -> None:
    runner = FakeRunner(stdout=stdout)
    outcome = read_screen("screen", runner=runner, platform="win32")
    assert outcome.status == "error"


def test_timeout_and_oserror_are_honest_errors() -> None:
    timeout = FakeRunner(error=subprocess.TimeoutExpired(cmd="powershell.exe", timeout=20))
    assert "zaman aşımı" in read_screen("screen", runner=timeout, platform="win32").message
    broken = FakeRunner(error=OSError(r"C:\gizli\yol\powershell.exe bulunamadı"))
    outcome = read_screen("screen", runner=broken, platform="win32")
    assert outcome.status == "error"
    assert "OSError" in outcome.message
    assert "gizli" not in outcome.message


def test_stderr_never_reaches_the_message() -> None:
    secret = "PAROLA-ekran-içeriği-C:\\Users\\umut"
    for code in (0, 1, 2, 3, 4):
        runner = FakeRunner(returncode=code, stdout="", stderr=secret)
        outcome = read_screen("screen", runner=runner, platform="win32")
        assert secret not in outcome.message
        assert secret not in repr(outcome)


def test_outcome_is_frozen() -> None:
    outcome = ScreenOutcome("empty", "x")
    with pytest.raises(AttributeError):
        outcome.status = "ok"  # type: ignore[misc]


@pytest.mark.disruptive
@pytest.mark.skipif(sys.platform != "win32", reason="Gerçek ekran OCR yalnız Windows'ta")
def test_real_windows_screen_read() -> None:
    outcome = read_screen("screen")
    assert outcome.status in {"ok", "empty", "unavailable"}
