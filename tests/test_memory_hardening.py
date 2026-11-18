"""MemoryStore sertleştirme testleri: atomik ekleme, kota sayımı, iz silme ve hızlı arama."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from artemis.brain.conversation import TurnRecord
from artemis.memory.protect import PlainProtector, UnreadableMemory
from artemis.memory.store import MemoryStore


class _BadRejectingProtector(PlainProtector):
    def unprotect(self, text: str) -> str:
        if text.startswith("BAD"):
            raise UnreadableMemory("Bozuk test kaydı.")
        return super().unprotect(text)


class _CountingProtector(PlainProtector):
    def __init__(self) -> None:
        self.unprotect_calls = 0

    def unprotect(self, text: str) -> str:
        self.unprotect_calls += 1
        return super().unprotect(text)


def test_add_note_begins_immediate_transaction_before_select(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bugünkü kodda add_note BEGIN IMMEDIATE çalıştırmadığı için bu test başarısız olur.
    statements: list[str] = []
    orig_connect = MemoryStore._connect

    def traced_connect(self: MemoryStore) -> closing[sqlite3.Connection]:
        conn_cm = orig_connect(self)
        conn_cm.thing.set_trace_callback(statements.append)
        return conn_cm

    monkeypatch.setattr(MemoryStore, "_connect", traced_connect)
    store = MemoryStore(tmp_path / "memory.sqlite3")
    statements.clear()

    status, note_id = store.add_note("ilk not", "t1", max_notes=5)
    assert status == "added" and note_id is not None

    begin_indices = [
        i for i, sql in enumerate(statements) if sql.startswith("BEGIN IMMEDIATE")
    ]
    select_indices = [
        i for i, sql in enumerate(statements) if sql.startswith("SELECT id, text FROM notes")
    ]
    assert begin_indices and select_indices
    assert begin_indices[0] < select_indices[0]


def test_add_note_counts_only_readable_notes_against_max_notes(tmp_path: Path) -> None:
    # Bugünkü kod okunamayan satırları da max_notes kotasına saydığı için bu test başarısız olur.
    db_path = tmp_path / "memory.sqlite3"
    store = MemoryStore(db_path, protector=_BadRejectingProtector())
    with closing(sqlite3.connect(db_path)) as conn, conn:
        for idx in (1, 2):
            conn.execute(
                "INSERT INTO notes (text, created_at, turn_id, private) VALUES (?,?,?,?)",
                (f"BAD{idx}", "2026-01-01T00:00:00+00:00", f"raw{idx}", 0),
            )

    status, note_id = store.add_note("yeni not", "t1", max_notes=2)
    assert status == "added" and note_id is not None


def test_delete_note_scrubs_residue_from_db_and_wal(tmp_path: Path) -> None:
    # Bugünkü kodda delete_note VACUUM ve wal_checkpoint yapmadığı için silinen iz dosyada kalır.
    db_path = tmp_path / "memory.sqlite3"
    store = MemoryStore(db_path)
    status, note_id = store.add_note("IZ-SILINECEK-4711", "t1", max_notes=5)
    assert status == "added" and note_id is not None

    assert store.delete_note(note_id) is True

    marker = b"IZ-SILINECEK-4711"
    assert marker not in db_path.read_bytes()
    wal_path = Path(f"{db_path}-wal")
    if wal_path.exists():
        assert marker not in wal_path.read_bytes()


def test_search_turns_decrypts_user_and_answer_only_for_hits(tmp_path: Path) -> None:
    # Bugünkü kodda search_turns her satır için 3 unprotect (15) yaptığı için başarısız olur.
    protector = _CountingProtector()
    store = MemoryStore(tmp_path / "memory.sqlite3", protector=protector)

    def make_turn(turn_id: str, user_text: str, answer: str) -> TurnRecord:
        return TurnRecord(
            turn_id=turn_id,
            user_text=user_text,
            answer=answer,
            steps=[],
            answer_private=False,
            tainted=False,
        )

    turns = [
        make_turn("t1", "elma aldım", "afiyet olsun"),
        make_turn("t2", "kahve demledim", "filtre mi"),
        make_turn("t3", "kitap okudum", "güzel seçim"),
        make_turn("t4", "sabah kahve içtim", "sade mi"),
        make_turn("t5", "yürüyüş yaptım", "harika"),
    ]
    for record in turns:
        store.add_turn(record)

    protector.unprotect_calls = 0
    hits = store.search_turns("kahve", limit=5)

    assert len(hits) == 2
    assert protector.unprotect_calls == 5 + 2 * 2
