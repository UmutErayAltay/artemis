"""Ana pencerenin "Windows açılışında başlat" menü akışı (DesktopWindow'dan ayrıldı).

Kaydı kuran nesne `window._autostart()`tir (testler onu yerine koyar); burası yalnız durumu okur ve
başarısız geçişte seçim kutusunu gerçek duruma döndürür.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QSystemTrayIcon

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow


def autostart_state(window: DesktopWindow) -> str:
    autostart = window._autostart()
    return autostart.status().state if autostart is not None else "off"


def toggle_autostart(window: DesktopWindow, enabled: bool) -> None:
    autostart = window._autostart()
    if autostart is None:
        message, success = "Ayarlar okunamadı; açılış kaydı değiştirilmedi.", False
    else:
        result = autostart.enable() if enabled else autostart.disable()
        message, success = result.message, result.success
    if not success and window._autostart_action is not None:
        # Seçim kutusu gerçek durumu göstermeli: başarısızlıkta eski haline döner.
        window._autostart_action.blockSignals(True)
        window._autostart_action.setChecked(not enabled)
        window._autostart_action.blockSignals(False)
    if window._tray is not None:
        window._tray.showMessage(
            "Artemis", message, QSystemTrayIcon.MessageIcon.Information, 6000
        )
