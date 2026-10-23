"""Uygulama simgesini (artemis.ico) üretir: lacivert yuvarlak kare, turkuaz "A".

Kullanım: `QT_QPA_PLATFORM=offscreen python scripts/make_icon.py`. Çıktı depoya girer; çalışma
anında çizim yapılmaz. Qt'nin ICO yazıcısı tek boyut yazdığı için çok boyutlu dosya elle
birleştirilir (her boyut PNG olarak gömülür; Vista ve sonrası destekler).
"""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QPolygonF

SIZES = (16, 24, 32, 48, 64, 128, 256)
TARGET = Path(__file__).resolve().parents[1] / "artemis" / "channels" / "desktop" / "artemis.ico"
NAVY = QColor("#111C2D")
TEAL = QColor("#53D6C7")


def _png(size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 64, size / 64)  # tüm çizim 64x64 tasarım ızgarasında
    painter.setBrush(NAVY)
    painter.setPen(QPen(TEAL, 3))
    painter.drawRoundedRect(3, 3, 58, 58, 18, 18)
    pen = QPen(TEAL, 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolyline(QPolygonF([QPointF(19, 47), QPointF(32, 16), QPointF(45, 47)]))
    painter.drawLine(24, 38, 40, 38)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data.data())


def build() -> bytes:
    pngs = [_png(size) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b""
    for size, png in zip(SIZES, pngs, strict=True):
        dim = 0 if size >= 256 else size  # ICO'da 256 "0" yazılır
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    return header + entries + b"".join(pngs)


if __name__ == "__main__":
    app = QGuiApplication([])
    TARGET.write_bytes(build())
    print(f"Yazıldı: {TARGET}")
