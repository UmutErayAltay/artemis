"""Genel kısayol doğrulama ve yaşam döngüsü yönetimi (DesktopWindow'dan ayrıldı)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from artemis.channels.desktop.hotkey import GlobalHotkey, PushToTalkHotkey, parse_hotkey
from artemis.config.settings import load_settings, same_hotkey
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow


def validate_hotkeys(
    *,
    listening: str,
    dictation: str,
    selection: str,
    ptt: str = "",
) -> str | None:
    """Tüm genel kısayolların söz dizimini ve ikili çakışmalarını doğrular."""
    listening_text = listening.strip()
    dictation_text = dictation.strip()
    selection_text = selection.strip()
    ptt_text = ptt.strip()

    if listening_text:
        try:
            parse_hotkey(listening_text)
        except ValueError:
            return t("Ses kısayolu geçersiz: ör. ctrl+space (boş bırakılırsa kapalı).")

    if ptt_text:
        try:
            parse_hotkey(ptt_text)
        except ValueError:
            return t("Bas-konuş kısayolu geçersiz: ör. ctrl+alt+v (boş bırakılırsa kapalı).")

    if not dictation_text:
        return t("Dikte kısayolu geçersiz: ör. ctrl+alt+d.")
    try:
        parse_hotkey(dictation_text)
    except ValueError:
        return t("Dikte kısayolu geçersiz: ör. ctrl+alt+d.")

    if not selection_text:
        return t("Seçili metin kısayolu geçersiz: ör. ctrl+alt+s.")
    try:
        parse_hotkey(selection_text)
    except ValueError:
        return t("Seçili metin kısayolu geçersiz: ör. ctrl+alt+s.")

    if same_hotkey(dictation_text, listening_text):
        return t("Dikte kısayolu ses kısayoluyla aynı olamaz; başka bir tuş seç.")
    if same_hotkey(selection_text, listening_text) or same_hotkey(
        selection_text, dictation_text
    ):
        return t("Seçili metin kısayolu ses ve dikte kısayolundan farklı olmalı.")
    if ptt_text and any(
        same_hotkey(ptt_text, other)
        for other in (listening_text, dictation_text, selection_text)
    ):
        return t("Bas-konuş kısayolu diğer kısayollarla aynı olamaz.")
    return None


def _stop_handle(window: DesktopWindow, attr: str) -> None:
    current = getattr(window, attr, None)
    if current is not None:
        current.stop()
        setattr(window, attr, None)


def _start_single(
    window: DesktopWindow,
    hotkey_cls: type[GlobalHotkey],
    *,
    attr: str,
    text: str,
    emit: Callable[[], Any],
    invalid_msg: str,
    busy_msg: str,
) -> None:
    _stop_handle(window, attr)
    cleaned = text.strip()
    if not cleaned:
        return
    try:
        hotkey = hotkey_cls(cleaned, emit)
    except ValueError:
        window._hotkey_problem(invalid_msg.format(text=cleaned))
        return
    if hotkey.start():
        setattr(window, attr, hotkey)
    elif sys.platform == "win32":
        window._hotkey_problem(busy_msg.format(text=cleaned))


def start_listening_hotkey(
    window: DesktopWindow, hotkey_cls: type[GlobalHotkey] = GlobalHotkey
) -> None:
    """Sesi açıp kapatan genel kısayolu başlatır veya yeniler; sinyal bağlantısı tekdir."""
    _stop_handle(window, "_hotkey")
    try:
        text = load_settings(window._config_path).voice.listening_hotkey.strip()
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        return
    _start_single(
        window,
        hotkey_cls,
        attr="_hotkey",
        text=text,
        emit=window._hotkey_pressed.emit,
        invalid_msg=t("Ses kısayolu tanınmadı: {text}"),
        busy_msg=t("Ses kısayolu ({text}) başka bir programda kayıtlı."),
    )


def start_dictation_hotkey(
    window: DesktopWindow, hotkey_cls: type[GlobalHotkey] = GlobalHotkey
) -> None:
    """Dikte kısayolunu başlatır veya yeniler (ADR 0057)."""
    _stop_handle(window, "_dictation_hotkey")
    try:
        config = load_settings(window._config_path).dictation
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        return
    if not config.enabled:
        return
    _start_single(
        window,
        hotkey_cls,
        attr="_dictation_hotkey",
        text=config.hotkey,
        emit=window._dictation_pressed.emit,
        invalid_msg=t("Dikte kısayolu tanınmadı: {text}"),
        busy_msg=t("Dikte kısayolu ({text}) başka bir programda kayıtlı."),
    )


def start_selection_hotkey(
    window: DesktopWindow, hotkey_cls: type[GlobalHotkey] = GlobalHotkey
) -> None:
    """Seçili metin kısayolunu başlatır veya yeniler (ADR 0062)."""
    _stop_handle(window, "_selection_hotkey")
    try:
        config = load_settings(window._config_path).selection_actions
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        return
    if not config.enabled:
        return
    _start_single(
        window,
        hotkey_cls,
        attr="_selection_hotkey",
        text=config.hotkey,
        emit=window._selection_pressed.emit,
        invalid_msg=t("Seçili metin kısayolu tanınmadı: {text}"),
        busy_msg=t("Seçili metin kısayolu ({text}) başka bir programda kayıtlı."),
    )


def start_ptt_hotkey(
    window: DesktopWindow, hotkey_cls: type[PushToTalkHotkey] = PushToTalkHotkey
) -> None:
    """Bas-konuş genel kısayolunu başlatır veya yeniler."""
    _stop_handle(window, "_ptt_hotkey")
    try:
        voice = load_settings(window._config_path).voice
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        return
    if not voice.ptt_enabled:
        return
    cleaned = voice.ptt_hotkey.strip()
    if not cleaned:
        return
    try:
        hotkey = hotkey_cls(cleaned, window._ptt_pressed.emit, window._ptt_released.emit)
    except ValueError:
        window._hotkey_problem(t("Bas-konuş kısayolu tanınmadı: {text}").format(text=cleaned))
        return
    if hotkey.start():
        window._ptt_hotkey = hotkey
    elif sys.platform == "win32":
        window._hotkey_problem(
            t("Bas-konuş kısayolu ({text}) başka bir programda kayıtlı.").format(text=cleaned)
        )


def reload_hotkeys(
    window: DesktopWindow,
    hotkey_cls: type[GlobalHotkey] = GlobalHotkey,
    ptt_cls: type[PushToTalkHotkey] = PushToTalkHotkey,
) -> None:
    """Tüm aktif genel kısayolları temizleyip güncel config'le yeniden başlatır."""
    if window._demo:
        return
    start_listening_hotkey(window, hotkey_cls)
    start_dictation_hotkey(window, hotkey_cls)
    start_selection_hotkey(window, hotkey_cls)
    start_ptt_hotkey(window, ptt_cls)
