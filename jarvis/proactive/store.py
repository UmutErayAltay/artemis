"""Proaktif olaylar için SQLite durumu: hatırlatmalar ve özet oluşumları.

Masaüstü uygulaması ve Telegram kanalı aynı dosyayı aynı anda kullanabilir. Bir
olay (occurrence), işlenip en fazla bir kez teslim edilmeden önce atomik olarak
talep edilmelidir (claimed). Her genel yöntem kendi kısa ömürlü bağlantısını
açar; bağlantı açık tutulmaz (Windows dosya kilitleri teardown sırasında sorun
yapar).
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class ProactiveStoreError(RuntimeError):
    """ProactiveStore işlemlerinde oluşan hatalar (SQLite/OS hatalarını sarar)."""


@dataclass(frozen=True)
class Reminder:
    """Hatırlatma kaydı.

    state: Bu hatırlatmanın event_id'si için occurrences tablosundaki durum.
           Henüz bir occurrence yoksa None.
    """

    id: int
    text: str
    due_at: datetime
    created_at: datetime
    created_tainted: bool
    # Kurulduğu turda buluta verilmeyecek (dosya/pano) içerik vardı: Telegram'a gitmez.
    created_private: bool
    cancelled: bool
    state: str | None


_REMINDER_COLUMNS = "id, text, due_at, created_at, created_tainted, created_private, cancelled_at"


def reminder_event_id(reminder_id: int) -> str:
    """Hatırlatma ID'sinden occurrence event_id'sini üretir."""
    return f"reminder:{reminder_id}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_tainted INTEGER NOT NULL,
    created_private INTEGER NOT NULL DEFAULT 0,
    cancelled_at TEXT
);
CREATE TABLE IF NOT EXISTS occurrences (
    event_id TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('claimed','delivered','failed','missed','uncertain')),
    owner TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(event_id, scheduled_for)
);
CREATE INDEX IF NOT EXISTS idx_occurrences_claimed_at ON occurrences(claimed_at);
CREATE INDEX IF NOT EXISTS idx_occurrences_finished_at ON occurrences(finished_at);
"""


def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} must be timezone-aware")


def _to_utc_iso(dt: datetime) -> str:
    """Aware datetime'ı UTC'ye çevirip isoformat (saniye hassasiyeti) string olarak döndürür."""
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _from_utc_iso(s: str) -> datetime:
    """UTC isoformat string'ini aware UTC datetime'a çevirir."""
    # Python 3.11+ fromisoformat Z'yi destekler; 3.12 kesin destekler.
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _row_to_reminder(row: sqlite3.Row, occ_state: str | None) -> Reminder:
    return Reminder(
        id=row["id"],
        text=row["text"],
        due_at=_from_utc_iso(row["due_at"]),
        created_at=_from_utc_iso(row["created_at"]),
        created_tainted=bool(row["created_tainted"]),
        created_private=bool(row["created_private"]),
        cancelled=row["cancelled_at"] is not None,
        state=occ_state,
    )


class ProactiveStore:
    """Proaktif olaylar (hatırlatmalar, özetler) için işlem güvenli SQLite deposu."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db, db:
                db.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as e:
            raise ProactiveStoreError(f"Proaktif depo açılamadı ({type(e).__name__})") from e

    def _connect(self) -> contextlib.closing[sqlite3.Connection]:
        # Varsayılan rollback günlüğü: WAL'ın -wal/-shm yan dosyaları Windows'ta kapanışta
        # kilit bırakabilir; iki sürecin eşzamanlılığı için `timeout` yeterlidir.
        db = sqlite3.connect(self.path, timeout=5.0)
        db.row_factory = sqlite3.Row
        return contextlib.closing(db)

    # --- Hatırlatmalar -----------------------------------------------------------

    def add_reminder(
        self, text: str, due_at: datetime, *, tainted: bool, private: bool = False, now: datetime
    ) -> Reminder:
        """Yeni bir hatırlatma ekler ve oluşturulan Reminder'ı döndürür."""
        _require_aware(due_at, "due_at")
        _require_aware(now, "now")
        due_iso = _to_utc_iso(due_at)
        created_iso = _to_utc_iso(now)
        try:
            with self._connect() as db, db:
                cursor = db.execute(
                    "INSERT INTO reminders (text, due_at, created_at, created_tainted, "
                    "created_private) VALUES (?,?,?,?,?)",
                    (text, due_iso, created_iso, int(tainted), int(private)),
                )
                reminder_id = cursor.lastrowid
                if reminder_id is None:
                    raise ProactiveStoreError("add_reminder: satır kimliği alınamadı")
                row = db.execute(
                    f"SELECT {_REMINDER_COLUMNS} FROM reminders WHERE id = ?",
                    (reminder_id,),
                ).fetchone()
                if row is None:
                    raise ProactiveStoreError("add_reminder: eklenen satır okunamadı")
                return _row_to_reminder(row, None)
        except sqlite3.Error as e:
            raise ProactiveStoreError(f"add_reminder failed: {e}") from e
        except OSError as e:
            raise ProactiveStoreError(f"add_reminder failed: {e}") from e

    def get_reminder(self, reminder_id: int) -> Reminder | None:
        """ID ile hatırlatma getirir; occurrence durumunu da dahil eder."""
        try:
            with self._connect() as db:
                row = db.execute(
                    f"SELECT {_REMINDER_COLUMNS} FROM reminders WHERE id = ?",
                    (reminder_id,),
                ).fetchone()
                if row is None:
                    return None
                event_id = reminder_event_id(reminder_id)
                occ_row = db.execute(
                    "SELECT state FROM occurrences WHERE event_id = ? "
                    "ORDER BY scheduled_for DESC LIMIT 1",
                    (event_id,),
                ).fetchone()
                occ_state = occ_row["state"] if occ_row else None
                return _row_to_reminder(row, occ_state)
        except sqlite3.Error as e:
            raise ProactiveStoreError(f"get_reminder failed: {e}") from e
        except OSError as e:
            raise ProactiveStoreError(f"get_reminder failed: {e}") from e

    def pending_reminders(self) -> list[Reminder]:
        """İptal edilmemiş ve henüz occurrence'ı olmayan hatırlatmalar (due_at, id sırasıyla)."""
        try:
            with self._connect() as db:
                rows = db.execute(
                    """
                    SELECT r.id, r.text, r.due_at, r.created_at, r.created_tainted,
                           r.created_private,
                           r.cancelled_at
                    FROM reminders r
                    WHERE r.cancelled_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM occurrences o
                          WHERE o.event_id = 'reminder:' || r.id
                      )
                    ORDER BY r.due_at ASC, r.id ASC
                    """
                ).fetchall()
                return [_row_to_reminder(row, None) for row in rows]
        except sqlite3.Error as e:
            raise ProactiveStoreError(f"pending_reminders failed: {e}") from e
        except OSError as e:
            raise ProactiveStoreError(f"pending_reminders failed: {e}") from e

    def due_reminders(self, now: datetime) -> list[Reminder]:
        """`due_at <= now` olan bekleyen hatırlatmalar."""
        _require_aware(now, "now")
        now_iso = _to_utc_iso(now)
        try:
            with self._connect() as db:
                rows = db.execute(
                    """
                    SELECT r.id, r.text, r.due_at, r.created_at, r.created_tainted,
                           r.created_private,
                           r.cancelled_at
                    FROM reminders r
                    WHERE r.cancelled_at IS NULL
                      AND r.due_at <= ?
                      AND NOT EXISTS (
                          SELECT 1 FROM occurrences o
                          WHERE o.event_id = 'reminder:' || r.id
                      )
                    ORDER BY r.due_at ASC, r.id ASC
                    """,
                    (now_iso,),
                ).fetchall()
                return [_row_to_reminder(row, None) for row in rows]
        except sqlite3.Error as e:
            raise ProactiveStoreError(f"due_reminders failed: {e}") from e
        except OSError as e:
            raise ProactiveStoreError(f"due_reminders failed: {e}") from e

    def cancel_reminder(self, reminder_id: int, now: datetime) -> bool:
        """Sadece bekleyen (pending) hatırlatmayı iptal eder; atomik UPDATE."""
        _require_aware(now, "now")
        now_iso = _to_utc_iso(now)
        event_id = reminder_event_id(reminder_id)
        try:
            with self._connect() as db, db:
                cursor = db.execute(
                    """
                    UPDATE reminders
                    SET cancelled_at = ?
                    WHERE id = ?
                      AND cancelled_at IS NULL
                      AND NOT EXISTS (SELECT 1 FROM occurrences WHERE event_id = ?)
                    """,
                    (now_iso, reminder_id, event_id),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as e:
            raise ProactiveStoreError(f"cancel_reminder failed: {e}") from e
        except OSError as e:
            raise ProactiveStoreError(f"cancel_reminder failed: {e}") from e

    # --- Occurrence talep/işleme ------------------------------------------------

    def claim(
        self, event_id: str, scheduled_for: datetime, kind: str, owner: str, now: datetime
    ) -> bool:
        """Bir occurrence satırını atomik olarak 'claimed' yapar.

        - reminder:<id> event_id'leri için: hatırlatma iptal edilmemişse (cancelled_at IS NULL)
          INSERT yapar.
        - Diğer event_id'ler için: doğrudan INSERT OR IGNORE.
        Returns True only if this call inserted the row.
        """
        _require_aware(scheduled_for, "scheduled_for")
        _require_aware(now, "now")
        scheduled_iso = _to_utc_iso(scheduled_for)
        claimed_iso = _to_utc_iso(now)
        try:
            with self._connect() as db, db:
                # reminder event_id mi?
                if event_id.startswith("reminder:"):
                    # BEGIN IMMEDIATE benzeri davranış için transaction içinde SELECT sonra INSERT
                    # NOT EXISTS ile iptal edilmemiş hatırlatma kontrolü
                    cursor = db.execute(
                        """
                        INSERT OR IGNORE INTO occurrences
                            (event_id, scheduled_for, kind, state, owner, claimed_at, finished_at,
                             detail)
                        SELECT ?, ?, ?, 'claimed', ?, ?, NULL, ''
                        WHERE NOT EXISTS (
                            SELECT 1 FROM reminders
                            WHERE id = CAST(substr(?, 10) AS INTEGER)
                              AND cancelled_at IS NOT NULL
                        )
                        """,
                        (event_id, scheduled_iso, kind, owner, claimed_iso, event_id),
                    )
                    return cursor.rowcount == 1
                else:
                    cursor = db.execute(
                        """
                        INSERT OR IGNORE INTO occurrences
                            (event_id, scheduled_for, kind, state, owner, claimed_at, finished_at,
                             detail)
                        VALUES (?, ?, ?, 'claimed', ?, ?, NULL, '')
                        """,
                        (event_id, scheduled_iso, kind, owner, claimed_iso),
                    )
                    return cursor.rowcount == 1
        except sqlite3.Error as e:
            raise ProactiveStoreError(f"claim failed: {e}") from e
        except OSError as e:
            raise ProactiveStoreError(f"claim failed: {e}") from e

    def finish(
        self, event_id: str, scheduled_for: datetime, state: str, detail: str, now: datetime
    ) -> None:
        """Claimed bir occurrence'ı bitirir (delivered/failed/missed/uncertain)."""
        _require_aware(scheduled_for, "scheduled_for")
        _require_aware(now, "now")
        valid_states = ("delivered", "failed", "missed", "uncertain")
        if state not in valid_states:
            raise ValueError(f"Invalid state: {state!r}, must be one of {valid_states}")
        scheduled_iso = _to_utc_iso(scheduled_for)
        finished_iso = _to_utc_iso(now)
        try:
            with self._connect() as db, db:
                cursor = db.execute(
                    """
                    UPDATE occurrences
                    SET state = ?, finished_at = ?, detail = ?
                    WHERE event_id = ? AND scheduled_for = ? AND state = 'claimed'
                    """,
                    (state, finished_iso, detail, event_id, scheduled_iso),
                )
                if cursor.rowcount == 0:
                    raise ProactiveStoreError(
                        f"Bitirilecek talep yok: {event_id!r} @ {scheduled_iso!r}"
                    )
        except sqlite3.Error as e:
            raise ProactiveStoreError(f"finish failed: {e}") from e
        except OSError as e:
            raise ProactiveStoreError(f"finish failed: {e}") from e

    def occurrence_state(self, event_id: str, scheduled_for: datetime) -> str | None:
        """Occurrence durumunu döndürür; yoksa None."""
        _require_aware(scheduled_for, "scheduled_for")
        scheduled_iso = _to_utc_iso(scheduled_for)
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT state FROM occurrences WHERE event_id = ? AND scheduled_for = ?",
                    (event_id, scheduled_iso),
                ).fetchone()
                return row["state"] if row else None
        except sqlite3.Error as e:
            raise ProactiveStoreError(f"occurrence_state failed: {e}") from e
        except OSError as e:
            raise ProactiveStoreError(f"occurrence_state failed: {e}") from e

    def mark_stale_claims(self, older_than: datetime, now: datetime) -> int:
        """`claimed` durumunda olup `claimed_at < older_than` olanları `uncertain` yapar."""
        _require_aware(older_than, "older_than")
        _require_aware(now, "now")
        older_iso = _to_utc_iso(older_than)
        now_iso = _to_utc_iso(now)
        detail = "Talep eden süreç işi bitirmedi."
        try:
            with self._connect() as db, db:
                cursor = db.execute(
                    """
                    UPDATE occurrences
                    SET state = 'uncertain', finished_at = ?, detail = ?
                    WHERE state = 'claimed' AND claimed_at < ?
                    """,
                    (now_iso, detail, older_iso),
                )
                return cursor.rowcount
        except sqlite3.Error as e:
            raise ProactiveStoreError(f"mark_stale_claims failed: {e}") from e
        except OSError as e:
            raise ProactiveStoreError(f"mark_stale_claims failed: {e}") from e

    def count_finished_between(
        self, start: datetime, end: datetime, states: tuple[str, ...] = ("delivered",)
    ) -> int:
        """`start <= finished_at < end` ve state `states` içinde olan occurrence sayısı."""
        _require_aware(start, "start")
        _require_aware(end, "end")
        start_iso = _to_utc_iso(start)
        end_iso = _to_utc_iso(end)
        if not states:
            return 0
        placeholders = ",".join("?" for _ in states)
        try:
            with self._connect() as db:
                row = db.execute(
                    f"""
                    SELECT COUNT(*) FROM occurrences
                    WHERE finished_at >= ? AND finished_at < ? AND state IN ({placeholders})
                    """,
                    (start_iso, end_iso, *states),
                ).fetchone()
                return row[0] if row else 0
        except sqlite3.Error as e:
            raise ProactiveStoreError(f"count_finished_between failed: {e}") from e
        except OSError as e:
            raise ProactiveStoreError(f"count_finished_between failed: {e}") from e
