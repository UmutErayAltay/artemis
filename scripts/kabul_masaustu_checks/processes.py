"""Tek-örnek soketi (F:102) ve temiz çıkış / süreç ağacı (R:154) kabul denetimleri."""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from scripts.kabul_masaustu_checks.common import (
    REPO,
    GlobalTimeoutError,
    RowResult,
    check_deadline,
    make_temp_config,
    remaining_timeout,
    subprocess_env,
)

HOST_SCRIPT = Path(__file__).with_name("desktop_host.py")
TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260

_PROC_LOCK = threading.Lock()
_ACTIVE_PROCS: list[subprocess.Popen[str]] = []
_ACTIVE_EXTRA_PIDS: set[int] = set()


def _track_proc(proc: subprocess.Popen[str]) -> None:
    with _PROC_LOCK:
        if proc not in _ACTIVE_PROCS:
            _ACTIVE_PROCS.append(proc)


def _track_pid(pid: int) -> None:
    if pid > 0 and pid != os.getpid():
        with _PROC_LOCK:
            _ACTIVE_EXTRA_PIDS.add(pid)


def _untrack_proc(proc: subprocess.Popen[str], extra_pids: Iterable[int] = ()) -> None:
    with _PROC_LOCK:
        if proc in _ACTIVE_PROCS:
            _ACTIVE_PROCS.remove(proc)
        _ACTIVE_EXTRA_PIDS.discard(proc.pid)
        for pid in extra_pids:
            _ACTIVE_EXTRA_PIDS.discard(pid)


def list_process_tree(root_pid: int) -> list[tuple[int, int, str]]:
    """Win32 Toolhelp anlık görüntüsünden kök süreç ve tüm alt süreçlerini döndürür."""
    if sys.platform != "win32":
        return []

    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            # 64-bit Windows'ta ULONG_PTR 8 bayttır; c_size_t ile hizalama korunur.
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * MAX_PATH),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid_handle = wintypes.HANDLE(-1).value
    if not snap or snap == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    entries: list[tuple[int, int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                entries.append(
                    (
                        int(entry.th32ProcessID),
                        int(entry.th32ParentProcessID),
                        str(entry.szExeFile),
                    )
                )
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snap)

    by_pid: dict[int, tuple[int, int, str]] = {item[0]: item for item in entries}
    children: dict[int, list[tuple[int, int, str]]] = {}
    for item in entries:
        children.setdefault(item[1], []).append(item)

    result: list[tuple[int, int, str]] = []
    seen: set[int] = {root_pid}
    if root_pid in by_pid:
        result.append(by_pid[root_pid])
    queue: list[int] = [root_pid]
    while queue:
        parent = queue.pop(0)
        for child in children.get(parent, ()):
            if child[0] not in seen:
                seen.add(child[0])
                queue.append(child[0])
                result.append(child)
    return result


def _format_proc_exit(proc: subprocess.Popen[str]) -> str:
    stdout, stderr = proc.communicate(timeout=2)
    return (
        f"Konak süreci erken kapandı (kod={proc.returncode}); "
        f"stdout={stdout.strip()!r}, stderr={stderr.strip()!r}"
    )


def _wait_state(
    proc: subprocess.Popen[str],
    state_file: Path,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float,
    desc: str,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Konak durum dosyası beklenen koşulu sağlayana dek yoklar; erken çöküşü yakalar."""
    limit = remaining_timeout(deadline, timeout, desc)
    wait_end = time.monotonic() + limit
    last_state: dict[str, Any] | None = None
    while time.monotonic() < wait_end:
        if proc.poll() is not None:
            raise RuntimeError(_format_proc_exit(proc))
        if state_file.exists():
            with contextlib.suppress(OSError, json.JSONDecodeError):
                raw = json.loads(state_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    last_state = raw
                    if predicate(raw):
                        return raw
        time.sleep(0.05)
    check_deadline(deadline, desc)
    raise TimeoutError(f"Konak durumu zamanında oluşmadı ({desc}); son durum={last_state!r}")


def _request_screenshot(
    proc: subprocess.Popen[str],
    shot_request: Path,
    out_dir: Path,
    filename: str,
    *,
    timeout: float = 10.0,
    deadline: float | None = None,
) -> Path:
    done_marker = shot_request.with_suffix(".done")
    error_marker = shot_request.with_suffix(".error")
    done_marker.unlink(missing_ok=True)
    error_marker.unlink(missing_ok=True)

    tmp_req = shot_request.with_name(f"{shot_request.name}.tmp")
    tmp_req.write_text(filename, encoding="utf-8")
    os.replace(tmp_req, shot_request)

    limit = remaining_timeout(deadline, timeout, filename)
    wait_end = time.monotonic() + limit
    while time.monotonic() < wait_end:
        if proc.poll() is not None:
            raise RuntimeError(_format_proc_exit(proc))
        if error_marker.exists():
            detail = error_marker.read_text(encoding="utf-8").strip()
            error_marker.unlink(missing_ok=True)
            raise AssertionError(f"Ekran görüntüsü alınamadı ({filename}): {detail}")
        if done_marker.exists():
            done_marker.unlink(missing_ok=True)
            out_path = out_dir / filename
            if not out_path.is_file() or out_path.stat().st_size <= 0:
                raise AssertionError(f"Ekran görüntüsü dosyası boş/eksik: {out_path}")
            return out_path
        time.sleep(0.05)
    check_deadline(deadline, filename)
    raise TimeoutError(f"Ekran görüntüsü zaman aşımı ({filename})")


def _socket_still_listening(state_dir: Path) -> bool:
    """Ana süreçte yeni QCoreApplication zorunluysa kurup soketin kapandığını doğrular."""
    from PySide6.QtCore import QCoreApplication

    from artemis.channels.desktop.instance import instance_name, notify_running

    app_ref = QCoreApplication.instance()
    if app_ref is None:
        app_ref = QCoreApplication([])
    return notify_running(instance_name(state_dir), show=False)


def _terminate_pid(pid: int) -> None:
    """Artık kalan alt süreç PID'ini doğrudan sonlandırır."""
    if pid <= 0 or pid == os.getpid():
        return
    if sys.platform == "win32":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process_terminate = 0x0001
        handle = kernel32.OpenProcess(process_terminate, False, pid)
        if not handle:
            return
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


def _collect_tree_pids(roots: Iterable[int]) -> list[int]:
    ordered: list[int] = []
    seen: set[int] = set()
    for root in roots:
        if root <= 0 or root == os.getpid():
            continue
        tree = list_process_tree(root) if sys.platform == "win32" else [(root, 0, "")]
        for pid, _ppid, _exe in reversed(tree):
            if pid > 0 and pid != os.getpid() and pid not in seen:
                seen.add(pid)
                ordered.append(pid)
    return ordered


def _cleanup_proc(
    proc: subprocess.Popen[str] | None,
    *,
    extra_pids: Iterable[int] = (),
    timeout: float = 5.0,
) -> None:
    extra = tuple(extra_pids)
    if proc is None:
        for pid in _collect_tree_pids(extra):
            _terminate_pid(pid)
        return
    roots = (proc.pid, *extra)
    try:
        initial_pids = _collect_tree_pids(roots)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=timeout)
        for pid in _collect_tree_pids((*roots, *initial_pids)):
            _terminate_pid(pid)
    except (OSError, subprocess.SubprocessError):
        for pid in _collect_tree_pids(roots):
            _terminate_pid(pid)
    finally:
        _untrack_proc(proc, extra)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()


def terminate_tracked_processes(timeout: float = 5.0) -> None:
    """Küresel zaman aşımında izlenen tüm alt süreç ağaçlarını zaman aşımıyla kapatır."""
    with _PROC_LOCK:
        procs = list(_ACTIVE_PROCS)
        extra = tuple(_ACTIVE_EXTRA_PIDS)
    for proc in procs:
        _cleanup_proc(proc, extra_pids=extra, timeout=timeout)
    for pid in _collect_tree_pids(extra):
        _terminate_pid(pid)
    with _PROC_LOCK:
        _ACTIVE_EXTRA_PIDS.clear()


def run_process_checks(
    tmp_root: Path,
    out_dir: Path,
    *,
    offscreen: bool = False,
    deadline: float | None = None,
) -> list[RowResult]:
    """İkinci örnek öne getirme (F:102) ve temiz çıkış / süreç ağacı (R:154) denetimleri."""
    check_deadline(deadline, "süreç denetimleri")
    results: list[RowResult] = []
    sub = tmp_root / "processes"
    sub.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg, _ws, state_dir = make_temp_config(sub)
    env = subprocess_env(sub, offscreen=offscreen)

    qsettings_dir = sub / "qsettings"
    state_file = sub / "host_state.json"
    hide_file = sub / "hide.req"
    quit_file = sub / "quit.req"
    shot_request = sub / "shot.req"

    proc1: subprocess.Popen[str] | None = None
    host_pid: int | None = None
    f102_recorded = False
    try:
        cmd1 = [
            sys.executable,
            str(HOST_SCRIPT),
            "--config",
            str(cfg),
            "--qsettings",
            str(qsettings_dir),
            "--state-file",
            str(state_file),
            "--hide-file",
            str(hide_file),
            "--quit-file",
            str(quit_file),
            "--shot-request",
            str(shot_request),
            "--shot-out",
            str(out_dir),
        ]
        if offscreen:
            cmd1.append("--offscreen")
        proc1 = subprocess.Popen(
            cmd1,
            cwd=REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _track_proc(proc1)

        initial_state = _wait_state(
            proc1,
            state_file,
            lambda st: (
                st.get("worker_ready") is True
                and st.get("visible") is True
                and (
                    st.get("platform") == "offscreen"
                    if offscreen
                    else st.get("platform") != "offscreen"
                )
            ),
            timeout=30.0,
            desc="ilk açılış hazır ve görünür",
            deadline=deadline,
        )
        host_pid = int(initial_state["pid"])
        _track_pid(host_pid)
        _request_screenshot(
            proc1,
            shot_request,
            out_dir,
            "R154_desktop_running.png",
            timeout=10.0,
            deadline=deadline,
        )

        # F:102: gizle -> CLI desktop ile 2. süreç başlat -> var olan pencere öne gelsin.
        hide_file.write_text("hide", encoding="utf-8")
        hidden_state = _wait_state(
            proc1,
            state_file,
            lambda st: st.get("visible") is False,
            timeout=10.0,
            desc="pencere gizlendi",
            deadline=deadline,
        )
        tray_available = bool(hidden_state.get("tray"))

        proc2 = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "__import__('artemis.channels.cli', fromlist=['main']).main()",
                "desktop",
                "--config",
                str(cfg),
            ],
            cwd=REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _track_proc(proc2)
        try:
            wait2 = remaining_timeout(deadline, 20.0, "ikinci desktop süreci")
            try:
                stdout2, stderr2 = proc2.communicate(timeout=wait2)
            except subprocess.TimeoutExpired as exc:
                check_deadline(deadline, "ikinci desktop süreci")
                raise exc
        finally:
            _cleanup_proc(proc2, timeout=5.0)

        if proc2.returncode != 0:
            raise AssertionError(
                f"İkinci desktop süreci başarısız (kod={proc2.returncode}): "
                f"stdout={stdout2!r}, stderr={stderr2!r}"
            )
        expected_msg = "Artemis zaten açık; panel öne getirildi."
        if expected_msg not in stdout2:
            raise AssertionError(
                f"Beklenen tek-örnek iletisi bulunamadı: {stdout2!r}"
            )

        _wait_state(
            proc1,
            state_file,
            lambda st: st.get("visible") is True,
            timeout=10.0,
            desc="ikinci çağrıyla pencere yeniden görünür",
            deadline=deadline,
        )
        if proc1.poll() is not None:
            raise AssertionError(f"Birincil süreç beklenmedik şekilde kapandı: {proc1.returncode}")
        live_tree = list_process_tree(proc1.pid)
        expected_pids = {proc1.pid, host_pid}
        if any(pid not in expected_pids for pid, _ppid, _exe in live_tree):
            raise AssertionError(f"Beklenmeyen alt/ikinci süreç ağacı: {live_tree!r}")

        _request_screenshot(
            proc1,
            shot_request,
            out_dir,
            "F102_second_launch_raised.png",
            timeout=10.0,
            deadline=deadline,
        )
        results.append(
            RowResult(
                row_id="F:102",
                status="PASS",
                observation=(
                    f"Gizlenen pencere (tepsi={tray_available}) ikinci `desktop` çağrısıyla "
                    f"({initial_state.get('platform')}) öne getirildi; tek süreç korundu."
                ),
                screenshots=("F102_second_launch_raised.png",),
            )
        )
        results.append(
            RowResult(
                row_id="F:102-tepsi",
                status="SKIPPED",
                observation=(
                    "Tepsi simgesine elle tıklama ve oturum tepsi davranışı insan doğrulaması "
                    "gerektirir; tek-örnek soketle ikinci desktop çağrısının gizli paneli öne "
                    "getirmesi doğrulandı"
                ),
            )
        )
        f102_recorded = True

        # R:154: temiz çıkış sonrası süreç ağacı ve tek-örnek soketi tamamen kapanmalı.
        quit_file.write_text("quit", encoding="utf-8")
        try:
            exit_code = proc1.wait(timeout=remaining_timeout(deadline, 30.0, "temiz çıkış"))
        except subprocess.TimeoutExpired as exc:
            check_deadline(deadline, "temiz çıkış")
            raise exc
        if exit_code != 0:
            raise AssertionError(f"Temiz çıkış kodu 0 değil: {exit_code}")

        time.sleep(0.3)
        remaining_tree = list_process_tree(proc1.pid) + list_process_tree(host_pid)
        if remaining_tree:
            raise AssertionError(f"Çıkış sonrası kalan süreçler: {remaining_tree!r}")
        if _socket_still_listening(state_dir):
            raise AssertionError("Çıkış sonrası tek-örnek yerel soketi hâlâ dinliyor.")

        results.append(
            RowResult(
                row_id="R:154",
                status="PASS",
                observation=(
                    "Temiz çıkış kod=0 döndü; süreç ağacı boş ve tek-örnek soketi kapalı."
                ),
                screenshots=("R154_desktop_running.png",),
            )
        )
    except GlobalTimeoutError:
        raise
    except (
        AssertionError,
        OSError,
        ValueError,
        RuntimeError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as error:
        if not f102_recorded:
            results.append(
                RowResult(row_id="F:102", status="FAIL", observation=str(error))
            )
            results.append(
                RowResult(
                    row_id="F:102-tepsi",
                    status="SKIPPED",
                    observation="F:102 başarısız olduğu için tepsi satırı atlandı.",
                )
            )
        results.append(RowResult(row_id="R:154", status="FAIL", observation=str(error)))
    finally:
        _cleanup_proc(proc1, extra_pids=(host_pid,) if host_pid is not None else ())

    return results
