"""Silme işlemlerinin kalıcı ve içerik kopyalamayan günlüğü."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from artemis.safety.audit import redact

UndoStatus = Literal["prepared", "completed", "failed", "uncertain"]
TerminalUndoStatus = Literal["completed", "failed", "uncertain"]
_TERMINAL_STATUSES: tuple[TerminalUndoStatus, ...] = (
    "completed",
    "failed",
    "uncertain",
)


class UndoJournalError(OSError):
    """Günlük okunamadı veya güvenilir biçimde güncellenemedi."""


@dataclass(frozen=True)
class UndoRecord:
    """Bir dosyanın geri dönüşüm kutusuna taşınma kaydı."""

    record_id: str
    turn_id: str
    root: str = field(repr=False)
    relative_path: str = field(repr=False)
    size: int
    created_at: str
    status: UndoStatus
    workspace_key: str = field(repr=False)

    def as_dict(self) -> dict[str, str | int]:
        """Dışarı yalnız gereken, önceden temizlenmiş metadata çıkarır."""
        return {
            "record_id": self.record_id,
            "turn_id": self.turn_id,
            "relative_path": self.relative_path,
            "size": self.size,
            "created_at": self.created_at,
            "status": self.status,
        }


class UndoJournal:
    """Silme içeriğini saklamadan işlem durumunu SQLite'a yazar."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "undo.sqlite3"

    def prepare(
        self, *, turn_id: str, root: Path, relative_path: str, size: int
    ) -> UndoRecord:
        resolved_root = str(root.resolve())
        record = UndoRecord(
            record_id=str(uuid.uuid4()),
            turn_id=str(redact(turn_id)),
            root=str(redact(resolved_root)),
            relative_path=str(redact(relative_path)),
            size=size,
            created_at=datetime.now(UTC).isoformat(),
            status="prepared",
            workspace_key=_workspace_key(resolved_root),
        )
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """INSERT INTO undo_records
                       (record_id, turn_id, root, relative_path, size, created_at,
                        status, workspace_key)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.record_id,
                        record.turn_id,
                        record.root,
                        record.relative_path,
                        record.size,
                        record.created_at,
                        record.status,
                        record.workspace_key,
                    ),
                )
        except (sqlite3.Error, OSError):
            raise UndoJournalError("Geri alma kaydı yazılamadı.") from None
        return record

    def set_status(
        self, record_id: str, status: TerminalUndoStatus
    ) -> UndoRecord:
        if status not in _TERMINAL_STATUSES:
            raise ValueError("Günlük durumu geçersiz.")
        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """SELECT record_id, turn_id, root, relative_path, size,
                              created_at, status, workspace_key
                       FROM undo_records WHERE record_id = ?""",
                    (record_id,),
                ).fetchone()
                if row is None:
                    raise UndoJournalError("Geri alma kaydı bulunamadı.")
                current_status: UndoStatus = row[6]
                if current_status == status:
                    return _record(row)
                if current_status != "prepared":
                    raise UndoJournalError("Sonuçlanmış geri alma kaydı değiştirilemez.")
                cursor = connection.execute(
                    """UPDATE undo_records SET status = ?
                       WHERE record_id = ? AND status = 'prepared'""",
                    (status, record_id),
                )
                if cursor.rowcount != 1:
                    row = connection.execute(
                        """SELECT record_id, turn_id, root, relative_path, size,
                                  created_at, status, workspace_key
                           FROM undo_records WHERE record_id = ?""",
                        (record_id,),
                    ).fetchone()
                    if row is not None and row[6] == status:
                        return _record(row)
                    raise UndoJournalError("Geri alma kaydı güncellenemedi.")
                row = connection.execute(
                    """SELECT record_id, turn_id, root, relative_path, size,
                              created_at, status, workspace_key
                       FROM undo_records WHERE record_id = ?""",
                    (record_id,),
                ).fetchone()
        except sqlite3.Error:
            raise UndoJournalError("Geri alma kaydı güncellenemedi.") from None
        if row is None:
            raise UndoJournalError("Geri alma kaydı bulunamadı.")
        return _record(row)

    def list_records(self, *, root: Path, limit: int = 20) -> list[UndoRecord]:
        if not 1 <= limit <= 100:
            raise ValueError("Kayıt sınırı 1 ile 100 arasında olmalı.")
        if not self.path.exists():
            return []
        try:
            resolved_root = str(root.resolve())
            with closing(self._connect(create=False)) as connection, connection:
                rows = connection.execute(
                    """SELECT record_id, turn_id, root, relative_path, size,
                              created_at, status, workspace_key
                       FROM undo_records WHERE workspace_key = ?
                       ORDER BY created_at DESC, record_id DESC LIMIT ?""",
                    (_workspace_key(resolved_root), limit),
                ).fetchall()
        except (sqlite3.Error, OSError):
            raise UndoJournalError("Geri alma kayıtları okunamadı.") from None
        return [_record(row) for row in rows]

    def _connect(self, *, create: bool = True) -> sqlite3.Connection:
        try:
            if create:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._restrict_permissions(self.path.parent, 0o700)
            if not create and not self.path.exists():
                raise UndoJournalError("Geri alma günlüğü bulunamadı.")
            connection = sqlite3.connect(self.path, timeout=5.0)
            try:
                if create:
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS undo_records (
                             record_id TEXT PRIMARY KEY,
                             turn_id TEXT NOT NULL,
                             root TEXT NOT NULL,
                             relative_path TEXT NOT NULL,
                             size INTEGER NOT NULL,
                             created_at TEXT NOT NULL,
                             status TEXT NOT NULL CHECK
                               (status IN ('prepared', 'completed', 'failed', 'uncertain')),
                             workspace_key TEXT NOT NULL
                           )"""
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS undo_root_created "
                        "ON undo_records(workspace_key, created_at DESC)"
                    )
                self._restrict_permissions(self.path, 0o600)
                return connection
            except (sqlite3.Error, OSError):
                connection.close()
                raise
        except (OSError, sqlite3.Error):
            raise UndoJournalError("Geri alma günlüğüne erişilemedi.") from None

    @staticmethod
    def _restrict_permissions(path: Path, mode: int) -> None:
        """POSIX'te izinleri daraltır; başarısızlıkta günlüğü kullanılamaz sayar."""
        if os.name != "nt":
            path.chmod(mode)
            if stat.S_IMODE(path.stat().st_mode) != mode:
                raise PermissionError("Geri alma günlüğü izinleri doğrulanamadı.")


def _workspace_key(root: str) -> str:
    return hashlib.sha256(root.encode("utf-8")).hexdigest()


def _record(row: tuple[object, ...]) -> UndoRecord:
    status = row[6]
    if status not in {"prepared", "completed", "failed", "uncertain"}:
        raise UndoJournalError("Geri alma günlüğü geçersiz kayıt içeriyor.")
    if not isinstance(row[4], int):
        raise UndoJournalError("Geri alma günlüğü geçersiz kayıt içeriyor.")
    return UndoRecord(
        record_id=str(row[0]),
        turn_id=str(row[1]),
        root=str(row[2]),
        relative_path=str(row[3]),
        size=row[4],
        created_at=str(row[5]),
        status=cast(UndoStatus, status),
        workspace_key=str(row[7]),
    )
