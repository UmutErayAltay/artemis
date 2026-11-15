""""?" baloncuğu: üstüne gelince açılır, ayrılınca kapanır; tıklayınca sabitlenir."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QEnterEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from artemis.channels.desktop.widgets import HelpTip


@pytest.fixture
def parts() -> tuple[QApplication, QWidget, HelpTip, QLabel]:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    window = QWidget()
    layout = QVBoxLayout(window)
    tip = HelpTip("Açıklama")
    other = QLabel("başka")
    layout.addWidget(tip)
    layout.addWidget(other)
    window.show()
    app.processEvents()
    return app, window, tip, other


def _enter(tip: HelpTip) -> None:
    point = QPointF(2, 2)
    QApplication.sendEvent(tip, QEnterEvent(point, point, point))


def _leave(tip: HelpTip) -> None:
    QApplication.sendEvent(tip, QEvent(QEvent.Type.Leave))


def test_hover_opens_and_leave_closes(parts) -> None:
    _app, window, tip, _other = parts
    _enter(tip)
    assert tip.is_open()
    _leave(tip)
    assert not tip.is_open()
    window.close()


def test_click_pins_until_outside_click_escape_or_second_click(parts) -> None:
    _app, window, tip, other = parts
    tip.click()
    _leave(tip)
    assert tip.is_open() and tip.isChecked()  # sabit: fare ayrılınca kapanmaz
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(1, 1), QPointF(1, 1), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(other, press)
    assert not tip.is_open() and not tip.isChecked()
    tip.click()
    QApplication.sendEvent(
        window, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    assert not tip.is_open()
    tip.click()
    tip.click()
    assert not tip.is_open()
    window.close()


def test_only_one_pinned_and_hiding_page_closes(parts) -> None:
    _app, window, tip, _other = parts
    second = HelpTip("İkinci")
    window.layout().addWidget(second)
    _app.processEvents()  # yeni düğme gerçekten görünür olsun (gizlenme olayını alsın)
    tip.click()
    second.click()
    assert second.is_open() and not tip.isChecked() and not tip.is_open()
    window.hide()
    assert not second.is_open() and not second.isChecked()
    window.close()


def test_native_tooltip_suppressed(parts) -> None:
    _app, window, tip, _other = parts
    assert tip.event(QEvent(QEvent.Type.ToolTip)) is True  # Qt ipucu ikinci kutu açmaz
    window.close()


def test_second_click_closes_even_while_hovered(parts) -> None:
    _app, window, tip, _other = parts
    _enter(tip)
    tip.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)  # imleç düğmenin üstünde
    tip.click()
    assert tip.is_open() and tip.isChecked()
    tip.click()
    assert not tip.is_open() and not tip.isChecked()
    window.close()
