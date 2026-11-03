"""Artemis'in kendi kalıcı hafızası: konuşma geçmişi, öz notlar ve geçmiş arama (ADR 0025).

Obsidian vault'tan ayrıdır: tek SQLite dosyası (`state_dir/memory.sqlite3`). Her işlem kısa
ömürlü bir bağlantı açar; masaüstü köprüsü ve araç iş parçacıkları aynı dosyayı güvenle
kullanır. Ağa çıkmaz, alt süreç başlatmaz.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from artemis.brain.conversation import TurnRecord
from artemis.memory.vault import tokenize, turkish_fold

# ponytail: arama son N tur üzerinde bellekte yapılır; N'i aşan arşiv için FTS5 veya embedding.
_SEARCH_SCAN_TURNS = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history_policy (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    retention_days INTEGER NOT NULL CHECK(retention_days >= 0)
);
INSERT OR IGNORE INTO history_policy VALUES (1, 0);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    record_json TEXT NOT NULL,
    user_text TEXT NOT NULL,
    answer TEXT NOT NULL,
    search_text TEXT NOT NULL,
    answer_private INTEGER NOT NULL,
    tainted INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    last_turn_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    turn_id TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Note:
    id: int
    text: str
    created_at: str


@dataclass(frozen=True)
class TurnHit:
    created_at: str
    user_text: str
    answer: str
    answer_private: bool
    tainted: bool
    score: float


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> closing[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        return closing(db)

    def history_status(self) -> dict[str, int]:
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            days = db.execute("SELECT retention_days FROM history_policy WHERE id = 1").fetchone()[
                0
            ]
        return {"turns": int(count), "retention_days": int(days)}

    def set_retention(self, days: int) -> int:
        if not 0 <= days <= 36500:
            raise ValueError("Saklama süresi 0–36500 gün olmalı; 0 otomatik silmeyi kapatır.")
        with self._connect() as db, db:
            db.execute("UPDATE history_policy SET retention_days = ? WHERE id = 1", (days,))
            return self._prune(db, days)

    @staticmethod
    def _prune(db: sqlite3.Connection, days: int) -> int:
        if not days:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        return db.execute("DELETE FROM turns WHERE created_at < ?", (cutoff,)).rowcount

    def apply_retention(self) -> int:
        with self._connect() as db, db:
            days = db.execute("SELECT retention_days FROM history_policy WHERE id = 1").fetchone()[
                0
            ]
            return self._prune(db, days)

    def delete_history(self) -> int:
        # Tek transaction: notlara dokunma; sıfırlama işaretleri artık gereksizdir.
        with self._connect() as db, db:
            count = db.execute("DELETE FROM turns").rowcount
            db.execute("DELETE FROM resets")
            return count

    def history_page(self, limit: int = 50) -> list[dict[str, str]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT created_at, user_text, answer FROM turns ORDER BY id DESC LIMIT ?",
                (min(max(limit, 1), 100),),
            ).fetchall()
        return [{"created_at": r[0], "user": r[1], "answer": r[2]} for r in rows]

    # --- katman 1: konuşma geçmişi ---------------------------------------------------

    def add_turn(self, record: TurnRecord) -> None:
        # Özel yanıt içeriği arama dizinine girmez: sıralama bile onu ele vermesin.
        text = record.user_text if record.answer_private else f"{record.user_text} {record.answer}"
        with self._connect() as db, db:
            db.execute(
                "INSERT INTO turns (turn_id, created_at, record_json, user_text, answer,"
                " search_text, answer_private, tainted) VALUES (?,?,?,?,?,?,?,?)",
                (
                    record.turn_id,
                    _now(),
                    record.model_dump_json(),
                    record.user_text,
                    record.answer,
                    turkish_fold(text),
                    int(record.answer_private),
                    int(record.tainted),
                ),
            )

    def mark_reset(self) -> None:
        with self._connect() as db, db:
            db.execute(
                "INSERT INTO resets (created_at, last_turn_id)"
                " SELECT ?, COALESCE(MAX(id), 0) FROM turns",
                (_now(),),
            )

    def recent_turns(self, limit: int) -> list[TurnRecord]:
        """Son sıfırlamadan sonraki en çok `limit` tur, eskiden yeniye."""
        if limit <= 0:
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT record_json FROM turns WHERE id > "
                "COALESCE((SELECT MAX(last_turn_id) FROM resets), 0) ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        records: list[TurnRecord] = []
        for (raw,) in reversed(rows):
            try:
                records.append(TurnRecord.model_validate_json(raw))
            except ValidationError:
                continue  # şeması eskimiş tek bir satır geri kalanını düşürmesin
        return records

    def search_turns(self, query: str, limit: int) -> list[TurnHit]:
        """tf-idf benzeri sözcük sıralaması (vault.lexical_search ile aynı fikir), yeni önde."""
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms or limit <= 0:
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT created_at, user_text, answer, search_text, answer_private, tainted"
                " FROM turns ORDER BY id DESC LIMIT ?",
                (_SEARCH_SCAN_TURNS,),
            ).fetchall()
        counts = [Counter(tokenize(row[3])) for row in rows]
        frequency = {term: sum(1 for c in counts if c[term]) for term in terms}
        total = len(rows)
        weights = {
            term: math.log(1 + (total - df + 0.5) / (df + 0.5)) for term, df in frequency.items()
        }
        hits: list[TurnHit] = []
        for row, count in zip(rows, counts, strict=True):
            score = sum((1 + math.log(count[t])) * weights[t] for t in terms if count[t])
            if score > 0:
                hits.append(TurnHit(row[0], row[1], row[2], bool(row[4]), bool(row[5]), score))
        hits.sort(key=lambda hit: -hit.score)  # kararlı sıralama: eşitlikte yeni tur önde
        return hits[:limit]

    # --- katman 2: öz notlar ---------------------------------------------------------

    def list_notes(self) -> list[Note]:
        """Yeniden eskiye."""
        with self._connect() as db:
            rows = db.execute("SELECT id, text, created_at FROM notes ORDER BY id DESC")
            return [Note(*row) for row in rows.fetchall()]

    def add_note(self, text: str, turn_id: str, max_notes: int) -> tuple[str, int | None]:
        """("added", id), ("duplicate", id) veya ("full", None)."""
        folded = turkish_fold(text)
        with self._connect() as db, db:
            rows = db.execute("SELECT id, text FROM notes").fetchall()
            for note_id, existing in rows:
                if turkish_fold(existing) == folded:
                    return "duplicate", note_id
            if len(rows) >= max_notes:
                return "full", None
            cursor = db.execute(
                "INSERT INTO notes (text, created_at, turn_id) VALUES (?,?,?)",
                (text, _now(), turn_id),
            )
            return "added", cursor.lastrowid

    def delete_note(self, note_id: int) -> bool:
        with self._connect() as db, db:
            return db.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount > 0
