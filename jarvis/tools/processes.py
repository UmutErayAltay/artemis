"""Sabit masaüstü işçisinin sınırlı süreç ve iletişim sınırı."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from jarvis.tools.platform import child_environment

MAX_REQUEST_BYTES = 1_100_000
MAX_RESPONSE_BYTES = 1_100_000
DEFAULT_TIMEOUT_SECONDS = 15.0
TERMINATE_GRACE_SECONDS = 0.25
KILL_WAIT_SECONDS = 2.0


@dataclass(frozen=True)
class DesktopProcessResult:
    """İşçinin sınırlı ve ham hata içermeyen cevabı."""

    status: Literal["ok", "sent", "error"]
    message: str
    data: dict[str, Any] = field(default_factory=dict, repr=False)


def _worker_cwd() -> str:
    """Kaynak ve kurulu dağıtımda sabit modülü bulacak kökü seçer."""
    return str(Path(__file__).resolve().parents[2])


async def _stop_worker(process: asyncio.subprocess.Process) -> bool:
    """Yalnız işçiyi durdurur ve sınırlı sürede çıkışını doğrular."""
    if process.returncode is not None:
        _close_worker_pipes(process)
        return True
    with suppress(ProcessLookupError):
        process.terminate()
    if await _wait_bounded(process.wait(), TERMINATE_GRACE_SECONDS):
        _close_worker_pipes(process)
        return True
    with suppress(ProcessLookupError):
        process.kill()
    stopped = await _wait_bounded(process.wait(), KILL_WAIT_SECONDS)
    _close_worker_pipes(process)
    return stopped


def _close_worker_pipes(process: asyncio.subprocess.Process) -> None:
    """Sınır aşıldığında okunmayacak işçi pipe taşımasını kapatır."""
    # asyncio.Process stdout okuyucusuna kapatma API'si sunmaz; sınırlı okumanın
    # ardından boru taşımasını bırakmamak için CPython'ın taşıma kancasını kullanırız.
    transport = getattr(process, "_transport", None)
    if transport is None:
        return
    for descriptor in (0, 1):
        pipe_transport = transport.get_pipe_transport(descriptor)
        if pipe_transport is not None:
            pipe_transport.close()


async def _wait_bounded(awaitable: Any, seconds: float) -> bool:
    try:
        await asyncio.wait_for(awaitable, timeout=seconds)
        return True
    except TimeoutError:
        return False


async def _create_worker() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "jarvis.tools.desktop_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=_worker_cwd(),
        env=child_environment(),
    )


async def _start_owned_worker() -> asyncio.subprocess.Process:
    """Başlatma sırasında çağıran iptal edilse bile oluşan işçiyi toplar."""
    creation = asyncio.create_task(_create_worker())
    try:
        return await asyncio.shield(creation)
    except asyncio.CancelledError:
        try:
            process = await asyncio.shield(creation)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            raise
        await _stop_worker(process)
        raise


async def _write_request(
    process: asyncio.subprocess.Process, request: bytes
) -> None:
    if process.stdin is None:
        raise RuntimeError("İşçi girdi kanalı oluşturulamadı.")
    process.stdin.write(request)
    await process.stdin.drain()
    process.stdin.close()


async def _read_response(process: asyncio.subprocess.Process) -> bytes:
    if process.stdout is None:
        raise RuntimeError("İşçi çıktı kanalı oluşturulamadı.")
    response = bytearray()
    while len(response) <= MAX_RESPONSE_BYTES:
        chunk = await process.stdout.read(min(65_536, MAX_RESPONSE_BYTES + 1 - len(response)))
        if not chunk:
            return bytes(response)
        response.extend(chunk)
    return bytes(response)


async def run_desktop_action(
    action: str,
    arguments: dict[str, Any],
    *,
    cancelled: asyncio.Event,
    deadline: float | None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> DesktopProcessResult:
    """Sabit eylemi JSON stdin ile çalıştırıp sınırlandırılmış yanıtı okur."""
    if cancelled.is_set():
        return DesktopProcessResult("error", "Masaüstü işlemi iptal edildi.")
    try:
        request = json.dumps(
            {"action": action, "arguments": arguments}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError):
        return DesktopProcessResult("error", "Masaüstü isteği geçersiz.")
    if len(request) > MAX_REQUEST_BYTES:
        return DesktopProcessResult("error", "Masaüstü isteği izin verilen sınırı aşıyor.")

    loop = asyncio.get_running_loop()
    timeout = timeout_seconds
    if deadline is not None:
        timeout = min(timeout, deadline - loop.time())
    if timeout <= 0:
        return DesktopProcessResult("error", "Tur süresi doldu; masaüstü işlemi başlatılmadı.")
    timeout_at = loop.time() + timeout

    process: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Task[Any]] = []
    try:
        process = await _start_owned_worker()
        writer = asyncio.create_task(_write_request(process, request))
        reader = asyncio.create_task(_read_response(process))
        cancellation = asyncio.create_task(cancelled.wait())
        tasks.extend((writer, reader, cancellation))

        done, _ = await asyncio.wait(
            {writer, reader, cancellation},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation in done and cancelled.is_set():
            cleaned = await _stop_worker(process)
            await _settle_tasks(tasks)
            return _failure("Masaüstü işlemi iptal edildi.", cleaned)
        if writer in done:
            try:
                writer.result()
            except (BrokenPipeError, ConnectionResetError):
                # İşçi girdi tamamlanmadan kapandı; çıkış kodu aşağıda okunur.
                pass
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                cleaned = await _stop_worker(process)
                await _settle_tasks(tasks)
                return _failure("Masaüstü işçisine istek gönderilemedi.", cleaned)

        if reader not in done:
            done, _ = await asyncio.wait(
                {reader, cancellation},
                timeout=max(0.0, timeout_at - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
        if cancellation in done and cancelled.is_set():
            cleaned = await _stop_worker(process)
            await _settle_tasks(tasks)
            return _failure("Masaüstü işlemi iptal edildi.", cleaned)
        if reader not in done:
            cleaned = await _stop_worker(process)
            await _settle_tasks(tasks)
            return _failure("Masaüstü işçisi zaman aşımına uğradı.", cleaned)

        stdout = reader.result()
        if len(stdout) > MAX_RESPONSE_BYTES:
            cleaned = await _stop_worker(process)
            await _settle_tasks(tasks)
            return _failure("Masaüstü işçisi çok büyük yanıt verdi.", cleaned)
        if not await _wait_bounded(process.wait(), KILL_WAIT_SECONDS):
            cleaned = await _stop_worker(process)
            await _settle_tasks(tasks)
            return _failure("Masaüstü işçisi zamanında kapanmadı.", cleaned)
        if process.returncode != 0:
            return DesktopProcessResult("error", "Masaüstü işçisi başarısız oldu.")
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return DesktopProcessResult("error", "Masaüstü işçisi geçersiz yanıt verdi.")
        if not isinstance(response, dict) or set(response) != {"status", "message", "data"}:
            return DesktopProcessResult("error", "Masaüstü işçisi geçersiz yanıt verdi.")
        status, message, data = response["status"], response["message"], response["data"]
        if (
            not isinstance(status, str)
            or status not in {"ok", "sent", "error"}
            or not isinstance(message, str)
            or len(message) > 300
            or not isinstance(data, dict)
        ):
            return DesktopProcessResult("error", "Masaüstü işçisi geçersiz yanıt verdi.")
        if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES - 512:
            return DesktopProcessResult("error", "Masaüstü işçisi çok büyük yanıt verdi.")
        return DesktopProcessResult(cast(Literal["ok", "sent", "error"], status), message, data)
    except asyncio.CancelledError:
        if process is not None:
            await _stop_worker(process)
        await _settle_tasks(tasks)
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        cleaned = True if process is None else await _stop_worker(process)
        await _settle_tasks(tasks)
        return _failure("Masaüstü işçisi başlatılamadı veya yanıtı okunamadı.", cleaned)
    finally:
        await _settle_tasks(tasks)


def _failure(message: str, cleaned: bool) -> DesktopProcessResult:
    cleanup = "confirmed" if cleaned else "uncertain"
    if not cleaned:
        message = f"{message} İşçi temizliği doğrulanamadı."
    return DesktopProcessResult("error", message, {"worker_cleanup": cleanup})


async def _settle_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
        with suppress(
            asyncio.CancelledError,
            BrokenPipeError,
            ConnectionResetError,
            OSError,
            ValueError,
            RuntimeError,
            subprocess.SubprocessError,
        ):
            await task
