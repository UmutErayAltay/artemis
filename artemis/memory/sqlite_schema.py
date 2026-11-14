"""SQLite şema sürümü (`PRAGMA user_version`) ve geçiş öncesi yedek.

Kendi hafıza veritabanlarımız (`memory.sqlite3`, `degisiklikler.sqlite3`) şimdiye dek
sürümsüzdü ve geçişler (ALTER) yedeksiz yapılıyordu. Burada sürüm kurulur; ileride bir yapı
değişirse geçişten önce `<db>.bak-v<eski>` yedeği alınır. Yedek tam kopyadır (içerik taşır),
dosyayla aynı gizlilik düzeyinde tutulur.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path


class SchemaTooNewError(sqlite3.DatabaseError):
    """Veritabanını daha yeni bir Artemis yazmış; sessizce devam edilmez."""


def ensure_schema(
    db: sqlite3.Connection,
    path: Path,
    version: int,
    migrate: Callable[[sqlite3.Connection, int], None],
) -> None:
    """Şemayı `version` düzeyine getirir; geçişten ÖNCE mevcut dolu veritabanını yedekler.

    `user_version == 0` yeni dosya ya da sürümsüz eski şema demektir; `migrate` idempotent
    olmalıdır (CREATE IF NOT EXISTS / sütun yoksa ALTER) ve her iki durumda çalışır.
    """
    current = int(db.execute("PRAGMA user_version").fetchone()[0])
    if current > version:
        raise SchemaTooNewError(
            f"{path.name} şema sürümü {current}; bu Artemis en çok {version} biliyor."
        )
    if current == version:
        return
    has_tables = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
    if has_tables:  # yeni oluşmuş boş dosyayı yedeklemenin anlamı yok
        _backup(db, path.with_name(f"{path.name}.bak-v{current}"))
    migrate(db, current)
    db.execute(f"PRAGMA user_version = {int(version)}")
    db.commit()


def _backup(db: sqlite3.Connection, target: Path) -> None:
    """Geçici dosyaya yedekler, sonra atomik değiştirir (eski yedek yarım kalmaz)."""
    temp = target.with_name(target.name + ".tmp")
    copy = sqlite3.connect(temp)
    try:
        with copy:
            db.backup(copy)
    except sqlite3.Error:
        copy.close()
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    copy.close()
    if os.name != "nt":
        temp.chmod(0o600)
    os.replace(temp, target)
