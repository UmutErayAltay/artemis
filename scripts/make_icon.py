"""Uygulama simgesini (artemis.ico) `scripts/artemis-logo.png` kaynağından üretir.

Kaynak: gpt-image-2.5-sunburst ile üretilen hilal + profil + yıldız logosu (2026-11-07, Umut seçti).
İlk kullanımda `--kaynak ham.png` verilirse saydam kenar kırpılıp 512 px kaynak olarak kaydedilir.
Kullanım: `QT_QPA_PLATFORM=offscreen python scripts/make_icon.py [--kaynak ham.png]`. Çıktı depoya
girer; çalışma anında çizim yapılmaz. Qt'nin ICO yazıcısı tek boyut yazdığı için çok boyutlu dosya
elle birleştirilir (her boyut PNG olarak gömülür; Vista ve sonrası destekler).
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt
from PySide6.QtGui import QGuiApplication, QImage

SIZES = (16, 24, 32, 48, 64, 128, 256)
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "artemis-logo.png"
TARGET = ROOT / "artemis" / "channels" / "desktop" / "artemis.ico"
SOURCE_SIZE = 512


def _opaque_bounds(image: QImage) -> QRect:
    """Saydam kenarı atar; küçük boyutta rozet tüm kareyi doldursun diye."""
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    xs: list[int] = []
    ys: list[int] = []
    for y in range(alpha.height()):
        row = alpha.constScanLine(y).tobytes()[: alpha.width()]
        hits = [x for x, value in enumerate(row) if value > 16]
        if hits:
            xs += (hits[0], hits[-1])
            ys.append(y)
    side = max(max(xs) - min(xs), max(ys) - min(ys)) + 1
    cx, cy = (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2
    return QRect(cx - side // 2, cy - side // 2, side, side)


def import_source(raw: Path) -> None:
    image = QImage(str(raw))
    if image.isNull():
        raise SystemExit(f"Okunamadı: {raw}")
    cropped = image.copy(_opaque_bounds(image))
    cropped.scaled(
        SOURCE_SIZE, SOURCE_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).save(str(SOURCE), "PNG")


def _png(source: QImage, size: int) -> bytes:
    image = source.scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data.data())


def build() -> bytes:
    source = QImage(str(SOURCE))
    if source.isNull():
        raise SystemExit(f"Kaynak logo yok: {SOURCE}")
    pngs = [_png(source, size) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b""
    for size, png in zip(SIZES, pngs, strict=True):
        dim = 0 if size >= 256 else size  # ICO'da 256 "0" yazılır
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    return header + entries + b"".join(pngs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kaynak", type=Path, help="ham logo PNG'si (kırpılıp kaynak olur)")
    args = parser.parse_args()
    app = QGuiApplication([])
    if args.kaynak:
        import_source(args.kaynak)
    TARGET.write_bytes(build())
    print(f"Yazıldı: {TARGET}")
