"""Gerçek SQLite üzerinde bütçenin süreçler arası kalıcılığını sınar."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest

from jarvis.brain.budget import BudgetLedger
from jarvis.brain.errors import BudgetExceeded


def test_reservation_checks_spending_and_outstanding_amounts(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.sqlite", Decimal("1"))
    first = ledger.reserve(Decimal("0.6"))
    ledger.settle(first, Decimal("0.3"))
    ledger.reserve(Decimal("0.7"))
    with pytest.raises(BudgetExceeded):
        ledger.reserve(Decimal("0.000000000000000000000000001"))
    with pytest.raises(BudgetExceeded):
        ledger.reserve(Decimal("1e-100"))
    assert ledger.totals() == (Decimal("0.3"), Decimal("0.7"))


def test_settlement_releases_unused_reservation(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.sqlite", Decimal("1"))
    token = ledger.reserve(Decimal("0.8"))
    ledger.settle(token, Decimal("0.2"))
    ledger.settle(token, Decimal("0.2"))
    assert ledger.spent == Decimal("0.2")
    assert ledger.reserved == 0
    ledger.reserve(Decimal("0.8"))
    with pytest.raises(ValueError):
        ledger.settle(token, Decimal("0.1"))
    with pytest.raises(ValueError):
        ledger.release(token)


def test_release_only_frees_unspent_reservation(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.sqlite", Decimal("1"))
    token = ledger.reserve(Decimal("1"))
    ledger.release(token)
    ledger.release(token)
    assert ledger.totals() == (Decimal(0), Decimal(0))
    with pytest.raises(ValueError):
        ledger.settle(token, Decimal("0.1"))
    ledger.reserve(Decimal("1"))


def test_overrun_is_recorded_before_error(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.sqlite", Decimal("1"))
    token = ledger.reserve(Decimal("0.8"))
    with pytest.raises(BudgetExceeded):
        ledger.settle(token, Decimal("1.2"))
    assert ledger.totals() == (Decimal("1.2"), Decimal(0))
    with pytest.raises(BudgetExceeded):
        ledger.reserve(Decimal("0.01"))


def test_parallel_instances_cannot_overreserve(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite"
    ledgers = [BudgetLedger(path, Decimal("1")) for _ in range(8)]
    barrier = Barrier(len(ledgers))

    def reserve(ledger: BudgetLedger) -> bool:
        barrier.wait(timeout=5)
        try:
            ledger.reserve(Decimal("0.3"))
        except BudgetExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=len(ledgers)) as executor:
        successes = list(executor.map(reserve, ledgers))
    assert successes.count(True) == 3
    assert ledgers[0].reserved == Decimal("0.9")


def test_crashed_reservation_survives_reopening_until_utc_day_ends(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite"
    now = datetime(2026, 1, 1, 23, 59, tzinfo=UTC)
    old = BudgetLedger(path, Decimal("1"), clock=lambda: now)
    token = old.reserve(Decimal("1"))
    reopened = BudgetLedger(path, Decimal("1"), clock=lambda: now)
    with pytest.raises(BudgetExceeded):
        reopened.reserve(Decimal("0.01"))
    now += timedelta(minutes=2)
    assert reopened.totals() == (Decimal(0), Decimal(0))
    reopened.reserve(Decimal("1"))
    old.settle(token, Decimal("0.6"))
    assert reopened.totals() == (Decimal(0), Decimal("1"))


def test_day_is_utc_not_local_date(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite"
    local = datetime(2026, 1, 2, 1, tzinfo=timezone(timedelta(hours=3)))
    ledger = BudgetLedger(path, Decimal("1"), clock=lambda: local)
    ledger.reserve(Decimal("1"))
    utc = BudgetLedger(path, Decimal("1"), clock=lambda: local.astimezone(UTC))
    assert utc.reserved == Decimal("1")


@pytest.mark.parametrize("amount", ["-1", "NaN", "Infinity", "-Infinity"])
def test_invalid_amounts_do_not_change_ledger(tmp_path: Path, amount: str) -> None:
    invalid = Decimal(amount)
    path = tmp_path / "budget.sqlite"
    with pytest.raises(ValueError):
        BudgetLedger(path, invalid)
    ledger = BudgetLedger(path, Decimal("1"))
    token = ledger.reserve(Decimal("0.5"))
    with pytest.raises(ValueError):
        ledger.reserve(invalid)
    with pytest.raises(ValueError):
        ledger.settle(token, invalid)
    assert ledger.totals() == (Decimal(0), Decimal("0.5"))


def test_unknown_reservation_is_an_error(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.sqlite", Decimal("1"))
    with pytest.raises(KeyError):
        ledger.release("missing")
    with pytest.raises(KeyError):
        ledger.settle("missing", Decimal(0))


def test_zero_cost_calls_fit_zero_budget(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.sqlite", Decimal(0))
    token = ledger.reserve(Decimal(0))
    ledger.settle(token, Decimal(0))
    assert ledger.totals() == (Decimal(0), Decimal(0))
