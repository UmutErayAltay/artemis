"""Üst çubuk günlük bütçe ve harcama sayacı (`BudgetLedger` salt-okunur görünümü)."""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from artemis.brain.budget import BudgetLedger, _sum_exact
from artemis.brain.errors import ProviderError
from artemis.channels.desktop.page_kit import repolish
from artemis.config.settings import load_settings
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow


@dataclass(frozen=True)
class BudgetSnapshot:
    """Günlük bütçe göstergesi anlık görüntüsü."""

    spent: Decimal
    reserved: Decimal
    limit: Decimal
    available: bool


def read_budget_snapshot(db_path: Path, daily_limit: Decimal) -> BudgetSnapshot:
    """`budget.sqlite3` varsa salt okunur okur; dosya yoksa diskte dosya yaratmaz."""
    if not db_path.is_file():
        return BudgetSnapshot(
            spent=Decimal(0),
            reserved=Decimal(0),
            limit=daily_limit,
            available=True,
        )
    day = datetime.now(UTC).date().isoformat()
    connection: sqlite3.Connection | None = None
    try:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.1)
        rows = connection.execute(
            "SELECT spent, reserved FROM budget_reservations WHERE day = ?",
            (day,),
        ).fetchall()
        spent_items: list[Decimal] = []
        reserved_items: list[Decimal] = []
        for actual_val, reserved_val in rows:
            spent_items.append(BudgetLedger._stored_amount(actual_val))
            reserved_items.append(BudgetLedger._stored_amount(reserved_val))
        return BudgetSnapshot(
            spent=_sum_exact(spent_items),
            reserved=_sum_exact(reserved_items),
            limit=daily_limit,
            available=True,
        )
    except (ProviderError, OSError, sqlite3.Error, ValueError):
        return BudgetSnapshot(
            spent=Decimal(0),
            reserved=Decimal(0),
            limit=daily_limit,
            available=False,
        )
    finally:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.close()


def format_budget_text(snapshot: BudgetSnapshot) -> str:
    if not snapshot.available:
        return t("Bütçe: —")
    used = _sum_exact((snapshot.spent, snapshot.reserved))
    if snapshot.limit > 0:
        return f"${used:.2f} / ${snapshot.limit:.2f}"
    return f"${used:.2f}"


def format_budget_tooltip(snapshot: BudgetSnapshot) -> str:
    if not snapshot.available:
        return t("Günlük bütçe kaydı şu an okunamadı.")
    used = _sum_exact((snapshot.spent, snapshot.reserved))
    base = t(
        "Bugünkü harcama (UTC): ${spent:.4f} · Bekleyen ayrım: ${reserved:.4f} · "
        "Günlük sınır: ${limit:.2f}"
    ).format(
        spent=snapshot.spent,
        reserved=snapshot.reserved,
        limit=snapshot.limit,
    )
    if snapshot.limit > 0 and used >= snapshot.limit * Decimal("0.8"):
        return base + "\n" + t("Günlük bütçe sınırının %80'ine yaklaşıldı veya aşıldı.")
    return base


def _resolve_budget_target(window: DesktopWindow) -> tuple[Path, Decimal] | None:
    config_path = getattr(window, "_config_path", None)
    if isinstance(config_path, Path) and config_path.is_file():
        try:
            settings = load_settings(config_path)
            return settings.state_dir / "budget.sqlite3", settings.daily_budget_usd
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            pass
    bridge = getattr(window, "bridge", None)
    target_fn = getattr(bridge, "budget_target", None)
    if callable(target_fn):
        result = target_fn()
        if isinstance(result, tuple) and len(result) == 2:
            return result
    return None


def refresh_budget(window: DesktopWindow) -> None:
    """Üst çubuktaki günlük bütçe çipini günceller."""
    chip = getattr(window, "budget_chip", None)
    if chip is None:
        return
    target = _resolve_budget_target(window)
    if target is None:
        return
    db_path, daily_limit = target
    snapshot = read_budget_snapshot(db_path, daily_limit)
    chip.setText(format_budget_text(snapshot))
    chip.setToolTip(format_budget_tooltip(snapshot))
    used = _sum_exact((snapshot.spent, snapshot.reserved))
    warning = (
        snapshot.available
        and snapshot.limit > 0
        and used >= snapshot.limit * Decimal("0.8")
    )
    wanted_name = "chipWarning" if warning else "chip"
    if chip.objectName() != wanted_name:
        chip.setObjectName(wanted_name)
        repolish(chip)
