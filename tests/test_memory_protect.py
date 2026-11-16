"""Konuşma hafızası şifrelemesi: sahte koruyucu ve Windows DPAPI sınamaları."""

from __future__ import annotations

import base64
import sqlite3
import sys
from pathlib import Path

import pytest

from artemis.brain.conversation import TurnRecord
from artemis.memory.protect import (
    PLAIN_PREFIX,
    PREFIX,
    DpapiProtector,
    PlainProtector,
    UnreadableMemory,
    protector_for,
)
from artemis.memory.store import MemoryStore


class FakeProtector:
    """Tersine çevrilebilir sahte koruyucu: 'fake1:' öneki + ters metin."""

    encrypts: bool = True
    prefix: str = "fake1:"
    FAKE_PREFIX = "fake1:"

    def protect(self, text: str) -> str:
        return self.FAKE_PREFIX + text[::-1]

    def unprotect(self, text: str) -> str:
        if text.startswith(self.FAKE_PREFIX):
            return text[len(self.FAKE_PREFIX) :][::-1]
        if text.startswith(PLAIN_PREFIX):
            return text[len(PLAIN_PREFIX) :]
        if text.startswith(PREFIX) or text.startswith("bad1:"):
            raise UnreadableMemory("Sahte koruyucu bu öneki çözemez.")
        return text


def _raw_turns(db_path: Path) -> list[tuple[int, str, str, str, str]]:
    with sqlite3.connect(db_path) as db:
        return db.execute(
            "SELECT id, record_json, user_text, answer, search_text FROM turns ORDER BY id"
        ).fetchall()


def _raw_notes(db_path: Path) -> list[tuple[int, str]]:
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT id, text FROM notes ORDER BY id").fetchall()


def _create_v1_plaintext_db(db_path: Path) -> None:
    """Şema sürümü 1 olan düz metin veritabanı hazırlar."""
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE history_policy (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                retention_days INTEGER NOT NULL CHECK(retention_days >= 0)
            );
            INSERT INTO history_policy VALUES (1, 0);
            CREATE TABLE turns (
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
            CREATE TABLE resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                last_turn_id INTEGER NOT NULL
            );
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                private INTEGER NOT NULL DEFAULT 0
            );
            PRAGMA user_version = 1;
            """
        )
        rec = TurnRecord(
            steps=(),
            answer_private=False,
            tainted=False,
            turn_id="v1-turn",
            created_at="2026-11-01T10:00:00+00:00",
            user_text="Kahve tercihim ne?",
            answer="Filtre kahve içersin.",
        )
        db.execute(
            "INSERT INTO turns (turn_id, created_at, record_json, user_text, answer,"
            " search_text, answer_private, tainted) VALUES (?,?,?,?,?,?,?,?)",
            (
                rec.turn_id,
                rec.created_at,
                rec.model_dump_json(),
                rec.user_text,
                rec.answer,
                "kahve tercihim ne? filtre kahve içersin.",
                0,
                0,
            ),
        )
        db.execute(
            "INSERT INTO notes (text, created_at, turn_id, private) VALUES (?,?,?,?)",
            ("Sabah filtre kahve.", "2026-11-01T10:00:00+00:00", "v1-turn", 0),
        )
        db.commit()


def test_store_writes_protected_values_and_reads_plaintext(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    store = MemoryStore(db_path, protector=FakeProtector())

    store.add_turn(
        TurnRecord(
            steps=(),
            answer_private=False,
            tainted=False,
            turn_id="t1",
            created_at="2026-11-10T12:00:00+00:00",
            user_text="İstanbul hava durumu nasıl?",
            answer="Bugün güneşli ve serin.",
        )
    )
    status, _ = store.add_note("Filtre kahve severim.", "t1", max_notes=20)
    assert status == "added"

    raw_turns = _raw_turns(db_path)
    assert len(raw_turns) == 1
    for col in raw_turns[0][1:]:
        assert col.startswith("fake1:")
        assert "İstanbul" not in col
        assert "güneşli" not in col

    raw_notes = _raw_notes(db_path)
    assert len(raw_notes) == 1
    assert raw_notes[0][1].startswith("fake1:")
    assert "Filtre" not in raw_notes[0][1]

    recent = store.recent_turns(10)
    assert len(recent) == 1
    assert recent[0].user_text == "İstanbul hava durumu nasıl?"
    assert recent[0].answer == "Bugün güneşli ve serin."

    page = store.history_page(10)
    assert page[0]["user"] == "İstanbul hava durumu nasıl?"
    assert page[0]["answer"] == "Bugün güneşli ve serin."

    hits = store.search_turns("güneşli", 5)
    assert len(hits) == 1
    assert hits[0].answer == "Bugün güneşli ve serin."

    notes = store.list_notes()
    assert len(notes) == 1
    assert notes[0].text == "Filtre kahve severim."


def test_v1_migration_encrypts_once_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    _create_v1_plaintext_db(db_path)

    store = MemoryStore(db_path, protector=FakeProtector())
    first_turns = _raw_turns(db_path)
    first_notes = _raw_notes(db_path)
    assert len(first_turns) == 1
    assert all(col.startswith("fake1:") for col in first_turns[0][1:])
    assert len(first_notes) == 1
    assert first_notes[0][1].startswith("fake1:")

    with sqlite3.connect(db_path) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2

    assert store.recent_turns(5)[0].user_text == "Kahve tercihim ne?"
    assert store.list_notes()[0].text == "Sabah filtre kahve."

    store_again = MemoryStore(db_path, protector=FakeProtector())
    assert _raw_turns(db_path) == first_turns
    assert _raw_notes(db_path) == first_notes
    assert store_again.recent_turns(5)[0].answer == "Filtre kahve içersin."


def test_disabled_protector_rewrites_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    _create_v1_plaintext_db(db_path)
    before_turns = _raw_turns(db_path)
    before_notes = _raw_notes(db_path)

    store = MemoryStore(db_path, protector=protector_for(False))
    disabled = protector_for(False)
    # Kapalıyken yazma her platformda düz metindir; Windows'ta önceki şifreli dönemin
    # kayıtları yine çözülür (kapatmak kaydı okunmaz yapmamalı).
    assert disabled.protect("düz metin") == "düz metin"
    if sys.platform == "win32":
        sealed = DpapiProtector().protect("eski şifreli kayıt")
        assert disabled.unprotect(sealed) == "eski şifreli kayıt"
    else:
        assert isinstance(disabled, PlainProtector)
    assert _raw_turns(db_path) == before_turns
    assert _raw_notes(db_path) == before_notes
    assert store.recent_turns(5)[0].user_text == "Kahve tercihim ne?"
    assert store.list_notes()[0].text == "Sabah filtre kahve."


def test_unreadable_rows_are_skipped_without_dropping_valid_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    protector = FakeProtector()
    store = MemoryStore(db_path, protector=protector)

    store.add_turn(
        TurnRecord(
            steps=(),
            answer_private=False,
            tainted=False,
            turn_id="ok-turn",
            created_at="2026-11-10T12:00:00+00:00",
            user_text="Sağlam soru",
            answer="Sağlam yanıt",
        )
    )
    store.add_note("Sağlam not", "ok-turn", max_notes=20)

    # Doğrudan çözülemeyen önekli bozuk satırlar ekle.
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO turns (turn_id, created_at, record_json, user_text, answer,"
            " search_text, answer_private, tainted) VALUES (?,?,?,?,?,?,?,?)",
            (
                "bad-turn",
                "2026-11-10T12:01:00+00:00",
                f"{PREFIX}bozuk",
                f"{PREFIX}bozuk",
                f"{PREFIX}bozuk",
                f"{PREFIX}bozuk",
                0,
                0,
            ),
        )
        db.execute(
            "INSERT INTO notes (text, created_at, turn_id, private) VALUES (?,?,?,?)",
            (f"{PREFIX}bozuk-not", "2026-11-10T12:01:00+00:00", "bad-turn", 0),
        )
        db.commit()

    recent = store.recent_turns(10)
    assert [r.turn_id for r in recent] == ["ok-turn"]
    assert len(store.history_page(10)) == 1
    assert len(store.search_turns("sağlam", 5)) == 1
    assert [n.text for n in store.list_notes()] == ["Sağlam not"]

    # Okunamayan satırlar silinmemeli.
    assert len(_raw_turns(db_path)) == 2
    assert len(_raw_notes(db_path)) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI yalnız Windows")
class TestDpapiProtector:
    def test_round_trip(self) -> None:
        protector = DpapiProtector()
        for sample in ("", "Merhaba Artemis! Şifreli Türkçe metin: ğüşıöç", '{"a": 1}'):
            encrypted = protector.protect(sample)
            assert encrypted.startswith(PREFIX)
            assert sample == "" or sample not in encrypted
            assert protector.unprotect(encrypted) == sample

    def test_tampered_ciphertext_raises_unreadable_memory(self) -> None:
        protector = DpapiProtector()
        encrypted = protector.protect("Gizli konuşma metni")
        raw = bytearray(base64.b64decode(encrypted[len(PREFIX) :]))
        raw[-1] ^= 0xFF
        tampered = PREFIX + base64.b64encode(bytes(raw)).decode("ascii")
        with pytest.raises(UnreadableMemory):
            protector.unprotect(tampered)


def test_enabling_encryption_on_v2_plaintext_db_encrypts_on_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    plain_store = MemoryStore(db_path, protector=PlainProtector())
    plain_store.add_turn(
        TurnRecord(
            steps=(),
            answer_private=False,
            tainted=False,
            turn_id="t1",
            created_at="2026-11-10T12:00:00+00:00",
            user_text="Düz metin soru",
            answer="Düz metin yanıt",
        )
    )
    plain_store.add_note("Düz metin not", "t1", max_notes=10)

    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    turns_before = _raw_turns(db_path)
    assert not turns_before[0][2].startswith("fake1:")
    assert not turns_before[0][2].startswith(PREFIX)

    enc_store = MemoryStore(db_path, protector=FakeProtector())
    turns_after = _raw_turns(db_path)
    notes_after = _raw_notes(db_path)
    for col in turns_after[0][1:]:
        assert col.startswith("fake1:")
    assert notes_after[0][1].startswith("fake1:")

    assert enc_store.recent_turns(5)[0].user_text == "Düz metin soru"
    assert enc_store.list_notes()[0].text == "Düz metin not"


def test_failing_protector_during_sweep_leaves_rows_unchanged_and_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    plain_store = MemoryStore(db_path, protector=PlainProtector())
    plain_store.add_turn(
        TurnRecord(
            steps=(),
            answer_private=False,
            tainted=False,
            turn_id="t1",
            created_at="2026-11-10T12:00:00+00:00",
            user_text="Kritik soru",
            answer="Kritik yanıt",
        )
    )
    plain_store.add_note("Kritik not", "t1", max_notes=10)
    turns_before = _raw_turns(db_path)
    notes_before = _raw_notes(db_path)

    class FailingProtector:
        encrypts: bool = True
        prefix: str = "fail1:"

        def protect(self, text: str) -> str:
            raise OSError("Donanım hatası")

        def unprotect(self, text: str) -> str:
            return text

    with pytest.raises(sqlite3.OperationalError) as exc_info:
        MemoryStore(db_path, protector=FailingProtector())
    assert "Hafıza şifrelemesi uygulanamadı." in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, OSError)

    assert _raw_turns(db_path) == turns_before
    assert _raw_notes(db_path) == notes_before


def test_user_text_dpapi_prefix_round_trips_through_plain_protector() -> None:
    protector = PlainProtector()
    user_text = "dpapi1:abc"
    protected = protector.protect(user_text)
    assert protected == "plain1:dpapi1:abc"
    assert protector.unprotect(protected) == user_text


def test_user_text_dpapi_prefix_written_with_encryption_off_reads_back_intact(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "memory.sqlite3"
    store = MemoryStore(db_path, protector=PlainProtector())
    store.add_turn(
        TurnRecord(
            steps=(),
            answer_private=False,
            tainted=False,
            turn_id="t1",
            created_at="2026-11-10T12:00:00+00:00",
            user_text="dpapi1:abc",
            answer="dpapi1:def",
        )
    )
    store.add_note("dpapi1:ghi", "t1", max_notes=10)

    raw_turns = _raw_turns(db_path)
    assert raw_turns[0][2] == "plain1:dpapi1:abc"
    assert raw_turns[0][3] == "plain1:dpapi1:def"
    raw_notes = _raw_notes(db_path)
    assert raw_notes[0][1] == "plain1:dpapi1:ghi"

    recent = store.recent_turns(5)
    assert recent[0].user_text == "dpapi1:abc"
    assert recent[0].answer == "dpapi1:def"
    notes = store.list_notes()
    assert notes[0].text == "dpapi1:ghi"


def test_stored_plain_prefix_encrypted_as_unescaped_text_by_sweep(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    plain_store = MemoryStore(db_path, protector=PlainProtector())
    plain_store.add_note("plain1:x", "t1", max_notes=10)

    raw_notes_before = _raw_notes(db_path)
    assert raw_notes_before[0][1] == "plain1:plain1:x"

    enc_store = MemoryStore(db_path, protector=FakeProtector())
    raw_notes_after = _raw_notes(db_path)
    assert raw_notes_after[0][1] == "fake1:x:1nialp"

    notes = enc_store.list_notes()
    assert notes[0].text == "plain1:x"
