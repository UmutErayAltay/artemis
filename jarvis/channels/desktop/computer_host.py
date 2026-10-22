"""Bilgisayar kullanımı oturumunun masaüstü tarafı (ADR 0039): Esc, durum satırı, onay.

Qt'siz tutulur; bridge işçi iş parçacığı bunu `services["computer_host"]` olarak kaydeder,
`computer_use` aracı yalnız burada bulunan Esc kısayoluna ve onay köprüsüne güvenir. Başka
kanalda (CLI, Telegram) host yoktur ve araç çalışmayı reddeder.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from jarvis.channels.desktop.hotkey import GlobalHotkey

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
        self._lock = threading.Lock()
        self._active = False

    @contextmanager
    def session(self) -> Iterator[SessionHandles]:
        with self._lock:
            if self._active:
                raise RuntimeError("Zaten bir bilgisayar kullanımı oturumu sürüyor.")
            self._active = True
        stop = threading.Event()

        def escape() -> None:
            # Bayrak tek başına adım arasında bakılır; süren model çağrısını ve beklemeyi
            # kesmek için tur da İptal düğmesiyle aynı yoldan iptal edilir.
            stop.set()
            if self._on_escape is not None:
                self._on_escape()

        hotkey = GlobalHotkey("escape", escape)
        try:
            # Kayıt başarısızsa oturum yine çalışır ama kullanıcı bunu bilmelidir.
            if not hotkey.start():
                self._emit_status(_ESC_UNAVAILABLE)
            yield stop, self._emit_status, self._approve
        finally:
            hotkey.stop()
            self._emit_status("")
            with self._lock:
                self._active = False
