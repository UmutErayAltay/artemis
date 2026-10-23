"""Windows süreç ağacı sahipliği; ctypes yalnız Windows çağrısında yüklenir."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from artemis.tools.code_agent_process import CLEANUP_SECONDS


class WindowsProcessTree:
    def __init__(
        self, proc: Any, *, kernel: Any = None, resume_allowed: Callable[[], bool] | None = None
    ) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_working", ctypes.c_size_t),
                ("max_working", ctypes.c_size_t),
                ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", IoCounters),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        # Any sınırı yalnız WinDLL içindir; Linux'ta ctypes.WinDLL bulunmaz.
        loader_name = "WinDLL"
        self._kernel: Any = (
            kernel
            if kernel is not None
            else getattr(ctypes, loader_name)("kernel32", use_last_error=True)
        )
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self._handle = self._kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._handle:
            raise OSError("Windows job object oluşturulamadı.")
        if not self._kernel.SetInformationJobObject(
            self._handle,
            9,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ) or not self._kernel.AssignProcessToJobObject(self._handle, int(proc._handle)):
            self._kernel.CloseHandle(self._handle)
            self._handle = None
            raise OSError("Windows süreç ağacı sahipliği kurulamadı.")

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("user_time", ctypes.c_int64),
                ("kernel_time", ctypes.c_int64),
                ("period_user", ctypes.c_int64),
                ("period_kernel", ctypes.c_int64),
                ("page_faults", wintypes.DWORD),
                ("total", wintypes.DWORD),
                ("active", wintypes.DWORD),
                ("terminated", wintypes.DWORD),
            ]

        self._accounting = Accounting
        # CREATE_SUSPENDED: ilk kullanıcı kodu ancak job üyeliği kurulduktan sonra koşar.
        try:
            if resume_allowed is None or resume_allowed():
                self._resume_owned_thread(proc.pid)
        except OSError:
            self.kill()
            raise

    def _resume_owned_thread(self, process_id: int) -> None:
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD),
                ("usage", wintypes.DWORD),
                ("thread_id", wintypes.DWORD),
                ("process_id", wintypes.DWORD),
                ("base_priority", wintypes.LONG),
                ("delta_priority", wintypes.LONG),
                ("flags", wintypes.DWORD),
            ]

        api = self._kernel
        api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenThread.restype = wintypes.HANDLE
        api.ResumeThread.argtypes = [wintypes.HANDLE]
        api.ResumeThread.restype = wintypes.DWORD
        snapshot = api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            raise OSError("Sahip olunan Windows thread bulunamadı.")
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = api.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.process_id == process_id:
                    handle = api.OpenThread(0x2, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                    if not handle:
                        raise OSError("Windows thread açılamadı.")
                    try:
                        if api.ResumeThread(handle) != 1:
                            raise OSError("Askıya alınmış Windows thread devam ettirilemedi.")
                        return
                    finally:
                        api.CloseHandle(handle)
                entry.size = ctypes.sizeof(entry)
                found = api.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError("Başlatılan Windows sürecinin thread'i bulunamadı.")
        finally:
            api.CloseHandle(snapshot)

    def kill(self) -> bool:
        import ctypes

        if not self._handle:
            return True
        killed = bool(self._kernel.TerminateJobObject(self._handle, 1))
        deadline = time.monotonic() + CLEANUP_SECONDS
        empty = False
        while killed:
            info = self._accounting()
            if not self._kernel.QueryInformationJobObject(
                self._handle,
                1,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            ):
                break
            if info.active == 0:
                empty = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        closed = bool(self._kernel.CloseHandle(self._handle))
        if closed:
            self._handle = None
        return killed and empty and closed
