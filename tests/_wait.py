"""Testler için ortak bekleme yardımcıları (sabit uyku yok; koşul olunca hemen döner)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

_GATE = threading.Event()  # hiç set edilmez; yalnız kesintisiz kısa bekleme için


def wait_until(
    predicate: Callable[[], bool], timeout: float = 2.0, interval: float = 0.005
) -> bool:
    """Koşul sağlanana dek (saniye cinsinden `timeout`) yoklar; sağlandıysa True döner."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        _GATE.wait(interval)
    return True


def wait_qt(predicate: Callable[[], bool], timeout: float = 3.0, interval: float = 0.005) -> bool:
    """`wait_until` gibi, ama her turda Qt olaylarını işler (timeout saniye)."""
    from PySide6.QtCore import QCoreApplication

    def ready() -> bool:
        QCoreApplication.processEvents()
        return predicate()

    return wait_until(ready, timeout, interval)
