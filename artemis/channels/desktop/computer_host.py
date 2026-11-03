"""Bilgisayar kullanımı oturumunun masaüstü tarafı (ADR 0039): Esc, durum satırı, onay.

Qt'siz tutulur; bridge işçi iş parçacığı bunu `services["computer_host"]` olarak kaydeder,
`computer_use` aracı yalnız burada bulunan Esc kısayoluna ve onay köprüsüne güvenir. Başka
kanalda (CLI) host yoktur ve araç çalışmayı reddeder. Gömülü Telegram kanalı kendi
host'unu kurar (ADR 0053); ekran kilidi (`SCREEN_LOCK`) iki host arasında ortaktır.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from artemis.channels.desktop.hotkey import GlobalHotkey
from artemis.computer.session import SCREEN_LOCK, ScreenBusyError

# (durdurma olayı, adım bildirimi, onay) — oturum boyunca araca verilen üçlü.
SessionHandles = tuple[
    threading.Event, Callable[[str], None], Callable[[str, dict[str, Any]], bool]
]

_ESC_UNAVAILABLE = "Esc kısayolu kullanılamıyor; durdurmak için İptal düğmesini kullanın"


class ComputerHost:
    def __init__(
        self,
        approve: Callable[[str, dict[str, Any]], bool],
        emit_status: Callable[[str], None],
        on_escape: Callable[[], object] | None = None,
    ) -> None:
        self._approve = approve
        self._emit_status = emit_status
        self._on_escape = on_escape
        self._stop: threading.Event | None = None

    @property
    def active(self) -> bool:
        return self._stop is not None

    def stop_active(self) -> bool:
        """Esc kısayolunun yaptığı: oturum bayrağını kaldırır ve turu iptal ettirir.

        Bayrak adım arasında bakılır; süren model çağrısını ve beklemeyi kesmek için tur da
        İptal düğmesiyle aynı yoldan iptal edilir. Oturum yoksa False döner.
        """
        stop = self._stop
        if stop is None:
            return False
        stop.set()
        if self._on_escape is not None:
            self._on_escape()
        return True

    def _escape(self) -> None:
        self.stop_active()

    @contextmanager
    def session(self) -> Iterator[SessionHandles]:
        # Ekran süreç genelinde tek oturumludur (masaüstü + Telegram); ikincisi reddedilir.
        if not SCREEN_LOCK.acquire(blocking=False):
            raise ScreenBusyError("Zaten bir bilgisayar kullanımı oturumu sürüyor.")
        stop = threading.Event()
        self._stop = stop
        hotkey = GlobalHotkey("escape", self._escape)
        try:
            # Kayıt başarısızsa oturum yine çalışır ama kullanıcı bunu bilmelidir.
            if not hotkey.start():
                self._emit_status(_ESC_UNAVAILABLE)
            yield stop, self._emit_status, self._approve
        finally:
            hotkey.stop()
            self._emit_status("")
            self._stop = None
            SCREEN_LOCK.release()
