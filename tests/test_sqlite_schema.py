"""Şema sürümü ve geçiş öncesi yedek: gerçek geçici SQLite dosyalarıyla."""

import sqlite3
from pathlib import Path

import pytest

from artemis.memory import companion
from artemis.memory.sqlite_schema import SchemaTooNewError, ensure_schema
from artemis.memory.store import MemoryStore


def _version(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def _bak(path: Path, v: int) -> Path:
    return path.with_name(f"{path.name}.bak-v{v}")


def test_memory_store_fresh_db_versioned_without_backup(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    MemoryStore(path)
    assert _version(path) == 1
    assert not _bak(path, 0).exists()


def test_memory_store_legacy_db_backed_up_then_versioned(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(path) as db:  # sürümsüz, notes.private'sız eski şema
        db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL,"
                   " created_at TEXT NOT NULL, turn_id TEXT NOT NULL)")
        db.execute("INSERT INTO notes VALUES (1, 'eski not', 'x', 't')")
    MemoryStore(path)
    assert _version(path) == 1
    with sqlite3.connect(_bak(path, 0)) as bak:
        assert bak.execute("SELECT * FROM notes").fetchall() == [(1, "eski not", "x", "t")]
        assert "private" not in {r[1] for r in bak.execute("PRAGMA table_info(notes)")}
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT text, private FROM notes").fetchall() == [("eski not", 0)]


def test_reopen_is_idempotent_and_keeps_old_backup_untouched(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    MemoryStore(path)
    MemoryStore(path)
    assert _version(path) == 1
    assert not _bak(path, 0).exists()


def test_newer_version_raises(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    MemoryStore(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 9")
    with pytest.raises(SchemaTooNewError):
        MemoryStore(path)


def test_companion_fresh_and_legacy(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with companion._connect(state):
        pass
    path = companion.companion_dir(state) / "degisiklikler.sqlite3"
    assert _version(path) == 1
    assert not _bak(path, 0).exists()

    legacy = tmp_path / "legacy"
    folder = companion.companion_dir(legacy)
    folder.mkdir(parents=True)
    lpath = folder / "degisiklikler.sqlite3"
    with sqlite3.connect(lpath) as db:
        db.execute("CREATE TABLE changes (id INTEGER PRIMARY KEY, at TEXT NOT NULL, file TEXT"
                   " NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL, source TEXT NOT NULL,"
                   " turn_id TEXT NOT NULL, tainted INTEGER NOT NULL, state TEXT NOT NULL)")
        db.execute("INSERT INTO changes VALUES (1, 'a', 'f', 'b', 'c', 's', 't', 1, 'completed')")
    with companion._connect(legacy):
        pass
    assert _version(lpath) == 1
    with sqlite3.connect(_bak(lpath, 0)) as bak:
        assert bak.execute("SELECT id, state FROM changes").fetchall() == [(1, "completed")]


def test_backup_replaces_older_backup_and_migrate_receives_version(tmp_path: Path) -> None:
    path = tmp_path / "x.sqlite3"
    seen: list[int] = []
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE t (v INTEGER)")
        db.execute("INSERT INTO t VALUES (1)")
    _bak(path, 0).write_bytes(b"eski yedek")
    db = sqlite3.connect(path)
    ensure_schema(db, path, 1, lambda _db, v: seen.append(v))
    db.close()
    assert seen == [0]
    with sqlite3.connect(_bak(path, 0)) as bak:
        assert bak.execute("SELECT v FROM t").fetchall() == [(1,)]
