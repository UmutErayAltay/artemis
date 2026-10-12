"""Günlük tavanı süreçler arasında atomik rezervasyonlarla korur."""

import sqlite3
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from uuid import uuid4

from jarvis.brain.errors import BudgetExceeded, ProviderError

# SQLite beklemesi senkron çalışır. Kilit beklemesini sağlayıcı denemesinin olağan
# süresinden kısa tutarak olay döngüsünün varsayılan zaman aşımı boyunca durmasını önler.
_SQLITE_BUSY_TIMEOUT_SECONDS = 0.1


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_amount(amount: Decimal) -> None:
    if not amount.is_finite() or amount < 0:
        raise ValueError("Maliyet sonlu ve negatif olmayan Decimal olmalı.")


def _sum_exact(amounts: Iterable[Decimal]) -> Decimal:
    values = list(amounts)
    if not values:
        return Decimal(0)
    # Varsayılan Decimal hassasiyeti çok küçük farkı yutarak tavana izin verebilir.
    lowest = min(int(value.as_tuple().exponent) for value in values)
    highest = max(value.adjusted() for value in values)
    with localcontext() as context:
        context.prec = max(28, highest - lowest + len(str(len(values))) + 2)
        return sum(values, Decimal(0))


class BudgetLedger:
    """Belirsiz çağrıların rezervasyonunu gün sonuna kadar harcanmış sayar.

    Çağrı hangi UTC gününde başlatıldıysa maliyeti o güne yazılır. Bağlantılar
    işlem başına açıldığından başka süreçlerin ve örneklerin rezervasyonları
    görünür; eski günler denetim için silinmez.
    """

    def __init__(
        self,
        path: Path,
        daily_limit: Decimal,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _validate_amount(daily_limit)
        self.path = path
        self.daily_limit = daily_limit
        self._clock = clock or _utc_now
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ProviderError("Günlük bütçe kaydı güvenli biçimde kullanılamıyor.") from None
        with self._transaction() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS budget_reservations (
                    token TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    reserved TEXT NOT NULL,
                    spent TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('reserved', 'settled', 'released'))
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS budget_day ON budget_reservations(day)"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=_SQLITE_BUSY_TIMEOUT_SECONDS,
                isolation_level=None,
            )
            with connection:
                # Okuma ve yazma arasına başka rezervasyon girmemelidir.
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        except (OSError, sqlite3.Error):
            # Disk, kilit ve bozuk veritabanı hataları ağ çağrısını durdurmalıdır.
            raise ProviderError("Günlük bütçe kaydı güvenli biçimde kullanılamıyor.") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error as error:
                    # Kapatma hatası da görünür olsun. Mevcut iptal veya asıl
                    # hatayı koru; eklenen metin yalnızca hata türünü açıklar.
                    active_error = sys.exception()
                    if active_error is not None:
                        active_error.add_note(
                            f"Bütçe SQLite bağlantısı kapatılamadı ({type(error).__name__})."
                        )
                    else:
                        raise ProviderError(
                            "Günlük bütçe kaydı güvenli biçimde kullanılamıyor."
                        ) from None

    def _day(self) -> str:
        current = self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("Bütçe saati saat dilimi içermeli.")
        return current.astimezone(UTC).date().isoformat()

    @staticmethod
    def _totals(connection: sqlite3.Connection, day: str) -> tuple[Decimal, Decimal]:
        rows = connection.execute(
            "SELECT spent, reserved FROM budget_reservations WHERE day = ?",
            (day,),
        )
        spent: list[Decimal] = []
        reserved: list[Decimal] = []
        # SQLite REAL toplamı kuruş altı değerlerde sınırı gevşetebilir.
        for actual_value, reserved_value in rows:
            spent.append(BudgetLedger._stored_amount(actual_value))
            reserved.append(BudgetLedger._stored_amount(reserved_value))
        return _sum_exact(spent), _sum_exact(reserved)

    @staticmethod
    def _stored_amount(value: object) -> Decimal:
        try:
            amount = Decimal(str(value))
            _validate_amount(amount)
        except (InvalidOperation, TypeError, ValueError):
            raise ProviderError(
                "Günlük bütçe kaydı güvenli biçimde kullanılamıyor."
            ) from None
        return amount

    def totals(self) -> tuple[Decimal, Decimal]:
        """Bugünkü gerçekleşen maliyet ve henüz kapanmamış rezervasyonu döndürür."""
        with self._transaction() as connection:
            return self._totals(connection, self._day())

    @property
    def spent(self) -> Decimal:
        return self.totals()[0]

    @property
    def reserved(self) -> Decimal:
        return self.totals()[1]

    def reserve(self, amount: Decimal) -> str:
        """Çağrı üst maliyetini ayırır; tavan aşılırsa kayıt yaratmaz."""
        _validate_amount(amount)
        token = uuid4().hex
        with self._transaction() as connection:
            day = self._day()
            spent, reserved = self._totals(connection, day)
            if _sum_exact((spent, reserved, amount)) > self.daily_limit:
                raise BudgetExceeded("Günlük maliyet tavanı bu çağrıya izin vermiyor.")
            connection.execute(
                """INSERT INTO budget_reservations
                (token, day, reserved, spent, status) VALUES (?, ?, ?, '0', 'reserved')""",
                (token, day, str(amount)),
            )
        return token

    def settle(self, token: str, actual: Decimal) -> None:
        """Doğrulanmış maliyeti yazar; aşılan tavan geçmiş harcamayı silemez."""
        _validate_amount(actual)
        exceeded = False
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT day, status, spent FROM budget_reservations WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                raise KeyError("Bütçe rezervasyonu bulunamadı.")
            day, status, prior_actual = row
            if status == "settled" and self._stored_amount(prior_actual) == actual:
                return
            if status != "reserved":
                raise ValueError("Kapanmış bütçe rezervasyonu değiştirilemez.")
            connection.execute(
                """UPDATE budget_reservations
                SET spent = ?, reserved = '0', status = 'settled' WHERE token = ?""",
                (str(actual), token),
            )
            spent, reserved = self._totals(connection, day)
            exceeded = _sum_exact((spent, reserved)) > self.daily_limit
        if exceeded:
            raise BudgetExceeded("Gerçekleşen maliyet günlük tavanı aştı; maliyet kaydedildi.")

    def release(self, token: str) -> None:
        """Yalnız hiç ücret doğurmadığı bilinen çağrının rezervasyonunu kaldırır."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM budget_reservations WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                raise KeyError("Bütçe rezervasyonu bulunamadı.")
            if row[0] == "settled":
                raise ValueError("Gerçekleşen maliyet serbest bırakılamaz.")
            connection.execute(
                """UPDATE budget_reservations
                SET reserved = '0', status = 'released' WHERE token = ?""",
                (token,),
            )
