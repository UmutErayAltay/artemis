"""Kısayol ayrıştırma; gerçek RegisterHotKey çağrılmaz."""

import pytest

from jarvis.channels.desktop.hotkey import parse_hotkey


def test_parse_hotkey_maps_modifiers_and_keys():
    assert parse_hotkey("ctrl+space") == (0x0002 | 0x4000, 0x20)
    assert parse_hotkey("Ctrl + Shift + M") == (0x0002 | 0x0004 | 0x4000, ord("M"))
    assert parse_hotkey("f12") == (0x4000, 0x7B)
    assert parse_hotkey("pause") == (0x4000, 0x13)


@pytest.mark.parametrize("text", ["", "ctrl+", "ctrl+ctrl+a", "hyper+a", "ctrl+f25", "<f12>"])
def test_parse_hotkey_rejects_invalid(text):
    with pytest.raises(ValueError):
        parse_hotkey(text)
