"""Seçili metin penceresi: kapat düğmesi, Esc ve çalışırken kilit (ADR 0062)."""

from __future__ import annotations

import os

import pytest


def _popup():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QWidget

    from artemis.channels.desktop.selection_popup import SelectionPopup

    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    popup = SelectionPopup(parent, on_action=lambda _name: None, on_write=lambda: None)
    return app, parent, popup


def test_close_button_hides_popup_and_is_named() -> None:
    _app, _parent, popup = _popup()
    popup.show_ready("3 karakter seçildi.")
    assert popup.isVisible()
    assert popup.close_button.accessibleName() == "Kapat"
    assert popup.close_button.toolTip() == "Kapat"
    popup.close_button.click()
    assert not popup.isVisible()


def test_escape_closes_popup() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    _app, _parent, popup = _popup()
    popup.show_ready("3 karakter seçildi.")
    popup.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    assert not popup.isVisible()


def test_working_disables_actions_and_keeps_result_box() -> None:
    _app, _parent, popup = _popup()
    popup.show_ready("412 karakter seçildi.")
    popup.show_working()
    assert popup.info.text() == "Çalışıyor…"
    assert not popup.result.isHidden()
    assert not popup.result_row.isHidden()
    for button in (*popup.buttons.values(), popup.copy_button, popup.write_button):
        assert not button.isEnabled()
    popup.show_result("Merhaba")
    assert popup.info.text() == "Sonuç hazır."
    assert all(button.isEnabled() for button in popup.buttons.values())
    assert popup.copy_button.isEnabled() and popup.write_button.isEnabled()
