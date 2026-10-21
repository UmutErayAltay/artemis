"""Sesi açıp kapatan Windows genel kısayolu (RegisterHotKey).

Kısayol ikinci bir ses ya da komut yolu DEĞİLDİR: yalnız panelin "Ses" düğmesini tıklatır,
mikrofonu açan/kapatan tek yol o düğmedir. RegisterHotKey tuşu yalnız bu sürece verir; pynput
gibi tüm klavyeyi dinleyen bir kanca kullanılmaz, yeni bağımlılık da gerekmez.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

_MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
_MOD_NOREPEAT = 0x4000  # Basılı tutmak art arda aç/kapa yapmasın.
_KEYS = {
    "space": 0x20, "pause": 0x13, "capslock": 0x14,
    **{f"f{number}": 0x6F + number for number in range(1, 25)},
}
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 1


def parse_hotkey(text: str) -> tuple[int, int]:
    """"ctrl+space" → (RegisterHotKey bayrakları, sanal tuş kodu); geçersizse ValueError."""
    *modifiers, key = [part.strip().lower() for part in text.split("+")]
    if len(set(modifiers)) != len(modifiers) or any(m not in _MODIFIERS for m in modifiers):
        raise ValueError(f"Kısayolda bilinmeyen ya da yinelenen değiştirici: {text!r}")
    if len(key) == 1 and key.isascii() and key.isalnum():
        virtual_key = ord(key.upper())
    elif key in _KEYS:
        virtual_key = _KEYS[key]
    else:
        raise ValueError(f"Kısayolda bilinmeyen tuş: {text!r}")
    return sum(_MODIFIERS[m] for m in modifiers) | _MOD_NOREPEAT, virtual_key


class GlobalHotkey:
    """Kendi iş parçacığında kayıt + ileti döngüsü; ``on_press`` o iş parçacığından çağrılır."""

    def __init__(self, text: str, on_press: Callable[[], None]) -> None:
        self._flags, self._virtual_key = parse_hotkey(text)
        self._on_press = on_press
        self._ready = threading.Event()
        self._registered = False
        self._thread_id = 0
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Kayıt başarılıysa True; tuş başka programda kayıtlıysa ya da Windows değilse False."""
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._run, name="jarvis-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        return self._registered

    def stop(self) -> None:
        if sys.platform != "win32" or self._thread is None:
            return
        import ctypes

        if self._registered:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        self._thread.join(1.0)
        self._thread = None

    def _run(self) -> None:
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        # Kayıt ve GetMessage aynı iş parçacığında olmalı: WM_HOTKEY kaydedenin kuyruğuna düşer.
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self._registered = bool(
            user32.RegisterHotKey(None, _HOTKEY_ID, self._flags, self._virtual_key)
        )
        self._ready.set()
        if not self._registered:
            return
        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == _WM_HOTKEY:
                    self._on_press()
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
