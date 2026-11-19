"""Canlı dil geçişinde mevcut widget ağacının metinlerini yeniden hedefler.

Her metin yuvasının ilk gözlenen Türkçe kaynağı ve son yazılan çevirisi widget/öğe üzerinde
saklanır. Böylece aynı İngilizce karşılığı paylaşan ("Test et"/"Test", "Vazgeç"/"İptal" gibi)
anahtarlar dil geçişlerinde bozulmaz; uygulama kodu son geçişten sonra metni değiştirirse
yeni metin fark edilip kaynağı yeniden çözülür.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from typing import cast

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from artemis.i18n import resolve_source, translate_source

# Widget ve öğe üzerindeki i18n durum yuvaları; uygulamanın kendi özellik/rolleriyle çakışmaz.
_STATE_PROP = "_artemis_i18n_state"
_CONTEXT_PROP = "_artemis_i18n_context"
_ITEM_STATE_ROLE = int(Qt.ItemDataRole.UserRole) + 4100
_ITEM_CONTEXT_ROLE = int(Qt.ItemDataRole.UserRole) + 4101

_Record = tuple[str, str]  # (kaynak_metin, son_yazılan_metin)


def set_i18n_context(target: QObject, context: str) -> None:
    """Canlı dil geçişinde kullanılacak çeviri bağlamını nesneye bağlar."""
    target.setProperty(_CONTEXT_PROP, context)


def get_i18n_context(target: QObject) -> str | None:
    """Nesneye bağlı çeviri bağlamını döndürür (yoksa None)."""
    raw = target.property(_CONTEXT_PROP)
    return raw if isinstance(raw, str) and raw else None


def _next_text(
    current: str,
    record: _Record | None,
    language: str,
    context: str | None,
) -> tuple[str, _Record | None]:
    """Mevcut metin ve saklı kayıttan yeni metni ve güncel kaydı hesaplar.

    Metin son yazdığımızdan farklıysa uygulama kodu değiştirmiştir; eski kayıt atılır ve
    yeni kaynak çözülür. Katalog dışı kullanıcı verisi (ör. kişi/dosya adı) değişmeden kalır.
    """
    if not current:
        return current, None
    if record is not None and record[1] == current:
        source = record[0]
    else:
        resolved = resolve_source(current, context=context)
        if resolved is None:
            return current, None
        source = resolved
    updated = translate_source(source, language, context=context)
    return updated, (source, updated)


def _get_object_state(obj: QObject) -> dict[str, _Record]:
    raw = obj.property(_STATE_PROP)
    if isinstance(raw, dict):
        return dict(cast(dict[str, _Record], raw))
    return {}


def _save_object_state(obj: QObject, state: dict[str, _Record]) -> None:
    obj.setProperty(_STATE_PROP, state)


def _resolve_object_slot(
    obj: QObject,
    slot: str,
    current: str,
    language: str,
    context: str | None = None,
) -> str | None:
    """QObject üzerindeki tek metin yuvasını günceller; değiştiyse yeni metni döndürür."""
    state = _get_object_state(obj)
    old_record = state.get(slot)
    if not current and old_record is None:
        return None
    effective_ctx = context if context is not None else get_i18n_context(obj)
    updated, new_record = _next_text(current, old_record, language, effective_ctx)
    if new_record is None:
        if slot in state:
            del state[slot]
            _save_object_state(obj, state)
    elif new_record != old_record:
        state[slot] = new_record
        _save_object_state(obj, state)
    return updated if updated != current else None


def _update_slot(
    obj: QObject,
    slot: str,
    getter: Callable[[], str],
    setter: Callable[[str], None],
    language: str,
    context: str | None = None,
) -> None:
    updated = _resolve_object_slot(obj, slot, getter(), language, context)
    if updated is not None:
        setter(updated)


def _retranslate_widget(
    widget: QWidget,
    language: str,
    done_tab_bars: set[int],
    done_actions: set[int],
) -> None:
    _update_slot(widget, "toolTip", widget.toolTip, widget.setToolTip, language)
    _update_slot(
        widget, "accessibleName", widget.accessibleName, widget.setAccessibleName, language
    )
    _update_slot(
        widget,
        "accessibleDescription",
        widget.accessibleDescription,
        widget.setAccessibleDescription,
        language,
    )
    _update_slot(widget, "windowTitle", widget.windowTitle, widget.setWindowTitle, language)
    _update_slot(widget, "statusTip", widget.statusTip, widget.setStatusTip, language)

    if isinstance(widget, QLabel):
        _update_slot(widget, "text", widget.text, widget.setText, language)
    if isinstance(widget, QAbstractButton):
        _update_slot(widget, "text", widget.text, widget.setText, language)
    if isinstance(widget, QLineEdit | QTextEdit | QPlainTextEdit):
        _update_slot(
            widget, "placeholderText", widget.placeholderText, widget.setPlaceholderText, language
        )
    if isinstance(widget, QGroupBox):
        _update_slot(widget, "title", widget.title, widget.setTitle, language)
    if isinstance(widget, QMenu):
        # QMenu.title() ve menuAction().text() aynı metni paylaşır; tek kez menuAction üzerinden
        # güncellenir ki alt menü eylemi aynı geçişte ikinci kez işlenip kaynağı bozmasın.
        menu_action = widget.menuAction()
        if menu_action is not None and id(menu_action) not in done_actions:
            _retranslate_action(
                menu_action, language, fallback_context=get_i18n_context(widget)
            )
            done_actions.add(id(menu_action))
    if isinstance(widget, QComboBox):
        _retranslate_combo(widget, language)
    if isinstance(widget, QTabWidget):
        _retranslate_tab_widget(widget, language, done_tab_bars)
    if isinstance(widget, QTabBar) and id(widget) not in done_tab_bars:
        _retranslate_tab_bar(widget, language)
        done_tab_bars.add(id(widget))
    if isinstance(widget, QListWidget):
        _retranslate_list_widget(widget, language)
    if isinstance(widget, QTreeWidget):
        _retranslate_tree_widget(widget, language)


def _retranslate_combo(combo: QComboBox, language: str) -> None:
    widget_ctx = get_i18n_context(combo)
    blocked = combo.blockSignals(True)
    try:
        for index in range(combo.count()):
            current = combo.itemText(index)
            raw_record = combo.itemData(index, _ITEM_STATE_ROLE)
            old_record: _Record | None = (
                cast(_Record, raw_record)
                if isinstance(raw_record, tuple) and len(raw_record) == 2
                else None
            )
            if not current and old_record is None:
                continue
            item_ctx_raw = combo.itemData(index, _ITEM_CONTEXT_ROLE)
            ctx = item_ctx_raw if isinstance(item_ctx_raw, str) and item_ctx_raw else widget_ctx
            updated, new_record = _next_text(current, old_record, language, ctx)
            if new_record != old_record:
                combo.setItemData(index, new_record, _ITEM_STATE_ROLE)
            if updated != current:
                combo.setItemText(index, updated)
    finally:
        combo.blockSignals(blocked)


def _retranslate_tab_widget(
    tabs: QTabWidget, language: str, done_tab_bars: set[int]
) -> None:
    # QTabWidget içindeki QTabBar aynı sekmeleri paylaşır; durum tek yerde (bar üzerinde)
    # tutulur ve aynı geçişte ikinci kez işlenmez.
    bar = tabs.tabBar()
    if bar is None or id(bar) in done_tab_bars:
        return
    _retranslate_tab_bar(bar, language, fallback_context=get_i18n_context(tabs))
    done_tab_bars.add(id(bar))


def _retranslate_tab_bar(
    bar: QTabBar, language: str, fallback_context: str | None = None
) -> None:
    ctx = get_i18n_context(bar) or fallback_context
    for index in range(bar.count()):
        updated_text = _resolve_object_slot(
            bar, f"tabText:{index}", bar.tabText(index), language, ctx
        )
        if updated_text is not None:
            bar.setTabText(index, updated_text)
        updated_tip = _resolve_object_slot(
            bar, f"tabToolTip:{index}", bar.tabToolTip(index), language, ctx
        )
        if updated_tip is not None:
            bar.setTabToolTip(index, updated_tip)


def _update_list_item(
    item: QListWidgetItem, language: str, widget_ctx: str | None
) -> None:
    raw_state = item.data(_ITEM_STATE_ROLE)
    state: dict[str, _Record] = (
        dict(cast(dict[str, _Record], raw_state)) if isinstance(raw_state, dict) else {}
    )
    item_ctx_raw = item.data(_ITEM_CONTEXT_ROLE)
    ctx = item_ctx_raw if isinstance(item_ctx_raw, str) and item_ctx_raw else widget_ctx
    changed_state = False

    for slot, getter, setter in (
        ("text", item.text, item.setText),
        ("toolTip", item.toolTip, item.setToolTip),
    ):
        current = getter()
        old_record = state.get(slot)
        if not current and old_record is None:
            continue
        updated, new_record = _next_text(current, old_record, language, ctx)
        if new_record is None:
            if slot in state:
                del state[slot]
                changed_state = True
        elif new_record != old_record:
            state[slot] = new_record
            changed_state = True
        if updated != current:
            setter(updated)

    if changed_state:
        item.setData(_ITEM_STATE_ROLE, state or None)


def _retranslate_list_widget(listing: QListWidget, language: str) -> None:
    widget_ctx = get_i18n_context(listing)
    blocked = listing.blockSignals(True)
    try:
        for row in range(listing.count()):
            item = listing.item(row)
            if item is not None:
                _update_list_item(item, language, widget_ctx)
    finally:
        listing.blockSignals(blocked)


def _update_tree_item(
    item: QTreeWidgetItem, columns: int, language: str, widget_ctx: str | None
) -> None:
    for col in range(columns):
        raw_state = item.data(col, _ITEM_STATE_ROLE)
        state: dict[str, _Record] = (
            dict(cast(dict[str, _Record], raw_state)) if isinstance(raw_state, dict) else {}
        )
        col_ctx_raw = item.data(col, _ITEM_CONTEXT_ROLE)
        ctx = col_ctx_raw if isinstance(col_ctx_raw, str) and col_ctx_raw else widget_ctx
        changed_state = False

        text_current = item.text(col)
        old_text = state.get("text")
        if text_current or old_text is not None:
            updated_text, new_text = _next_text(text_current, old_text, language, ctx)
            if new_text is None:
                if "text" in state:
                    del state["text"]
                    changed_state = True
            elif new_text != old_text:
                state["text"] = new_text
                changed_state = True
            if updated_text != text_current:
                item.setText(col, updated_text)

        tip_current = item.toolTip(col)
        old_tip = state.get("toolTip")
        if tip_current or old_tip is not None:
            updated_tip, new_tip = _next_text(tip_current, old_tip, language, ctx)
            if new_tip is None:
                if "toolTip" in state:
                    del state["toolTip"]
                    changed_state = True
            elif new_tip != old_tip:
                state["toolTip"] = new_tip
                changed_state = True
            if updated_tip != tip_current:
                item.setToolTip(col, updated_tip)

        if changed_state:
            item.setData(col, _ITEM_STATE_ROLE, state or None)

    for child_index in range(item.childCount()):
        child = item.child(child_index)
        if child is not None:
            _update_tree_item(child, columns, language, widget_ctx)


def _retranslate_tree_widget(tree: QTreeWidget, language: str) -> None:
    widget_ctx = get_i18n_context(tree)
    columns = tree.columnCount()
    blocked = tree.blockSignals(True)
    try:
        header = tree.headerItem()
        if header is not None:
            _update_tree_item(header, columns, language, widget_ctx)
        for index in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(index)
            if item is not None:
                _update_tree_item(item, columns, language, widget_ctx)
    finally:
        tree.blockSignals(blocked)


def _retranslate_action(
    action: QAction, language: str, fallback_context: str | None = None
) -> None:
    ctx = get_i18n_context(action) or fallback_context
    before_tip = action.toolTip()
    updated_text = _resolve_object_slot(action, "text", action.text(), language, ctx)
    if updated_text is not None:
        action.setText(updated_text)
    # Qt açıkça setToolTip çağrılmadıysa toolTip()'i text()'ten türetir; text değişince
    # toolTip de kendiliğinden değiştiyse ayrı ipucu yazıp bunu sabitlemeyiz.
    after_tip = action.toolTip()
    if after_tip != before_tip:
        state = _get_object_state(action)
        if "toolTip" in state:
            del state["toolTip"]
            _save_object_state(action, state)
    else:
        updated_tip = _resolve_object_slot(action, "toolTip", after_tip, language, ctx)
        if updated_tip is not None:
            action.setToolTip(updated_tip)
    updated_status = _resolve_object_slot(action, "statusTip", action.statusTip(), language, ctx)
    if updated_status is not None:
        action.setStatusTip(updated_status)


def _collect_objects(roots: Iterable[QObject]) -> list[QObject]:
    seen: set[int] = set()
    result: list[QObject] = []
    queue: deque[QObject] = deque(root for root in roots if root is not None)
    while queue:
        obj = queue.popleft()
        oid = id(obj)
        if oid in seen:
            continue
        seen.add(oid)
        result.append(obj)
        for child in obj.findChildren(QObject):
            if id(child) not in seen:
                queue.append(child)
        if isinstance(obj, QWidget):
            for action in obj.actions():
                if id(action) not in seen:
                    queue.append(action)
        elif isinstance(obj, QAction):
            submenu = obj.menu()
            if submenu is not None and id(submenu) not in seen:
                queue.append(submenu)
    return result


def retranslate_widgets(roots: Iterable[QObject], language: str) -> None:
    """Verilen kök nesneleri ve tüm alt nesnelerini hedef dile yerinde çevirir."""
    done_tab_bars: set[int] = set()
    done_actions: set[int] = set()
    for obj in _collect_objects(roots):
        if isinstance(obj, QWidget):
            _retranslate_widget(obj, language, done_tab_bars, done_actions)
        elif isinstance(obj, QAction) and id(obj) not in done_actions:
            submenu = obj.menu()
            fallback_ctx = get_i18n_context(submenu) if submenu is not None else None
            _retranslate_action(obj, language, fallback_context=fallback_ctx)
            done_actions.add(id(obj))
