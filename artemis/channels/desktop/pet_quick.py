"""Pet'in altındaki hızlı yazma kutusu (ADR 0048 madde 5).

Yalnız düz metin alır ve `submit` geri çağrısına iletir; komut ya da onay mantığı yoktur
(ADR 0023). Geri çağrı False dönerse (hazır değil/meşgul) metin korunur ve kısa ipucu görünür.

Renkler sabit değil tema jetonlarından gelir (`apply_theme`): kutu ayrı bir pencere olduğu için
ana pencerenin stil sayfasını devralmaz, bu yüzden paleti kendisi uygular.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget

from artemis.channels.desktop import themes

WIDTH = 280
MAX_CHARS = 2000
HINT = "Şu an alınamadı; metin korundu."


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
        self._palette = themes.THEMES[themes.DEFAULT_THEME]
        self.setFixedWidth(WIDTH)
        self.setAccessibleName("Artemis hızlı yazma kutusu")
        self.setToolTip("Artemis'e yaz ve Enter'a bas; Esc kapatır")
        self.input = QLineEdit(self)
        self.input.setMaxLength(MAX_CHARS)
        self.input.setPlaceholderText("Artemis'e yaz…")
        self.input.setAccessibleName("Artemis'e mesaj")
        self.input.returnPressed.connect(self.send)
        self.hint = QLabel(HINT, self)
        self.hint.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)
        layout.addWidget(self.input)
        layout.addWidget(self.hint)
        self.apply_theme(self._palette)
        self.adjustSize()

    def apply_theme(self, palette: dict[str, str]) -> None:
        self._palette = palette
        # Alan şeffaf kalır ve odak çerçevesi çizmez: kutunun kendisi yüzeydir.
        self.input.setStyleSheet(
            f"QLineEdit, QLineEdit:focus {{ background: transparent; border: none;"
            f" color: {palette['text']}; font-size: 14px; padding: 4px 0; }}"
        )
        self.hint.setStyleSheet(
            f"QLabel {{ color: {palette['warning']}; font-size: 12px; background: transparent; }}"
        )
        self.update()

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
        p.setPen(QPen(QColor(self._palette["border"]), 1.2))
        p.setBrush(QColor(self._palette["surface"]))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 12, 12)
        p.end()
