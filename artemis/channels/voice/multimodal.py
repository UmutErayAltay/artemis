"""Ses + görüntü çok kipli ekran bağlamı yakalama ve kapı yardımcıları (ADR 0085).

Ekran görüntüsü yalnız bellekte PNG baytları olarak üretilir; diske asla yazılmaz.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Protocol

from artemis.computer.png import screenshot_png
from artemis.config.settings import Settings, VoiceConfig
from artemis.config.work_profile import effective_settings_for_profile, is_local_only_profile

logger = logging.getLogger("artemis.channels.voice.multimodal")


class ScreenCaptureBackend(Protocol):
    """Test edilebilir bellek içi ekran yakalama arka ucu sözleşmesi."""

    def monitors(self) -> list[tuple[int, int, int, int]]: ...

    def capture(self, left: int, top: int, width: int, height: int) -> tuple[bytes, int, int]: ...


def is_voice_screen_context_allowed(settings: Settings) -> bool:
    """Sesli komutta ekran görüntüsü eklenmesi için üçlü gizlilik kapısını doğrular.

    Etkin çalışma profili uygulanır; `multimodal_screen_context`, `direct_audio_input` ve
    `perception.allow_cloud_screen` birlikte açık değilse veya `private` profil etkinse False.
    """
    effective = effective_settings_for_profile(settings)
    return bool(
        effective.voice.multimodal_screen_context
        and effective.voice.direct_audio_input
        and effective.perception.allow_cloud_screen
        and not is_local_only_profile(effective.work_profile)
    )


def capture_active_screen_png(
    max_width: int = 1280,
    *,
    backend: ScreenCaptureBackend | None = None,
    platform: str | None = None,
) -> bytes | None:
    """Birincil monitörün PNG baytlarını yalnızca RAM'de yakalar; başarısızlıkta None döner."""
    if max_width <= 0:
        return None
    active_backend: ScreenCaptureBackend
    if backend is None:
        active_platform = sys.platform if platform is None else platform
        if active_platform != "win32":
            return None
        from artemis.computer import windows

        active_backend = windows
    else:
        if platform is not None and platform != "win32":
            return None
        active_backend = backend

    try:
        monitor_list = active_backend.monitors()
        if not monitor_list:
            return None
        left, top, width, height = monitor_list[0]
        if width <= 0 or height <= 0:
            return None
        bgra, cap_width, cap_height = active_backend.capture(left, top, width, height)
        png_bytes, _size = screenshot_png(bgra, cap_width, cap_height, max_width)
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        logger.warning(
            "Çok kipli ses ekran yakalaması atlandı (%s).", type(error).__name__
        )
        return None
    return png_bytes


def maybe_capture_voice_screen(
    config: VoiceConfig,
    *,
    is_private: bool,
    has_audio: bool,
    capture_screen: Callable[[], bytes | None] | None,
) -> bytes | None:
    """Ses denetleyicisi kapıları uygunsa bellek içi ekran PNG'sini döndürür."""
    if (
        not has_audio
        or capture_screen is None
        or is_private
        or not config.multimodal_screen_context
        or not config.direct_audio_input
    ):
        return None
    return capture_screen()
