"""Ana pencerenin tema uygulaması (DesktopWindow'dan ayrıldı): stil, ikon ve nokta renkleri.

Tema durumu (`_theme`, `_status_tone`, `_voice_state`) DesktopWindow'dadır; burası yalnız
`window` üzerindeki widget'ları o temanın jetonlarıyla boyar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QWidget

from artemis.channels.desktop import shell_assets, themes
from artemis.channels.desktop.chat_widgets import StepBlock
from artemis.channels.desktop.window_layout import _NAV_COMPACT_WIDTH
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow

# Durum ve ses noktaları tema renk anahtarıyla okunur; hex değer koda gömülmez.
STATUS_TONES = {"ready": "success", "running": "warning", "error": "error"}
VOICE_TONES = {
    "off": "muted",
    "preparing": "warning",
    "listening": "success",
    "awaiting_command": "success",
    "thinking": "warning",
    "speaking": "success",
    "error": "error",
}


def dot_style(color: str) -> str:
    """Durum noktasının rengi; metin ayrı etikette düz kalır."""
    return f"QLabel {{ color: {color}; font-size: 12px; background: transparent; }}"


def apply_theme(window: DesktopWindow, name: str) -> None:
    window._theme = name if name in themes.THEMES else themes.DEFAULT_THEME
    style = themes.stylesheet(window._theme)
    window.setStyleSheet(style)
    # Her yoldan gelen tema değişimi Görünüm kartlarını da eşitler; set_theme sinyal yaymaz.
    window.settings_panel.set_theme(window._theme)
    window.settings_panel.providers.set_theme(themes.THEMES[window._theme])
    window.automation_panel.set_theme(themes.THEMES[window._theme])
    palette = themes.THEMES[window._theme]
    for block in window.findChildren(StepBlock):
        block.apply_theme(palette)
    window.chat.apply_theme(palette)
    window.brand_pet.set_palette_tokens(palette)
    for request_id in list(window._op_widgets):
        window._render_card(request_id)
    if hasattr(window, "overlay"):
        window.overlay.setStyleSheet(style)
        theme_placeholders(window, window.overlay)
    # Masaüstü pet ve yanındaki hızlı kutu ayrı pencereler; aynı jetonlarla boyanır
    # (pet.apply_theme hızlı kutuyu da günceller; sabit lacivert yok).
    window.pet.apply_theme(window._theme)
    if hasattr(window, "selection_popup"):
        window.selection_popup.apply_theme(palette)
    if hasattr(window, "selection_badge"):
        window.selection_badge.apply_theme(palette)
    theme_placeholders(window, window)
    refresh_tones(window)
    refresh_icons(window)
    dark = window._theme.startswith("dark")
    window._theme_toggle_text = t("Açık tema") if dark else t("Koyu tema")
    compact = window.sidebar.width() == _NAV_COMPACT_WIDTH
    window.theme_toggle.setText("" if compact else window._theme_toggle_text)
    window.theme_toggle.setAccessibleName(
        t("Açık temaya geç") if dark else t("Koyu temaya geç")
    )
    if not window._demo:
        window._appearance.setValue("theme", window._theme)


def refresh_tones(window: DesktopWindow) -> None:
    """Durum ve ses noktaları tema değişince yeni renk anahtarıyla yeniden boyanır."""
    window.status_dot.setStyleSheet(
        dot_style(window._token(STATUS_TONES[window._status_tone]))
    )
    tone = VOICE_TONES.get(window._voice_state, VOICE_TONES["off"])
    window.voice_dot.setStyleSheet(dot_style(window._token(tone)))


def refresh_icons(window: DesktopWindow) -> None:
    """İkonlar temanın rengini çalışma anında alır; menü ve çalışma alanı ayrı olabilir."""
    nav_color = window._token("nav_text")
    for button, name in window._nav_icon_names.items():
        button.setIcon(shell_assets.icon(name, nav_color))
    dark = window._theme.startswith("dark")
    window.theme_toggle.setIcon(shell_assets.icon("sun" if dark else "moon", nav_color, 16))
    muted, accent = window._token("muted"), window._token("accent")
    for button, name in (
        (window.voice_button, "mic"),
        (window.composer_voice, "mic"),
        (window.auto_button, "shield"),
    ):
        button.setIcon(shell_assets.icon(name, muted, 18, accent))


def theme_placeholders(window: DesktopWindow, root: QWidget) -> None:
    color = QColor(themes.THEMES[window._theme]["muted"])
    for widget in [*root.findChildren(QLineEdit), *root.findChildren(QPlainTextEdit)]:
        palette = widget.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, color)
        widget.setPalette(palette)
