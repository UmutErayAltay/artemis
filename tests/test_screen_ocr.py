"""Ekran OCR çalıştırıcısı: PowerShell ÇALIŞMAZ; sahte runner sözleşmeyi yakalar."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from typing import Any

import pytest

from artemis.perception.screen import (
    SCREEN_SCRIPT,
    SCREEN_TEXT_LIMIT,
    ScreenOutcome,
    read_image,
    read_screen,
)
from artemis.tools.platform import child_environment


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
    assert os.path.isabs(args[0]) and args[0].endswith("powershell.exe")
    assert args[1:5] == [
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
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
    monkeypatch.setenv("ARTEMIS_TELEGRAM_BOT_TOKEN", "123:fake-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("ARTEMIS_UNLISTED_SETTING", "1")
    runner = FakeRunner(stdout=ok_stdout())
    read_screen(scope, runner=runner, platform="win32")  # type: ignore[arg-type]
    env = runner.calls[0][1]["env"]
    assert env["ARTEMIS_SCREEN_SCOPE"] == scope
    for key in ("ARTEMIS_TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY", "ARTEMIS_UNLISTED_SETTING"):
        assert key not in env
    assert env["ARTEMIS_OWN_PID"] == str(os.getpid())
    assert set(env) <= set(child_environment()) | {"ARTEMIS_SCREEN_SCOPE", "ARTEMIS_OWN_PID"}


def test_script_contract_and_nothing_written_to_disk() -> None:
    for needle in (
        "CopyFromScreen",
        "OcrEngine",
        "SetProcessDPIAware",
        "ARTEMIS_SCREEN_SCOPE",
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
    assert "$env:ARTEMIS_SCREEN_SCOPE" in SCREEN_SCRIPT


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


def test_powershell_command_uses_absolute_system_path() -> None:
    screen_runner = FakeRunner(stdout=ok_stdout())
    read_screen("screen", runner=screen_runner, platform="win32")
    screen_cmd = screen_runner.calls[0][0]
    assert os.path.isabs(screen_cmd[0])
    assert screen_cmd[0].endswith("powershell.exe")

    image_runner = FakeRunner(stdout=ok_stdout())
    read_image("fake.png", runner=image_runner, platform="win32")
    image_cmd = image_runner.calls[0][0]
    assert os.path.isabs(image_cmd[0])
    assert image_cmd[0].endswith("powershell.exe")


def test_target_script_skips_cloaked_windows() -> None:
    """Metin düzeyinde denetim: PowerShell çalıştırılmadan Target() DWMWA_CLOAKED kontrolü."""
    assert "DwmGetWindowAttribute(" in SCREEN_SCRIPT
    assert "out int" in SCREEN_SCRIPT
    target_start = SCREEN_SCRIPT.index("public static IntPtr Target(")
    target_end = SCREEN_SCRIPT.index("return IntPtr.Zero;", target_start)
    target_body = SCREEN_SCRIPT[target_start:target_end]
    assert "DwmGetWindowAttribute(" in target_body
    assert "14" in target_body
    assert "out cloaked, 4" in target_body


def test_screen_script_uses_per_monitor_v2_dpi_with_fallback_before_capture() -> None:
    assert "SetProcessDpiAwarenessContext" in SCREEN_SCRIPT
    assert "-4" in SCREEN_SCRIPT
    assert "SetProcessDPIAware" in SCREEN_SCRIPT
    call_idx = SCREEN_SCRIPT.index("SetProcessDpiAwarenessContext([IntPtr](-4))")
    fallback_idx = SCREEN_SCRIPT.index("SetProcessDPIAware()", call_idx)
    assert call_idx < fallback_idx < SCREEN_SCRIPT.index("[ArtemisNative]::Target")
    assert call_idx < SCREEN_SCRIPT.index("[ArtemisNative]::GetWindowRect")
    assert call_idx < SCREEN_SCRIPT.index("OcrEngine")


def test_image_script_downscales_oversized_images_instead_of_exiting_4() -> None:
    from artemis.perception.screen import IMAGE_SCRIPT

    # Büyük görsel WinRT BitmapTransform ile küçültülür; çözme WinRT'de kalır (HEIC vb. GDI+
    # olmadan okunur). Canlı doğrulama 2026-11-18: 14000x3000 PNG okundu.
    assert "MaxImageDimension" in IMAGE_SCRIPT
    assert "BitmapTransform" in IMAGE_SCRIPT and "ScaledWidth" in IMAGE_SCRIPT
    assert "System.Drawing" not in IMAGE_SCRIPT
    assert re.search(r"-gt \$maxDim\)\s*\{\s*exit 4", IMAGE_SCRIPT) is None
    assert "$width -le 0 -or $height -le 0" in IMAGE_SCRIPT
