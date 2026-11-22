"""Sesi açıp kapatan Windows genel kısayolu (RegisterHotKey).

Kısayol ikinci bir ses ya da komut yolu DEĞİLDİR: yalnız panelin "Ses" düğmesini tıklatır,
mikrofonu açan/kapatan tek yol o düğmedir. RegisterHotKey tuşu yalnız bu sürece verir; pynput
gibi tüm klavyeyi dinleyen bir kanca kullanılmaz, yeni bağımlılık da gerekmez.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

from artemis.i18n import t

_MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
_MODIFIER_VKS: dict[str, tuple[int, ...]] = {
    "ctrl": (0x11,),
    "alt": (0x12,),
    "shift": (0x10,),
    "win": (0x5B, 0x5C),
}
_MOD_NOREPEAT = 0x4000  # Basılı tutmak art arda aç/kapa yapmasın.
_KEYS = {
    "space": 0x20, "pause": 0x13, "capslock": 0x14, "escape": 0x1B,
    **{f"f{number}": 0x6F + number for number in range(1, 25)},
}
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 1


def hotkey_virtual_keys(text: str) -> tuple[int, tuple[tuple[int, ...], ...]]:
    """Kısayoldan birincil sanal tuş kodunu ve değiştirici VK gruplarını döndürür."""
    _, virtual_key = parse_hotkey(text)
    *modifiers, _key = [part.strip().lower() for part in text.split("+")]
    return virtual_key, tuple(_MODIFIER_VKS[m] for m in modifiers)


def parse_hotkey(text: str) -> tuple[int, int]:
    """"ctrl+space" → (RegisterHotKey bayrakları, sanal tuş kodu); geçersizse ValueError."""
    *modifiers, key = [part.strip().lower() for part in text.split("+")]
    # Tek başına harf/rakam/boşluk yazmayı ele geçirir; yazı üretmeyen tuşlar (F1-F24, Pause,
    # Esc) tek başına kalabilir: Esc, computer_use'un durdurma kısayoludur (ADR 0039).
    if not modifiers and (key == "space" or (len(key) == 1 and key.isalnum())):
        raise ValueError(
            t("Kısayolda en az bir değiştirici olmalı: {text}").format(text=repr(text))
        )
    if len(set(modifiers)) != len(modifiers) or any(m not in _MODIFIERS for m in modifiers):
        raise ValueError(
            t("Kısayolda bilinmeyen ya da yinelenen değiştirici: {text}").format(text=repr(text))
        )
    if len(key) == 1 and key.isascii() and key.isalnum():
        virtual_key = ord(key.upper())
    elif key in _KEYS:
        virtual_key = _KEYS[key]
    else:
        raise ValueError(t("Kısayolda bilinmeyen tuş: {text}").format(text=repr(text)))
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
        self._thread = threading.Thread(target=self._run, name="artemis-hotkey", daemon=True)
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
        message = wintypes.MSG()
        if self._registered:
            # PostThreadMessageW kuyruk yoksa başarısız olur; _ready öncesi kuyruğu oluştur.
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        self._ready.set()
        if not self._registered:
            return
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == _WM_HOTKEY:
                    self._on_press()
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)


RegisterLoop = Callable[
    [int, int, Callable[[bool], None], Callable[[], None], threading.Event],
    None,
]


class PushToTalkHotkey:
    """Bas-konuş genel kısayolu: basınca `on_press`, bırakınca `on_release` (1:1 eşli).

    `RegisterHotKey` (`_WM_HOTKEY`) ile başlar, ardından basılı tutma bitene dek yalnız hedef
    tuş ve değiştiricilerin durumunu `GetAsyncKeyState` ile yoklar. `pynput` veya genel klavye
    kancası (`SetWindowsHookEx`) kullanmaz.
    """

    def __init__(
        self,
        text: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        *,
        poll_interval: float = 0.025,
        is_key_down: Callable[[int], bool] | None = None,
        register_loop: RegisterLoop | None = None,
    ) -> None:
        self._flags, self._virtual_key = parse_hotkey(text)
        _, self._modifier_vks = hotkey_virtual_keys(text)
        self._on_press = on_press
        self._on_release = on_release
        self._poll_interval = max(0.005, float(poll_interval))
        self._is_key_down = is_key_down
        self._register_loop = register_loop
        self._state_lock = threading.Lock()
        self._holding = False
        self._ready = threading.Event()
        self._stop_event = threading.Event()
        self._registered = False
        self._thread_id = 0
        self._thread: threading.Thread | None = None
        self._release_thread: threading.Thread | None = None

    def _key_down(self, vk: int) -> bool:
        if self._is_key_down is not None:
            return bool(self._is_key_down(vk))
        if sys.platform != "win32":
            return False
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

    def _combo_down(self) -> bool:
        if not self._key_down(self._virtual_key):
            return False
        return all(any(self._key_down(vk) for vk in group) for group in self._modifier_vks)

    def start(self) -> bool:
        """Kayıt başarılıysa True; başka programda kayıtlıysa ya da Windows değilse False."""
        if self._register_loop is None and sys.platform != "win32":
            return False
        self._ready.clear()
        self._stop_event.clear()
        self._registered = False
        self._thread = threading.Thread(
            target=self._run, name="artemis-ptt-hotkey", daemon=True
        )
        self._thread.start()
        self._ready.wait(2.0)
        return self._registered

    def stop(self) -> None:
        self._stop_event.set()
        if (
            sys.platform == "win32"
            and self._register_loop is None
            and self._registered
            and self._thread_id
        ):
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(1.0)
            self._thread = None
        if self._release_thread is not None:
            self._release_thread.join(1.0)
            self._release_thread = None
        self._finish_release()

    def _handle_press(self) -> None:
        with self._state_lock:
            if self._holding or self._stop_event.is_set():
                return
            self._holding = True
        self._on_press()
        if self._stop_event.is_set():
            self._finish_release()
            return
        watcher = threading.Thread(
            target=self._watch_release, name="artemis-ptt-release", daemon=True
        )
        with self._state_lock:
            self._release_thread = watcher
        watcher.start()

    def _watch_release(self) -> None:
        try:
            while not self._stop_event.wait(self._poll_interval):
                if not self._combo_down():
                    break
        finally:
            self._finish_release()

    def _finish_release(self) -> None:
        emit = False
        with self._state_lock:
            if self._holding:
                self._holding = False
                emit = True
        if emit:
            self._on_release()

    def _run(self) -> None:
        if self._register_loop is not None:

            def on_ready(ok: bool) -> None:
                self._registered = bool(ok)
                self._ready.set()

            try:
                self._register_loop(
                    self._flags,
                    self._virtual_key,
                    on_ready,
                    self._handle_press,
                    self._stop_event,
                )
            finally:
                if not self._ready.is_set():
                    self._ready.set()
                self._finish_release()
            return
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self._registered = bool(
            user32.RegisterHotKey(None, _HOTKEY_ID, self._flags, self._virtual_key)
        )
        message = wintypes.MSG()
        if self._registered:
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        self._ready.set()
        if not self._registered:
            return
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if self._stop_event.is_set():
                    break
                if message.message == _WM_HOTKEY:
                    self._handle_press()
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
            self._finish_release()
