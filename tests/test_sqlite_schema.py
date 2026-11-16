"""Şema sürümü ve geçici geçiş yedeği: gerçek geçici SQLite dosyalarıyla."""

import os
import sqlite3
from pathlib import Path

import pytest

from artemis.memory import companion, sqlite_schema
from artemis.memory.sqlite_schema import SchemaError, SchemaTooNewError, ensure_schema
from artemis.memory.store import MemoryStore


def _version(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def _baks(path: Path) -> list[Path]:
    return [p for p in path.parent.iterdir() if ".bak" in p.name]


def _legacy_notes_db(path: Path) -> None:
    with sqlite3.connect(path) as db:  # sürümsüz, notes.private'sız eski şema
        db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL,"
                   " created_at TEXT NOT NULL, turn_id TEXT NOT NULL)")
        db.execute("INSERT INTO notes VALUES (1, 'eski not', 'x', 't')")


def test_memory_store_fresh_db_versioned_without_backup(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    MemoryStore(path)
    assert _version(path) == 2
    assert _baks(path) == []


def test_memory_store_legacy_db_migrated_and_backup_removed(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    _legacy_notes_db(path)
    MemoryStore(path)
    assert _version(path) == 2
    assert _baks(path) == []  # başarılı geçiş: yedek kalmaz (silinen içeriği saklamasın)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT text, private FROM notes").fetchall() == [("eski not", 0)]


def test_failed_migration_rolls_back_keeps_backup_and_names_it(tmp_path: Path) -> None:
    path = tmp_path / "x.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE t (v INTEGER)")
        db.execute("INSERT INTO t VALUES (1)")

    def bad(db: sqlite3.Connection, _v: int) -> None:
        db.execute("ALTER TABLE t ADD COLUMN w INTEGER")
        raise sqlite3.OperationalError("yarıda")

    db = sqlite3.connect(path)
    with pytest.raises(SchemaError, match=r"\.bak-v0"):
        ensure_schema(db, path, 1, bad)
    db.close()
    assert _version(path) == 0
    with sqlite3.connect(path) as check:  # geri alındı: sütun yok
        assert [r[1] for r in check.execute("PRAGMA table_info(t)")] == ["v"]
    (bak,) = _baks(path)
    with sqlite3.connect(bak) as check:
        assert check.execute("SELECT v FROM t").fetchall() == [(1,)]


def test_suppressed_alter_failure_does_not_bump_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "memory.sqlite3"
    _legacy_notes_db(path)
    real = sqlite3.Connection.execute

    class Guard(sqlite3.Connection):
        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:  # type: ignore[override]
            if sql.startswith("ALTER TABLE notes"):
                raise sqlite3.OperationalError("kilitli")
            return real(self, sql, *args)  # type: ignore[arg-type]

    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=Guard, **k))
    with pytest.raises(SchemaError):
        MemoryStore(path)
    monkeypatch.undo()
    assert _version(path) == 0


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    MemoryStore(path)
    MemoryStore(path)
    assert _version(path) == 2 and _baks(path) == []


def test_newer_version_raises(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    MemoryStore(path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 9")
    with pytest.raises(SchemaTooNewError):
        MemoryStore(path)


def test_purge_paths_remove_leftover_backup(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    leftover = path.with_name("memory.sqlite3.bak-v0")
    for action in (store.delete_history, lambda: store.delete_note(1)):
        leftover.write_bytes(b"eski icerik")
        store.add_note("not", turn_id="t", max_notes=10)
        action()
        assert not leftover.exists()


def test_companion_fresh_and_legacy(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with companion._connect(state):
        pass
    path = companion.companion_dir(state) / "degisiklikler.sqlite3"
    assert _version(path) == 1 and _baks(path) == []

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
    assert _version(lpath) == 1 and _baks(lpath) == []
    with sqlite3.connect(lpath) as db:
        assert db.execute("SELECT id, state, private FROM changes").fetchall() == [
            (1, "completed", 1)
        ]


def test_backup_refuses_link_target_and_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Gerçek symlink Windows'ta ayrıcalık ister; _is_link kararı doğrudan zorlanır.
    for i, linked in enumerate(("x0.sqlite3.bak-v0", tmp_path.name)):
        path = tmp_path / f"x{i}.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE t (v INTEGER)")
        db.close()
        monkeypatch.setattr(sqlite_schema, "_is_link", lambda p, n=linked: p.name == n)
        db = sqlite3.connect(path)
        with pytest.raises(SchemaError):
            ensure_schema(db, path, 1, lambda _d, _v: None)
        db.close()
        assert _version(path) == 0 and _baks(path) == []


def test_backup_temp_is_exclusive_random_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "x.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE t (v INTEGER)")
    (tmp_path / "x.sqlite3.tmp").write_text("onceden var")  # eski öngörülebilir ad kullanılmaz
    seen: list[str] = []
    real = sqlite_schema.tempfile.mkstemp

    def spy(**kw: object) -> tuple[int, str]:
        fd, name = real(**kw)  # type: ignore[arg-type]
        seen.append(name)
        return fd, name

    monkeypatch.setattr(sqlite_schema.tempfile, "mkstemp", spy)
    db = sqlite3.connect(path)
    ensure_schema(db, path, 1, lambda _d, _v: None)
    db.close()
    assert len(seen) == 1 and not os.path.exists(seen[0])
    assert (tmp_path / "x.sqlite3.tmp").read_text() == "onceden var"
