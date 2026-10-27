"""Pet'in altındaki hızlı yazma kutusu (ADR 0048 madde 5).

Yalnız düz metin alır ve `submit` geri çağrısına iletir; komut ya da onay mantığı yoktur
(ADR 0023). Geri çağrı False dönerse (hazır değil/meşgul) metin korunur ve kısa ipucu görünür.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget

WIDTH = 240
MAX_CHARS = 2000
HINT = "Şu an alınamadı; metin korundu."
_BORDER = QColor("#2A3B52")
_SURFACE = QColor(0x11, 0x1C, 0x2D, 245)
_TEXT = "#E6EDF5"


class QuickBox(QWidget):
    def __init__(self, parent: QWidget, submit: Callable[[str], bool]) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._submit = submit
        self.setFixedWidth(WIDTH)
        self.setAccessibleName("Artemis hızlı yazma kutusu")
        self.setToolTip("Artemis'e yaz ve Enter'a bas; Esc kapatır")
        self.input = QLineEdit(self)
        self.input.setMaxLength(MAX_CHARS)
        self.input.setPlaceholderText("Artemis'e yaz…")
        self.input.setAccessibleName("Artemis'e mesaj")
        self.input.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none; color: {_TEXT};"
            " font: 12px 'Segoe UI'; padding: 4px 2px; }"
        )
        self.input.returnPressed.connect(self.send)
        self.hint = QLabel(HINT, self)
        self.hint.setStyleSheet("color: #FFD78C; font: 11px 'Segoe UI';")
        self.hint.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(0)
        layout.addWidget(self.input)
        layout.addWidget(self.hint)
        self.adjustSize()

    def send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        if self._submit(text):
            self.input.clear()
            self.hint.hide()
            self.hide()
            return
        self.hint.show()
        self.adjustSize()

    def open(self) -> None:
        self.hint.hide()
        self.adjustSize()
        self.show()
        self.activateWindow()
        self.input.setFocus()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate:
            self.hide()  # odak kaybı kutuyu kapatır
        return super().event(event)

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(_BORDER, 1.2))
        p.setBrush(_SURFACE)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 13, 13)
        p.end()
