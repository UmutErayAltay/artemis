"""SQLite şema sürümü (`PRAGMA user_version`) ve geçici geçiş öncesi yedek.

Kendi hafıza veritabanlarımız (`memory.sqlite3`, `degisiklikler.sqlite3`) şimdiye dek
sürümsüzdü. Sürüm yükseltilirken geçiş tek işlemde (transaction) koşar. Yedek yalnız geçiş
süresince yaşar: başarıyla biterse hemen silinir, çünkü kalıcı yedek kullanıcının sonradan
sildiği (`memory_forget`, geçmiş silme) içeriği saklamaya devam ederdi. Geçiş başarısız olursa
yedek kalır ve hata yolunu söyler; kullanıcı silme işlemleri artık kalan yedeği de siler
(`purge_backups`). Yedek tam kopyadır, veritabanıyla aynı gizlilik düzeyinde (0o600) açılır.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path


class SchemaError(sqlite3.DatabaseError):
    """Şema sürümü/geçişi güvenilir biçimde tamamlanamadı; sessizce devam edilmez."""


class SchemaTooNewError(SchemaError):
    """Veritabanını daha yeni bir Artemis yazmış."""


def ensure_schema(
    db: sqlite3.Connection,
    path: Path,
    version: int,
    migrate: Callable[[sqlite3.Connection, int], None],
) -> None:
    """Şemayı `version` düzeyine getirir; dolu veritabanını geçiş süresince yedekler.

    `user_version == 0` yeni dosya ya da sürümsüz eski şema demektir; `migrate` idempotent
    olmalıdır (CREATE IF NOT EXISTS / sütun yoksa ALTER), commit etmemelidir (`executescript`
    kullanmaz) ve her iki durumda çalışır. Başarısızlıkta işlem geri alınır, sürüm artmaz.
    """
    current = _user_version(db)
    if current > version:
        raise SchemaTooNewError(
            f"{path.name} şema sürümü {current}; bu Artemis en çok {version} biliyor."
        )
    if current == version:
        return
    has_tables = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
    # Yeni oluşmuş boş dosyayı yedeklemenin anlamı yok.
    backup = _backup(db, path, current) if has_tables else None
    try:
        db.execute("BEGIN IMMEDIATE")
        # Kilidi beklerken başka bağlantı geçişi bitirmiş olabilir.
        if _user_version(db) < version:
            migrate(db, current)
            db.execute(f"PRAGMA user_version = {int(version)}")
        db.commit()
    except (sqlite3.Error, OSError) as error:
        with contextlib.suppress(sqlite3.Error):
            db.rollback()
        where = f" Yedek: {backup}" if backup is not None else ""
        raise SchemaError(f"{path.name} şema geçişi başarısız, geri alındı.{where}") from error
    if backup is not None:
        _unlink_backup(backup)  # başarı: yedek silinen içeriği saklamasın


def purge_backups(path: Path) -> None:
    """Kullanıcı silme işlemlerinden sonra kalan geçiş yedeklerini siler."""
    for leftover in path.parent.glob(f"{path.name}.bak-v*"):
        _unlink_backup(leftover)


def _user_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def _is_link(path: Path) -> bool:
    return os.path.islink(path) or os.path.isjunction(path)


def _backup(db: sqlite3.Connection, path: Path, current: int) -> Path:
    """`<db>.bak-v<eski>`: mkstemp (O_EXCL, rastgele ad, 0o600) + atomik os.replace."""
    target = path.with_name(f"{path.name}.bak-v{current}")
    if _is_link(path.parent) or _is_link(path) or _is_link(target):
        raise SchemaError("Yedek konumu sembolik bağlantı veya junction olamaz.")
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.bak-")
    os.close(fd)
    temp = Path(temp_name)
    copy: sqlite3.Connection | None = None
    try:
        if os.name != "nt" and stat.S_IMODE(temp.stat().st_mode) != 0o600:
            raise SchemaError("Yedek dosyası izinleri doğrulanamadı.")
        copy = sqlite3.connect(temp)
        db.backup(copy)
        copy.close()
        copy = None
        os.replace(temp, target)
    except (sqlite3.Error, OSError) as error:
        if copy is not None:
            copy.close()
        _unlink_backup(temp)
        raise SchemaError("Geçiş öncesi yedek alınamadı.") from error
    return target


def _unlink_backup(path: Path) -> None:
    for candidate in (path, *(path.with_name(path.name + s) for s in ("-wal", "-shm", "-journal"))):
        with contextlib.suppress(OSError):
            candidate.unlink()
