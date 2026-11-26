"""Windows arka ucunun saf yardımcıları. Fareye/klavyeye dokunan hiçbir test yoktur."""

from __future__ import annotations

import ctypes
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from artemis.computer import windows as w


def test_normalize_corners() -> None:
    assert w._normalize(0, 0, 0, 0, 1920, 1080) == (0, 0)
    assert w._normalize(1919, 1079, 0, 0, 1920, 1080) == (65535, 65535)
    assert w._normalize(5, 5, 0, 0, 1, 1) == (65535, 65535)


def test_normalize_uses_virtual_desktop_origin() -> None:
    assert w._normalize(-1920, -1080, -1920, -1080, 3840, 2160) == (0, 0)
    assert w._normalize(1919, 1079, -1920, -1080, 3840, 2160) == (65535, 65535)
    assert w._normalize(-1920, 0, -1920, 0, 3840, 1080) == (0, 0)


def test_order_monitors_puts_primary_first_then_sorts_by_position() -> None:
    items = [
        (0, 0, 1920, 1080, False),
        (100, 200, 1920, 1080, True),
        (-100, 50, 1920, 1080, False),
        (-100, 0, 1920, 1080, False),
    ]

    assert w._order_monitors(items) == [
        (100, 200, 1920, 1080),
        (-100, 0, 1920, 1080),
        (-100, 50, 1920, 1080),
        (0, 0, 1920, 1080),
    ]


def test_key_order_and_extended() -> None:
    ctrl, shift, t = w._vk("ctrl"), w._vk("shift"), w._vk("t")
    assert w._key_inputs(("ctrl", "shift", "t")) == [
        (ctrl, False), (shift, False), (t, False), (t, True), (shift, True), (ctrl, True),
    ]  # fmt: skip
    assert w._vk("left") in w._EXTENDED
    assert w._vk("enter") not in w._EXTENDED
    assert w._vk("f24") == 0x87 and w._vk("a") == 0x41 and w._vk("0") == 0x30


def test_unknown_or_empty_keys() -> None:
    with pytest.raises(ValueError):
        w._vk("hyper")
    with pytest.raises(ValueError):
        w._key_inputs(())


def test_unicode_units() -> None:
    assert w._unicode_units("ğüşıöç") == [0x11F, 0xFC, 0x15F, 0x131, 0xF6, 0xE7]
    assert w._unicode_units("😀") == [0xD83D, 0xDE00]


@pytest.mark.skipif(
    sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8, reason="64-bit Windows"
)
def test_input_struct_size() -> None:
    assert ctypes.sizeof(w._INPUT) == 40


@pytest.mark.skipif(sys.platform == "win32", reason="Windows dışı")
def test_non_windows_raises() -> None:
    with pytest.raises(RuntimeError):
        w.capture(0, 0, 1, 1)


@pytest.mark.disruptive
def test_capture_monitor_length() -> None:
    rect = w.monitors()[0]
    data, width, height = w.capture(*rect)
    assert len(data) == width * height * 4


def test_type_text_sends_each_unit_separately_with_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    # Win11 Not Defteri toplu gönderimde metni bozuyordu (gerçek cihaz, 2026-11-05).
    calls: list[object] = []
    monkeypatch.setattr(w, "_require_windows", lambda: None)
    monkeypatch.setattr(w, "_send", lambda inputs: calls.append([i.ki.wScan for i in inputs]))
    monkeypatch.setattr(w.time, "sleep", lambda s: calls.append(s))
    w.type_text("aü")
    assert calls == [[0x61, 0x61], w._TYPE_DELAY_SECONDS, [0xFC, 0xFC], w._TYPE_DELAY_SECONDS]
    assert w._TYPE_DELAY_SECONDS >= 0.03


def test_type_text_keeps_surrogate_pair_in_one_send_and_stops_between_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR 0056: iptal vekil çiftinin ortasına düşemez; her SendInput basma ve bırakmayı birlikte
    # taşır (basılı tuş kalmaz). Durdurma sonrası kalan karakter gönderilmez.
    batches: list[list[tuple[int, bool]]] = []
    monkeypatch.setattr(w, "_require_windows", lambda: None)
    monkeypatch.setattr(
        w,
        "_send",
        lambda inputs: batches.append(
            [(i.ki.wScan, bool(i.ki.dwFlags & w._KEYEVENTF_KEYUP)) for i in inputs]
        ),
    )
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    answers = iter([True, True, False])
    sent = w.type_text("a😀b", lambda: next(answers))
    assert sent == 2
    assert batches == [
        [(0x61, False), (0x61, True)],
        [(0xD83D, False), (0xD83D, True), (0xDE00, False), (0xDE00, True)],
    ]


def test_focused_value_contains_returns_none_on_nul_without_calling_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def runner(_command: Any, **kwargs: Any) -> SimpleNamespace:
        if any("\x00" in value for value in kwargs["env"].values()):
            raise ValueError("embedded null character")
        return SimpleNamespace(stdout="1", returncode=0)

    monkeypatch.setattr(w.sys, "platform", "win32")
    assert w.focused_value_contains("Ay\x00ar", 1.0, runner=runner) is None
    assert w.focused_value_contains("Ayar", 1.0, runner=runner) is True


def test_window_rect_reads_rect_and_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    from ctypes import wintypes

    class _FakeUser32:
        def __init__(self, ok: bool) -> None:
            self.ok = ok

        def GetWindowRect(self, hwnd: int, rect_ptr: Any) -> int:
            if not self.ok:
                return 0
            rect = ctypes.cast(rect_ptr, ctypes.POINTER(wintypes.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 10, 20, 310, 220
            return 1

    monkeypatch.setattr(w.sys, "platform", "win32")
    monkeypatch.setattr(w, "_libs", lambda: (_FakeUser32(True), None, None))
    assert w.window_rect(7) == (10, 20, 310, 220)
    with pytest.raises(OSError):
        w.window_rect(0)
    monkeypatch.setattr(w, "_libs", lambda: (_FakeUser32(False), None, None))
    with pytest.raises(OSError):
        w.window_rect(7)


def test_uia_script_uses_per_monitor_v2_dpi_with_fallback_before_uia() -> None:
    assert "SetProcessDpiAwarenessContext" in w._UIA_SCRIPT
    assert "-4" in w._UIA_SCRIPT
    assert "SetProcessDPIAware" in w._UIA_SCRIPT
    call_idx = w._UIA_SCRIPT.index("SetProcessDpiAwarenessContext([IntPtr](-4))")
    fallback_idx = w._UIA_SCRIPT.index("SetProcessDPIAware()", call_idx)
    uia_idx = w._UIA_SCRIPT.index("UIAutomationClient")
    assert call_idx < fallback_idx < uia_idx


def test_focus_script_checks_is_password_before_patterns() -> None:
    assert "IsPassword" in w._FOCUS_SCRIPT
    pw_idx = w._FOCUS_SCRIPT.index("IsPassword")
    assert pw_idx < w._FOCUS_SCRIPT.index("ValuePattern")
    assert pw_idx < w._FOCUS_SCRIPT.index("TextPattern")
    assert "'?'; exit 0" in w._FOCUS_SCRIPT[pw_idx : w._FOCUS_SCRIPT.index("ValuePattern")]


def test_focused_value_contains_returns_none_for_password_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(w.sys, "platform", "win32")
    runner = lambda *_a, **_k: SimpleNamespace(stdout="?\n", returncode=0)  # noqa: E731
    assert w.focused_value_contains("secret", 1.0, runner=runner) is None


def test_hover_moves_pointer_without_clicking(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[list[Any]] = []
    monkeypatch.setattr(w, "_require_windows", lambda: None)
    monkeypatch.setattr(w, "_move_event", lambda x, y: ("move", x, y))
    monkeypatch.setattr(w, "_send", lambda inputs: sent.append(list(inputs)))
    w.hover(320, 480)
    assert sent == [[("move", 320, 480)]]


def test_visible_windows_filters_minimized_tiny_and_sanitizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctypes import wintypes

    win_defs = {
        1: (True, False, "Brave\nAnime\tBölüm 5", (1920, 0, 3840, 1080), ("C:\\brave.exe", 11)),
        2: (True, True, "Küçültülmüş", (0, 0, 800, 600), ("C:\\min.exe", 12)),
        3: (True, False, "Mini", (0, 0, 10, 10), ("C:\\tiny.exe", 13)),
        4: (False, False, "Gizli", (0, 0, 800, 600), ("C:\\hid.exe", 14)),
        5: (True, False, "   ", (0, 0, 800, 600), ("C:\\blank.exe", 15)),
    }

    class _FakeUser32:
        def EnumWindows(self, callback: Any, _lparam: int) -> int:
            for hwnd in win_defs:
                callback(hwnd, 0)
            return 1

        def IsWindowVisible(self, hwnd: int) -> int:
            return 1 if win_defs[hwnd][0] else 0

        def IsIconic(self, hwnd: int) -> int:
            return 1 if win_defs[hwnd][1] else 0

        def GetWindowTextLengthW(self, hwnd: int) -> int:
            return len(win_defs[hwnd][2])

        def GetWindowTextW(self, hwnd: int, buf: Any, size: int) -> int:
            text = win_defs[hwnd][2][: size - 1]
            buf.value = text
            return len(text)

        def GetWindowRect(self, hwnd: int, rect_ptr: Any) -> int:
            rect = ctypes.cast(rect_ptr, ctypes.POINTER(wintypes.RECT)).contents
            left, top, right, bottom = win_defs[hwnd][3]
            rect.left, rect.top, rect.right, rect.bottom = left, top, right, bottom
            return 1

    monkeypatch.setattr(w.sys, "platform", "win32")
    monkeypatch.setattr(w, "set_dpi_aware", lambda: None)
    monkeypatch.setattr(w, "_libs", lambda: (_FakeUser32(), None, None))
    monkeypatch.setattr(w, "_window_process", lambda hwnd: win_defs[int(hwnd)][4])
    monkeypatch.setattr(w, "_ENUM_WINDOWS_PROC", lambda fn: fn)
    monkeypatch.setattr(w, "_dwmapi", lambda: None)  # Linux CI'da ctypes.WinDLL yok
    listed = w.visible_windows()
    assert len(listed) == 1
    assert listed[0] == w.WindowInfo(
        hwnd=1, title="Brave Anime Bölüm 5", exe="C:\\brave.exe", pid=11, rect=(1920, 0, 3840, 1080)
    )
    monkeypatch.setattr(w.sys, "platform", "linux")
    assert w.visible_windows() == []


def test_visible_windows_excludes_progman_workerw_and_dwm_cloaked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctypes import wintypes

    # (cls, title, rect, cloaked, proc)
    win_defs = {
        1: (
            "Progman",
            "Program Manager",
            (0, 0, 1920, 1080),
            0,
            (r"C:\Windows\explorer.exe", 10),
        ),
        2: (
            "WorkerW",
            "Masaüstü Katmanı",
            (0, 0, 1920, 1080),
            0,
            (r"C:\Windows\explorer.exe", 10),
        ),
        3: (
            "ApplicationFrameWindow",
            "Ayarlar (Cloaked)",
            (0, 0, 1200, 800),
            2,
            (r"C:\Windows\ImmersiveControlPanel\SystemSettings.exe", 20),
        ),
        4: (
            "Notepad",
            "not.txt - Not Defteri",
            (100, 100, 900, 700),
            0,
            (r"C:\Windows\notepad.exe", 30),
        ),
    }

    class _FakeUser32:
        def EnumWindows(self, callback: Any, _lparam: int) -> int:
            for hwnd in win_defs:
                callback(hwnd, 0)
            return 1

        def IsWindowVisible(self, _hwnd: int) -> int:
            return 1

        def IsIconic(self, _hwnd: int) -> int:
            return 0

        def GetClassNameW(self, hwnd: int, buf: Any, size: int) -> int:
            cls = win_defs[hwnd][0][: size - 1]
            buf.value = cls
            return len(cls)

        def GetWindowTextLengthW(self, hwnd: int) -> int:
            return len(win_defs[hwnd][1])

        def GetWindowTextW(self, hwnd: int, buf: Any, size: int) -> int:
            text = win_defs[hwnd][1][: size - 1]
            buf.value = text
            return len(text)

        def GetWindowRect(self, hwnd: int, rect_ptr: Any) -> int:
            rect = ctypes.cast(rect_ptr, ctypes.POINTER(wintypes.RECT)).contents
            left, top, right, bottom = win_defs[hwnd][2]
            rect.left, rect.top, rect.right, rect.bottom = left, top, right, bottom
            return 1

    class _FakeDwmapi:
        def DwmGetWindowAttribute(
            self, hwnd: int, attr: int, out_ptr: Any, _size: int
        ) -> int:
            assert attr == w._DWMWA_CLOAKED
            out = ctypes.cast(out_ptr, ctypes.POINTER(wintypes.DWORD)).contents
            out.value = win_defs[int(hwnd)][3]
            return 0

    monkeypatch.setattr(w.sys, "platform", "win32")
    monkeypatch.setattr(w, "set_dpi_aware", lambda: None)
    monkeypatch.setattr(w, "_libs", lambda: (_FakeUser32(), None, None))
    monkeypatch.setattr(w, "_dwmapi", lambda: _FakeDwmapi())
    monkeypatch.setattr(w, "_window_process", lambda hwnd: win_defs[int(hwnd)][4])
    monkeypatch.setattr(w, "_ENUM_WINDOWS_PROC", lambda fn: fn)

    listed = w.visible_windows()
    assert len(listed) == 1
    assert listed[0].hwnd == 4
    assert listed[0].title == "not.txt - Not Defteri"

    monkeypatch.setattr(w.sys, "platform", "linux")
    assert w.visible_windows() == []
