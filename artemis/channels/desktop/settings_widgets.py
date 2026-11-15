"""Ayar panelinin küçük widget'ları: bölüm listesi, arama kutusu, tema kartı, not satırları."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.form_kit import meta_label
from artemis.channels.desktop.themes import DEFAULT_THEME, THEMES
from artemis.text import search_fold

_ITEM_HEIGHT = 30  # bölüm satırı; dar pencerede liste kendi kaydırıcısına ihtiyaç duymasın
_HEADER_HEIGHT = 22  # grup başlığı, grubun ilk satırının üstünde
_GROUP_ROLE = Qt.ItemDataRole.UserRole + 1
_DIRTY_ROLE = Qt.ItemDataRole.UserRole + 2
_DIRTY_A11Y = "kaydedilmemiş değişiklik var"


def _fold(text: str) -> str:
    """Arama için: büyük/küçük harf ve Türkçe İ/ı farkı yok sayılır."""
    return search_fold(text)


class _SectionDelegate(QStyledItemDelegate):
    """Grup başlığını grubun ilk satırının üstüne çizer. Başlık ayrı bir öğe olmadığı için
    seçilemez ve klavye gezintisinde atlanır; satır sırası sayfa sırasıyla aynı kalır."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.muted = QColor(THEMES[DEFAULT_THEME]["muted"])
        self.accent = QColor(THEMES[DEFAULT_THEME]["accent"])

    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:
        hint = index.data(Qt.ItemDataRole.SizeHintRole)
        return hint if isinstance(hint, QSize) else super().sizeHint(option, index)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        title = index.data(_GROUP_ROLE)
        if title:
            painter.save()
            font = QFont(option.font)
            font.setPixelSize(11)
            font.setBold(True)
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 104)
            painter.setFont(font)
            painter.setPen(self.muted)
            rect = QRect(option.rect.left() + 12, option.rect.top(), option.rect.width() - 12,
                         _HEADER_HEIGHT - 3)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom),
                             str(title))
            painter.restore()
            option = QStyleOptionViewItem(option)
            option.rect = option.rect.adjusted(0, _HEADER_HEIGHT, 0, 0)
        super().paint(painter, option, index)
        if index.data(_DIRTY_ROLE):  # renk tek başına değil: erişilebilir metin de var
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.accent)
            painter.drawEllipse(QPoint(option.rect.right() - 14, option.rect.center().y()), 4, 4)
            painter.restore()


class _SearchBox(QLineEdit):
    """Esc aramayı temizler."""

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self.text():
            self.clear()
        else:
            super().keyPressEvent(event)


class _SectionList(QListWidget):
    """Bölüm listesi. Kaydırma dışında, sabit durur; `scroll_area` sağdaki sayfa kaydırma
    alanına işaret eder (başa dönme ve görünür kılma için)."""

    scroll_area: QScrollArea

    def _on_header(self, position: QPoint) -> bool:
        item = self.itemAt(position)
        return (
            item is not None and bool(item.data(_GROUP_ROLE))
            and position.y() - self.visualItemRect(item).top() < _HEADER_HEIGHT
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._on_header(event.position().toPoint()):  # başlık alanı seçim yapmaz
            super().mousePressEvent(event)


# Tema adının yanındaki açıklama; anahtarlar themes.THEME_LABELS ile aynı.
_THEME_HINTS = {
    "light": "Açık gezinme, açık çalışma alanı",
    "dark": "Koyu gezinme, koyu çalışma alanı",
    "light-mixed": "Koyu gezinme, açık çalışma alanı",
    "dark-mixed": "Açık gezinme, koyu çalışma alanı",
}


def _swatch(key: str) -> QPixmap:
    """Gerçek arayüze yakın küçük önizleme: gezinme, çalışma alanı, balon ve birincil düğme."""
    colors = THEMES[key]
    width, height = 128, 76
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0.5, 0.5, width - 1, height - 1), 8, 8)
    painter.setClipPath(clip)
    painter.fillRect(0, 0, width, height, QColor(colors["bg"]))
    painter.fillRect(0, 0, 32, height, QColor(colors["nav"]))
    nav_text = QColor(colors["nav_muted"])
    painter.fillRect(6, 24, 20, 6, QColor(colors["nav_selected"]))  # seçili gezinme satırı
    for top in (10, 24, 36, 48):
        painter.fillRect(8, top + 1, 16, 3, nav_text)
    ink = QColor(colors["muted"])
    painter.fillRect(40, 10, 40, 4, ink)  # asistan cevabı
    painter.fillRect(40, 18, 56, 4, ink)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(colors["accent_soft"]))
    painter.drawRoundedRect(QRectF(66, 30, 54, 16), 6, 6)  # kullanıcı balonu
    painter.setBrush(QColor(colors["accent"]))
    painter.drawRoundedRect(QRectF(88, 56, 32, 11), 4, 4)  # birincil düğme
    painter.setBrush(QColor(colors["surface"]))
    painter.setPen(QColor(colors["border"]))
    painter.drawRoundedRect(QRectF(40, 56, 42, 11), 4, 4)  # ikincil alan
    painter.setClipping(False)
    painter.setPen(QColor(colors["border"]))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(0.5, 0.5, width - 1, height - 1), 8, 8)
    painter.end()
    return pixmap


class _ThemeCard(QFrame):
    """Tema kartı: tek seçim göstergesi 2px vurgu kenarlığıdır; radyo düğmesi yalnız klavye ve
    ekran okuyucu için kalır (göstergesi QSS ile gizli), kart her yerinden tıklanır."""

    def __init__(self, key: str, label: str) -> None:
        super().__init__()
        self.key = key
        self.setObjectName("themeCard")
        self.setProperty("selected", False)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 12, 12, 12)
        row.setSpacing(12)
        swatch = QLabel()
        swatch.setPixmap(_swatch(key))
        swatch.setAccessibleName(f"{label} önizleme")
        self.radio = QRadioButton(label)
        self.radio.toggled.connect(self._selected)
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addStretch(1)
        text.addWidget(self.radio)
        hint = meta_label(_THEME_HINTS.get(key, ""))
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addWidget(hint)
        text.addStretch(1)
        row.addWidget(swatch)
        row.addLayout(text, 1)

    def _selected(self, on: bool) -> None:
        self.setProperty("selected", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event: Any) -> None:
        self.radio.click()
        super().mousePressEvent(event)


class _SavedNote(QLabel):
    """Anında kaydedilen bir denetim çalışınca kısa süre "Kaydedildi" der (renge bağlı değil)."""

    def __init__(self) -> None:
        super().__init__("Kaydedildi")
        self.setObjectName("meta")
        self.setAccessibleName("Kayıt durumu")
        self.setVisible(False)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(2500)
        self._timer.timeout.connect(self.hide)

    def flash(self) -> None:
        self.show()
        self._timer.start()


class _Notice(QLabel):
    """Üst satırdaki durum yazısı: `setText` geçicidir (~6 sn), `show_error` kalıcıdır.

    Alt çubuk kalktığı için mesajlar sayfayı itmeden köşede belirir; hata ise kullanıcı bir
    şey yapana (yeni kayıt/geri alma/yeni metin) kadar görünür kalmalı."""

    def __init__(self) -> None:
        super().__init__("")
        self.setObjectName("meta")
        self.setAccessibleName("Durum")
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Uzun mesaj satırın asgari genişliğini şişirip dar pencerede yatay kaydırma açmasın.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(6000)
        self._timer.timeout.connect(self.clear)

    def setText(self, arg__1: str) -> None:
        super().setText(arg__1)
        if arg__1:
            self._timer.start()
        else:
            self._timer.stop()

    def show_error(self, text: str) -> None:
        super().setText(text)
        self._timer.stop()


class _WheelGuard(QObject):
    """Kaydırılan sayfadaki açılır liste/sayı kutusu tekerlekle değişmez; tekerlek sayfayı kaydırır.

    Kullanıcı sayfada aşağı inerken imlecin altına gelen seçim sessizce değişiyordu
    (2026-11-13). Değer yalnız tıklayıp seçerek ya da klavyeyle değişir.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.Wheel or not isinstance(
            watched, QComboBox | QAbstractSpinBox | QSlider
        ):
            return False
        parent = watched.parentWidget()
        while parent is not None and not isinstance(parent, QAbstractScrollArea):
            parent = parent.parentWidget()
        if parent is None:
            return False  # kaydırılan sayfada değil: normal davranış
        QApplication.sendEvent(parent.viewport(), event)
        return True


def _install_wheel_guard() -> None:
    app = QApplication.instance()
    if app is not None and app.findChild(_WheelGuard) is None:
        app.installEventFilter(_WheelGuard(app))
