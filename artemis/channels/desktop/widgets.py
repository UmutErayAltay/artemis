"""Ana pencereden bağımsız küçük widget'lar."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

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
