"""Sistem kontrolü: PowerShell/shutdown.exe ÇALIŞMAZ; sahte runner sözleşmeyi yakalar."""

from __future__ import annotations

import base64
import json
import subprocess
from typing import Any

import pytest

from jarvis.tools.platform import child_environment
from jarvis.tools.system_control import (
    BRIGHTNESS_SCRIPT,
    LOCK_SCRIPT,
    SLEEP_SCRIPT,
    VOLUME_SCRIPT,
    WINDOW_CONTROL_SCRIPT,
    WINDOW_LIMIT,
    WINDOW_LIST_SCRIPT,
    WINDOW_TITLE_LIMIT,
    SystemControl,
    SystemOutcome,
)


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


def make(runner: FakeRunner, platform: str = "win32") -> SystemControl:
    return SystemControl(runner=runner, platform=platform)


def script_of(args: list[str]) -> str:
    assert args[5] == "-EncodedCommand"
    return base64.b64decode(args[6]).decode("utf-16-le")


def window(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": 1234,
        "title": "Not Defteri",
        "process": "notepad",
        "active": True,
    }
    entry.update(overrides)
    return entry


def windows_stdout(entries: list[Any], truncated: bool = False) -> str:
    payload = {"windows": entries, "truncated": truncated}
    return "uyarı\n" + json.dumps(payload, ensure_ascii=False) + "\n"


# --- Windows dışı -----------------------------------------------------------------------


def test_non_windows_never_calls_runner_for_any_method() -> None:
    runner = FakeRunner()
    control = make(runner, platform="linux")
    outcomes = [
        control.list_windows(),
        control.control_window(5, "focus"),
        control.set_volume(30, None),
        control.set_brightness(50),
        control.power("shutdown"),
        control.power("lock"),
    ]
    for outcome in outcomes:
        assert outcome.status == "unavailable"
        assert "Windows" in outcome.message
    assert runner.calls == []


# --- argv / ortam sözleşmesi ---------------------------------------------------------------


def test_powershell_argv_is_fixed_and_values_only_in_env() -> None:
    runner = FakeRunner(stdout='{"ok":true}')
    make(runner).control_window(987654, "left")
    args, kwargs = runner.calls[0]
    assert args[:6] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
    ]
    assert len(args) == 7
    assert script_of(args) == WINDOW_CONTROL_SCRIPT
    assert "987654" not in " ".join(args)
    assert "left" not in " ".join(args[:6])
    assert kwargs["env"]["JARVIS_WINDOW_ID"] == "987654"
    assert kwargs["env"]["JARVIS_WINDOW_ACTION"] == "left"
    assert set(child_environment()).issubset(kwargs["env"])


def test_subprocess_kwargs_are_honest_defaults() -> None:
    runner = FakeRunner(stdout=windows_stdout([]))
    SystemControl(runner=runner, platform="win32", timeout=7.5).list_windows()
    _, kwargs = runner.calls[0]
    assert kwargs["timeout"] == 7.5
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_each_script_is_sent_encoded() -> None:
    cases = [
        (lambda c: c.list_windows(), WINDOW_LIST_SCRIPT, windows_stdout([])),
        (lambda c: c.set_volume(10, None), VOLUME_SCRIPT, '{"level":10,"muted":false}'),
        (lambda c: c.set_brightness(10), BRIGHTNESS_SCRIPT, '{"level":10}'),
        (lambda c: c.power("lock"), LOCK_SCRIPT, ""),
        (lambda c: c.power("sleep"), SLEEP_SCRIPT, ""),
    ]
    for call, script, stdout in cases:
        runner = FakeRunner(stdout=stdout)
        call(make(runner))
        assert script_of(runner.calls[0][0]) == script


def test_volume_values_travel_in_env_including_empty() -> None:
    runner = FakeRunner(stdout='{"level":40,"muted":true}')
    make(runner).set_volume(40, True)
    env = runner.calls[0][1]["env"]
    assert env["JARVIS_VOLUME_LEVEL"] == "40"
    assert env["JARVIS_VOLUME_MUTE"] == "1"
    runner = FakeRunner(stdout='{"level":40,"muted":false}')
    make(runner).set_volume(None, False)
    env = runner.calls[0][1]["env"]
    assert env["JARVIS_VOLUME_LEVEL"] == ""
    assert env["JARVIS_VOLUME_MUTE"] == "0"


def test_scripts_use_stop_error_preference_and_catch() -> None:
    for script in (
        WINDOW_LIST_SCRIPT,
        WINDOW_CONTROL_SCRIPT,
        VOLUME_SCRIPT,
        BRIGHTNESS_SCRIPT,
        LOCK_SCRIPT,
        SLEEP_SCRIPT,
    ):
        assert script.startswith("$ErrorActionPreference = 'Stop'")
        assert "catch { exit 4 }" in script


# --- kapatma / yeniden başlatma ------------------------------------------------------------


def test_shutdown_argv_is_exact_and_bypasses_powershell() -> None:
    runner = FakeRunner()
    outcome = make(runner).power("shutdown", delay_seconds=90)
    args, _ = runner.calls[0]
    assert args[:5] == ["shutdown.exe", "/s", "/t", "90", "/c"]
    assert len(args) == 6
    assert args[5].startswith("Jarvis: ")
    assert "90" in args[5]
    assert "shutdown /a" in args[5]
    assert outcome.status == "sent"
    assert "90" in outcome.message
    assert "iptal" in outcome.message.lower()


def test_restart_argv_uses_r_flag_and_default_delay() -> None:
    runner = FakeRunner()
    outcome = make(runner).power("restart")
    args, _ = runner.calls[0]
    assert args[:5] == ["shutdown.exe", "/r", "/t", "60", "/c"]
    assert outcome.status == "sent"


@pytest.mark.parametrize("delay", [29, 601, 0, -5])
@pytest.mark.parametrize("action", ["shutdown", "restart"])
def test_bad_shutdown_delay_raises_before_runner(action: str, delay: int) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        make(runner).power(action, delay_seconds=delay)  # type: ignore[arg-type]
    assert runner.calls == []


@pytest.mark.parametrize("delay", [30, 600])
def test_delay_bounds_are_inclusive(delay: int) -> None:
    runner = FakeRunner()
    assert make(runner).power("shutdown", delay_seconds=delay).status == "sent"
    assert runner.calls[0][0][3] == str(delay)


def test_shutdown_exit_codes() -> None:
    assert make(FakeRunner(returncode=1190)).power("shutdown").message == (
        "Zaten planlanmış bir kapatma var."
    )
    outcome = make(FakeRunner(returncode=5)).power("restart")
    assert outcome.status == "error"
    assert "5" in outcome.message


def test_cancel_shutdown_argv_and_exit_codes() -> None:
    runner = FakeRunner()
    outcome = make(runner).power("cancel_shutdown")
    assert runner.calls[0][0] == ["shutdown.exe", "/a"]
    assert outcome == SystemOutcome("ok", "Planlanan kapatma iptal edildi.")
    outcome = make(FakeRunner(returncode=1116)).power("cancel_shutdown")
    assert outcome.status == "error"
    assert outcome.message == "İptal edilecek planlanmış kapatma yok."
    assert make(FakeRunner(returncode=2)).power("cancel_shutdown").status == "error"


def test_invalid_power_action_raises() -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        make(runner).power("hibernate")  # type: ignore[arg-type]
    assert runner.calls == []


# --- kilit / uyku -------------------------------------------------------------------------


def test_lock_success_is_sent_not_ok() -> None:
    outcome = make(FakeRunner()).power("lock")
    assert outcome.status == "sent"
    assert outcome.message == "Kilitleme komutu gönderildi."


def test_lock_failure_is_error() -> None:
    assert make(FakeRunner(returncode=4)).power("lock").status == "error"


def test_sleep_is_sent_and_failure_is_error() -> None:
    assert make(FakeRunner()).power("sleep").status == "sent"
    assert make(FakeRunner(returncode=4)).power("sleep").status == "error"


# --- pencere listesi -----------------------------------------------------------------------


def test_list_windows_parses_and_reports_count() -> None:
    entries = [window(), window(id=7, title="Chrome", process="chrome", active=False)]
    outcome = make(FakeRunner(stdout=windows_stdout(entries))).list_windows()
    assert outcome.status == "ok"
    assert [w.id for w in outcome.windows] == [1234, 7]
    assert outcome.windows[0].active is True
    assert outcome.windows[1].process == "chrome"
    assert outcome.truncated is False


def test_list_windows_empty_is_ok_with_message() -> None:
    outcome = make(FakeRunner(stdout=windows_stdout([]))).list_windows()
    assert outcome.status == "ok"
    assert outcome.windows == ()
    assert "bulunamadı" in outcome.message


def test_list_windows_cleans_control_chars_and_newlines() -> None:
    entry = window(title="Konu\x00:\x1b[31m gizli\nsatır\ttab\x7f", process="chr\x00ome")
    outcome = make(FakeRunner(stdout=windows_stdout([entry]))).list_windows()
    title = outcome.windows[0].title
    assert all(ch.isprintable() for ch in title)
    assert "\n" not in title
    assert title == "Konu:[31m gizli satır tab"
    assert outcome.windows[0].process == "chrome"


def test_list_windows_truncates_long_title_and_process() -> None:
    entry = window(title="x" * 500, process="p" * 500)
    outcome = make(FakeRunner(stdout=windows_stdout([entry]))).list_windows()
    assert len(outcome.windows[0].title) == WINDOW_TITLE_LIMIT
    assert len(outcome.windows[0].process) == 64


def test_list_windows_caps_at_limit_and_marks_truncated() -> None:
    entries = [window(id=i + 1, title=f"Pencere {i}", active=False) for i in range(80)]
    outcome = make(FakeRunner(stdout=windows_stdout(entries))).list_windows()
    assert len(outcome.windows) == WINDOW_LIMIT
    assert outcome.truncated is True


def test_list_windows_respects_script_truncated_flag() -> None:
    outcome = make(FakeRunner(stdout=windows_stdout([window()], truncated=True))).list_windows()
    assert outcome.truncated is True


def test_list_windows_drops_malformed_entries() -> None:
    entries: list[Any] = [
        "metin",
        window(id=0),
        window(id=-3),
        window(id="12"),
        window(id=True),
        window(title=5),
        window(title="\x00\x01"),
        window(process=None),
        window(active="evet"),
        {"id": 9},
        window(id=42, title="Geçerli"),
    ]
    outcome = make(FakeRunner(stdout=windows_stdout(entries))).list_windows()
    assert [w.id for w in outcome.windows] == [42]


def test_list_windows_errors() -> None:
    assert make(FakeRunner(returncode=4)).list_windows().status == "error"
    assert make(FakeRunner(stdout="json değil")).list_windows().status == "error"
    assert make(FakeRunner(stdout='{"windows": 5}')).list_windows().status == "error"
    assert make(FakeRunner(stdout="[1, 2]")).list_windows().status == "error"


# --- pencere kontrolü ----------------------------------------------------------------------


@pytest.mark.parametrize("action", ["focus", "maximize", "minimize", "restore", "left", "right"])
def test_control_window_success_for_every_action(action: str) -> None:
    runner = FakeRunner(stdout='{"ok":true}')
    outcome = make(runner).control_window(10, action)  # type: ignore[arg-type]
    assert outcome.status == "ok"
    assert runner.calls[0][1]["env"]["JARVIS_WINDOW_ACTION"] == action


@pytest.mark.parametrize(
    ("code", "fragment"),
    [(5, "bulunamadı"), (6, "odağı engelledi"), (7, "doğrulanamadı"), (2, "çıkış kodu 2")],
)
def test_control_window_exit_codes(code: int, fragment: str) -> None:
    outcome = make(FakeRunner(returncode=code)).control_window(10, "focus")
    assert outcome.status == "error"
    assert fragment in outcome.message


def test_control_window_exit_zero_without_ok_json_is_error() -> None:
    assert make(FakeRunner(stdout="")).control_window(10, "focus").status == "error"
    assert make(FakeRunner(stdout='{"ok":false}')).control_window(10, "focus").status == "error"


@pytest.mark.parametrize("window_id", [0, -1])
def test_control_window_invalid_id_raises(window_id: int) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        make(runner).control_window(window_id, "focus")
    assert runner.calls == []


def test_control_window_invalid_action_raises() -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        make(runner).control_window(5, "close")  # type: ignore[arg-type]
    assert runner.calls == []


# --- ses -----------------------------------------------------------------------------------


def test_volume_match_within_one_is_ok_with_readback() -> None:
    outcome = make(FakeRunner(stdout='{"level":31,"muted":false}')).set_volume(30, None)
    assert outcome.status == "ok"
    assert outcome.level == 31
    assert outcome.muted is False


def test_volume_level_mismatch_is_error_with_readback() -> None:
    outcome = make(FakeRunner(stdout='{"level":50,"muted":false}')).set_volume(30, None)
    assert outcome.status == "error"
    assert outcome.level == 50


def test_volume_mute_mismatch_is_error() -> None:
    outcome = make(FakeRunner(stdout='{"level":30,"muted":false}')).set_volume(None, True)
    assert outcome.status == "error"
    assert outcome.muted is False


def test_volume_mute_only_ignores_level_readback() -> None:
    outcome = make(FakeRunner(stdout='{"level":77,"muted":true}')).set_volume(None, True)
    assert outcome.status == "ok"
    assert outcome.muted is True


def test_volume_no_device_is_unavailable() -> None:
    outcome = make(FakeRunner(returncode=3)).set_volume(10, None)
    assert outcome.status == "unavailable"
    assert outcome.message == "Ses aygıtı bulunamadı."


@pytest.mark.parametrize("stdout", ["", "bozuk", '{"level":"10","muted":false}', '{"level":10}'])
def test_volume_malformed_output_is_error(stdout: str) -> None:
    assert make(FakeRunner(stdout=stdout)).set_volume(10, None).status == "error"


@pytest.mark.parametrize(("level", "muted"), [(None, None), (-1, None), (101, None), (True, None)])
def test_volume_invalid_input_raises_before_runner(level: Any, muted: bool | None) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        make(runner).set_volume(level, muted)
    assert runner.calls == []


# --- parlaklık -----------------------------------------------------------------------------


def test_brightness_match_is_ok_with_level() -> None:
    outcome = make(FakeRunner(stdout='{"level":69}')).set_brightness(70)
    assert outcome.status == "ok"
    assert outcome.level == 69
    assert make(FakeRunner(stdout='{"level":70}')).set_brightness(70).level == 70


def test_brightness_mismatch_is_error() -> None:
    outcome = make(FakeRunner(stdout='{"level":40}')).set_brightness(70)
    assert outcome.status == "error"
    assert outcome.level == 40


def test_brightness_unsupported_display_is_unavailable() -> None:
    outcome = make(FakeRunner(returncode=3)).set_brightness(70)
    assert outcome.status == "unavailable"
    assert outcome.message == "Bu ekran parlaklık ayarını desteklemiyor."


def test_brightness_other_failures_are_errors() -> None:
    assert make(FakeRunner(returncode=4)).set_brightness(70).status == "error"
    assert make(FakeRunner(stdout="yok")).set_brightness(70).status == "error"


@pytest.mark.parametrize("level", [-1, 101])
def test_brightness_invalid_level_raises(level: int) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        make(runner).set_brightness(level)
    assert runner.calls == []


# --- ortak hata yolları --------------------------------------------------------------------


def test_timeout_is_error_for_every_runner_backed_method() -> None:
    error = subprocess.TimeoutExpired(cmd="x", timeout=1)
    for call in (
        lambda c: c.list_windows(),
        lambda c: c.control_window(3, "focus"),
        lambda c: c.set_volume(10, None),
        lambda c: c.set_brightness(10),
        lambda c: c.power("lock"),
        lambda c: c.power("shutdown"),
        lambda c: c.power("cancel_shutdown"),
    ):
        outcome = call(make(FakeRunner(error=error)))
        assert outcome.status == "error"
        assert "zaman aşımı" in outcome.message


def test_os_error_is_error_and_leaks_only_type_name() -> None:
    error = OSError("C:\\gizli\\yol bulunamadı")
    outcome = make(FakeRunner(error=error)).power("shutdown")
    assert outcome.status == "error"
    assert "OSError" in outcome.message
    assert "gizli" not in outcome.message


def test_stderr_never_reaches_message() -> None:
    runner = FakeRunner(returncode=9, stderr="SIR-GIZLI-METIN")
    outcome = make(runner).control_window(3, "focus")
    assert "SIR-GIZLI-METIN" not in outcome.message


def test_payload_survives_line_separators_and_bom() -> None:
    from jarvis.tools.system_control import _parse_payload

    title = "Sekme başlığı\u0085son"
    stdout = "﻿" + json.dumps({"title": title}, ensure_ascii=False) + "\n"
    assert _parse_payload(stdout) == {"title": title}


def test_window_titles_drop_bidi_and_separator_characters() -> None:
    from jarvis.tools.system_control import _clean_line

    assert _clean_line("a\u202eb\u2028c\u200bd", 50) == "ab cd"


def test_window_control_uses_the_same_filter_as_the_list() -> None:
    from jarvis.tools import system_control

    assert "[JarvisWin]::Listed($h)" in system_control.WINDOW_CONTROL_SCRIPT
