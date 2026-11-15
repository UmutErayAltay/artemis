"""Qt bağlantısında self'i yakalayan lambda/partial referans döngüsü kurar; gc bunu GUI dışı
bir thread'de çözerse Qt silinmiş çocuklara dokunup çöker (0638b18). gc kapalıyken nesne ölmeli."""

from __future__ import annotations

import gc
import os
import weakref
from pathlib import Path
from typing import Any

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from artemis.channels.desktop.automation_panel import AutomationPanel
from artemis.channels.desktop.selection_popup import SelectionPopup
from artemis.channels.desktop.settings_panel import SettingsPanel


def _app() -> QApplication:
    current = QApplication.instance()
    return current if isinstance(current, QApplication) else QApplication([])


def _assert_dies_without_gc(holder: list[Any]) -> None:
    """Çağıranın yerel değişkeni nesneyi diri tutmasın diye tek elemanlı liste alır."""
    app = _app()
    widget = holder.pop()
    ref = weakref.ref(widget)
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    # Önceki durum geri yüklenir: gui_gc otomatik GC'yi kapalı tutar, burada açılırsa
    # kalan testlerde döngüler köprü iş parçacığında toplanıp Qt'yi çökertir.
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        del widget
        assert ref() is None  # döngü olsaydı gc.collect olmadan yaşamaya devam ederdi
    finally:
        if was_enabled:
            gc.enable()


def test_automation_panel_has_no_cycle() -> None:
    _app()
    panel = AutomationPanel(demo=True)
    panel.show()  # showEvent demo anlık görüntüyü çizer: satır düğmeleri bağlanır
    panel.routines.link.click()
    for button in (*panel.run_buttons.values(), *panel.report_buttons.values()):
        button.click()
    assert panel.run_buttons or panel.summary_buttons
    holder = [panel]
    del panel, button
    _assert_dies_without_gc(holder)


def test_selection_popup_has_no_cycle() -> None:
    _app()
    calls: list[Any] = []
    parent = QWidget()
    popup = SelectionPopup(parent, on_action=calls.append, on_write=lambda: None)
    popup.show_ready("3 karakter seçildi.")
    next(iter(popup.buttons.values())).click()
    assert len(calls) == 1
    popup.close_button.click()
    holder = [popup]
    del popup
    _assert_dies_without_gc(holder)


def test_triggers_section_and_dialog_have_no_cycle(tmp_path: Path) -> None:
    from artemis.channels.desktop.trigger_dialog import TriggerDialog
    from artemis.channels.desktop.triggers_section import TriggersSection

    _app()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "workspace": "desktop", "state_dir": "state", "audit_path": "audit.jsonl",
        "primary_model": "first",
        "models": {"first": {"provider": "openrouter", "model": "first-model",
                             "input_per_million": "0", "output_per_million": "0"}},
        "triggers": {"enabled": True, "items": [{"name": "bir", "folder": str(tmp_path)}]},
    }), encoding="utf-8")
    section = TriggersSection(path)
    section.proactive_button.click()
    dialog = TriggerDialog(None, [], ["rutin"], "", section)
    dialog.suggest_radio.setChecked(True)
    dialog.quick_buttons[0].click()
    dialog.submit()
    holder = [dialog]
    del dialog
    _assert_dies_without_gc(holder)
    holder = [section]
    del section
    _assert_dies_without_gc(holder)


def test_settings_panel_has_no_cycle(tmp_path: Path) -> None:
    _app()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "workspace": "desktop", "state_dir": "state", "audit_path": "audit.jsonl",
        "primary_model": "first",
        "models": {"first": {"provider": "openrouter", "model": "first-model",
                             "input_per_million": "0", "output_per_million": "0"}},
    }), encoding="utf-8")
    panel = SettingsPanel(path)
    seen: list[str] = []
    panel.theme_changed.connect(seen.append)
    card = next(iter(panel.theme_cards.values()))
    card.radio.setChecked(True)
    assert seen
    holder = [panel]
    del panel, card
    _assert_dies_without_gc(holder)
