"""Canlı dil geçişinde mevcut widget ağacının metinlerini yeniden hedefler.

t() kurulumda metni sabitler; canlı geçişte görünen metin ters sözlükle kaynağa bulunup yeni dile
çevrilir; değer içeren biçimli metinler bir sonraki güncellemede yeniden yazılır.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QPlainTextEdit,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QWidget,
)

from artemis.i18n import retarget


def _update_str(getter: object, setter: object, language: str) -> None:
    current = getter()  # type: ignore[operator]
    if not current:
        return
    updated = retarget(current, language)
    if updated != current:
        setter(updated)  # type: ignore[operator]


def _retranslate_widget(widget: QWidget, language: str) -> None:
    _update_str(widget.toolTip, widget.setToolTip, language)
    _update_str(widget.accessibleName, widget.setAccessibleName, language)
    _update_str(widget.accessibleDescription, widget.setAccessibleDescription, language)
    _update_str(widget.windowTitle, widget.setWindowTitle, language)
    _update_str(widget.statusTip, widget.setStatusTip, language)

    if isinstance(widget, QLabel):
        _update_str(widget.text, widget.setText, language)
    if isinstance(widget, QAbstractButton):
        _update_str(widget.text, widget.setText, language)
    if isinstance(widget, QLineEdit | QTextEdit | QPlainTextEdit):
        _update_str(widget.placeholderText, widget.setPlaceholderText, language)
    if isinstance(widget, QGroupBox):
        _update_str(widget.title, widget.setTitle, language)
    if isinstance(widget, QMenu):
        _update_str(widget.title, widget.setTitle, language)
    if isinstance(widget, QComboBox):
        _retranslate_combo(widget, language)
    if isinstance(widget, QTabWidget):
        _retranslate_tab_widget(widget, language)
    if isinstance(widget, QTabBar):
        _retranslate_tab_bar(widget, language)
    if isinstance(widget, QListWidget):
        _retranslate_list_widget(widget, language)
    if isinstance(widget, QTreeWidget):
        _retranslate_tree_widget(widget, language)


def _retranslate_combo(combo: QComboBox, language: str) -> None:
    blocked = combo.blockSignals(True)
    try:
        for index in range(combo.count()):
            current = combo.itemText(index)
            if not current:
                continue
            updated = retarget(current, language)
            if updated != current:
                combo.setItemText(index, updated)
    finally:
        combo.blockSignals(blocked)


def _retranslate_tab_widget(tabs: QTabWidget, language: str) -> None:
    for index in range(tabs.count()):
        current = tabs.tabText(index)
        if current:
            updated = retarget(current, language)
            if updated != current:
                tabs.setTabText(index, updated)
        tip = tabs.tabToolTip(index)
        if tip:
            updated_tip = retarget(tip, language)
            if updated_tip != tip:
                tabs.setTabToolTip(index, updated_tip)


def _retranslate_tab_bar(bar: QTabBar, language: str) -> None:
    for index in range(bar.count()):
        current = bar.tabText(index)
        if not current:
            continue
        updated = retarget(current, language)
        if updated != current:
            bar.setTabText(index, updated)


def _retranslate_list_widget(listing: QListWidget, language: str) -> None:
    for row in range(listing.count()):
        item = listing.item(row)
        if item is None:
            continue
        text = item.text()
        if text:
            updated = retarget(text, language)
            if updated != text:
                item.setText(updated)
        tip = item.toolTip()
        if tip:
            updated_tip = retarget(tip, language)
            if updated_tip != tip:
                item.setToolTip(updated_tip)


def _retranslate_tree_widget(tree: QTreeWidget, language: str) -> None:
    header = tree.headerItem()
    if header is not None:
        for col in range(tree.columnCount()):
            text = header.text(col)
            if text:
                updated = retarget(text, language)
                if updated != text:
                    header.setText(col, updated)
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        if item is None:
            continue
        for col in range(tree.columnCount()):
            text = item.text(col)
            if text:
                updated = retarget(text, language)
                if updated != text:
                    item.setText(col, updated)


def _retranslate_action(action: QAction, language: str) -> None:
    _update_str(action.text, action.setText, language)
    _update_str(action.toolTip, action.setToolTip, language)


def retranslate_widgets(roots: Iterable[QObject], language: str) -> None:
    """Verilen kök nesneleri ve tüm alt nesnelerini hedef dile yerinde çevirir."""
    seen: set[int] = set()
    for root in roots:
        if root is None:
            continue
        objects: list[QObject] = [root, *root.findChildren(QObject)]
        for obj in objects:
            oid = id(obj)
            if oid in seen:
                continue
            seen.add(oid)
            if isinstance(obj, QWidget):
                _retranslate_widget(obj, language)
            elif isinstance(obj, QAction):
                _retranslate_action(obj, language)
