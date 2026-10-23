"""Sahip olunan süreç ağacı ve sınırlı çıktı: cleanup da iş kadar süre sınırlıdır."""

from __future__ import annotations

import importlib
import os
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any, TextIO

from artemis.tools.platform import child_environment

OUTPUT_LIMIT = 256 * 1024
CLEANUP_SECONDS = 2.0
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def process_options() -> dict[str, Any]:
    if os.name == "nt":
        return {
            "creationflags": NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x4
        }
    return {"start_new_session": True, "creationflags": NO_WINDOW}


def kill_tree(proc: Any) -> bool:
    """PID yalnız yeni yaratılan sürece aittir; restart'taki PID'ler kullanılmaz."""
    try:
        if os.name == "nt":
            # Platform aracı lazy; başlatılan süreç/çocukları tek sahiplik sınırıdır.
            result = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                env=child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CLEANUP_SECONDS,
                creationflags=NO_WINDOW,
                check=False,
            )
            return result.returncode == 0
        posix = importlib.import_module("os")
        signals = importlib.import_module("signal")
        posix.killpg(proc.pid, signals.SIGKILL)
        return True
    except ProcessLookupError:
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


class ProcessCapture:
    """Boruları sürekli boşaltır; taşan çıktıyı tutmaz ve başarı diye yorumlamaz."""

    def __init__(
        self, proc: Any, prompt: str, *, resume_allowed: Callable[[], bool] | None = None
    ) -> None:
        self.proc = proc
        self._windows_tree = None
        if os.name == "nt":
            from artemis.tools.code_agent_windows import WindowsProcessTree

            self._windows_tree = WindowsProcessTree(proc, resume_allowed=resume_allowed)
        self.output: list[str] = ["", ""]
        self.overflow = threading.Event()
        self.io_failed = threading.Event()
        self.threads = [
            threading.Thread(target=self._read, args=(proc.stdout, 0), daemon=True),
            threading.Thread(target=self._read, args=(proc.stderr, 1), daemon=True),
            threading.Thread(target=self._write, args=(prompt,), daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def exited(self) -> bool:
        if os.name != "nt" and hasattr(os, "WNOWAIT"):
            # Grupliderini cleanup öncesi reap etmeyiz: PID başka sürece verilemez.
            posix = importlib.import_module("os")
            observed = posix.waitid(
                posix.P_PID, self.proc.pid, posix.WEXITED | posix.WNOHANG | posix.WNOWAIT
            )
            return observed is not None
        return self.proc.poll() is not None

    def _read(self, stream: TextIO, index: int) -> None:
        try:
            while chunk := stream.read(4096):
                remaining = OUTPUT_LIMIT - len(self.output[index])
                self.output[index] += chunk[: max(0, remaining)]
                if len(chunk) > remaining:
                    self.overflow.set()
        except (OSError, ValueError):
            self.io_failed.set()
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                self.io_failed.set()

    def _write(self, prompt: str) -> None:
        try:
            self.proc.stdin.write(prompt)
            self.proc.stdin.close()
        except (OSError, ValueError):
            self.io_failed.set()

    def finish(self, kill: bool) -> bool:
        deadline = time.monotonic() + CLEANUP_SECONDS
        # Her segment sonunda alt süreçler de temizlenir; root exit tek başına yetmez.
        clean = (
            self._windows_tree.kill() if self._windows_tree is not None else kill_tree(self.proc)
        )
        try:
            self.proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            clean = False
        for thread in self.threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return clean and all(not thread.is_alive() for thread in self.threads)
