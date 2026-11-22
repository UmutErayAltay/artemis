"""DesktopBridge için Bas-Konuş (Push-to-Talk) oturum eşgüdümü."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.bridge import DesktopBridge


def ptt_press(bridge: DesktopBridge, state_cls: type[Any]) -> bool:
    """Bas-konuş başlangıcı: ses açıksa bağlanır, kapalıysa tek tur için sesi açar."""
    with bridge._lock:
        if not bridge._ready.is_set() or bridge._stopped.is_set() or bridge._shutdown:
            return False
        dictation = bridge._dictation
        if dictation is not None and dictation.busy:
            bridge._events.put(
                state_cls("dictation_message", t("Dikte sürerken bas-konuş kullanılamaz."))
            )
            return False
        bridge._ptt_holding = True
        need_start = not bridge._voice_starting and bridge._voice_task is None
        if need_start:
            bridge._ptt_auto_voice = True
            bridge._voice_starting = True
            bridge._voice_stop_requested = False
    loop = bridge._loop
    if loop is None:
        if need_start:
            with bridge._lock:
                bridge._voice_starting = False
                bridge._ptt_auto_voice = False
                bridge._ptt_holding = False
        return False
    if need_start:
        try:
            asyncio.run_coroutine_threadsafe(bridge._start_voice_on_worker(), loop)
        except RuntimeError:
            with bridge._lock:
                bridge._voice_starting = False
                bridge._ptt_auto_voice = False
                bridge._ptt_holding = False
            return False
        return True
    return bridge._post(_press_on_worker, bridge)


def _press_on_worker(bridge: DesktopBridge) -> None:
    task = bridge._ptt_drain_task
    if task is not None and not task.done():
        task.cancel()
        bridge._ptt_drain_task = None
    controller = bridge._voice_controller
    if controller is not None and bridge._ptt_holding and not bridge._shutdown:
        controller.ptt_press()


def ptt_release(bridge: DesktopBridge) -> bool:
    """Bas-konuş bitişi: biriken sesi işler ve otomatik açılan sesi tur sonunda kapatır."""
    with bridge._lock:
        if not bridge._ready.is_set() or bridge._stopped.is_set() or bridge._shutdown:
            return False
        if not bridge._ptt_holding and bridge._voice_controller is None:
            return False
        bridge._ptt_holding = False
        auto_voice = bridge._ptt_auto_voice
    return bridge._post(_release_on_worker, bridge, auto_voice)


def _release_on_worker(bridge: DesktopBridge, auto_voice: bool) -> None:
    controller = bridge._voice_controller
    if controller is None:
        return
    controller.ptt_release()
    if auto_voice and not bridge._ptt_holding:
        prev = bridge._ptt_drain_task
        if prev is not None and not prev.done():
            prev.cancel()
        bridge._ptt_drain_task = asyncio.create_task(controller.drain_and_stop())
