"""Artemis'in kendi küçük hafıza dosyaları (ADR 0054): `state_dir/hafiza/`.

Vault kullanıcının dış içeriğidir ve tura kendiliğinden girmez (ADR 0015). Bu dosyalar ise
Artemis'in kendi sınırlı belleğidir; her kurulumda vardır. Her yazma önce `prepared` olarak
`degisiklikler.sqlite3` günlüğüne girer, dosya doğrulanınca `completed` olur; kullanıcı
değişiklikleri görüp geri alabilir (Faz B).
"""

import os
import sqlite3
import tempfile
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COMPANION_FILES = {
    "kimlik": "Kimlik.md",
    "isler": "Isler.md",
    "son_oturum": "SonOturum.md",
}

_TEMPLATES = {
    "kimlik": "# Kimlik\n\nKullanıcının adı, hitabı ve çalışma tercihleri buraya yazılır.\n",
    "isler": "# Açık işler\n\nHenüz kayıtlı açık iş yok.\n",
    "son_oturum": "# Son oturum\n\nSon oturumun kısa özeti ve kaldığımız yer buraya yazılır.\n",
}


def companion_dir(state_dir: Path) -> Path:
    return state_dir / "hafiza"


def ensure_skeleton(state_dir: Path) -> list[str]:
    """Eksik dosyaları "x" kipiyle açar: var olan kullanıcı/Artemis içeriği ASLA ezilmez.

    Symlink reddedilir; yoksa hafıza klasörü bağlantı üzerinden state_dir dışına yazardı.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    folder = companion_dir(state_dir)
    # mkdir'den ÖNCE: bağlantı hedefinde klasör oluşturmak state_dir dışına yazmaktır.
    if folder.is_symlink():
        raise OSError("Hafıza klasörü sembolik bağlantı olamaz.")
    folder.mkdir(exist_ok=True)
    created: list[str] = []
    for key, name in COMPANION_FILES.items():
        target = folder / name
        if target.is_symlink():
            raise OSError(f"{name} sembolik bağlantı olamaz.")
        try:
            with target.open("x", encoding="utf-8", newline="") as stream:
                stream.write(_TEMPLATES[key])
        except FileExistsError:
            continue
        created.append(name)
    return created


def read_companion(state_dir: Path, key: str, limit: int) -> str:
    """Dosyanın başını `limit` karaktere kadar okur; yok/bağlantı/okunamaz ise boş döner."""
    folder = companion_dir(state_dir)
    path = folder / COMPANION_FILES[key]
    if folder.is_symlink() or path.is_symlink():
        return ""
    try:
        with path.open("r", encoding="utf-8") as stream:
            return stream.read(limit)
    except (OSError, UnicodeError):
        return ""


# Dosya başına en çok karakter: sınırı aşan metin sessizce kesilmez, reddedilir.
MAX_CHARS = {"kimlik": 4000, "isler": 6000, "son_oturum": 3000}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    file TEXT NOT NULL,
    before TEXT NOT NULL,
    after TEXT NOT NULL,
    source TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    tainted INTEGER NOT NULL,
    state TEXT NOT NULL
)
"""


class CompanionConflictError(Exception):
    """Geri alma, dosya o değişiklikten sonra yeniden değiştiği için reddedildi."""


def _connect(state_dir: Path) -> closing[sqlite3.Connection]:
    folder = companion_dir(state_dir)
    if folder.is_symlink():
        raise OSError("Hafıza klasörü sembolik bağlantı olamaz.")
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "degisiklikler.sqlite3", timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(_SCHEMA)
    return closing(db)


def _target(state_dir: Path, key: str) -> Path:
    if key not in COMPANION_FILES:
        raise ValueError(f"Bilinmeyen hafıza dosyası: {key}")
    folder = companion_dir(state_dir)
    path = folder / COMPANION_FILES[key]
    if folder.is_symlink() or path.is_symlink():
        raise OSError("Hafıza dosyası veya klasörü sembolik bağlantı olamaz.")
    return path


def _read_exact(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()
    except FileNotFoundError:
        return ""


def write_companion(
    state_dir: Path, key: str, text: str, *, source: str, turn_id: str, tainted: bool
) -> int:
    """Dosyayı tamamen değiştirir; günlük `prepared` → `completed`/`failed`. Kimlik döner."""
    path = _target(state_dir, key)
    if source != "undo" and len(text) > MAX_CHARS[key]:
        raise ValueError(f"{COMPANION_FILES[key]} en çok {MAX_CHARS[key]} karakter olabilir.")
    before = _read_exact(path)
    if source != "undo" and content_tainted(state_dir, key):
        # Makbuz/oturum güncellemesi eski bölümleri korur; lekeli metin temiz bir yazımla
        # "aklanmasın". Leke yalnız kullanıcının geri alma işlemiyle (kendi geçmişinden) düşer.
        tainted = True
    with _connect(state_dir) as db:
        with db:
            cursor = db.execute(
                "INSERT INTO changes (at, file, before, after, source, turn_id, tainted, state)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared')",
                (
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    key, before, text, source, turn_id, int(tainted),
                ),
            )
        change_id = int(cursor.lastrowid or 0)
        temp_name = ""
        try:
            fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".yaz-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
            os.replace(temp_name, path)
            if _read_exact(path) != text:
                raise OSError("Yazılan hafıza dosyası geri okunduğunda eşleşmedi.")
        except (OSError, UnicodeError):
            if temp_name:
                with suppress(OSError):
                    os.unlink(temp_name)
            with db:
                db.execute("UPDATE changes SET state = 'failed' WHERE id = ?", (change_id,))
            raise
        with db:
            db.execute("UPDATE changes SET state = 'completed' WHERE id = ?", (change_id,))
    return change_id


def list_changes(state_dir: Path, limit: int = 10) -> list[dict[str, Any]]:
    """En yeni önce; yalnız tamamlanmış değişiklikler."""
    with _connect(state_dir) as db:
        rows = db.execute(
            "SELECT id, at, file, before, after, source, turn_id, tainted FROM changes"
            " WHERE state = 'completed' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    names = ("id", "at", "file", "before", "after", "source", "turn_id", "tainted")
    return [{**dict(zip(names, row, strict=True)), "tainted": bool(row[7])} for row in rows]


def undo_change(state_dir: Path, change_id: int, *, turn_id: str, tainted: bool) -> int:
    """`before` içeriğini yalnız dosya hâlâ o değişikliğin `after` hâlindeyse geri yazar."""
    with _connect(state_dir) as db:
        row = db.execute(
            "SELECT file, before, after FROM changes WHERE id = ? AND state = 'completed'",
            (change_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"{change_id} numaralı tamamlanmış değişiklik yok.")
    key, before, after = row
    if _read_exact(_target(state_dir, key)) != after:
        raise CompanionConflictError(
            "Dosya bu değişiklikten sonra yeniden değişti; üzerine yazmamak için geri alınmadı."
        )
    # Geri yüklenen metin daha önce lekeli bir turda yazıldıysa leke işareti de geri gelir;
    # yoksa temiz turdaki bir "geri al", dış içerikten gelen metni temiz gibi gösterirdi.
    with _connect(state_dir) as db:
        restored_tainted = db.execute(
            "SELECT 1 FROM changes WHERE file = ? AND state = 'completed' AND tainted = 1"
            " AND after = ? LIMIT 1",
            (key, before),
        ).fetchone()
    return write_companion(
        state_dir, key, before, source="undo", turn_id=turn_id,
        tainted=tainted or restored_tainted is not None,
    )


def content_tainted(state_dir: Path, key: str) -> bool:
    """Dosyanın şu anki içeriği dış içerik okunan bir turda mı yazıldı (son tamamlanan yazım).

    Günlük hiç oluşmadıysa yazım da olmamıştır: iskelet şablonu temizdir (yeni kurulumda her tur
    lekeli sayılmasın). Var olan günlük okunamazsa güvenli taraf lekeli kabul etmektir.
    """
    if not (companion_dir(state_dir) / "degisiklikler.sqlite3").is_file():
        return False
    try:
        with _connect(state_dir) as db:
            row = db.execute(
                "SELECT tainted FROM changes WHERE file = ? AND state = 'completed'"
                " ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        # Günlük okunamıyorsa içerik temiz sayılmaz: güvenli taraf lekeli kabul etmektir.
        return True
    return bool(row and row[0])
