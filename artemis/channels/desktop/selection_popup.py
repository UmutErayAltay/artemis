"""Seçili metin eylemleri için imleç yanındaki küçük pencere (ADR 0062).

Hızlı kutunun (`pet_quick.QuickBox`) pencere türünü ve boyama biçimini kullanır; yeni bir tasarım
dili yoktur. Pencere yalnız düğmelere basıldığını bildirir: model, pano ve yazım işi köprüde
yapılır. "Kopyala" sonucu panoya GUI tarafında yazar (kullanıcının açık isteği).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QIcon, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from artemis.channels.desktop import themes
from artemis.channels.desktop.selection import ACTION_LABELS, ACTIONS
from artemis.i18n import t

WIDTH = 340
_ICON = Path(__file__).with_name("artemis.ico")


class SelectionPopup(QWidget):
    def __init__(
        self,
        parent: QWidget,
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
        self._on_action = on_action
        self._on_write = on_write
        self._palette = themes.THEMES[themes.DEFAULT_THEME]
        self.setFixedWidth(WIDTH)
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
        self.close_button.setFixedSize(24, 24)
        self.close_button.setAccessibleName(t("Kapat"))
        self.close_button.setToolTip(t("Kapat"))
        self.close_button.clicked.connect(self.hide)
        self.buttons: dict[str, QPushButton] = {}
        actions = QHBoxLayout()
        for action in ACTIONS:
            label = t(ACTION_LABELS[action])
            button = QPushButton(label, self)
            button.setAccessibleName(label)
            button.setProperty("action", action)
            button.clicked.connect(self._action_clicked)
            self.buttons[action] = button
            actions.addWidget(button)
        self.result = QPlainTextEdit(self)
        self.result.setReadOnly(True)
        self.result.setAccessibleName(t("Eylem sonucu"))
        self.result.setFixedHeight(120)
        self.result.hide()
        self.copy_button = QPushButton(t("Kopyala"), self)
        self.copy_button.clicked.connect(self._copy)
        self.write_button = QPushButton(t("Yerine yaz"), self)
        self.write_button.clicked.connect(self._write)
        self.result_row = QWidget(self)
        row = QHBoxLayout(self.result_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.copy_button)
        row.addWidget(self.write_button)
        self.result_row.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self.title)
        text.addWidget(self.info)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(self.header_icon, 0, Qt.AlignmentFlag.AlignTop)
        header.addLayout(text, 1)
        header.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        layout.addLayout(actions)
        layout.addWidget(self.result)
        layout.addWidget(self.result_row)
        self.apply_theme(self._palette)

    def apply_theme(self, palette: dict[str, str]) -> None:
        self._palette = palette
        self.setStyleSheet(
            f"QLabel {{ color: {palette['text']}; font-size: 12px; background: transparent; }}"
            f"QPlainTextEdit {{ color: {palette['text']}; background: transparent;"
            f" border: 1px solid {palette['border']}; border-radius: 6px; font-size: 13px; }}"
            f"QPushButton {{ color: {palette['text']}; background: transparent;"
            f" border: 1px solid {palette['border']}; border-radius: 6px; padding: 4px 8px; }}"
            f"QPushButton:disabled {{ color: {palette['muted']}; }}"
            f"QLabel#title {{ font-size: 13px; font-weight: 600; }}"
            f"QLabel#meta {{ color: {palette['muted']}; font-size: 12px; }}"
            f"QPushButton#close {{ color: {palette['muted']}; background: transparent;"
            f" border: none; border-radius: 6px; padding: 0; font-size: 16px; }}"
            f"QPushButton#close:focus {{ border: 1px solid {palette['accent']}; }}"
        )
        self.update()

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

    def show_ready(self, info: str) -> None:
        """Yeni seçim: önceki sonuç silinir, pencere imlecin yanında açılır."""
        self.result.clear()
        self.result.hide()
        self.result_row.hide()
        self.info.setText(info)
        self._busy(False)
        self._present()

    def show_working(self) -> None:
        # Sonuç kutusu boş da olsa görünür kalır: sonuç gelince pencere boyu değişmez.
        self.result.clear()
        self.result.show()
        self.result_row.show()
        self.info.setText(t("Çalışıyor…"))
        self._busy(True)

    def show_result(self, text: str) -> None:
        self.result.setPlainText(text)
        self.result.show()
        self.result_row.show()
        self.info.setText(t("Sonuç hazır."))
        self._busy(False)
        self._present()

    def show_message(self, text: str) -> None:
        if self.isVisible():
            self.info.setText(text)
            self._busy(False)

    def _present(self) -> None:
        if not self.isVisible():
            self.adjustSize()
            self.move(self._inside_screen(QCursor.pos() + QPoint(12, 12)))
        self.adjustSize()
        self.show()
        self.activateWindow()

    def _inside_screen(self, point: QPoint) -> QPoint:
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(point.x(), area.left()), area.right() - WIDTH)
        y = min(max(point.y(), area.top()), area.bottom() - self.sizeHint().height())
        return QPoint(x, y)

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
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(self._palette["border"]), 1.2))
        p.setBrush(QColor(self._palette["surface"]))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 12, 12)
        p.end()
