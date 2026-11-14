"""Form diyaloglarının ortak kabuğu: kaydırılan gövde, sabit düğme çubuğu, ekrana sığan yükseklik.

Tetikleyici ve ajan formları 744-812px'e uzuyordu; 1366x768 ekranda (görev çubuğu hariç ~720px)
Ekle/Vazgeç ekran dışında kalabilirdi (UX-08). Gövde QScrollArea'da kayar, düğmeler altta sabit
durur. Düğme sırası ApprovalDialog ile aynıdır: ikincil solda, birincil sağda (UX-07).
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

_SCREEN_MARGIN = 48  # başlık çubuğu ve görev çubuğu payı


class FormDialog(QDialog):
    """`form` düzenine alan ekle, sonunda `add_buttons()` çağır."""

    def __init__(self, title: str, min_width: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(min_width)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("title")
        outer.addWidget(heading)
        self.body_scroll = QScrollArea()
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("flat")
        self.form = QVBoxLayout(body)
        self.form.setContentsMargins(0, 0, 12, 0)  # sağda kaydırma çubuğuna pay
        self.form.setSpacing(12)
        self.body_scroll.setWidget(body)
        outer.addWidget(self.body_scroll, 1)
        self._outer = outer

    def add_buttons(self, ok_text: str) -> None:
        """Vazgeç (ikincil) + birincil eylem; Enter birincil eylemi çalıştırır."""
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.cancel_button = QPushButton("Vazgeç")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        self.ok_button = QPushButton(ok_text)
        self.ok_button.setObjectName("primary")
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.submit)
        row.addWidget(self.cancel_button)
        row.addWidget(self.ok_button)
        self._outer.addLayout(row)

    def submit(self) -> None:  # alt sınıf doğrular ve accept() çağırır
        raise NotImplementedError

    def fit_to_available(self, available: QRect) -> None:
        """Yüksekliği ekranın kullanılabilir alanına sığdırır; fazlası gövdede kayar."""
        limit = max(200, available.height() - _SCREEN_MARGIN)
        self.setMaximumHeight(limit)
        # QScrollArea kendi önerisini ~24 satırla sınırlar; gövdenin doğal yüksekliği esas alınır ki
        # ekran yetiyorsa kaydırma çubuğu hiç çıkmasın.
        body = self.body_scroll.widget()
        assert body is not None
        chrome = self.sizeHint().height() - self.body_scroll.sizeHint().height()
        natural = chrome + body.sizeHint().height()
        self.resize(self.width(), min(natural, limit))

    def showEvent(self, event: QShowEvent) -> None:
        screen = self.screen()
        if screen is not None:
            self.fit_to_available(screen.availableGeometry())
        super().showEvent(event)
