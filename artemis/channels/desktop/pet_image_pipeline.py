"""Pet üretici resim modeli hattı (ADR 0048 eki): temel görsel, 8 şerit, dilimleme, satır ölçeği.

Qt'ye bağlıdır ama ağa değil: model çağrısı `Generate` olarak enjekte edilir (üretimde
`brain.images.generate_image`). Model çıktısı güvenilmezdir; görsel yalnız QImage ile çözülür,
boyut/oran doğrulanır. Bir satır iki denemede de başarısız olursa o satır temel görselden
prosedürel animatörle üretilir (`pet_animator.procedural_row`) ve kullanıcıya söylenir.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from statistics import median

from PySide6.QtCore import QBuffer, QIODevice, QRect, Qt
from PySide6.QtGui import QImage, QPainter

from artemis.brain.errors import BudgetExceeded, ProviderError
from artemis.channels.desktop import pet_animator
from artemis.channels.desktop import pet_sprite as ps
from artemis.channels.desktop.pet_sprite import CELL_H, CELL_W, FRAMES

Generate = Callable[[str, bytes | None, str], bytes]  # (istem, referans PNG, boyut) -> PNG/WebP

BASE_SIZE = "1024x1024"
STRIP_SIZE = "1536x1024"
MAX_SIDE = 4096
ALPHA_MIN = 16  # bunun altı saydam sayılır
FILL = 0.92  # temel karakterin hücreye oranı; her satır buna göre normalize edilir
BLOCK = 4  # bağlı bölge analizinde indirgeme hücresi (piksel)
SLIVER = 0.12  # kenara değen parça gövde alanının altındaysa komşu artığıdır
BASELINE = 204  # hücrede ortak zemin çizgisi (animatörün ayak noktası + 4)
BG_TOLERANCE = 32
_ARGB = QImage.Format.Format_ARGB32_Premultiplied

STYLE = (
    "A cute chibi mascot character, full body, facing the viewer, crisp pixel-art outlines, "
    "centered, transparent background, no text. Character: "
)
ROWS: tuple[tuple[int, str], ...] = (
    (ps.IDLE, "a smooth idle breathing animation with one blink"),
    (ps.RUN_RIGHT, "a run cycle, the character running to the right, facing right"),
    (ps.WAVING, "the character cheerfully waving one hand"),
    (ps.JUMPING, "a jump: crouch, take off, in the air, coming down, landing"),
    (ps.FAILED, "a sad dizzy failure animation, the character slumps down with swirly eyes"),
    (ps.WAITING, "the character waiting impatiently, looking around and tapping a foot"),
    (ps.RUNNING, "the character busily working, typing on a tiny laptop"),
    (ps.REVIEW, "the character thinking, inspecting with a magnifying glass or hand on chin"),
)


class PetImageError(ValueError):
    """Model görseli kullanılamıyor; mesaj kullanıcıya gösterilebilir (Türkçe)."""


class Cancelled(Exception):
    """Kullanıcı üretimi iptal etti."""


@dataclass
class Frame:
    """Alfa sınırına kırpılmış kare; `top` şerit içindeki üst y (zıplama yüksekliği korunur)."""

    image: QImage
    top: int


def row_prompt(row: int) -> str:
    desc = dict(ROWS)[row]
    return (
        f"Using the same exact character, draw a horizontal sprite strip of {FRAMES[row]} "
        f"equally spaced frames of {desc}, same style and colors, transparent background, "
        "no text, no grid lines."
    )


# ----------------------------------------------------------------- görsel yardımcıları


def decode(data: bytes, size: str) -> QImage:
    """Model çıktısını QImage'e çözer; çözülemeyen ya da dev görseli reddeder.

    Oran zorlanmaz: canlı denemede CLI proxy `size`'ı yok saydı (1024x1024 isteğine dikey,
    1536x1024 isteğine ~3:1 şerit döndü). Kırpma ve dilimleme zaten alfa sınırına göre çalışır.
    """
    del size  # istek boyutu modele ipucudur, doğrulama ölçütü değil
    image = QImage.fromData(data)
    if image.isNull() or max(image.width(), image.height()) > MAX_SIDE:
        raise PetImageError("Model görseli çözülemedi ya da çok büyük.")
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def to_png(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):  # type: ignore[call-overload]
        raise PetImageError("Görsel PNG olarak yazılamadı.")
    return bytes(buffer.data().data())


def _alpha(image: QImage) -> bytes:
    return bytes(image.convertToFormat(QImage.Format.Format_ARGB32).constBits())[3::4]


_THRESHOLD = bytes(1 if a > ALPHA_MIN else 0 for a in range(256))


def _mask(image: QImage) -> bytes:
    return _alpha(image).translate(_THRESHOLD)


def remove_background(image: QImage) -> QImage:
    """Opak görselde dört köşeden renk toleranslı taşırma ile zemini saydamlaştırır.

    Zaten saydam alan varsa dokunmaz. İçerideki kapalı boşluklar (kolların arası) kalır.
    """
    img = image.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = img.width(), img.height()
    buf = bytearray(bytes(img.constBits())[: w * h * 4])
    if min(buf[3::4]) < 250:
        return img
    seen = bytearray(w * h)
    for sx, sy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        i0 = (sy * w + sx) * 4
        ref = tuple(buf[i0:i0 + 3])
        stack = [sy * w + sx]
        while stack:
            p = stack.pop()
            if seen[p]:
                continue
            q = p * 4
            if (abs(buf[q] - ref[0]) > BG_TOLERANCE or abs(buf[q + 1] - ref[1]) > BG_TOLERANCE
                    or abs(buf[q + 2] - ref[2]) > BG_TOLERANCE):
                continue
            seen[p] = 1
            buf[q + 3] = 0
            x = p % w
            if x:
                stack.append(p - 1)
            if x < w - 1:
                stack.append(p + 1)
            if p >= w:
                stack.append(p - w)
            if p < w * (h - 1):
                stack.append(p + w)
    return QImage(bytes(buf), w, h, w * 4, QImage.Format.Format_ARGB32).copy()


def _bbox(mask: bytes, w: int, x0: int, x1: int) -> tuple[int, int] | None:
    """Sütun aralığındaki dolu satırların (üst, alt+1) değeri; boşsa None."""
    rows = [y for y in range(len(mask) // w) if any(mask[y * w + x0:y * w + x1])]
    return (rows[0], rows[-1] + 1) if rows else None


def _cols(mask: bytes, w: int, h: int) -> bytes:
    """Sütun doluluğu (her sütun için 0/1): satırları büyük tamsayıyla OR'lamak hızlıdır."""
    acc = 0
    for y in range(h):
        acc |= int.from_bytes(mask[y * w:(y + 1) * w], "big")
    return acc.to_bytes(w, "big")


def _runs(cols: bytes) -> list[tuple[int, int]]:
    w = len(cols)
    runs: list[tuple[int, int]] = []
    start = None
    for x, v in enumerate(cols):
        if v and start is None:
            start = x
        elif not v and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, w))
    return runs


def _merge_fragments(runs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Medyan genişliğin %15'inden dar parçaları (kıvılcım vb.) yakın komşusuna katar."""
    if len(runs) < 2:
        return runs
    limit = 0.15 * median(b - a for a, b in runs)
    out = list(runs)
    while len(out) > 1:
        tiny = next((i for i, (a, b) in enumerate(out) if b - a < limit), None)
        if tiny is None:
            break
        a, b = out.pop(tiny)
        if tiny == 0:
            left = False
        elif tiny == len(out):
            left = True
        else:
            left = a - out[tiny - 1][1] <= out[tiny][0] - b
        j = tiny - 1 if left else tiny
        out[j] = (min(out[j][0], a), max(out[j][1], b))
    return out


def _min_cuts(counts: list[int], expected: int) -> list[tuple[int, int]]:
    """Eşit aralık sınırlarının ±%20 penceresinde en az opak pikselli sütundan keser.

    Kareler birbirine değiyorsa (kuyruk/pati ucu) düz eşit bölme karakteri ortadan keser;
    en ince sütun bitişik karenin parçasını en az taşır. Eşitlikte sınıra en yakın sütun.
    İçerik aralığı (ilk-son dolu sütun) bölünür: şerit kenarındaki boşluk ölçüyü bozmaz.
    """
    filled = [x for x, n in enumerate(counts) if n]
    if not filled:
        return []
    first, end = filled[0], filled[-1] + 1
    width = (end - first) / expected
    win = round(0.2 * width)
    cuts = [first]
    for i in range(1, expected):
        mid = first + round(i * width)
        lo, hi = max(cuts[-1] + 1, mid - win), min(end - 1, mid + win)
        if lo > hi:
            cuts.append(min(max(mid, lo), end - 1))
            continue
        cuts.append(min(range(lo, hi + 1), key=lambda x: (counts[x], abs(x - mid))))
    cuts.append(end)
    return list(zip(cuts, cuts[1:], strict=False))


def _blocks(mask: bytes, w: int, h: int) -> tuple[bytes, int, int]:
    """Maskeyi BLOCK×BLOCK hücrelere indirger (hücre = içinde opak piksel var mı); hızlıdır."""
    dw, dh = -(-w // BLOCK), -(-h // BLOCK)
    out = bytearray()
    for r in range(dh):
        acc = 0
        for y in range(r * BLOCK, min(h, (r + 1) * BLOCK)):
            acc |= int.from_bytes(mask[y * w:(y + 1) * w].ljust(dw * BLOCK, b"\0"), "big")
        row = acc.to_bytes(dw * BLOCK, "big")
        cell = 0
        for k in range(BLOCK):
            cell |= int.from_bytes(row[k::BLOCK], "big")
        out += cell.to_bytes(dw, "big")
    return bytes(out), dw, dh


def _components(grid: bytes, dw: int, dh: int) -> list[list[int]]:
    """4 komşulu bağlı bölgeler (hücre indeksleri); blok indirgeme küçük boşlukları kapatır."""
    seen = bytearray(len(grid))
    comps: list[list[int]] = []
    for start in range(len(grid)):
        if not grid[start] or seen[start]:
            continue
        seen[start] = 1
        stack, comp = [start], []
        while stack:
            p = stack.pop()
            comp.append(p)
            x = p % dw
            for q in (p - 1 if x else -1, p + 1 if x < dw - 1 else -1, p - dw, p + dw):
                if 0 <= q < len(grid) and grid[q] and not seen[q]:
                    seen[q] = 1
                    stack.append(q)
        comps.append(comp)
    return comps


def clean_frame(image: QImage) -> QImage:
    """Ana gövdeyi ve ona yakın parçaları (asa, yıldız, dizüstü) tutar, komşu artıklarını siler.

    Silinen: karenin sol/sağ kenarına değen ve gövdeye göre küçük (SLIVER) parçalar ile
    gövdeden uzak (gövde boyutunun >%15'i) kıvılcımlar. `image` yerinde değişir.
    """
    w, h = image.width(), image.height()
    grid, dw, dh = _blocks(_mask(image), w, h)
    comps = _components(grid, dw, dh)
    if len(comps) < 2:
        return image
    main = max(comps, key=len)
    mxs, mys = [p % dw for p in main], [p // dw for p in main]
    box = (min(mxs), min(mys), max(mxs), max(mys))
    near = 0.15 * max(box[2] - box[0] + 1, box[3] - box[1] + 1)
    painter = QPainter(image)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    for comp in comps:
        if comp is main:
            continue
        xs, ys = [p % dw for p in comp], [p // dw for p in comp]
        gap = max(box[0] - max(xs), min(xs) - box[2], box[1] - max(ys), min(ys) - box[3], 0)
        edge = min(xs) == 0 or max(xs) == dw - 1
        if gap > near or (edge and len(comp) < SLIVER * len(main)):
            for p in comp:
                painter.fillRect(
                    QRect((p % dw) * BLOCK, (p // dw) * BLOCK, BLOCK, BLOCK), Qt.GlobalColor.black
                )
    painter.end()
    return image


def slice_strip(strip: QImage, expected: int) -> list[Frame | None]:
    """Şeridi saydam sütun boşluklarından böler; sayı tutmazsa en ince sütundan keser.

    Opak zeminli şerit önce `remove_background`'dan geçer. Her kare `clean_frame`'den geçer.
    Boş dilim `None` olur.
    """
    strip = remove_background(strip)
    w, h = strip.width(), strip.height()
    mask = _mask(strip)
    runs = _merge_fragments(_runs(_cols(mask, w, h)))
    spans = runs if len(runs) == expected else _min_cuts(
        [sum(mask[x::w]) for x in range(w)], expected)
    frames: list[Frame | None] = []
    for x0, x1 in spans:
        crop = strip.copy(QRect(x0, 0, x1 - x0, h))
        if not any(_mask(crop)):
            frames.append(None)
            continue
        clean = trim_or_none(clean_frame(crop))
        frames.append(None if clean is None else Frame(*clean))
    return frames + [None] * (expected - len(frames))


def trim_or_none(image: QImage) -> tuple[QImage, int] | None:
    """Alfa sınırına kırpılmış görsel ve üst y'si; boşsa None."""
    w = image.width()
    mask = _mask(image)
    box = _bbox(mask, w, 0, w)
    if box is None:
        return None
    cols = _cols(mask, w, image.height())
    left = len(cols) - len(cols.lstrip(b"\x00"))
    right = len(cols.rstrip(b"\x00"))
    return image.copy(QRect(left, box[0], right - left, box[1] - box[0])), box[0]


def trim(image: QImage) -> QImage:
    trimmed = trim_or_none(image)
    if trimmed is None:
        raise PetImageError("Model görseli boş çıktı.")
    return trimmed[0]


# ------------------------------------------------------------- yerleşim ve sayfa


def _envelope(frames: list[Frame | None]) -> tuple[int, int, int]:
    """(zemin y, zarf yüksekliği, en geniş kare) — zemin: en alttaki kare tabanı."""
    live = [f for f in frames if f is not None]
    ground = max(f.top + f.image.height() for f in live)
    return ground, ground - min(f.top for f in live), max(f.image.width() for f in live)


def row_scale(frames: list[Frame | None], base: QImage) -> float:
    """Satırı temel karakterin hücredeki boyutuna eşler (model her şeritte farklı boyutta çizer).

    Boyut ölçüsü opak alanın karekökü: yatay koşu ya da kıvrılmış poz, bbox yüksekliği/genişliği
    gibi poza göre oynamaz. Satırın medyan karesi alınır, tek tuhaf kare ölçeği bozmaz. Sonra
    zıplama zarfı ve en geniş kare hücreye sığacak şekilde kısılır.
    """
    live = [f for f in frames if f is not None]
    if not live:
        raise PetImageError("Hiçbir satırdan kullanılabilir kare çıkmadı.")
    base_fit = min(FILL * CELL_H / base.height(), 0.98 * CELL_W / base.width())
    ratio = (sum(_mask(base)) / median(sum(_mask(f.image)) for f in live)) ** 0.5
    _ground, height, width = _envelope(frames)
    return float(min(base_fit * ratio, (BASELINE - 2) / height, 0.98 * CELL_W / width))


def place_row(frames: list[Frame | None], scale: float) -> list[QImage]:
    """Kareleri ortak zemine hizalar, yatayda ortalar; her biri 192×208 hücre."""
    ground = _envelope(frames)[0] if any(frames) else 0
    cells: list[QImage] = []
    for frame in frames:
        cell = QImage(CELL_W, CELL_H, _ARGB)
        cell.fill(0)
        if frame is not None:
            img = frame.image.scaled(
                max(1, round(frame.image.width() * scale)),
                max(1, round(frame.image.height() * scale)),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            bottom = BASELINE - round((ground - frame.top - frame.image.height()) * scale)
            painter = QPainter(cell)
            painter.drawImage((CELL_W - img.width()) // 2, bottom - img.height(), img)
            painter.end()
        cells.append(cell)
    return cells


def assemble(
    rows: dict[int, list[Frame | None]], base: QImage
) -> tuple[QImage, list[int]]:
    """Satırlardan 1536×1872 sayfa. Eksik satır prosedürel dolar; (sayfa, yedek satırlar)."""
    cells: dict[int, list[QImage]] = {
        r: place_row(f, row_scale(f, base)) for r, f in rows.items() if any(f)
    }
    if ps.RUN_RIGHT in cells:  # sola koşma = sağa koşmanın yatay aynası, çağrı yok
        cells[ps.RUN_LEFT] = [c.flipped(Qt.Orientation.Horizontal) for c in cells[ps.RUN_RIGHT]]
    fallback = [r for r in range(len(FRAMES)) if r not in cells]
    for r in fallback:
        cells[r] = pet_animator.procedural_row(base, r)
    sheet = QImage(pet_animator.SHEET_W, pet_animator.SHEET_H, _ARGB)
    sheet.fill(0)
    painter = QPainter(sheet)
    for r, row_cells in cells.items():
        for c, cell in enumerate(row_cells):
            painter.drawImage(c * CELL_W, r * CELL_H, cell)
    painter.end()
    return sheet, fallback


# ------------------------------------------------------------------ model adımları


def make_base(description: str, generate: Generate) -> tuple[QImage, bytes]:
    """İlk temel görsel (generations); (kırpılmış görsel, referans PNG)."""
    return _base(generate(STYLE + description, None, BASE_SIZE))


def edit_base(change: str, base_png: bytes, generate: Generate) -> tuple[QImage, bytes]:
    """Düzeltme (edits): mevcut temel referans, aynı karakter korunur."""
    prompt = (
        f"Using the same exact character, apply this change: {change}. Keep the same style, "
        "full body, facing the viewer, centered, transparent background, no text."
    )
    return _base(generate(prompt, base_png, BASE_SIZE))


def _base(data: bytes) -> tuple[QImage, bytes]:
    image = trim(remove_background(decode(data, BASE_SIZE)))
    return image, to_png(image)


def animate(
    base_png: bytes, base: QImage, generate: Generate, *,
    progress: Callable[[int, int, int], None] = lambda done, total, row: None,
    row_ready: Callable[[int, list[QImage]], None] = lambda row, cells: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[QImage, list[int]]:
    """8 çağrı (satır başına en çok 2 deneme); (sayfa, prosedürel yedeğe düşen satırlar).

    `BudgetExceeded` yedeğe düşmez, üretimi durdurur (ücret/izin sorunu satır sorunu değil).
    """
    rows: dict[int, list[Frame | None]] = {}
    for done, (row, _) in enumerate(ROWS):
        if cancelled():
            raise Cancelled
        progress(done, len(ROWS), row)
        for _attempt in range(2):
            try:
                strip = decode(generate(row_prompt(row), base_png, STRIP_SIZE), STRIP_SIZE)
                frames = slice_strip(strip, FRAMES[row])
                if not any(frames):
                    raise PetImageError("Şerit boş.")
            except BudgetExceeded:
                raise
            except (ProviderError, PetImageError):
                if cancelled():
                    raise Cancelled from None
                continue
            rows[row] = frames
            row_ready(row, place_row(frames, row_scale(frames, base)))
            break
    sheet, fallback = assemble(rows, base)
    return sheet, fallback
