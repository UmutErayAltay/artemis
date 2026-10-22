"""Windows arka ucunun saf yardımcıları. Fareye/klavyeye dokunan hiçbir test yoktur."""

from __future__ import annotations

import ctypes
import sys

import pytest

from jarvis.computer import windows as w


def test_normalize_corners() -> None:
    assert w._normalize(0, 0, 0, 0, 1920, 1080) == (0, 0)
    assert w._normalize(1919, 1079, 0, 0, 1920, 1080) == (65535, 65535)
    assert w._normalize(5, 5, 0, 0, 1, 1) == (65535, 65535)


def test_normalize_uses_virtual_desktop_origin() -> None:
    assert w._normalize(-1920, -1080, -1920, -1080, 3840, 2160) == (0, 0)
    assert w._normalize(1919, 1079, -1920, -1080, 3840, 2160) == (65535, 65535)
    assert w._normalize(-1920, 0, -1920, 0, 3840, 1080) == (0, 0)
    assert w._normalize(1919, 0, -1920, 0, 3840, 1080) == (65535, 65535)


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
