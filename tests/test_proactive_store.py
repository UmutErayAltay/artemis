"""ProactiveStore testleri: gerçek SQLite (tmp_path), mock yok."""

from __future__ import annotations

import concurrent.futures
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from artemis.proactive.store import (
    ProactiveStore,
    ProactiveStoreError,
    Reminder,
    reminder_event_id,
)


def _aware_now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    """Naive datetime'ı UTC aware yapar (test yardımcı fonksiyonu)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _trunc_seconds(dt: datetime) -> datetime:
    """Datetime'ı saniye hassasiyetine yuvarlar (microsecond=0)."""
    return dt.replace(microsecond=0)


def test_add_get_reminder(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    now = _aware_now()
    due = now + timedelta(hours=1)

    r = store.add_reminder("Test hatırlatma", due, tainted=False, now=now)
    assert isinstance(r, Reminder)
    assert r.id == 1
    assert r.text == "Test hatırlatma"
    # Depoda saniye hassasiyeti saklanır; microsecond=0 ile karşılaştır
    assert r.due_at == _trunc_seconds(due.astimezone(UTC))
    assert r.created_at == _trunc_seconds(now.astimezone(UTC))
    assert r.created_tainted is False
    assert r.cancelled is False
    assert r.state is None

    # Aynı veriyi get_reminder ile oku
    r2 = store.get_reminder(1)
    assert r2 is not None
    assert r2.id == r.id
    assert r2.text == r.text
    assert r2.due_at == r.due_at
    assert r2.created_at == r.created_at
    assert r2.created_tainted == r.created_tainted
    assert r2.cancelled == r.cancelled
    assert r2.state is None

    # Olmayan ID
    assert store.get_reminder(999) is None


def test_pending_reminders_ordering(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    now = _aware_now()

    # 3 hatırlatma: due_at sırası karışık, id sırası da karışık
    r1 = store.add_reminder("Erken", now + timedelta(hours=1), tainted=False, now=now)
    r2 = store.add_reminder("Geç", now + timedelta(hours=3), tainted=False, now=now)
    r3 = store.add_reminder("Orta", now + timedelta(hours=2), tainted=False, now=now)

    pending = store.pending_reminders()
    assert len(pending) == 3
    # due_at ASC, id ASC
    assert [p.id for p in pending] == [r1.id, r3.id, r2.id]
    assert [p.due_at for p in pending] == sorted([r1.due_at, r2.due_at, r3.due_at])


def test_due_reminders_boundary(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    now = _aware_now()

    # due_at == now olan hatırlatma
    r_due_now = store.add_reminder("Şimdi", now, tainted=False, now=now)
    # Gelecek
    r_future = store.add_reminder("Yarın", now + timedelta(days=1), tainted=False, now=now)
    # Geçmiş
    r_past = store.add_reminder("Dün", now - timedelta(days=1), tainted=False, now=now)

    due = store.due_reminders(now)
    # due_at <= now olanlar: r_past ve r_due_now (r_future hariç)
    due_ids = {r.id for r in due}
    assert r_past.id in due_ids
    assert r_due_now.id in due_ids
    assert r_future.id not in due_ids


def test_cancel_reminder_pending_true(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    now = _aware_now()
    r = store.add_reminder("İptal edilecek", now + timedelta(hours=1), tainted=False, now=now)

    # Pending iken iptal -> True
    assert store.cancel_reminder(r.id, now) is True
    # Tekrar iptal -> False (zaten iptal)
    assert store.cancel_reminder(r.id, now) is False

    # get_reminder cancelled=True göstermeli
    got = store.get_reminder(r.id)
    assert got is not None
    assert got.cancelled is True


def test_cancel_after_claim_false(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    now = _aware_now()
    r = store.add_reminder("Claim sonrası iptal", now + timedelta(hours=1), tainted=False, now=now)
    event_id = reminder_event_id(r.id)
    scheduled = now + timedelta(minutes=30)

    # Önce claim et
    assert store.claim(event_id, scheduled, "reminder", "owner1", now) is True
    # Sonra iptal etmeye çalış -> False (occurrence var)
    assert store.cancel_reminder(r.id, now) is False

    got = store.get_reminder(r.id)
    assert got is not None
    assert got.cancelled is False
    assert got.state == "claimed"


def test_claim_after_cancel_false(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    now = _aware_now()
    r = store.add_reminder("İptal後 claim", now + timedelta(hours=1), tainted=False, now=now)
    event_id = reminder_event_id(r.id)
    scheduled = now + timedelta(minutes=30)

    # Önce iptal et
    assert store.cancel_reminder(r.id, now) is True
    # Sonra claim etmeye çalış -> False (hatırlatma iptal)
    assert store.claim(event_id, scheduled, "reminder", "owner1", now) is False


def test_claim_twice_second_false(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    now = _aware_now()
    event_id = "briefing:daily"
    scheduled = now + timedelta(hours=1)

    assert store.claim(event_id, scheduled, "briefing", "owner1", now) is True
    assert store.claim(event_id, scheduled, "briefing", "owner2", now) is False

    state = store.occurrence_state(event_id, scheduled)
    assert state == "claimed"


def test_concurrent_claim_race(tmp_path: Path) -> None:
    """İki ProactiveStore örneği, 8 thread ile aynı claim için yarışır -> tam bir tane True."""
    db_path = tmp_path / "proactive.sqlite3"
    # Önce şemayı oluştur
    ProactiveStore(db_path)

    event_id = "race:event"
    scheduled = _aware_now() + timedelta(hours=1)
    kind = "test"
    num_threads = 8
    results: list[bool] = []
    lock = threading.Lock()

    def worker(owner: str) -> None:
        store = ProactiveStore(db_path)
        now = _aware_now()
        ok = store.claim(event_id, scheduled, kind, owner, now)
        with lock:
            results.append(ok)

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, f"owner{i}") for i in range(num_threads)]
        concurrent.futures.wait(futures)

    assert sum(results) == 1, f"Exactly one claim should succeed, got {sum(results)}"


def test_finish_twice_second_raises(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    now = _aware_now()
    event_id = "event:1"
    scheduled = now + timedelta(hours=1)

    store.claim(event_id, scheduled, "test", "owner", now)
    store.finish(event_id, scheduled, "delivered", "OK", now)

    # İkinci finish -> ProactiveStoreError (rowcount == 0)
    with pytest.raises(ProactiveStoreError):
        store.finish(event_id, scheduled, "delivered", "OK", now)


def test_finish_bad_state_raises_value_error(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    now = _aware_now()
    event_id = "event:1"
    scheduled = now + timedelta(hours=1)

    store.claim(event_id, scheduled, "test", "owner", now)
    with pytest.raises(ValueError):
        store.finish(event_id, scheduled, "invalid_state", "detail", now)


def test_mark_stale_claims_only_old_claimed(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    now = _aware_now()
    base = now - timedelta(hours=2)

    # 3 occurrence: iki claimed (eski ve yeni), bir delivered
    store.claim("event:old", base - timedelta(hours=1), "test", "owner", base - timedelta(hours=1))
    store.claim(
        "event:new", base + timedelta(minutes=30), "test", "owner", base + timedelta(minutes=30)
    )
    store.claim("event:done", base - timedelta(hours=2), "test", "owner", base - timedelta(hours=2))
    store.finish(
        "event:done", base - timedelta(hours=2), "delivered", "OK", base - timedelta(hours=1)
    )

    # older_than = base (yani base'den eski claimed'ler uncertain olmalı)
    count = store.mark_stale_claims(base, now)
    assert count == 1  # sadece event:old

    assert store.occurrence_state("event:old", base - timedelta(hours=1)) == "uncertain"
    assert store.occurrence_state("event:new", base + timedelta(minutes=30)) == "claimed"
    assert store.occurrence_state("event:done", base - timedelta(hours=2)) == "delivered"


def test_count_finished_between_boundaries(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    base = _aware(datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC))

    # 3 finished occurrence
    for i, (finish_time, state) in enumerate(
        [
            (base - timedelta(hours=1), "delivered"),  # start öncesi
            (base, "delivered"),  # start == finish_at (dahil)
            (base + timedelta(hours=1), "delivered"),  # arada
            (base + timedelta(hours=2), "failed"),  # farklı state
            (base + timedelta(hours=3), "delivered"),  # end hariç (end == finish_at)
        ]
    ):
        event_id = f"event:{i}"
        scheduled = finish_time - timedelta(minutes=30)
        store.claim(event_id, scheduled, "test", "owner", scheduled)
        store.finish(event_id, scheduled, state, "detail", finish_time)

    start = base
    end = base + timedelta(hours=3)

    # delivered olanlar, start <= finished_at < end
    count = store.count_finished_between(start, end, ("delivered",))
    assert count == 2  # base ve base+1h

    # failed da dahil
    count = store.count_finished_between(start, end, ("delivered", "failed"))
    assert count == 3  # base, base+1h, base+2h

    # end boundary: base+3h hariç
    count = store.count_finished_between(start, base + timedelta(hours=3), ("delivered",))
    assert count == 2


def test_naive_datetime_raises_value_error(tmp_path: Path) -> None:
    store = ProactiveStore(tmp_path / "proactive.sqlite3")
    naive = datetime(2025, 1, 1, 12, 0, 0)  # tzinfo yok

    with pytest.raises(ValueError):
        store.add_reminder("test", naive, tainted=False, now=_aware_now())
    with pytest.raises(ValueError):
        store.add_reminder("test", _aware_now(), tainted=False, now=naive)
    with pytest.raises(ValueError):
        store.due_reminders(naive)
    with pytest.raises(ValueError):
        store.cancel_reminder(1, naive)
    with pytest.raises(ValueError):
        store.claim("event", naive, "kind", "owner", _aware_now())
    with pytest.raises(ValueError):
        store.claim("event", _aware_now(), "kind", "owner", naive)
    with pytest.raises(ValueError):
        store.finish("event", naive, "delivered", "d", _aware_now())
    with pytest.raises(ValueError):
        store.finish("event", _aware_now(), "delivered", "d", naive)
    with pytest.raises(ValueError):
        store.occurrence_state("event", naive)
    with pytest.raises(ValueError):
        store.mark_stale_claims(naive, _aware_now())
    with pytest.raises(ValueError):
        store.mark_stale_claims(_aware_now(), naive)
    with pytest.raises(ValueError):
        store.count_finished_between(naive, _aware_now())
    with pytest.raises(ValueError):
        store.count_finished_between(_aware_now(), naive)


def test_data_survives_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "proactive.sqlite3"
    now = _aware_now()

    store1 = ProactiveStore(db_path)
    r = store1.add_reminder("Kalıcı hatırlatma", now + timedelta(hours=1), tainted=True, now=now)
    event_id = reminder_event_id(r.id)
    scheduled = now + timedelta(minutes=30)
    store1.claim(event_id, scheduled, "reminder", "owner1", now)
    store1.finish(event_id, scheduled, "delivered", "Teslim edildi", now)

    # Yeni instance ile aç
    store2 = ProactiveStore(db_path)
    r2 = store2.get_reminder(r.id)
    assert r2 is not None
    assert r2.text == "Kalıcı hatırlatma"
    assert r2.created_tainted is True
    assert r2.state == "delivered"

    state = store2.occurrence_state(event_id, scheduled)
    assert state == "delivered"


def test_parent_is_regular_file_raises(tmp_path: Path) -> None:
    # Parent bir dosya (dizin değil)
    file_path = tmp_path / "parent_file.txt"
    file_path.write_text("not a directory")
    db_path = file_path / "proactive.sqlite3"  # Bu bir dosya altında dosya yaratmaya çalışır

    with pytest.raises(ProactiveStoreError):
        ProactiveStore(db_path)


def test_reminder_event_id_format() -> None:
    assert reminder_event_id(1) == "reminder:1"
    assert reminder_event_id(42) == "reminder:42"
    assert reminder_event_id(0) == "reminder:0"
