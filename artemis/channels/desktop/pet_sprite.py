"""Sprite pet'ler (ADR 0048): Codex uyumlu `pet.json` + spritesheet yükleme, kayıt ve içe aktarma.

Güven sınırı burasıdır: dışarıdan gelen klasör/zip yalnız `pet.json` + tek görsel olabilir,
her üye `safe_join(allow_symlinks=False)` ve boyut sınırlarından geçer, görsel yalnız Qt
`QImage` ile çözülür. Topluluk pet'lerinin lisansı kullanıcının sorumluluğundadır.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import IO, Literal

import yaml
from pydantic import ValidationError
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QPixmap

from artemis.config.settings import load_settings
from artemis.safety.paths import safe_join

log = logging.getLogger(__name__)

ORB = "orb"
COLS = 8
CELL_W, CELL_H = 192, 208
# v1 ve v2: v2'nin iki ek bakış satırı yok sayılır.
SHEET_SIZES = frozenset({(1536, 1872), (1536, 2288)})
# Satır sırası: idle, running-right, running-left, waving, jumping, failed, waiting, running, review
FRAMES = (6, 8, 8, 4, 5, 8, 6, 6, 6)
IDLE, RUN_RIGHT, RUN_LEFT, WAVING, JUMPING, FAILED, WAITING, RUNNING, REVIEW = range(9)
FPS = 8.0  # kare hızı (sn başına); tek ayar noktası
STATE_ROW = {
    "idle": IDLE, "off": IDLE, "listening": REVIEW, "thinking": RUNNING,
    "speaking": WAVING, "approval": WAITING, "error": FAILED,
}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
IMAGE_SUFFIXES = (".webp", ".png")
_CHUNK = 1024 * 1024

Source = Literal["artemis", "codex"]


class PetError(Exception):
    """Pet kullanılamıyor; mesaj kullanıcıya gösterilebilir (Türkçe)."""


class PetFormatError(PetError):
    """pet.json ya da görsel biçime uymuyor."""


class PetImportError(PetError):
    """İçe aktarma güvenlik ya da boyut sınırına takıldı."""


class PetExistsError(PetImportError):
    """Aynı kimlikli pet var; yalnız açık `overwrite` ile ezilir."""


@dataclass(frozen=True)
class PetInfo:
    id: str
    display_name: str
    description: str
    folder: Path
    sheet: Path
    source: Source


def codex_pets_dir() -> Path:
    """Codex pet'leri (salt okunur); CODEX_HOME varsa o, yoksa ~/.codex."""
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "pets"


def pets_dir_for(config_path: Path) -> Path:
    """Artemis'in yazılabilir pet klasörü: state_dir/pets."""
    try:
        return load_settings(config_path).state_dir / "pets"
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        # Bozuk config'te pencere zaten hatayı gösterir; pet'ler config yanında aranır.
        return config_path.resolve().parent / "pets"


def _text(data: dict[str, object], key: str, limit: int, *, required: bool = True) -> str:
    value = data.get(key, None if required else "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise PetFormatError(f"pet.json: '{key}' alanı eksik ya da metin değil.")
    if len(value) > limit:
        raise PetFormatError(f"pet.json: '{key}' alanı {limit} karakterden uzun.")
    return value


def _header_size(path: Path) -> QSize:
    """Çözmeden başlıktan boyut; boyut doğrulanmadan piksel ayrılmaz. Okuyucu burada kapanır
    (Windows'ta açık tutamaç klasörün silinmesini engeller)."""
    return QImageReader(str(path)).size()


def read_pet(folder: Path, source: Source) -> PetInfo:
    """pet.json ve görsel başlığını doğrular (görseli çözmez; bkz. `load_sheet`)."""
    # Artemis klasörü yazılabilirdir, bağlantı kabul edilmez; Codex'inkine dokunmadan okuruz.
    links = source == "codex"
    try:
        meta = safe_join(folder, "pet.json", allow_symlinks=links)
        if not meta.is_file() or meta.stat().st_size > MAX_JSON_BYTES:
            raise PetFormatError("pet.json yok ya da 64 KB'tan büyük.")
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:  # JSONDecodeError/UnicodeDecodeError ValueError'dır
        if isinstance(error, PetFormatError):
            raise
        raise PetFormatError(f"pet.json okunamadı: {error}") from error
    if not isinstance(data, dict):
        raise PetFormatError("pet.json bir nesne olmalı.")
    pet_id = _text(data, "id", 64)
    if not ID_RE.fullmatch(pet_id):
        raise PetFormatError("Pet kimliği küçük harf, rakam ve '-' olmalı (en çok 64 karakter).")
    name = _text(data, "displayName", 80)
    description = _text(data, "description", 500, required=False)
    sheet_name = _text(data, "spritesheetPath", 120)
    if not sheet_name.lower().endswith(IMAGE_SUFFIXES):
        raise PetFormatError("Spritesheet .webp ya da .png olmalı.")
    try:
        sheet = safe_join(folder, sheet_name, allow_symlinks=links)
        if not sheet.is_file():
            raise PetFormatError("pet.json'daki spritesheet dosyası yok.")
        if sheet.stat().st_size > MAX_IMAGE_BYTES:
            raise PetFormatError("Spritesheet 20 MB'tan büyük.")
    except (OSError, ValueError) as error:
        if isinstance(error, PetFormatError):
            raise
        raise PetFormatError(f"Spritesheet yolu geçersiz: {error}") from error
    size = _header_size(sheet)
    if not size.isValid():
        raise PetFormatError("Spritesheet görseli okunamadı (WebP/PNG değil ya da bozuk).")
    if (size.width(), size.height()) not in SHEET_SIZES:
        raise PetFormatError(
            f"Spritesheet boyutu {size.width()}×{size.height()}; 1536×1872 ya da 1536×2288 olmalı."
        )
    return PetInfo(pet_id, name, description, folder, sheet, source)


def load_sheet(info: PetInfo) -> QImage:
    image = QImage()
    if not image.load(str(info.sheet)) or image.isNull():
        raise PetFormatError("Spritesheet görseli çözülemedi.")
    if (image.width(), image.height()) not in SHEET_SIZES:
        raise PetFormatError("Spritesheet boyutu beklenenden farklı.")
    return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _cell(sheet: QImage, row: int, col: int) -> QImage:
    return sheet.copy(QRect(col * CELL_W, row * CELL_H, CELL_W, CELL_H))


def preview(info: PetInfo, height: int = 48) -> QPixmap:
    """Liste simgesi: idle satırının ilk karesi."""
    cell = _cell(load_sheet(info), IDLE, 0)
    smooth = Qt.TransformationMode.SmoothTransformation
    return QPixmap.fromImage(cell.scaledToHeight(height, smooth))


def row_for(
    state: str, reaction: str | None, react_age: float, dragging: bool, run_right: bool
) -> int:
    """Durum -> sprite satırı (ADR 0048 madde 2). Sürükleme her şeyin üstündedir."""
    if dragging:
        return RUN_RIGHT if run_right else RUN_LEFT
    if state in STATE_ROW and STATE_ROW[state] != IDLE:
        return STATE_ROW[state]
    if reaction == "happy" and react_age < FRAMES[JUMPING] / FPS:
        return JUMPING  # bir tur
    if reaction == "sad":
        return FAILED
    return IDLE


class SpritePet:
    """Yüklü spritesheet; kareleri BODY yüksekliğine ölçekleyip önbelleğe alır."""

    def __init__(self, info: PetInfo, height: int) -> None:
        self.info = info
        self._sheet = load_sheet(info)
        self._height = height
        self._cache: dict[tuple[int, int], QPixmap] = {}

    def frame_index(self, row: int, seconds: float, reduced: bool) -> int:
        count = FRAMES[row]
        if reduced:
            return 0
        index = int(seconds * FPS)
        return min(index, count - 1) if row == JUMPING else index % count

    def pixmap(self, row: int, col: int) -> QPixmap:
        key = (row, col)
        if key not in self._cache:
            cell = _cell(self._sheet, row, col)
            self._cache[key] = QPixmap.fromImage(
                cell.scaledToHeight(self._height, Qt.TransformationMode.SmoothTransformation)
            )
        return self._cache[key]


# ---------------------------------------------------------------- kayıt


def list_pets(pets_dir: Path, codex_dir: Path | None = None) -> list[PetInfo]:
    """Artemis pet'leri önce; aynı kimlikte Artemis'inki Codex'inkini gölgeler. Bozuklar atlanır."""
    found: dict[str, PetInfo] = {}
    sources: tuple[tuple[Path, Source], ...] = (
        (pets_dir, "artemis"), (codex_dir if codex_dir is not None else codex_pets_dir(), "codex")
    )
    for root, source in sources:
        try:
            folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            continue  # klasör yok ya da okunamıyor: kaynak boş sayılır
        for folder in folders:
            try:
                info = read_pet(folder, source)
            except PetError as error:
                log.warning("Pet atlandı (%s): %s", folder.name, error)
                continue
            found.setdefault(info.id, info)
    return list(found.values())


def find_pet(pet_id: str, pets_dir: Path, codex_dir: Path | None = None) -> PetInfo | None:
    return next((p for p in list_pets(pets_dir, codex_dir) if p.id == pet_id), None)


# ------------------------------------------------------------ içe aktarma

# (ad, bildirilen boyut, açıcı)
_Entry = tuple[str, int, Callable[[], IO[bytes]]]


def file_opener(path: Path) -> Callable[[], IO[bytes]]:
    return lambda: path.open("rb")


def _folder_entries(src: Path) -> list[_Entry]:
    entries: list[_Entry] = []
    for path in sorted(src.iterdir()):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise PetImportError(f"Sembolik bağlantı kabul edilmez: {path.name}")
        if not stat.S_ISREG(mode):
            raise PetImportError(f"Klasör içinde alt klasör/özel dosya kabul edilmez: {path.name}")
        entries.append((path.name, path.stat().st_size, file_opener(path)))
    return entries


def _zip_entries(archive: zipfile.ZipFile) -> list[_Entry]:
    entries: list[_Entry] = []
    for member in archive.infolist():
        if stat.S_ISLNK(member.external_attr >> 16):
            raise PetImportError(f"Zip içinde sembolik bağlantı kabul edilmez: {member.filename}")
        if member.is_dir():
            raise PetImportError("Zip içinde klasör kabul edilmez; dosyalar kökte olmalı.")
        entries.append(
            (member.filename, member.file_size, partial(archive.open, member))
        )
    return entries


def _stage(entries: list[_Entry], staging: Path) -> None:
    if len(entries) != 2:
        raise PetImportError("Yalnız pet.json ve tek bir spritesheet görseli kabul edilir.")
    if sum(size for _, size, _ in entries) > MAX_ZIP_BYTES:
        raise PetImportError("Toplam boyut 25 MB sınırını aşıyor.")
    names = [name for name, _, _ in entries]
    if "pet.json" not in names or sum(n.lower().endswith(IMAGE_SUFFIXES) for n in names) != 1:
        raise PetImportError("Yalnız pet.json ve tek bir .webp/.png görsel kabul edilir.")
    total = 0
    for name, size, opener in entries:
        try:
            target = safe_join(staging, name, allow_symlinks=False)
        except ValueError as error:
            raise PetImportError(f"Güvensiz dosya yolu ({name}): {error}") from error
        if target.parent != staging.resolve():
            raise PetImportError(f"Alt klasör kabul edilmez: {name}")
        limit = MAX_JSON_BYTES if name == "pet.json" else MAX_IMAGE_BYTES
        if size > limit:
            raise PetImportError(f"{name} boyut sınırını aşıyor.")
        with opener() as source, target.open("xb") as out:
            written = 0
            while chunk := source.read(_CHUNK):
                written += len(chunk)
                total += len(chunk)
                # Bildirilen boyuta güvenilmez: gerçek okunan bayt sayılır (zip bombası).
                if written > limit or total > MAX_ZIP_BYTES:
                    raise PetImportError(f"{name} açılırken boyut sınırı aşıldı.")
                out.write(chunk)


def import_pet(src: Path, pets_dir: Path, *, overwrite: bool = False) -> PetInfo:
    """Klasör ya da .zip'ten state_dir/pets/<id>/ içine kurar; yarım kurulum kalmaz."""
    try:
        pets_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=pets_dir))
    except OSError as error:
        raise PetImportError(f"Pet klasörü hazırlanamadı: {error}") from error
    try:
        if src.is_dir():
            _stage(_folder_entries(src), staging)
        else:
            try:
                with zipfile.ZipFile(src) as archive:
                    _stage(_zip_entries(archive), staging)
            except PetError:
                raise
            # Şifreli üye RuntimeError, desteklenmeyen sıkıştırma NotImplementedError, bozuk
            # akış zlib.error/EOFError verir; arayüz yalnız PetError bekler.
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError,
                    zlib.error) as error:
                raise PetImportError("Dosya geçerli ya da desteklenen bir .zip değil.") from error
        info = read_pet(staging, "artemis")
        load_sheet(info)  # bozuk gövde kurulumdan önce yakalanır
        target = pets_dir / info.id
        if target.exists() or target.is_symlink():
            if not overwrite:
                raise PetExistsError(f"'{info.id}' kimlikli pet zaten kurulu.")
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
        os.replace(staging, target)
        return read_pet(target, "artemis")
    except OSError as error:
        raise PetImportError(f"Pet kurulamadı: {error}") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def delete_pet(pets_dir: Path, pet_id: str) -> None:
    """Yalnız Artemis'in kendi klasöründeki pet silinir; Codex pet'lerine hiç dokunulmaz."""
    if not ID_RE.fullmatch(pet_id):
        raise PetError("Geçersiz pet kimliği.")
    try:
        folder = safe_join(pets_dir, pet_id, allow_symlinks=False)
        if not folder.is_dir():
            raise PetError("Pet bulunamadı.")
        shutil.rmtree(folder)
    except (OSError, ValueError) as error:
        raise PetError(f"Pet silinemedi: {error}") from error

