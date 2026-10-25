"""Canlı ses ölçümünde süreç belleği tepe değeri ve NVIDIA GPU bellek örneklemesi.

Değer okunamazsa null kalır; tahmin üretilmez. GPU belleği aygıt geneli değerdir: başka
süreçlerin kullanımı da içindedir, bu yüzden eşzamanlı GPU süreçleri ayrıca sayılır.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from artemis.tools.platform import child_environment

_NVIDIA_TIMEOUT_SECONDS = 2.0
_MIB = 1024 * 1024

QueryRunner = Callable[[list[str]], str | None]


def process_peak_memory_bytes() -> int | None:
    """Bu sürecin şimdiye dek ulaştığı en yüksek fiziksel bellek (RSS/working set)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            process, ctypes.byref(counters), counters.cb
        ):
            return None
        return int(counters.PeakWorkingSetSize)
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux kilobayt, macOS bayt bildirir.
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def run_nvidia_smi(arguments: list[str]) -> str | None:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, *arguments], capture_output=True, text=True,
            timeout=_NVIDIA_TIMEOUT_SECONDS, env=child_environment(), check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _numbers(line: str) -> list[int] | None:
    try:
        return [int(part.strip()) for part in line.split(",")]
    except ValueError:
        return None


class GpuSampler:
    """Ölçüm boyunca aralıklı örnekler; tepe kullanılan ve en düşük boş VRAM'i tutar."""

    def __init__(self, query: QueryRunner = run_nvidia_smi, interval: float = 5.0) -> None:
        self._query = query
        self.interval = interval
        self.samples = 0
        self.peak_used_bytes: int | None = None
        self.min_free_bytes: int | None = None
        self.total_bytes: int | None = None
        self.max_other_processes: int | None = None

    def sample(self, own_pid: int) -> None:
        memory = self._query([
            "--query-gpu=memory.used,memory.free,memory.total", "--format=csv,noheader,nounits",
        ])
        if memory is None:
            return
        # Birden çok GPU'da ilk aygıt kullanılır; Artemis'in hedef makinesi tek GPU'dur.
        first = next((line for line in memory.splitlines() if line.strip()), "")
        values = _numbers(first)
        if values is None or len(values) != 3:
            return
        used, free, total = (value * _MIB for value in values)
        self.samples += 1
        self.peak_used_bytes = max(used, self.peak_used_bytes or 0)
        if self.min_free_bytes is None or free < self.min_free_bytes:
            self.min_free_bytes = free
        self.total_bytes = total
        apps = self._query(["--query-compute-apps=pid", "--format=csv,noheader,nounits"])
        if apps is not None:
            pids = {line.strip() for line in apps.splitlines() if line.strip()}
            others = len(pids - {str(own_pid)})
            self.max_other_processes = max(others, self.max_other_processes or 0)

    async def run(self, own_pid: int, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.to_thread(self.sample, own_pid)
            try:
                await asyncio.wait_for(stop.wait(), self.interval)
            except TimeoutError:
                continue

    def report(self) -> dict[str, Any]:
        return {
            "gpu_samples": self.samples,
            "gpu_peak_memory_bytes": self.peak_used_bytes,
            "free_vram_bytes": self.min_free_bytes,
            "total_vram_bytes": self.total_bytes,
            "contention": (
                None if self.max_other_processes is None
                else {"max_other_gpu_processes": self.max_other_processes}
            ),
        }
