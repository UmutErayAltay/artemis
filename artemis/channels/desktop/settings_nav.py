"""Ayar paneli bölüm gezinme ve arama yardımcıları (settings_panel.py'den ayrıldı)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QAbstractButton, QGroupBox, QLabel, QTabBar

from artemis.channels.desktop.settings_widgets import (
    _DIRTY_A11Y,
    _DIRTY_ROLE,
    _GROUP_ROLE,
    _HEADER_HEIGHT,
    _ITEM_HEIGHT,
    _fold,
)
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.settings_panel import SettingsPanel

_GROUP_TARGET_ROLE = Qt.ItemDataRole.UserRole + 2  # seçicide grup başlığı -> ilk sayfa satırı


def build_section_combo(panel: SettingsPanel) -> None:
    """Bölüm listesinin açılır kopyası: sayfa satırı veri=satır; grup başlığı ilk sayfaya gider."""
    bold = QFont(panel.section_combo.font())
    bold.setBold(True)
    for row, (group_name, first) in enumerate(panel._row_groups):
        if first:
            panel.section_combo.addItem(group_name)
            index = panel.section_combo.count() - 1
            panel.section_combo.setItemData(index, row, _GROUP_TARGET_ROLE)
            panel.section_combo.setItemData(index, bold, Qt.ItemDataRole.FontRole)
        panel.section_combo.addItem(panel.sections.item(row).text(), row)


def combo_picked(panel: SettingsPanel, index: int) -> None:
    row = panel.section_combo.itemData(index)
    if not isinstance(row, int):
        row = panel.section_combo.itemData(index, _GROUP_TARGET_ROLE)
    if isinstance(row, int):
        panel.sections.setCurrentRow(row)
        sync_combo(panel, row)  # başlık seçildiyse kutuda sayfa adı görünsün


def sync_combo(panel: SettingsPanel, row: int) -> None:
    panel.section_combo.setCurrentIndex(panel.section_combo.findData(row))


def set_compact(panel: SettingsPanel, compact: bool) -> None:
    """Dar panelde liste ve arama gizlenir; yerine 'Bölüm' seçici gelir, süzgeç temizlenir."""
    if compact == panel._compact:
        return
    panel._compact = compact
    if compact:
        panel.search.clear()
    panel.nav.setVisible(not compact)
    panel.section_combo.setVisible(compact)


def page_text(panel: SettingsPanel, row: int) -> str:
    """Sayfada görünen metinler: başlık, alt sekmeler, etiketler, kutu ve grup adları."""
    page = panel.stack.widget(row)
    assert page is not None
    parts = [panel.sections.item(row).text()]
    for bar in page.findChildren(QTabBar):
        parts.extend(bar.tabText(i) for i in range(bar.count()))
    parts += [label.text() for label in page.findChildren(QLabel)]
    parts += [button.text() for button in page.findChildren(QAbstractButton)]
    parts += [box.title() for box in page.findChildren(QGroupBox)]
    return _fold("\n".join(parts))


def filter_sections(panel: SettingsPanel, text: str) -> None:
    needle = _fold(text.strip())
    shown = [
        row for row in range(panel.sections.count())
        if not needle or needle in page_text(panel, row)
    ]
    seen: set[str] = set()
    for row in range(panel.sections.count()):
        item = panel.sections.item(row)
        item.setHidden(row not in shown)
        group = panel._row_groups[row][0]
        header = row in shown and group not in seen  # boş grubun başlığı da gizlenir
        if row in shown:
            seen.add(group)
        item.setData(_GROUP_ROLE, group if header else None)
        item.setSizeHint(QSize(0, _ITEM_HEIGHT + (_HEADER_HEIGHT if header else 0)))
    panel.no_results.setVisible(bool(needle) and not shown)
    if shown and panel.sections.currentRow() not in shown:
        panel.sections.setCurrentRow(shown[0])
    if needle and len(shown) == 1:
        panel.sections.setCurrentRow(shown[0])


def pick_first_match(panel: SettingsPanel) -> None:
    for row in range(panel.sections.count()):
        if not panel.sections.item(row).isHidden():
            panel.sections.setCurrentRow(row)
            return


def refresh_dirty_dots(panel: SettingsPanel) -> None:
    for row in range(panel.sections.count()):
        item = panel.sections.item(row)
        dirty = row in panel._dirty_rows
        item.setData(_DIRTY_ROLE, dirty)
        spoken = f"{item.text()}, {panel._row_groups[row][0]}"
        item.setData(
            Qt.ItemDataRole.AccessibleTextRole,
            spoken + (f", {t(_DIRTY_A11Y)}" if dirty else ""),
        )
        item.setToolTip(t(_DIRTY_A11Y) if dirty else "")
    panel.sections.viewport().update()


SECTION_ROUTES: dict[str, tuple[str, int | None]] = {
    _fold(key): value
    for key, value in {
        "ses": ("Ses", None),
        "voice": ("Ses", None),
        "dinleme": ("Ses", 0),
        "listening": ("Ses", 0),
        "dinleme (giriş)": ("Ses", 0),
        "listening (input)": ("Ses", 0),
        "mikrofon": ("Ses", 0),
        "microphone": ("Ses", 0),
        "tanıma": ("Ses", 1),
        "recognition": ("Ses", 1),
        "ses tanıma": ("Ses", 1),
        "speech recognition": ("Ses", 1),
        "yanıt": ("Ses", 2),
        "response": ("Ses", 2),
        "yanıt (çıkış)": ("Ses", 2),
        "response (output)": ("Ses", 2),
        "ses modelleri": ("Ses", 3),
        "voice models": ("Ses", 3),
        "modeller ve ölçüm": ("Ses", 3),
        "models & metrics": ("Ses", 3),
        "models and metrics": ("Ses", 3),
        "ses ölçümü": ("Ses", 3),
        "voice telemetry": ("Ses", 3),
        "bas-konuş": ("Dikte ve Kısayollar", None),
        "push-to-talk": ("Dikte ve Kısayollar", None),
        "dikte": ("Dikte ve Kısayollar", None),
        "dictation": ("Dikte ve Kısayollar", None),
        "kısayollar": ("Dikte ve Kısayollar", None),
        "shortcuts": ("Dikte ve Kısayollar", None),
        "hızlı açılış": ("Dikte ve Kısayollar", None),
        "quick launch": ("Dikte ve Kısayollar", None),
        "dikte ve kısayollar": ("Dikte ve Kısayollar", None),
        "dictation & shortcuts": ("Dikte ve Kısayollar", None),
        "model": ("Model ve Bağlantılar", None),
        "model ve bağlantılar": ("Model ve Bağlantılar", None),
        "models & connections": ("Model ve Bağlantılar", None),
        "modeller": ("Model ve Bağlantılar", 0),
        "models": ("Model ve Bağlantılar", 0),
        "modeller ve roller": ("Model ve Bağlantılar", 0),
        "models & roles": ("Model ve Bağlantılar", 0),
        "model ekle ve bağlantılar": ("Model ve Bağlantılar", 1),
        "add model & connections": ("Model ve Bağlantılar", 1),
        "görünüm": ("Görünüm", None),
        "appearance": ("Görünüm", None),
        "otomasyon": ("Otomasyon Kuralları", None),
        "automation": ("Otomasyon Kuralları", None),
        "otomasyon ve rutinler": ("Otomasyon Kuralları", None),
        "automation & routines": ("Otomasyon Kuralları", None),
        "automation and routines": ("Otomasyon Kuralları", None),
        "otomasyon kuralları": ("Otomasyon Kuralları", None),
        "automation rules": ("Otomasyon Kuralları", None),
        "rutinler": ("Otomasyon Kuralları", 0),
        "routines": ("Otomasyon Kuralları", 0),
        "tetikleyiciler": ("Otomasyon Kuralları", 1),
        "triggers": ("Otomasyon Kuralları", 1),
        "gün özeti": ("Otomasyon Kuralları", 2),
        "daily summary": ("Otomasyon Kuralları", 2),
        "araştırma": ("Otomasyon Kuralları", 3),
        "research": ("Otomasyon Kuralları", 3),
        "bilgi ve beceriler": ("Bilgi ve Beceriler", None),
        "knowledge & skills": ("Bilgi ve Beceriler", None),
        "beceriler": ("Bilgi ve Beceriler", 0),
        "skills": ("Bilgi ve Beceriler", 0),
        "dosya araması": ("Bilgi ve Beceriler", 1),
        "file search": ("Bilgi ve Beceriler", 1),
        "bağlantılar": ("Bağlantılar", None),
        "connections": ("Bağlantılar", None),
        "telegram": ("Bağlantılar", 0),
        "takvim": ("Bağlantılar", 1),
        "calendar": ("Bağlantılar", 1),
        "posta": ("Bağlantılar", 1),
        "mail": ("Bağlantılar", 1),
        "takvim ve posta": ("Bağlantılar", 1),
        "calendar & mail": ("Bağlantılar", 1),
        "calendar and mail": ("Bağlantılar", 1),
        "github": ("Bağlantılar", 2),
        "github ve kod ajanları": ("Bağlantılar", 2),
        "github & code agents": ("Bağlantılar", 2),
        "mcp": ("Bağlantılar", 3),
        "mcp sunucuları": ("Bağlantılar", 3),
        "mcp servers": ("Bağlantılar", 3),
        "uygulamalar": ("Uygulamalar ve Güvenlik", None),
        "applications": ("Uygulamalar ve Güvenlik", None),
        "güvenlik": ("Uygulamalar ve Güvenlik", None),
        "security": ("Uygulamalar ve Güvenlik", None),
        "gizlilik ve güvenlik": ("Uygulamalar ve Güvenlik", None),
        "privacy & security": ("Uygulamalar ve Güvenlik", None),
        "uygulamalar ve güvenlik": ("Uygulamalar ve Güvenlik", None),
        "applications & security": ("Uygulamalar ve Güvenlik", None),
        "hakkında": ("Hakkında ve Güncelleme", None),
        "about": ("Hakkında ve Güncelleme", None),
        "güncelleme": ("Hakkında ve Güncelleme", None),
        "updates": ("Hakkında ve Güncelleme", None),
        "hakkında ve güncelleme": ("Hakkında ve Güncelleme", None),
        "about & updates": ("Hakkında ve Güncelleme", None),
    }.items()
}
