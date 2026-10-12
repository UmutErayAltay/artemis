"""Sınır hataları kontrollü Python süreçleriyle denenir; masaüstü çağrısı yapılmaz."""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import pytest

from jarvis.tools.platform import child_environment
from jarvis.tools.processes import run_desktop_action


def install_python_worker(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    async def spawn() -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=child_environment(),
        )

    monkeypatch.setattr("jarvis.tools.processes._create_worker", spawn)


async def test_rejects_malformed_worker_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    install_python_worker(
        monkeypatch, "import sys; sys.stdin.buffer.read(); sys.stdout.write('not-json')"
    )
    result = await run_desktop_action(
        "read_clipboard", {}, cancelled=asyncio.Event(), deadline=None
    )
    assert result.status == "error"
    assert result.message == "Masaüstü işçisi geçersiz yanıt verdi."


async def test_nonzero_worker_exit_is_error_without_echoing_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_python_worker(
        monkeypatch,
        "import sys; sys.stdin.buffer.read(); sys.stderr.write('synthetic-secret'); sys.exit(7)",
    )
    result = await run_desktop_action(
        "read_clipboard", {}, cancelled=asyncio.Event(), deadline=None
    )
    assert result.status == "error"
    assert "synthetic-secret" not in result.message


async def test_cancel_stops_worker_before_late_file_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "late-effect"
    code = (
        "import pathlib,sys,time; sys.stdin.buffer.read(); time.sleep(0.6); "
        f"pathlib.Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )
    install_python_worker(monkeypatch, code)
    cancelled = asyncio.Event()
    task = asyncio.create_task(
        run_desktop_action("read_clipboard", {}, cancelled=cancelled, deadline=None)
    )
    await asyncio.sleep(0.1)
    cancelled.set()
    result = await task
    assert result.status == "error" and "iptal" in result.message
    await asyncio.sleep(0.7)
    assert not marker.exists()


async def test_timeout_terminates_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    install_python_worker(monkeypatch, "import sys,time; sys.stdin.buffer.read(); time.sleep(5)")
    result = await run_desktop_action(
        "read_clipboard",
        {},
        cancelled=asyncio.Event(),
        deadline=None,
        timeout_seconds=0.05,
    )
    assert result.status == "error" and "zaman aşımı" in result.message


async def test_worker_status_sent_is_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    install_python_worker(
        monkeypatch,
        "import json,sys; sys.stdin.buffer.read(); "
        "sys.stdout.write(json.dumps({'status':'sent','message':'sent','data':{}}))",
    )
    result = await run_desktop_action(
        "open_application",
        {"executable": "safe.exe"},
        cancelled=asyncio.Event(),
        deadline=None,
    )
    assert result.status == "sent"


async def test_large_worker_output_is_bounded_and_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    install_python_worker(
        monkeypatch,
        "import sys; sys.stdin.buffer.read(); sys.stdout.write('x' * 2000000); sys.stdout.flush()",
    )
    result = await run_desktop_action(
        "read_clipboard", {}, cancelled=asyncio.Event(), deadline=None
    )
    assert result.status == "error"
    assert result.message == "Masaüstü işçisi çok büyük yanıt verdi."


async def test_fragmented_worker_json_is_read_through_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_python_worker(
        monkeypatch,
        "import sys,time; sys.stdin.buffer.read(); "
        "sys.stdout.write('{\"status\":'); sys.stdout.flush(); time.sleep(0.03); "
        "sys.stdout.write('\"ok\",\"message\":\"done\",\"data\":{}}'); sys.stdout.flush()",
    )
    result = await run_desktop_action(
        "read_clipboard", {}, cancelled=asyncio.Event(), deadline=None
    )
    assert result.status == "ok"
    assert result.message == "done"


def test_worker_rejects_unhashable_action(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.tools import desktop_worker

    class Input:
        buffer = io.BytesIO(b'{"action":[],"arguments":{}}')

    monkeypatch.setattr(desktop_worker.sys, "stdin", Input())
    assert desktop_worker._request() is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:password@example.org/",
        "https://example.org/\x7f",
        "https://example.org/\n",
    ],
)
def test_worker_rejects_unsafe_browser_urls(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    from jarvis.tools import desktop_worker

    class Desktop:
        def open_browser(self, value: str) -> None:
            raise AssertionError("Unsafe URL reached desktop adapter")

    monkeypatch.setattr(desktop_worker, "WindowsDesktop", Desktop)
    result = desktop_worker._dispatch("open_browser", {"url": url})
    assert result["status"] == "error"
