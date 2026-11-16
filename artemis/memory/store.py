"""Artemis'in kendi kalıcı hafızası: konuşma geçmişi, öz notlar ve geçmiş arama (ADR 0025).

Obsidian vault'tan ayrıdır: tek SQLite dosyası (`state_dir/memory.sqlite3`). Her işlem kısa
ömürlü bir bağlantı açar; masaüstü köprüsü ve araç iş parçacıkları aynı dosyayı güvenle
kullanır. Ağa çıkmaz, alt süreç başlatmaz.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections import Counter
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from artemis.brain.conversation import TurnRecord
from artemis.memory.protect import PREFIX, PlainProtector, Protector, UnreadableMemory
from artemis.memory.sqlite_schema import ensure_schema, purge_backups
from artemis.memory.vault import tokenize
from artemis.text import turkish_fold

_logger = logging.getLogger(__name__)

# ponytail: arama son N tur üzerinde bellekte yapılır; N'i aşan arşiv için FTS5 veya embedding.
_SEARCH_SCAN_TURNS = 2000

# 1: sürümsüz dönemin şeması (notes.private dahil).
# 2: sütun düzeyinde şifreleme desteği (ADR 0076). Yapı değişince artır ve _migrate'e ekle.
_SCHEMA_VERSION = 2
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
    turn_id TEXT NOT NULL,
    private INTEGER NOT NULL DEFAULT 0
);
"""


def _migrate(
    db: sqlite3.Connection, from_version: int, protector: Protector | None = None
) -> None:
    # executescript commit ederdi; geçiş tek işlemde kalsın diye deyimler tek tek koşar.
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    columns = {row[1] for row in db.execute("PRAGMA table_info(notes)")}
    if "private" not in columns:
        # ADR 0056 geçişi: eski notların gizlilik kökeni bilinmez ve notlar lekeyi de
        # saklamadığı için çıkarılamaz. Gizli değil sayılırlar; bu sınır README'de
        # yazılıdır ve kullanıcı şüpheli notu memory_forget ile silebilir.
        # suppress: eşzamanlı bir geçiş sütunu az önce eklediyse hata beklenendir.
        with suppress(sqlite3.OperationalError):
            db.execute("ALTER TABLE notes ADD COLUMN private INTEGER NOT NULL DEFAULT 0")
        # Bastırılan hata gerçek bir başarısızlığı gizleyebilir: sütun yoksa sürüm artmasın.
        if "private" not in {row[1] for row in db.execute("PRAGMA table_info(notes)")}:
            raise sqlite3.OperationalError("notes.private sütunu eklenemedi.")
    if from_version < 2 and protector is not None and not isinstance(protector, PlainProtector):
        _encrypt_existing_rows(db, protector)


def _encrypt_existing_rows(db: sqlite3.Connection, protector: Protector) -> None:
    """Açık geçişte (1 -> 2) düz metin satırları tek işlemde şifreler (ADR 0076)."""
    turn_rows = db.execute(
        "SELECT id, record_json, user_text, answer, search_text FROM turns"
    ).fetchall()
    for row_id, record_json, user_text, answer, search_text in turn_rows:
        new_record = (
            record_json if record_json.startswith(PREFIX) else protector.protect(record_json)
        )
        new_user = user_text if user_text.startswith(PREFIX) else protector.protect(user_text)
        new_answer = answer if answer.startswith(PREFIX) else protector.protect(answer)
        new_search = (
            search_text if search_text.startswith(PREFIX) else protector.protect(search_text)
        )
        if (
            new_record != record_json
            or new_user != user_text
            or new_answer != answer
            or new_search != search_text
        ):
            db.execute(
                "UPDATE turns SET record_json = ?, user_text = ?, answer = ?, search_text = ?"
                " WHERE id = ?",
                (new_record, new_user, new_answer, new_search, row_id),
            )
    note_rows = db.execute("SELECT id, text FROM notes").fetchall()
    for row_id, text in note_rows:
        if not text.startswith(PREFIX):
            db.execute(
                "UPDATE notes SET text = ? WHERE id = ?",
                (protector.protect(text), row_id),
            )


@dataclass(frozen=True)
class Note:
    id: int
    text: str
    created_at: str
    # Gizli (izinsiz dosya/pano/ekran görülen) turda yazıldı: buluta maskeli gider (ADR 0056).
    private: bool = False


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
    def __init__(self, path: Path, protector: Protector | None = None) -> None:
        self.path = path
        self._protector: Protector = protector if protector is not None else PlainProtector()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            ensure_schema(
                db,
                path,
                _SCHEMA_VERSION,
                lambda conn, from_version: _migrate(conn, from_version, self._protector),
            )

    @staticmethod
    def _log_unreadable(table: str, row_id: int, error: UnreadableMemory) -> None:
        _logger.warning(
            "Okunamayan hafıza satırı atlandı: tablo=%s id=%s hata=%s",
            table,
            row_id,
            type(error).__name__,
        )

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
            pruned = self._prune(db, days)
        if pruned:
            purge_backups(self.path)
        return pruned

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
            pruned = self._prune(db, days)
        if pruned:
            purge_backups(self.path)
        return pruned

    def delete_history(self) -> int:
        # Tek transaction: notlara dokunma; sıfırlama işaretleri artık gereksizdir.
        with self._connect() as db:
            with db:
                count = db.execute("DELETE FROM turns").rowcount
                db.execute("DELETE FROM resets")
            # DELETE yalnız satırı işaretler: metin WAL'da ve boş sayfalarda kalır. Kullanıcı
            # "sil" dediğinde dosyada okunabilir iz bırakmamak için WAL ana dosyaya işlenip
            # kesilir, dosya VACUUM ile yeniden yazılır. (SSD/yedek kopyalarını kapsamaz.)
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        purge_backups(self.path)  # başarısız geçişten kalan yedek silineni saklamasın
        return count

    def history_page(self, limit: int = 50) -> list[dict[str, str]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, created_at, user_text, answer FROM turns ORDER BY id DESC LIMIT ?",
                (min(max(limit, 1), 100),),
            ).fetchall()
        items: list[dict[str, str]] = []
        for row_id, created_at, user_text, answer in rows:
            try:
                plain_user = self._protector.unprotect(user_text)
                plain_answer = self._protector.unprotect(answer)
            except UnreadableMemory as error:
                self._log_unreadable("turns", int(row_id), error)
                continue
            items.append({"created_at": created_at, "user": plain_user, "answer": plain_answer})
        return items

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
                    # Tur başı zamanı: modele giden [tarih saat] öneki geçmişte de aynı kalır.
                    record.created_at or _now(),
                    self._protector.protect(record.model_dump_json()),
                    self._protector.protect(record.user_text),
                    self._protector.protect(record.answer),
                    self._protector.protect(turkish_fold(text)),
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
                "SELECT id, record_json, created_at FROM turns WHERE id > "
                "COALESCE((SELECT MAX(last_turn_id) FROM resets), 0) ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        records: list[TurnRecord] = []
        for row_id, raw, created_at in reversed(rows):
            try:
                plain_raw = self._protector.unprotect(raw)
            except UnreadableMemory as error:
                self._log_unreadable("turns", int(row_id), error)
                continue
            try:
                # Sütun kaynaktır: eski kayıtların JSON'unda zaman yoktur.
                record = TurnRecord.model_validate_json(plain_raw)
                records.append(record.model_copy(update={"created_at": created_at}))
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
                "SELECT id, created_at, user_text, answer, search_text, answer_private, tainted"
                " FROM turns ORDER BY id DESC LIMIT ?",
                (_SEARCH_SCAN_TURNS,),
            ).fetchall()
        decoded: list[tuple[str, str, str, str, bool, bool]] = []
        for row_id, created_at, user_text, answer, search_text, answer_private, tainted in rows:
            try:
                plain_user = self._protector.unprotect(user_text)
                plain_answer = self._protector.unprotect(answer)
                plain_search = self._protector.unprotect(search_text)
            except UnreadableMemory as error:
                self._log_unreadable("turns", int(row_id), error)
                continue
            decoded.append(
                (
                    created_at,
                    plain_user,
                    plain_answer,
                    plain_search,
                    bool(answer_private),
                    bool(tainted),
                )
            )
        counts = [Counter(tokenize(row[3])) for row in decoded]
        frequency = {term: sum(1 for c in counts if c[term]) for term in terms}
        total = len(decoded)
        weights = {
            term: math.log(1 + (total - df + 0.5) / (df + 0.5)) for term, df in frequency.items()
        }
        hits: list[TurnHit] = []
        for row, count in zip(decoded, counts, strict=True):
            score = sum((1 + math.log(count[t])) * weights[t] for t in terms if count[t])
            if score > 0:
                hits.append(TurnHit(row[0], row[1], row[2], row[4], row[5], score))
        hits.sort(key=lambda hit: -hit.score)  # kararlı sıralama: eşitlikte yeni tur önde
        return hits[:limit]

    # --- katman 2: öz notlar ---------------------------------------------------------

    def list_notes(self) -> list[Note]:
        """Yeniden eskiye."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, text, created_at, private FROM notes ORDER BY id DESC"
            ).fetchall()
        notes: list[Note] = []
        for row_id, text, created_at, private in rows:
            try:
                plain_text = self._protector.unprotect(text)
            except UnreadableMemory as error:
                self._log_unreadable("notes", int(row_id), error)
                continue
            notes.append(Note(row_id, plain_text, created_at, bool(private)))
        return notes

    def add_note(
        self, text: str, turn_id: str, max_notes: int, private: bool = False
    ) -> tuple[str, int | None]:
        """("added", id), ("duplicate", id) veya ("full", None).

        Aynı metin gizli bir turda yeniden yazılırsa var olan not gizli işaretlenir: gizlilik
        yalnız artar, temiz bir tekrar onu düşürmez (ADR 0056)."""
        folded = turkish_fold(text)
        with self._connect() as db, db:
            rows = db.execute("SELECT id, text FROM notes").fetchall()
            for note_id, existing in rows:
                try:
                    plain_existing = self._protector.unprotect(existing)
                except UnreadableMemory as error:
                    self._log_unreadable("notes", int(note_id), error)
                    continue
                if turkish_fold(plain_existing) == folded:
                    if private:
                        db.execute("UPDATE notes SET private = 1 WHERE id = ?", (note_id,))
                    return "duplicate", note_id
            if len(rows) >= max_notes:
                return "full", None
            cursor = db.execute(
                "INSERT INTO notes (text, created_at, turn_id, private) VALUES (?,?,?,?)",
                (self._protector.protect(text), _now(), turn_id, int(private)),
            )
            return "added", cursor.lastrowid

    def delete_note(self, note_id: int) -> bool:
        with self._connect() as db, db:
            deleted = db.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount > 0
        if deleted:
            purge_backups(self.path)
        return deleted
