"""Yoldaş hafıza yazımlarının sertleştirme testleri (junction, WAL, kilit, uncertain)."""

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from artemis.memory import companion
from artemis.memory.companion import (
    companion_dir,
    content_private,
    content_tainted,
    ensure_skeleton,
    list_changes,
    read_companion,
    undo_change,
    write_companion,
)


def test_junction_folder_rejected_without_external_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mevcut kodda yalnız .is_symlink() denetlendiği için junction yolu reddedilmezdi.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    outside = tmp_path / "outside"
    folder = companion_dir(state_dir)
    monkeypatch.setattr(
        os.path, "isjunction", lambda p: Path(p) == folder, raising=False
    )
    with pytest.raises(OSError):
        ensure_skeleton(state_dir)
    with pytest.raises(OSError):
        write_companion(
            state_dir, "isler", "yeni\n", source="t", turn_id="1", tainted=False
        )
    assert read_companion(state_dir, "isler", 100) == ""
    assert not outside.exists()
    assert list(state_dir.iterdir()) == []

    state2 = tmp_path / "state2"
    ensure_skeleton(state2)
    folder2 = companion_dir(state2)
    db2 = folder2 / "degisiklikler.sqlite3"
    # Var olan dosya üzerinde junction bildirilirse okuma boş dönmeli, db bağlantısı reddedilmeli.
    monkeypatch.setattr(
        os.path, "isjunction", lambda p: Path(p) in (folder2, db2), raising=False
    )
    assert read_companion(state2, "isler", 100) == ""
    with pytest.raises(OSError):
        write_companion(state2, "isler", "x\n", source="t", turn_id="2", tainted=False)


def test_connect_closes_db_when_wal_pragma_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mevcut kodda WAL PRAGMA try dışında olduğundan hata durumunda db.close() çağrılmazdı.
    class _ConnWrapper:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner
            self.closed = False

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            if "journal_mode=WAL" in sql:
                raise sqlite3.OperationalError("wal kilitli")
            return self._inner.execute(sql, *args, **kwargs)

        def close(self) -> None:
            self.closed = True
            self._inner.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    wrappers: list[_ConnWrapper] = []
    real_connect = sqlite3.connect

    def fake_connect(*args: Any, **kwargs: Any) -> _ConnWrapper:
        conn = _ConnWrapper(real_connect(*args, **kwargs))
        wrappers.append(conn)
        return conn

    monkeypatch.setattr("artemis.memory.companion.sqlite3.connect", fake_connect)
    try:
        with pytest.raises(sqlite3.OperationalError, match="wal kilitli"):
            companion._connect(tmp_path)
        assert len(wrappers) == 1 and wrappers[0].closed is True
    finally:
        for conn in wrappers:
            if not conn.closed:
                conn._inner.close()


def test_content_tainted_and_private_fail_closed_on_directory_db(tmp_path: Path) -> None:
    # Mevcut kodda .is_file() klasör için False döndüğünden fail-closed yerine temiz sayılırdı.
    state_dir = tmp_path / "state"
    assert content_tainted(state_dir, "isler") is False
    assert content_private(state_dir, "isler") is False

    (companion_dir(state_dir) / "degisiklikler.sqlite3").mkdir(parents=True)
    assert content_tainted(state_dir, "isler") is True
    assert content_private(state_dir, "isler") is True


def test_write_companion_serializes_with_write_lock(tmp_path: Path) -> None:
    # Mevcut kodda _WRITE_LOCK olmadığı ve yazma kilitlenmediği için hemen tamamlanırdı.
    ensure_skeleton(tmp_path)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            write_companion(
                tmp_path, "isler", "es zamanli\n", source="t", turn_id="1", tainted=False
            )
        except BaseException as exc:  # noqa: BLE001 - iş parçacığı hatasını ana akışa taşı
            errors.append(exc)

    companion._WRITE_LOCK.acquire()
    try:
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=0.3)
        assert thread.is_alive()
    finally:
        companion._WRITE_LOCK.release()

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert read_companion(tmp_path, "isler", 100) == "es zamanli\n"
    # RLock sayesinde undo_change -> write_companion zinciri kilitlenmeden çalışır.
    change_id = list_changes(tmp_path, 1)[0]["id"]
    undo_change(tmp_path, change_id, turn_id="2", tainted=False)


def test_readback_mismatch_records_uncertain_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mevcut kodda os.replace sonrası uyuşmazlık 'failed' yazılır ve content_tainted False dönerdi.
    ensure_skeleton(tmp_path)
    target = companion_dir(tmp_path) / "Isler.md"
    real_read_exact = companion._read_exact
    calls: dict[Path, int] = {}

    def fake_read_exact(path: Path) -> str:
        if path == target:
            calls[path] = calls.get(path, 0) + 1
            if calls[path] == 2:
                return "BASKA"
        return real_read_exact(path)

    monkeypatch.setattr("artemis.memory.companion._read_exact", fake_read_exact)
    with pytest.raises(OSError):
        write_companion(
            tmp_path, "isler", "yeni\n", source="t", turn_id="1", tainted=False
        )

    with sqlite3.connect(companion_dir(tmp_path) / "degisiklikler.sqlite3") as db:
        states = [row[0] for row in db.execute("SELECT state FROM changes ORDER BY id")]
    assert states == ["uncertain"]
    assert content_tainted(tmp_path, "isler") is True
    assert content_private(tmp_path, "isler") is True
