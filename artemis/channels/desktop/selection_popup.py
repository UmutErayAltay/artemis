"""Seçili metin eylemleri için fare simgesi ve çevresel/kart menü (ADR 0062).

Metin seçimi bitince imlecin yanında odak almayan küçük simge (`SelectionBadge`) açılır;
tıklanınca veya kısayola basılınca farenin etrafında `Çevir / Düzelt / Özetle / Açıkla`
düğmeleri radyal olarak belirir, bir eylem seçilince sonuca uygun kompakt karta dönüşür.
"Kopyala" sonucu panoya GUI tarafında yazar (kullanıcının açık isteği).
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any, Literal

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop import themes
from artemis.channels.desktop.page_kit import repolish
from artemis.channels.desktop.selection import ACTION_LABELS, ACTIONS
from artemis.channels.desktop.selection_mouse import SelectionCandidate
from artemis.i18n import t

WIDTH = 340
_BADGE_SIZE = 32
_BADGE_HIDE_MS = 5000
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_ICON = Path(__file__).with_name("artemis.ico")


def _apply_win32_noactivate(widget: QWidget) -> None:
    """Windows'ta araç penceresinin tıklamada odak çalmamasını sağlar."""
    if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
        return
    with contextlib.suppress(OSError, ValueError, RuntimeError, ctypes.ArgumentError):
        hwnd = int(widget.winId())
        if hwnd <= 0:
            return
        loader_name = "WinDLL"
        user32: Any = getattr(ctypes, loader_name)("user32", use_last_error=True)
        get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t
        current = int(get_style(hwnd, _GWL_EXSTYLE))
        set_style(hwnd, _GWL_EXSTYLE, current | _WS_EX_NOACTIVATE)


def _clamp_to_screen(point: QPoint, size: QSize) -> QPoint:
    screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
    if screen is None:
        return point
    area = screen.availableGeometry()
    max_x = max(area.left(), area.right() - max(size.width(), 1))
    max_y = max(area.top(), area.bottom() - max(size.height(), 1))
    x = min(max(point.x(), area.left()), max_x)
    y = min(max(point.y(), area.top()), max_y)
    return QPoint(x, y)


class SelectionBadge(QWidget):
    """Metin seçimi sonrası imleç yanında beliren odak-almayan küçük simge."""

    def __init__(
        self,
        parent: QWidget | None,
        on_activate: Callable[[SelectionCandidate], object],
    ) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(_BADGE_SIZE, _BADGE_SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(t("Seçili metin eylemlerini aç"))
        self.setToolTip(t("Seçili metin eylemleri (Çevir, Düzelt, Özetle, Açıkla)"))
        self._on_activate = on_activate
        self._palette = themes.THEMES[themes.DEFAULT_THEME]
        self._pixmap = QIcon(str(_ICON)).pixmap(18, 18)
        self._hovered = False
        self._candidate: SelectionCandidate | None = None
        self._bounds_lock = threading.Lock()
        self._global_rect: tuple[int, int, int, int] | None = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_BADGE_HIDE_MS)
        self._hide_timer.timeout.connect(self.hide)

    def apply_theme(self, palette: dict[str, str]) -> None:
        self._palette = palette
        self.update()

    def show_for(self, candidate: SelectionCandidate) -> None:
        self._candidate = candidate
        target = QPoint(candidate.x + 12, candidate.y - (_BADGE_SIZE + 8))
        self.move(_clamp_to_screen(target, self.size()))
        self.show()
        self.raise_()
        self._hide_timer.start()

    def _sync_global_rect(self) -> None:
        if self.isVisible():
            rect = self.frameGeometry()
            bounds: tuple[int, int, int, int] | None = (
                rect.x(),
                rect.y(),
                rect.width(),
                rect.height(),
            )
        else:
            bounds = None
        with self._bounds_lock:
            self._global_rect = bounds

    def contains_global(self, x: int, y: int) -> bool:
        # Fare izleyici iş parçacığından çağrılır; Qt widget'ına doğrudan erişmez.
        with self._bounds_lock:
            rect = self._global_rect
        if rect is None:
            return False
        rx, ry, rw, rh = rect
        return rx <= x < rx + rw and ry <= y < ry + rh

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        _apply_win32_noactivate(self)
        self._sync_global_rect()

    def hideEvent(self, event: Any) -> None:
        super().hideEvent(event)
        self._sync_global_rect()

    def moveEvent(self, event: Any) -> None:
        super().moveEvent(event)
        self._sync_global_rect()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._sync_global_rect()

    def enterEvent(self, event: Any) -> None:
        self._hovered = True
        self._hide_timer.stop()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._hovered = False
        if self.isVisible():
            self._hide_timer.start()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._candidate is not None:
            candidate = self._candidate
            self._hide_timer.stop()
            self.hide()
            self._on_activate(candidate)
            return
        super().mousePressEvent(event)

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen_color = self._palette["accent"] if self._hovered else self._palette["border"]
        p.setPen(QPen(QColor(pen_color), 1.4))
        p.setBrush(QColor(self._palette["surface"]))
        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        p.drawEllipse(rect)
        px_x = (self.width() - self._pixmap.width()) // 2
        px_y = (self.height() - self._pixmap.height()) // 2
        p.drawPixmap(px_x, px_y, self._pixmap)
        p.end()


class SelectionPopup(QWidget):
    """Radyal eylem menüsü + sonuç kartı."""

    def __init__(
        self,
        parent: QWidget | None,
        on_action: Callable[[str], object],
        on_write: Callable[[], object],
    ) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._on_action = on_action
        self._on_write = on_write
        self._palette = themes.THEMES[themes.DEFAULT_THEME]
        self._mode: Literal["radial", "card"] = "card"
        self._anchor: QPoint | None = None
        self._bounds_lock = threading.Lock()
        self._global_rect: tuple[int, int, int, int] | None = None
        self.setAccessibleName(t("Artemis seçili metin eylemleri"))

        self.header_icon = QLabel(self)
        self.header_icon.setPixmap(QIcon(str(_ICON)).pixmap(20, 20))
        self.title = QLabel(t("Seçili metin"), self)
        self.title.setObjectName("title")
        self.info = QLabel(self)
        self.info.setObjectName("meta")
        self.info.setWordWrap(True)
        self.close_button = QPushButton("×", self)
        self.close_button.setObjectName("close")
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setFixedSize(24, 24)
        self.close_button.setAccessibleName(t("Kapat"))
        self.close_button.setToolTip(t("Kapat"))
        self.close_button.clicked.connect(self.hide)

        self.buttons: dict[str, QPushButton] = {}
        for action in ACTIONS:
            label = t(ACTION_LABELS[action])
            button = QPushButton(label, self)
            button.setAccessibleName(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setProperty("action", action)
            button.clicked.connect(self._action_clicked)
            self.buttons[action] = button

        self.result = QPlainTextEdit(self)
        self.result.setReadOnly(True)
        self.result.setAccessibleName(t("Eylem sonucu"))
        self.result.setFixedHeight(120)
        self.result.hide()
        self.copy_button = QPushButton(t("Kopyala"), self)
        self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_button.clicked.connect(self._copy)
        self.write_button = QPushButton(t("Yerine yaz"), self)
        self.write_button.setObjectName("primaryAction")
        self.write_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.write_button.clicked.connect(self._write)
        self.result_row = QWidget(self)
        self.result_row.setObjectName("resultRow")
        self.result_row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        res_row = QHBoxLayout(self.result_row)
        res_row.setContentsMargins(0, 0, 0, 0)
        res_row.setSpacing(6)
        res_row.addWidget(self.copy_button)
        res_row.addWidget(self.write_button)
        self.result_row.hide()

        self.radial_container = QWidget(self)
        self.radial_container.setObjectName("radialContainer")
        self.radial_container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.radial_grid = QGridLayout(self.radial_container)
        self.radial_grid.setContentsMargins(2, 2, 2, 2)
        self.radial_grid.setHorizontalSpacing(6)
        self.radial_grid.setVerticalSpacing(6)
        self.radial_container.hide()

        self.card_container = QWidget(self)
        self.card_container.setObjectName("cardContainer")
        self.card_container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        card_layout = QVBoxLayout(self.card_container)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(8)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self.title)
        text.addWidget(self.info)
        self.header_row = QHBoxLayout()
        self.header_row.setSpacing(8)
        self.header_row.addWidget(self.header_icon, 0, Qt.AlignmentFlag.AlignTop)
        self.header_row.addLayout(text, 1)
        self.card_actions = QHBoxLayout()
        self.card_actions.setSpacing(5)
        card_layout.addLayout(self.header_row)
        card_layout.addLayout(self.card_actions)
        card_layout.addWidget(self.result)
        card_layout.addWidget(self.result_row)

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(14, 12, 14, 12)
        self._main_layout.setSpacing(0)
        self._main_layout.addWidget(self.radial_container)
        self._main_layout.addWidget(self.card_container)

        self._set_mode("card")
        self.apply_theme(self._palette)

    def _set_mode(self, mode: Literal["radial", "card"]) -> None:
        self._mode = mode
        if mode == "radial":
            self.card_container.hide()
            self.header_row.removeWidget(self.close_button)
            for action in ACTIONS:
                self.card_actions.removeWidget(self.buttons[action])
            self.radial_container.show()
            self.radial_grid.addWidget(
                self.buttons["translate"], 0, 1, Qt.AlignmentFlag.AlignCenter
            )
            self.radial_grid.addWidget(
                self.buttons["explain"], 1, 0, Qt.AlignmentFlag.AlignCenter
            )
            self.radial_grid.addWidget(
                self.close_button, 1, 1, Qt.AlignmentFlag.AlignCenter
            )
            self.radial_grid.addWidget(
                self.buttons["fix"], 1, 2, Qt.AlignmentFlag.AlignCenter
            )
            self.radial_grid.addWidget(
                self.buttons["summarize"], 2, 1, Qt.AlignmentFlag.AlignCenter
            )
            for button in self.buttons.values():
                button.setObjectName("radialAction")
                button.setMinimumWidth(72)
                button.show()
                repolish(button)
            self.close_button.setFixedSize(28, 28)
            self.close_button.setProperty("radial", True)
            self.close_button.show()
            repolish(self.close_button)
            self.setMinimumWidth(0)
            self.setMaximumWidth(16777215)
            self._main_layout.setContentsMargins(6, 6, 6, 6)
            self.radial_grid.activate()
            self._main_layout.activate()
            self.resize(self.sizeHint())
        else:
            self.radial_container.hide()
            for widget in (*self.buttons.values(), self.close_button):
                self.radial_grid.removeWidget(widget)
            self.card_container.show()
            self.header_row.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
            for action in ACTIONS:
                self.card_actions.addWidget(self.buttons[action])
            for button in self.buttons.values():
                button.setObjectName("")
                button.setMinimumWidth(0)
                button.show()
                repolish(button)
            self.close_button.setFixedSize(24, 24)
            self.close_button.setProperty("radial", False)
            self.close_button.show()
            repolish(self.close_button)
            self.setFixedWidth(WIDTH)
            self._main_layout.setContentsMargins(14, 12, 14, 12)
            self._main_layout.activate()
            self.resize(self.sizeHint())
        self.update()

    def apply_theme(self, palette: dict[str, str]) -> None:
        self._palette = palette
        self.setStyleSheet(
            "QWidget { background: transparent; }"
            "#radialContainer, #cardContainer, #resultRow"
            " { background: transparent; border: none; }"
            f"QLabel {{ color: {palette['text']}; font-size: 12px; background: transparent; }}"
            f"QLabel#title {{ font-size: 13px; font-weight: 600; }}"
            f"QLabel#meta {{ color: {palette['muted']}; font-size: 12px; }}"
            f"QPlainTextEdit {{ color: {palette['text']}; background: {palette['soft']};"
            f" border: 1px solid {palette['control_border']}; border-radius: 8px;"
            f" padding: 6px 8px; font-size: 13px; }}"
            f"QPushButton {{ color: {palette['text']}; background: {palette['soft']};"
            f" border: 1px solid {palette['control_border']}; border-radius: 7px;"
            f" min-height: 22px; padding: 3px 6px; font-size: 12px; }}"
            f"QPushButton:hover {{ border-color: {palette['accent']};"
            f" background: {palette['accent_soft']}; }}"
            f"QPushButton:focus {{ border: 1px solid {palette['accent']}; padding: 3px 6px; }}"
            f"QPushButton#primaryAction {{ color: {palette['accent_text']};"
            f" background: {palette['accent']}; border: 1px solid {palette['accent']};"
            f" padding: 3px 10px; font-weight: 600; }}"
            f"QPushButton#primaryAction:hover {{ background: {palette['accent']}; }}"
            f"QPushButton#radialAction {{ color: {palette['text']};"
            f" background: {palette['surface']}; border: 1px solid {palette['control_border']};"
            f" border-radius: 15px; min-height: 22px; padding: 4px 12px;"
            f" font-size: 12px; font-weight: 600; }}"
            f"QPushButton#radialAction:hover {{ border-color: {palette['accent']};"
            f" background: {palette['accent_soft']}; }}"
            f"QPushButton#radialAction:pressed {{ background: {palette['accent']};"
            f" color: {palette['accent_text']}; border-color: {palette['accent']}; }}"
            f"QPushButton#radialAction:focus {{ border: 1px solid {palette['accent']};"
            f" padding: 4px 12px; }}"
            f"QPushButton:disabled {{ color: {palette['muted']};"
            f" border-color: {palette['border']}; background: transparent; }}"
            f"QPushButton#close {{ color: {palette['muted']}; background: transparent;"
            f" border: none; border-radius: 12px; min-width: 24px; max-width: 24px;"
            f" min-height: 24px; max-height: 24px; padding: 0px;"
            f" font-size: 15px; font-weight: 500; }}"
            f"QPushButton#close:hover {{ color: {palette['text']};"
            f" background: {palette['soft']}; }}"
            f'QPushButton#close[radial="true"] {{ background: {palette["surface"]};'
            f' border: 1px solid {palette["control_border"]}; border-radius: 14px;'
            f" min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px;"
            f" padding: 0px; }}"
            f'QPushButton#close[radial="true"]:hover {{ color: {palette["text"]};'
            f' border-color: {palette["accent"]}; background: {palette["accent_soft"]}; }}'
        )
        self.update()

    def _sync_global_rect(self) -> None:
        if self.isVisible():
            rect = self.frameGeometry()
            bounds: tuple[int, int, int, int] | None = (
                rect.x(),
                rect.y(),
                rect.width(),
                rect.height(),
            )
        else:
            bounds = None
        with self._bounds_lock:
            self._global_rect = bounds

    def contains_global(self, x: int, y: int) -> bool:
        # Fare izleyici iş parçacığından çağrılır; Qt widget'ına doğrudan erişmez.
        with self._bounds_lock:
            rect = self._global_rect
        if rect is None:
            return False
        rx, ry, rw, rh = rect
        return rx <= x < rx + rw and ry <= y < ry + rh

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._sync_global_rect()

    def hideEvent(self, event: Any) -> None:
        super().hideEvent(event)
        self._sync_global_rect()

    def moveEvent(self, event: Any) -> None:
        super().moveEvent(event)
        self._sync_global_rect()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._sync_global_rect()

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.result.toPlainText())
        self.info.setText(t("Kopyalandı."))

    def _action_clicked(self) -> None:
        if (button := self.sender()) is not None:
            self._on_action(str(button.property("action")))

    def _write(self) -> None:
        self.hide()  # odak hedef pencereye dönebilsin; başarısızsa sonuç yeniden gösterilir
        self._on_write()

    def _busy(self, busy: bool) -> None:
        for button in (*self.buttons.values(), self.copy_button, self.write_button):
            button.setEnabled(not busy)

    def show_ready(self, info: str, *, anchor: QPoint | None = None) -> None:
        """Yeni seçim: önceki sonuç silinir, farenin etrafında radyal menü açılır."""
        self._anchor = QPoint(anchor) if anchor is not None else QCursor.pos()
        self.result.clear()
        self.result.hide()
        self.result_row.hide()
        self.info.setText(info)
        self._busy(False)
        self._set_mode("radial")
        self.adjustSize()
        size = self.sizeHint()
        centered = QPoint(
            self._anchor.x() - size.width() // 2,
            self._anchor.y() - size.height() // 2,
        )
        self.move(_clamp_to_screen(centered, size))
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def show_working(self) -> None:
        # Sonuç kutusu boş da olsa görünür kalır: sonuç gelince pencere boyu değişmez.
        was_radial = self._mode == "radial"
        self._set_mode("card")
        self.result.clear()
        self.result.show()
        self.result_row.show()
        self.info.setText(t("Çalışıyor…"))
        self._busy(True)
        self._present_card(reposition=was_radial)

    def show_result(self, text: str) -> None:
        was_radial = self._mode == "radial"
        self._set_mode("card")
        self.result.setPlainText(text)
        self.result.show()
        self.result_row.show()
        self.info.setText(t("Sonuç hazır."))
        self._busy(False)
        self._present_card(reposition=was_radial)

    def show_message(self, text: str) -> None:
        if self.isVisible():
            was_radial = self._mode == "radial"
            if was_radial:
                self._set_mode("card")
            self.info.setText(text)
            self._busy(False)
            self.adjustSize()
            if was_radial and self._anchor is not None:
                target = QPoint(self._anchor.x() - WIDTH // 2, self._anchor.y() - 24)
                self.move(_clamp_to_screen(target, self.sizeHint()))

    def _present_card(self, *, reposition: bool = False) -> None:
        self.adjustSize()
        size = self.sizeHint()
        if not self.isVisible() or reposition:
            base = (
                QPoint(self._anchor.x() - WIDTH // 2, self._anchor.y() - 24)
                if self._anchor is not None
                else QCursor.pos() + QPoint(12, 12)
            )
            self.move(_clamp_to_screen(base, size))
        else:
            self.move(_clamp_to_screen(self.pos(), size))
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate:
            self.hide()  # hızlı kutu gibi: odak kaybı kapatır
        return super().event(event)

    def paintEvent(self, event: Any) -> None:
        if self._mode == "radial":
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(self._palette["control_border"]), 1.0))
        p.setBrush(QColor(self._palette["surface"]))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 12, 12)
        p.end()
