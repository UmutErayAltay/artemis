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


def _select_active_monitor(
    monitors: list[tuple[int, int, int, int]],
    window_rect: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int]:
    """Ön plan penceresiyle en fazla kesişen monitörü seçer; yoksa birincil monitör."""
    if window_rect is None:
        return monitors[0]
    w_left, w_top, w_right, w_bottom = window_rect
    best = monitors[0]
    best_area = 0
    for mon in monitors:
        m_left, m_top, m_width, m_height = mon
        m_right = m_left + m_width
        m_bottom = m_top + m_height
        ix = min(m_right, w_right) - max(m_left, w_left)
        iy = min(m_bottom, w_bottom) - max(m_top, w_top)
        if ix > 0 and iy > 0:
            area = ix * iy
            if area > best_area:
                best_area = area
                best = mon
    return best


def capture_active_screen_png(
    max_width: int = 1280,
    *,
    backend: ScreenCaptureBackend | None = None,
    platform: str | None = None,
) -> bytes | None:
    """Etkin monitörün PNG baytlarını yalnızca RAM'de yakalar; başarısızlıkta None döner."""
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
        window_rect: tuple[int, int, int, int] | None = None
        fg_getter = getattr(active_backend, "foreground_window", None)
        if callable(fg_getter):
            try:
                _hwnd, raw_rect = fg_getter()
                if isinstance(raw_rect, tuple) and len(raw_rect) == 4:
                    window_rect = raw_rect
            except (OSError, RuntimeError, ValueError, TypeError):
                window_rect = None
        left, top, width, height = _select_active_monitor(monitor_list, window_rect)
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
