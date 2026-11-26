"""Seçili metin penceresi: kapat düğmesi, Esc ve çalışırken kilit (ADR 0062)."""

from __future__ import annotations

import os
from typing import Any

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


def test_radial_menu_places_actions_around_anchor_and_resizes_cleanly() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QPoint

    _app, _parent, popup = _popup()
    popup.show_ready("12 karakter seçildi.", anchor=QPoint(400, 300))
    assert popup._mode == "radial"
    assert not popup.radial_container.isHidden()
    assert popup.card_container.isHidden()

    c = popup.close_button.geometry().center()
    assert popup.buttons["translate"].geometry().center().y() < c.y()
    assert popup.buttons["summarize"].geometry().center().y() > c.y()
    assert popup.buttons["explain"].geometry().center().x() < c.x()
    assert popup.buttons["fix"].geometry().center().x() > c.x()

    popup_center = popup.geometry().center()
    assert abs(popup_center.x() - 400) <= 60
    assert abs(popup_center.y() - 300) <= 60

    popup.show_working()
    assert popup._mode == "card"
    assert popup.radial_container.isHidden()
    assert not popup.card_container.isHidden()
    assert popup.width() == 340

    popup.show_ready("5 karakter seçildi.", anchor=QPoint(420, 310))
    assert popup._mode == "radial"
    assert popup.buttons["translate"].isVisibleTo(popup)


def test_selection_badge_activates_without_focus_steal_and_follows_theme() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QWidget

    from artemis.channels.desktop import themes
    from artemis.channels.desktop.selection_mouse import SelectionCandidate
    from artemis.channels.desktop.selection_popup import SelectionBadge

    QApplication.instance() or QApplication([])
    parent = QWidget()
    activated: list[SelectionCandidate] = []
    badge = SelectionBadge(parent, on_activate=activated.append)
    assert bool(badge.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
    assert badge.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    cand = SelectionCandidate(x=320, y=240, hwnd=77, pre_text="Selam")
    badge.show_for(cand)
    assert badge.isVisible()
    center = badge.frameGeometry().center()
    assert badge.contains_global(center.x(), center.y())

    badge.apply_theme(themes.THEMES["light"])
    press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(16, 16),
        QPointF(center),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    badge.mousePressEvent(press)
    assert not badge.isVisible()
    assert activated == [cand]


def test_radial_popup_has_transparent_background_and_no_close_focus_with_global_theme() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QColor, QPixmap
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop import themes
    from artemis.channels.desktop.selection_popup import SelectionPopup

    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    prev_sheet = app.styleSheet()
    popup: SelectionPopup | None = None
    try:
        for theme_name in ("dark", "light"):
            app.setStyleSheet(themes.stylesheet(theme_name))
            popup = SelectionPopup(None, on_action=lambda _name: None, on_write=lambda: None)
            popup.apply_theme(themes.THEMES[theme_name])
            popup.show_ready("4 karakter seçildi.", anchor=QPoint(300, 260))
            app.processEvents()

            assert popup.close_button.focusPolicy() == Qt.FocusPolicy.NoFocus
            assert popup.radial_container.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            assert popup.card_container.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            assert popup.result_row.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

            canvas = QPixmap(popup.size())
            canvas.fill(Qt.GlobalColor.transparent)
            popup.render(canvas)
            image = canvas.toImage()
            assert QColor.fromRgba(image.pixel(0, 0)).alpha() == 0
            assert QColor.fromRgba(image.pixel(2, 2)).alpha() == 0
            assert popup.close_button.width() >= 28
            assert popup.close_button.height() >= 28
            popup.close()
    finally:
        if popup is not None:
            popup.close()
        app.setStyleSheet(prev_sheet)


def test_contains_global_is_thread_safe_without_touching_qt_widgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    pytest.importorskip("PySide6")
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.selection_mouse import SelectionCandidate
    from artemis.channels.desktop.selection_popup import SelectionBadge, SelectionPopup

    app = QApplication.instance() or QApplication([])
    badge = SelectionBadge(None, on_activate=lambda _c: None)
    popup = SelectionPopup(None, on_action=lambda _a: None, on_write=lambda: None)
    try:
        badge.show_for(SelectionCandidate(x=320, y=240, hwnd=77, pre_text="Selam"))
        popup.show_ready("4 karakter seçildi.", anchor=QPoint(400, 300))
        app.processEvents()

        b_center = badge.frameGeometry().center()
        p_center = popup.frameGeometry().center()

        # Arka plan iş parçacığından contains_global çağrılınca isVisible/frameGeometry
        # çağrılmamalı (çağrılırsa AssertionError ile patlat).
        def forbid_qt(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("Qt widget yöntemi başka iş parçacığından çağrıldı")

        monkeypatch.setattr(badge, "isVisible", forbid_qt)
        monkeypatch.setattr(badge, "frameGeometry", forbid_qt)
        monkeypatch.setattr(popup, "isVisible", forbid_qt)
        monkeypatch.setattr(popup, "frameGeometry", forbid_qt)

        results: list[bool] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(badge.contains_global(b_center.x(), b_center.y()))
                results.append(badge.contains_global(-9999, -9999))
                results.append(popup.contains_global(p_center.x(), p_center.y()))
                results.append(popup.contains_global(-9999, -9999))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(2.0)
        assert not errors
        assert results == [True, False, True, False]
    finally:
        monkeypatch.undo()
        badge.close()
        badge.deleteLater()
        popup.close()
        popup.deleteLater()
        app.processEvents()


