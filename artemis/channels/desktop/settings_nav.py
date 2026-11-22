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
