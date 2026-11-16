"""Ana pencereden bağımsız küçük widget'lar."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QEnterEvent, QHideEvent, QKeyEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop import themes


class PetFace(QWidget):
    """Seçili petin sabit yüzü: gerçek pet görseli varsa o, yoksa tema renkli düz çizim.

    Başlık (32px), karşılama (96px) ve asistan balonu (28px) aynı sınıfı kullanır; böylece
    "uygulamanın yüzü" tek kaynaktan gelir. Yedek çizim sabit lacivert yerine tema
    jetonlarını okur, bu yüzden dört temada da uyumlu kalır.
    """

    def __init__(self, size: int, palette: dict[str, str] | None = None) -> None:
        super().__init__()
        self._palette = palette or themes.THEMES[themes.DEFAULT_THEME]
        self._pixmap: QPixmap | None = None
        self.setFixedSize(size, size)
        self.setAccessibleName("Seçili pet")

    def has_image(self) -> bool:
        return self._pixmap is not None

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        self.update()

    def set_palette_tokens(self, palette: dict[str, str]) -> None:
        self._palette = palette
        self.update()

    def paintEvent(self, event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        side = self.width()
        if self._pixmap is not None:
            scaled = self._pixmap.scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            ratio = scaled.devicePixelRatio() or 1.0
            p.drawPixmap(
                round((side - scaled.width() / ratio) / 2),
                round((side - scaled.height() / ratio) / 2),
                scaled,
            )
            p.end()
            return
        t = self._palette
        # Çizim 54x56 koordinatlarında kalır; kutuya orantılı küçültülür.
        scale = side / 56
        p.translate((side - 54 * scale) / 2, 0)
        p.scale(scale, scale)
        p.setPen(QPen(QColor(t["muted"]), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(27, 12), QPointF(27, 5))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t["accent"]))
        p.drawEllipse(QPointF(27, 5), 3.2, 3.2)
        p.setBrush(QColor(t["accent_soft"]))
        p.setPen(QPen(QColor(t["accent"]), 2))
        p.drawEllipse(QRectF(3, 11, 48, 43))
        p.setPen(Qt.PenStyle.NoPen)
        cheek = QColor(t["warning"])
        cheek.setAlpha(70)
        p.setBrush(cheek)
        for x in (11, 43):
            p.drawEllipse(QPointF(x, 40), 4.5, 3)
        p.setBrush(QColor(t["text"]))
        for x in (19, 35):
            p.drawEllipse(QPointF(x, 31), 3.6, 4.4)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(t["text"]), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(QRectF(21, 34, 12, 8), 200 * 16, 140 * 16)
        p.end()


class _HelpPopup(QFrame):
    """Baloncuğun kendisi: odak almayan, çerçevesiz küçük pencere (tema `#helpPopup`)."""

    def __init__(self, parent: QWidget) -> None:
        # Ebeveyn = düğmenin penceresi: tema pencereye uygulanır, ayrı pencere de onu miras alır.
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("helpPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setMaximumWidth(320)
        layout.addWidget(self.label)


class HelpTip(QToolButton):
    """Bir ayarın/işlemin yanındaki küçük "?": neyi etkilediğini ve bedelini (bellek, hız, gizlilik)
    söyler.

    Fare üstüne gelince baloncuk açılır, ayrılınca kapanır; tıklayınca (ya da Boşluk/Enter) açık
    kalır ve yeniden tıklama, başka yere tıklama ya da Esc ile kapanır. Aynı anda tek baloncuk
    sabitlenir. Metin ekran okuyucuya açıklama olarak gider; Qt'nin kendi ipucu bastırılır ki
    baloncukla üst üste iki kutu çıkmasın.
    """

    _pinned: HelpTip | None = None

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("helpTip")
        self.setText("?")
        self.setToolTip(text)  # metnin tek kaynağı; yerel ipucu event() içinde bastırılır
        self.setAccessibleName("Yardım")
        self.setAccessibleDescription(text)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCheckable(True)  # sabitliyken işaretli görünür
        self.setFixedSize(18, 18)
        self._popup: _HelpPopup | None = None
        self.toggled.connect(self._pin_changed)

    def is_open(self) -> bool:
        return self._popup is not None and self._popup.isVisible()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            return True
        return super().event(event)

    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        self._open()

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        if not self.isChecked():
            self._close()

    def hideEvent(self, event: QHideEvent) -> None:
        # Sayfa değişince ya da pencere gizlenince baloncuk ekranda asılı kalmasın.
        self.setChecked(False)
        self._close()
        super().hideEvent(event)

    def _pin_changed(self, pinned: bool) -> None:
        app = QApplication.instance()
        if pinned:
            previous = HelpTip._pinned
            if previous is not None and previous is not self:
                previous.setChecked(False)
            HelpTip._pinned = self
            if app is not None:
                app.installEventFilter(self)
            self._open()
        else:
            if HelpTip._pinned is self:
                HelpTip._pinned = None
            if app is not None:
                app.removeEventFilter(self)
            # İkinci tıklama imleç hâlâ üstündeyken de kapatır; yeniden üstüne gelince açılır.
            self._close()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Sabit baloncuk: dışarı tıklama, Esc ya da uygulamadan çıkış kapatır."""
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and watched is not self and not (
            isinstance(watched, QWidget) and self._popup is not None
            and (watched is self._popup or self._popup.isAncestorOf(watched))
        ):
            if isinstance(watched, QWidget) and watched.window() is not None:
                self.setChecked(False)
        elif kind == QEvent.Type.KeyPress and isinstance(event, QKeyEvent) and (
            event.key() == Qt.Key.Key_Escape
        ):
            self.setChecked(False)
            return True
        elif kind == QEvent.Type.ApplicationDeactivate:
            self.setChecked(False)
        return False

    def _open(self) -> None:
        if self._popup is None:
            self._popup = _HelpPopup(self.window())
        self._popup.label.setText(self.toolTip())
        self._popup.adjustSize()
        point = self.mapToGlobal(QPoint(self.width() + 6, -4))
        screen = self.screen().availableGeometry() if self.screen() else None
        if screen is not None:
            size = self._popup.size()
            if point.x() + size.width() > screen.right():
                point.setX(self.mapToGlobal(QPoint(0, 0)).x() - size.width() - 6)
            point.setY(max(screen.top(), min(point.y(), screen.bottom() - size.height())))
        self._popup.move(point)
        self._popup.show()

    def _close(self) -> None:
        if self._popup is not None:
            self._popup.hide()


def with_help(widget: QWidget | QLayout, text: str) -> QHBoxLayout:
    """`widget`/düzen + sağında "?" baloncuğu; satır sola yaslanır, baloncuk metne yapışır."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    if isinstance(widget, QLayout):
        row.addLayout(widget)
    else:
        row.addWidget(widget)
    row.addWidget(HelpTip(text), 0, Qt.AlignmentFlag.AlignVCenter)
    row.addStretch(1)
    return row
