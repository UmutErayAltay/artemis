"""Kabuk ikonları ve küçük sunum yardımcıları (Qt dışı mantık: yalnız selamlama).

İkonlar tek renkli SVG şablonudur ve çalışma anında temanın NAV/metin rengiyle
boyanır. Neden: karma temada menü koyu, çalışma alanı açık olabilir; sabit renkli
dosya ikonları bir tarafta kaybolurdu. Renk başına bir kez çizilir, sonra önbellekten gelir.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# 24x24 görünüm kutusu; çizgi ikonlar `stroke`, dolu olanlar kendi `fill`ini taşır.
_ICONS: dict[str, str] = {
    "chat": (
        '<path d="M5 6a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H11l-4 3.5V16'
        'a2 2 0 0 1-2-2z"/>'
    ),
    "projects": (
        '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
    ),
    "pets": (
        '<g fill="{c}" stroke="none"><circle cx="6.5" cy="10.5" r="1.9"/>'
        '<circle cx="10.5" cy="6.8" r="1.9"/><circle cx="14.5" cy="6.8" r="1.9"/>'
        '<circle cx="18" cy="10.5" r="1.9"/>'
        '<path d="M12 12.3c-3 0-5.5 2.8-5.5 5 0 1.6 1.4 2.3 3 2.3 1 0 1.6-.4 2.5-.4s1.5.4 2.5.4'
        'c1.6 0 3-.7 3-2.3 0-2.2-2.5-5-5.5-5z"/></g>'
    ),
    "history": '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 1.8"/>',
    "settings": (
        '<path d="M4 8h7M17 8h3M4 16h3M13 16h7"/>'
        '<circle cx="14" cy="8" r="2.2"/><circle cx="10" cy="16" r="2.2"/>'
    ),
    "mic": (
        '<rect x="9" y="3.5" width="6" height="11" rx="3"/>'
        '<path d="M6 11.5a6 6 0 0 0 12 0M12 17.5V21"/>'
    ),
    "shield": (
        '<path d="M12 3l7 3v5.5c0 4.2-2.9 7.6-7 9.5-4.1-1.9-7-5.3-7-9.5V6z"/>'
        '<path d="M9 12l2.2 2.2L15 10"/>'
    ),
    "sun": (
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6L7 7M17 17l1.4 1.4'
        'M5.6 18.4L7 17M17 7l1.4-1.4"/>'
    ),
    "moon": '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
}

# Sayfa adı -> ikon adı (kenar çubuğu).
NAV_ICONS = {
    "Sohbet": "chat", "Projeler": "projects", "Petler": "pets",
    "Geçmiş": "history", "Ayarlar": "settings",
}

_cache: dict[tuple[str, str, int], QPixmap] = {}


def svg_text(name: str, color: str) -> str:
    body = _ICONS[name].replace("{c}", color)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        f"{body}</svg>"
    )


def pixmap(name: str, color: str, size: int = 20) -> QPixmap:
    key = (name, color, size)
    cached = _cache.get(key)
    if cached is None:
        cached = QPixmap(size, size)
        cached.fill(Qt.GlobalColor.transparent)
        renderer = QSvgRenderer(QByteArray(svg_text(name, color).encode("utf-8")))
        painter = QPainter(cached)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        _cache[key] = cached
    return cached


def icon(name: str, color: str, size: int = 20, checked_color: str | None = None) -> QIcon:
    """`checked_color` verilirse işaretli durum o renkle çizilir (açık/kapalı anahtarlar)."""
    result = QIcon()
    result.addPixmap(pixmap(name, color, size), QIcon.Mode.Normal, QIcon.State.Off)
    result.addPixmap(
        pixmap(name, checked_color or color, size), QIcon.Mode.Normal, QIcon.State.On
    )
    return result


def icon_size(size: int = 20) -> QSize:
    return QSize(size, size)


def greeting_for_hour(hour: int) -> str:
    """Saate göre Türkçe selamlama; kişi adı bilinçli olarak yoktur (kullanıcı ayarı yok)."""
    if 5 <= hour < 12:
        return "Günaydın"
    if 12 <= hour < 18:
        return "İyi günler"
    if 18 <= hour < 23:
        return "İyi akşamlar"
    return "İyi geceler"
