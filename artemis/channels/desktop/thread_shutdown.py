"""Panel kapanırken çalışan QThread'i güvenle durdurur (2026-11-18 derin tarama).

Neden: ebeveyni panel olan ve hâlâ çalışan QThread panel yıkılınca "Destroyed while thread is
still running" ile süreci çökertir. Önce kesme istenir ve sınırlı süre beklenir; bitmezse
sinyalleri koparılıp ebeveynden ayrılır ve bitene dek `DETACHED` içinde canlı tutulur.
"""

from __future__ import annotations

import contextlib
import functools
import weakref
from typing import Any

from PySide6.QtCore import QThread

DETACHED: list[QThread] = []


def _drop_detached(ref: weakref.ref[QThread]) -> None:
    thread = ref()
    if thread is not None and thread in DETACHED:
        DETACHED.remove(thread)


def shutdown_thread(
    thread: QThread | None, *connections: tuple[str, Any], wait_ms: int
) -> bool:
    """Çalışan iş parçacığını durdurur; süre dolarsa panelle yıkılmaması için ayırır.

    Ayrıldıysa True döner."""
    if thread is None or not thread.isRunning():
        return False
    thread.requestInterruption()
    if thread.wait(wait_ms):
        return False
    for signal_name, slot in connections:
        signal = getattr(thread, signal_name, None)
        if signal is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect(slot)
    thread.setParent(None)
    DETACHED.append(thread)
    thread.finished.connect(functools.partial(_drop_detached, weakref.ref(thread)))
    if thread.isFinished() and thread in DETACHED:
        DETACHED.remove(thread)
    return True


_UNPARENTED: set[QThread] = set()


def start_unparented(thread: QThread) -> None:
    """İş parçacığını ebeveynsiz başlatır; bitene dek modül düzeyinde canlı tutar.

    Neden: panel `close` olmadan silinirse (ör. ebeveyn pencere yıkılır) ebeveyni panel olan
    çalışan QThread "Destroyed while thread is still running" ile süreci çökertir (Karne 7:
    Ayarlar açılışında arka plan model taraması). Ebeveynsiz iş parçacığı panelle yıkılmaz;
    bitince kendini siler. Alıcı panel silinmişse Qt bağlantıyı kendisi koparır.
    """
    thread.setParent(None)
    _UNPARENTED.add(thread)
    thread.finished.connect(functools.partial(_release_unparented, weakref.ref(thread)))
    thread.start()


def _release_unparented(ref: weakref.ref[QThread]) -> None:
    thread = ref()
    if thread is not None:
        _UNPARENTED.discard(thread)
        thread.deleteLater()
