"""DesktopBridge için bağlamsal çalışma profili geçişi (ADR 0083)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from artemis.config.work_profile import is_valid_work_profile, should_mute_tts

if TYPE_CHECKING:
    from artemis.channels.desktop.bridge import DesktopBridge


def set_work_profile(bridge: DesktopBridge, profile: str) -> bool:
    """Çalışma profilini çalışan runtime'da günceller; Uygula veya yeniden başlatma beklemez."""
    if not is_valid_work_profile(profile):
        return False
    loop = bridge._loop
    if loop is None or bridge._runtime is None or bridge._shutdown:
        return False
    try:
        loop.call_soon_threadsafe(_apply_work_profile_on_worker, bridge, profile)
    except RuntimeError:
        return False
    return True


def _apply_work_profile_on_worker(bridge: DesktopBridge, profile: str) -> None:
    runtime = bridge._runtime
    if runtime is None or bridge._shutdown:
        return
    runtime.context.settings = runtime.context.settings.model_copy(
        update={"work_profile": profile}
    )
    controller = bridge._voice_controller
    if controller is not None:
        controller.set_muted(should_mute_tts(profile))
    dictation = bridge._dictation
    if dictation is not None and not dictation.busy:
        dictation.shutdown()
        bridge._dictation = None
        bridge._dictation_signature = None


def set_computer_mode(bridge: DesktopBridge, mode: str) -> bool:
    """Bilgisayar kullanımı kipini çalışan runtime'da günceller."""
    if mode not in {"accept", "auto"}:
        return False
    loop = bridge._loop
    if loop is None or bridge._runtime is None:
        return False
    try:
        loop.call_soon_threadsafe(_apply_computer_mode_on_worker, bridge, mode)
    except RuntimeError:
        return False
    return True


def _apply_computer_mode_on_worker(bridge: DesktopBridge, mode: str) -> None:
    runtime = bridge._runtime
    if runtime is None:
        return
    # Tek gerçek kaynak araç bağlamıdır: araçlar ayarı oradan okur (kural 10).
    computer_use = runtime.context.settings.computer_use.model_copy(update={"mode": mode})
    runtime.context.settings = runtime.context.settings.model_copy(
        update={"computer_use": computer_use}
    )
