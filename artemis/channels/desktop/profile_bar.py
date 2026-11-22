"""Üst çubuk bağlamsal çalışma profili seçicisi (`work`, `game`, `private`, `meeting`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu, QPushButton

from artemis.channels.desktop.page_kit import repolish
from artemis.channels.desktop.retranslate import set_i18n_context
from artemis.channels.desktop.settings_io import save_work_profile
from artemis.config.work_profile import (
    PROFILE_DESCRIPTIONS,
    PROFILE_LABELS,
    WORK_PROFILES,
    is_valid_work_profile,
)
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow


def build_profile_button(window: DesktopWindow) -> QPushButton:
    """Üst çubuk için tek tıkla açılan 4 profilli kompakt çip düğmesi kurar."""
    button = QPushButton()
    button.setObjectName("chip")
    set_i18n_context(button, "work_profile")
    button.setAccessibleName(t("Çalışma profili", context="work_profile"))
    button.setCursor(Qt.CursorShape.PointingHandCursor)

    menu = QMenu(button)
    group = QActionGroup(menu)
    group.setExclusive(True)
    actions: dict[str, QAction] = {}
    for profile in WORK_PROFILES:
        action = menu.addAction(t(PROFILE_LABELS[profile], context="work_profile"))
        set_i18n_context(action, "work_profile")
        action.setCheckable(True)
        action.setData(profile)
        group.addAction(action)
        actions[profile] = action
    menu.triggered.connect(window._on_profile_action)
    button.setMenu(menu)

    window.profile_button = button
    window._profile_actions = actions
    initial = getattr(window, "_work_profile", "work")
    refresh_profile_button(window, initial if is_valid_work_profile(initial) else "work")
    return button


def refresh_profile_button(window: DesktopWindow, profile: str) -> None:
    """Profil düğmesinin metin, stil ve menü işaretini disk yazmadan eşitler."""
    active = profile if is_valid_work_profile(profile) else "work"
    window._work_profile = active
    button = getattr(window, "profile_button", None)
    if button is None:
        return
    description = t(PROFILE_DESCRIPTIONS[active], context="work_profile")
    button.setText(t(PROFILE_LABELS[active], context="work_profile"))
    button.setToolTip(description)
    button.setAccessibleDescription(description)
    wanted_name = "chip" if active == "work" else "chipWarning"
    if button.objectName() != wanted_name:
        button.setObjectName(wanted_name)
        repolish(button)
    actions = getattr(window, "_profile_actions", {})
    for key, action in actions.items():
        blocked = action.blockSignals(True)
        action.setChecked(key == active)
        action.blockSignals(blocked)
    from artemis.channels.desktop.window_layout import fit_topbar

    fit_topbar(window)


def switch_work_profile(window: DesktopWindow, profile: str) -> bool:
    """Çalışma profilini kalıcı kılar, canlı çalışma zamanına uygular ve üst çubuğu günceller."""
    if not is_valid_work_profile(profile):
        return False
    previous = getattr(window, "_work_profile", "work")
    if not window._demo:
        try:
            save_work_profile(window._config_path, profile)
        except (OSError, ValueError):
            window._set_status(t("Çalışma profili kaydedilemedi."), "error")
            refresh_profile_button(window, previous)
            return False
    if window._worker_ready:
        window.bridge.set_work_profile(profile)
    runtime_dump = getattr(window, "_runtime_dump", None)
    if isinstance(runtime_dump, dict):
        runtime_dump["work_profile"] = profile
    refresh_profile_button(window, profile)
    return True
