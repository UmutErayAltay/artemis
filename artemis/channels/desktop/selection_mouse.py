"""Fare ile metin seçimi algılayıcı (ADR 0062).

Genel fare kancası (`SetWindowsHookEx`) veya ek bağımlılık kullanmadan yalnızca sol tuş
sürükleme ve çift tıklama bitişini yoklar. Seçim bitince odaktaki UIA öğesini yoklar;
parola alanlarını ve engelli süreçleri (`BLOCKED_PROCESSES`, terminaller, parola
yöneticileri, Artemis'in kendi penceresi) her zaman yok sayar.
"""

from __future__ import annotations

import contextlib
import math
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from artemis.channels.desktop.selection_uia import probe_uia_selection
from artemis.computer.actions import process_block_reason


@dataclass(frozen=True)
class SelectionCandidate:
    """Fareyle seçim sonrası eylem simgesi açılabilecek aday konum ve hedef."""

    x: int
    y: int
    hwnd: int
    pre_text: str | None = None


@dataclass(frozen=True)
class MouseSnapshot:
    """Tek yoklama anındaki fare düğmesi, imleç ve ön plan pencere durumu."""

    left_down: bool
    x: int
    y: int
    hwnd: int
    pid: int
    exe_path: str


class SelectionMouseWatcher:
    """Sol fare sürükleme / çift tıklama seçimi sonrası küçük simge adayı üretir."""

    def __init__(
        self,
        *,
        on_candidate: Callable[[SelectionCandidate], None],
        on_dismiss: Callable[[], None],
        is_own_widget_at: Callable[[int, int], bool] = lambda _x, _y: False,
        own_pid: int | None = None,
        poll_interval: float = 0.03,
        settle_delay: float = 0.05,
        read_mouse: Callable[[], MouseSnapshot] | None = None,
        probe_selection: Callable[[], tuple[str | None, bool, bool]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_candidate = on_candidate
        self._on_dismiss = on_dismiss
        self._is_own_widget_at = is_own_widget_at
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._poll_interval = max(0.01, float(poll_interval))
        self._settle_delay = max(0.0, float(settle_delay))
        self._read_mouse = read_mouse or self._read_mouse_default
        self._probe_selection = probe_selection or probe_uia_selection
        self._clock = clock

        self._prev_down = False
        self._down_pos = (0, 0)
        self._down_time = 0.0
        self._down_hwnd = 0
        self._last_click: tuple[float, int, int, int] | None = None

        self._cached_hwnd = 0
        self._cached_exe = ""
        self._cached_pid = 0

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """İzleyici iş parçacığını başlatır; Windows dışı ve sahte okumasız ortamda False."""
        if sys.platform != "win32" and self._read_mouse == self._read_mouse_default:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="artemis-selection-mouse", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """İzleyici iş parçacığını temiz biçimde durdurur."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(1.0)
            self._thread = None

    def step(self, snap: MouseSnapshot, now: float, *, settle: bool = False) -> None:
        """Tek bir fare anlık görüntüsünü durum makinesinden geçirir."""
        if not self._prev_down and snap.left_down:
            self._prev_down = True
            self._down_pos = (snap.x, snap.y)
            self._down_time = now
            self._down_hwnd = snap.hwnd
            if not self._is_own_widget_at(snap.x, snap.y):
                self._on_dismiss()
            return

        if not (self._prev_down and not snap.left_down):
            return

        self._prev_down = False
        if (
            snap.hwnd <= 0
            or snap.pid == self._own_pid
            or not snap.exe_path.strip()
            or self._is_own_widget_at(snap.x, snap.y)
        ):
            self._last_click = None
            return

        if process_block_reason(snap.exe_path, snap.pid, self._own_pid) is not None:
            self._last_click = None
            return

        dist = math.hypot(snap.x - self._down_pos[0], snap.y - self._down_pos[1])
        elapsed = now - self._down_time
        is_drag = snap.hwnd == self._down_hwnd and dist >= 10.0 and elapsed <= 15.0
        is_dbl = False
        if dist < 8.0 and self._last_click is not None:
            prev_t, prev_hwnd, prev_x, prev_y = self._last_click
            is_dbl = (
                snap.hwnd == prev_hwnd
                and (now - prev_t) <= 0.48
                and math.hypot(snap.x - prev_x, snap.y - prev_y) <= 8.0
            )
        if is_dbl:
            self._last_click = None
        elif dist < 8.0:
            self._last_click = (now, snap.hwnd, snap.x, snap.y)
        else:
            self._last_click = None

        if not (is_drag or is_dbl):
            return

        if settle and self._settle_delay > 0 and self._stop_event.wait(self._settle_delay):
            return

        text, is_password, is_text_like = self._probe_selection()
        if is_password:
            return
        if text is not None and text.strip():
            self._on_candidate(
                SelectionCandidate(snap.x, snap.y, snap.hwnd, pre_text=text)
            )
        elif is_text_like:
            self._on_candidate(
                SelectionCandidate(snap.x, snap.y, snap.hwnd, pre_text=None)
            )

    def _run(self) -> None:
        while not self._stop_event.wait(self._poll_interval):
            try:
                snap = self._read_mouse()
            except OSError:
                continue
            self.step(snap, self._clock(), settle=True)

    def _read_mouse_default(self) -> MouseSnapshot:
        if sys.platform != "win32":
            return MouseSnapshot(False, 0, 0, 0, 0, "")
        import ctypes
        from ctypes import wintypes

        from artemis.computer.windows import foreground_process

        loader_name = "WinDLL"
        user32: Any = getattr(ctypes, loader_name)("user32", use_last_error=True)
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.GetForegroundWindow.restype = wintypes.HWND
        left_down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return MouseSnapshot(left_down, 0, 0, 0, 0, "")
        hwnd = int(user32.GetForegroundWindow() or 0)
        if hwnd <= 0:
            self._cached_hwnd, self._cached_exe, self._cached_pid = 0, "", 0
            return MouseSnapshot(left_down, int(pt.x), int(pt.y), 0, 0, "")
        if hwnd != self._cached_hwnd:
            exe_path, pid = ("", 0)
            with contextlib.suppress(OSError, RuntimeError):
                exe_path, pid = foreground_process()
            self._cached_hwnd, self._cached_exe, self._cached_pid = hwnd, exe_path, pid
        return MouseSnapshot(
            left_down, int(pt.x), int(pt.y), hwnd, self._cached_pid, self._cached_exe
        )
