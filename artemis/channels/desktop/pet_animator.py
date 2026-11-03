"""Pet üretici animatörü (ADR 0048 madde 8): tek temel görselden Codex spritesheet'i.

Saf QPainter ve belirleyicidir (rastgelelik yok). Temel görsel herhangi bir QImage olabilir
(SVG çizimi bugün, resim modeli çıktısı Paket 3b'de); boyut `_fit_cell` ile hücreye uydurulur.
Çıktı `pet_sprite.import_pet`'ten geçirilerek kurulur, böylece doğrulama tek yerde kalır.
"""

from __future__ import annotations

import json
import math
import tempfile
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QByteArray, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from artemis.channels.desktop import pet_sprite as ps
from artemis.channels.desktop.pet_creator import SvgError
from artemis.channels.desktop.pet_sprite import CELL_H, CELL_W, COLS, FRAMES, PetError, PetInfo

SHEET_W, SHEET_H = COLS * CELL_W, 9 * CELL_H
_FOOT = (CELL_W / 2, 200.0)  # tabandaki ayak noktası (temel hücre koordinatı)
_BASE_SCALE = 0.9  # karakter hücre yüksekliğinin ~%90'ı
_ARGB = QImage.Format.Format_ARGB32_Premultiplied
STATE_NAMES = (
    "Bekleme", "Sağa koşma", "Sola koşma", "El sallama", "Zıplama", "Hata", "Onay bekleme",
    "Çalışma", "İnceleme",
)
BLINK_FRAME = 4  # idle'ın 6 karesinden biri göz kapalı


def render_svg(svg: str) -> QImage:
    """Temizlenmiş SVG'yi 192×208 saydam hücreye çizer; geçersiz/boş çizimde SvgError."""
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise SvgError("SVG çizilemedi (geçersiz).")
    image = QImage(CELL_W, CELL_H, _ARGB)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    alpha = bytes(image.constBits())[3::4]
    if not any(alpha):
        raise SvgError("SVG boş çizildi: görünür bir şey yok.")
    return image


def _fit_cell(image: QImage) -> QImage:
    """Herhangi bir boyuttaki görseli oranı koruyup hücreye sığdırır, alta ortalar."""
    source = image.convertToFormat(_ARGB)
    if (source.width(), source.height()) != (CELL_W, CELL_H):
        source = source.scaled(
            CELL_W, CELL_H, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    cell = QImage(CELL_W, CELL_H, _ARGB)
    cell.fill(0)
    painter = QPainter(cell)
    painter.drawImage((CELL_W - source.width()) // 2, CELL_H - source.height(), source)
    painter.end()
    return cell


def _gray(base: QImage) -> QImage:
    """Aynı alfa, gri tonlama (hata karesi için)."""
    gray = base.convertToFormat(QImage.Format.Format_ARGB32).convertToFormat(
        QImage.Format.Format_Grayscale8
    ).convertToFormat(_ARGB)
    painter = QPainter(gray)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, base)
    painter.end()
    return gray


class _Pose:
    """Bir karenin dönüşümü: kayma, dönüş (derece), ölçek, gri oranı, göz kapalı, nokta sayısı."""

    def __init__(self, dx: float = 0, dy: float = 0, rot: float = 0, sx: float = 1,
                 sy: float = 1, gray: float = 0, blink: bool = False, dots: int = 0) -> None:
        self.dx, self.dy, self.rot, self.sx, self.sy = dx, dy, rot, sx, sy
        self.gray, self.blink, self.dots = gray, blink, dots


def _wave(i: int, n: int) -> float:
    return math.sin(2 * math.pi * i / n)


_JUMP_DY = (0, -20, -28, -20, 0)
_JUMP_SQUASH = ((1.08, 0.92), (0.97, 1.04), (0.96, 1.05), (0.97, 1.04), (1.08, 0.92))

_POSES: tuple[Callable[[int, int], _Pose], ...] = (
    # idle: hafif nefes + ezilme, tek göz kırpma karesi
    lambda i, n: _Pose(dy=-1.5 * _wave(i, n), sx=1 + 0.02 * _wave(i, n),
                       sy=1 - 0.02 * _wave(i, n), blink=i == BLINK_FRAME),
    # running-right
    lambda i, n: _Pose(dx=3 * _wave(i, n), dy=-5 * abs(_wave(i, n * 2)), rot=8),
    # running-left
    lambda i, n: _Pose(dx=-3 * _wave(i, n), dy=-5 * abs(_wave(i, n * 2)), rot=-8),
    # waving: yana yatma salınımı
    lambda i, n: _Pose(rot=6 * _wave(i, n), dx=1.5 * _wave(i, n)),
    # jumping: yay + kalkış/iniş ezilmesi
    lambda i, n: _Pose(dy=_JUMP_DY[i], sx=_JUMP_SQUASH[i][0], sy=_JUMP_SQUASH[i][1]),
    # failed: titreme + çökme + solma
    lambda i, n: _Pose(dx=4 * _wave(i * 2, n), dy=min(i, 3), rot=-3, sy=0.95,
                       gray=0.85 * min(1.0, (i + 1) / 3)),
    # waiting: yavaş sallanma + üç nokta
    lambda i, n: _Pose(rot=4 * _wave(i, n), dx=2 * _wave(i, n), dots=i // 2 + 1),
    # running (çalışma döngüsü): hızlı sekme + hafif dönüş
    lambda i, n: _Pose(dy=-4 * abs(_wave(i, n * 2)), rot=3 * _wave(i, n)),
    # review: öne eğilme ~1.05 ölçek + yavaş baş sallama
    lambda i, n: _Pose(dy=2 * _wave(i, n), rot=4 + 2 * _wave(i, n), sx=1.05, sy=1.05),
)


def _draw(painter: QPainter, image: QImage, pose: _Pose, opacity: float) -> None:
    painter.save()
    painter.setOpacity(opacity)
    painter.translate(_FOOT[0] + pose.dx, _FOOT[1] + 4 + pose.dy)
    painter.rotate(pose.rot)
    painter.scale(_BASE_SCALE * pose.sx, _BASE_SCALE * pose.sy)
    painter.translate(-_FOOT[0], -_FOOT[1])
    painter.drawImage(0, 0, image)
    painter.restore()


def _frame(base: QImage, blink: QImage, gray: QImage, row: int, i: int) -> QImage:
    pose = _POSES[row](i, FRAMES[row])
    cell = QImage(CELL_W, CELL_H, _ARGB)
    cell.fill(0)
    painter = QPainter(cell)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    _draw(painter, blink if pose.blink else base, pose, 1.0 - pose.gray)
    if pose.gray:
        _draw(painter, gray, pose, pose.gray)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(90, 90, 90, 230))
    for k in range(pose.dots):
        painter.drawEllipse(QPointF(128 + 16 * k, 26), 4.5, 4.5)
    painter.end()
    return cell


def build_sheet(base: QImage, blink: QImage | None = None) -> QImage:
    """1536×1872 Codex spritesheet'i. `blink` göz kapalı varyantıdır (yoksa temel görsel)."""
    base_cell = _fit_cell(base)
    blink_cell = _fit_cell(blink) if blink is not None else base_cell
    gray = _gray(base_cell)
    sheet = QImage(SHEET_W, SHEET_H, _ARGB)
    sheet.fill(0)
    painter = QPainter(sheet)
    for row, count in enumerate(FRAMES):
        for col in range(count):
            painter.drawImage(
                col * CELL_W, row * CELL_H, _frame(base_cell, blink_cell, gray, row, col)
            )
    painter.end()
    return sheet


def procedural_row(base: QImage, row: int) -> list[QImage]:
    """Tek satırın prosedürel kareleri (resim hattında yedek: model şeridi alınamayınca)."""
    cell = _fit_cell(base)
    gray = _gray(cell)
    return [_frame(cell, cell, gray, row, i) for i in range(FRAMES[row])]


def state_frames(sheet: QImage, row: int) -> list[QPixmap]:
    """Önizleme için bir satırın kareleri."""
    return [
        QPixmap.fromImage(sheet.copy(QRect(c * CELL_W, row * CELL_H, CELL_W, CELL_H)))
        for c in range(FRAMES[row])
    ]


def install(
    sheet: QImage, pet_id: str, name: str, description: str, pets_dir: Path, *,
    overwrite: bool = False,
) -> PetInfo:
    """pet.json + spritesheet.webp yazar, `import_pet` ile kurar (PetError / PetExistsError)."""
    with tempfile.TemporaryDirectory(prefix="pet-creator-") as tmp:
        folder = Path(tmp)
        out = sheet.convertToFormat(QImage.Format.Format_ARGB32)
        if not out.save(str(folder / "spritesheet.webp"), None, 100):
            raise PetError("Spritesheet WebP olarak yazılamadı.")
        (folder / "pet.json").write_text(
            json.dumps(
                {"id": pet_id, "displayName": name, "description": description,
                 "spritesheetPath": "spritesheet.webp"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return ps.import_pet(folder, pets_dir, overwrite=overwrite)
