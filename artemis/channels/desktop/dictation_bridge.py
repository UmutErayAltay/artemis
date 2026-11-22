"""DesktopBridge için dikte oturumu, noktalama düzeltme ve yeniden yazım eşgüdümü."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from artemis.brain.models import Completion, Message
from artemis.channels.desktop.dictation import (
    Dictation,
    clean_transcript,
    foreground_target,
    type_into_target,
)
from artemis.channels.desktop.dictation_polish import PolishFn, make_dictation_polisher
from artemis.config.settings import DictationConfig

if TYPE_CHECKING:
    from artemis.channels.desktop.bridge import DesktopBridge


def reload_dictation_settings_on_worker(
    bridge: DesktopBridge, dictation: DictationConfig
) -> None:
    """Yeni dikte ayarlarını işçi döngüsünde canlıya alır; boşta olan oturumu sıfırlar."""
    runtime = bridge._runtime
    if runtime is None or bridge._shutdown:
        return
    bridge._dictation_override = dictation
    runtime.context.settings = runtime.context.settings.model_copy(
        update={"dictation": dictation}
    )
    if bridge._dictation is not None and not bridge._dictation.busy:
        bridge._dictation.shutdown()
        bridge._dictation = None
        bridge._dictation_signature = None


def toggle_dictation_on_worker(bridge: DesktopBridge) -> None:
    """Dikte oturumunu güncel ayar imzasıyla kurar veya sürüyorsa kaydı durdurup yazar."""
    from artemis.channels.desktop import bridge as bridge_mod

    runtime = bridge._runtime
    if runtime is None or bridge._shutdown:
        return
    settings = runtime.settings
    dictation_cfg = bridge._dictation_override or settings.dictation
    if not dictation_cfg.enabled:
        return
    signature = (
        dictation_cfg.max_seconds,
        settings.voice.input_device,
        settings.voice.stt_mode,
        runtime.context.settings.work_profile,
        dictation_cfg.polish_punctuation,
    )
    if (
        bridge._dictation is not None
        and not bridge._dictation.busy
        and signature != bridge._dictation_signature
    ):
        bridge._dictation.shutdown()
        bridge._dictation = None
    if bridge._dictation is None:
        bridge._dictation_signature = signature
        polish: PolishFn | None = None
        if dictation_cfg.polish_punctuation:

            async def complete(
                messages: list[Message], model: str | None, timeout: float
            ) -> Completion:
                return await bridge_mod._complete_selection(
                    runtime.provider, messages, model, timeout
                )

            polish = make_dictation_polisher(
                settings_getter=lambda: runtime.context.settings,
                complete=complete,
                audit=runtime.audit,
                timeout=min(settings.provider_timeout_seconds * 2, settings.max_seconds),
            )
        bridge._dictation = Dictation(
            max_seconds=dictation_cfg.max_seconds,
            audit=runtime.audit,
            open_microphone=lambda: bridge_mod.SoundDeviceMicrophone(
                settings.voice, device=settings.voice.input_device
            ),
            get_recognizer=lambda: bridge_mod.build_dictation_recognizer(runtime),
            backend=bridge_mod.windows_backend(),
            emit=lambda kind, text: bridge._events.put(
                bridge_mod.State(f"dictation_{kind}", text)
            ),
            voice_active=lambda: bridge.voice_active,
            other_busy=lambda: bridge._selection is not None and bridge._selection.busy,
            polish=polish,
        )
    bridge._dictation.toggle()


def repaste_dictation(
    bridge: DesktopBridge, text: str, target_hwnd: int | None = None
) -> bool:
    """Son dikte metnini dış hedef pencereye yeniden yazar; yoksa panoya kopyalar."""
    cleaned = clean_transcript(text)
    if not cleaned:
        return False
    loop = bridge._loop
    if loop is None or bridge._runtime is None or bridge._shutdown:
        return False
    try:
        loop.call_soon_threadsafe(_schedule_repaste, bridge, cleaned, target_hwnd)
    except RuntimeError:
        return False
    return True


def _schedule_repaste(
    bridge: DesktopBridge, text: str, target_hwnd: int | None
) -> None:
    if bridge._shutdown:
        return
    task = asyncio.create_task(_repaste_async(bridge, text, target_hwnd))
    task.add_done_callback(lambda done: done.cancelled() or done.exception())


async def _repaste_async(
    bridge: DesktopBridge, text: str, target_hwnd: int | None
) -> None:
    from artemis.channels.desktop import bridge as bridge_mod

    target = target_hwnd
    if target is None and bridge._dictation is not None:
        target = bridge._dictation.last_target
    backend = bridge_mod.windows_backend()
    sel_backend = bridge_mod.windows_selection_backend(backend)
    try:
        if target is not None and await asyncio.to_thread(sel_backend.focus_window, target):
            own_pid = os.getpid()
            if foreground_target(backend, own_pid) == target:
                outcome, _typed = await type_into_target(
                    backend,
                    text,
                    target,
                    own_pid,
                    closing=lambda: bridge._shutdown,
                    esc_unavailable=lambda: None,
                )
                if outcome == "typed":
                    return
        await asyncio.to_thread(sel_backend.write_clipboard, text)
    except (OSError, RuntimeError, ValueError):
        return
