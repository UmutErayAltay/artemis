"""Tetikleyiciler (ADR 0061): klasör yoklama, taban çizgisi, kararlı boyut, sınır, düğme.

Gerçek makineye dokunulmaz: klasörler tmp_path altındadır, saat enjekte edilir, toast kapalıdır.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from artemis.brain.fake import ScriptedProvider
from artemis.channels.runtime import Runtime, create_runtime
from artemis.config.settings import (
    ProactiveConfig,
    RoutinesConfig,
    Settings,
    TriggerConfig,
    TriggersConfig,
)
from artemis.proactive.delivery import Deliverer
from artemis.proactive.scheduler import ProactiveScheduler
from artemis.proactive.triggers import build_notice, sanitize_name, scan_folder

NOW = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class Panel:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, str]] = []

    def __call__(
        self, title: str, body: str, routine: str, suggest: str, day: str = ""
    ) -> bool:
        self.items.append((title, body, routine, suggest))
        return True


def trigger(folder: Path, name: str = "indirilenler", **fields: Any) -> TriggerConfig:
    return TriggerConfig(name=name, folder=str(folder), **fields)


def make_runtime(
    tmp_path: Path,
    *triggers: TriggerConfig,
    enabled: bool = True,
    routines: bool = False,
    **proactive: Any,
) -> Runtime:
    workspace = tmp_path / "desktop"
    workspace.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        workspace=workspace,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        proactive=ProactiveConfig(enabled=True, **proactive),
        triggers=TriggersConfig(enabled=enabled, items=triggers),
        routines=RoutinesConfig(enabled=routines),
    )
    return create_runtime(
        tmp_path / "unused.yaml",
        lambda name, arguments: False,
        provider=ScriptedProvider([]),
        settings=settings,
    )


def make_scheduler(
    runtime: Runtime, panel: Panel, clock: Clock, owner: str = "a", telegram: Any = None
) -> ProactiveScheduler:
    deliverer = Deliverer(
        runtime.settings,
        runtime.audit,
        panel=lambda title, body: panel(title, body, "", ""),
        routine_panel=panel,
        # Leke bayrağını taşıyan yol (masaüstü köprüsünün kullandığı); eski yol dış alanlı
        # öneriyi düşürür (#39).
        routine_panel_ex=lambda t, b, r, s, d, _tainted: panel(t, b, r, s, d),
        toast=None,
        telegram=telegram,
    )
    return ProactiveScheduler(
        runtime, runtime.context.services["proactive_store"], deliverer, clock=clock, owner=owner
    )


def write(folder: Path, name: str, size: int = 10) -> Path:
    path = folder / name
    path.write_bytes(b"x" * size)
    return path


def audit_of(runtime: Runtime) -> list[dict[str, Any]]:
    return [json.loads(x) for x in runtime.audit.path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    folder = tmp_path / "Downloads"
    folder.mkdir()
    return folder


async def run_ticks(scheduler: ProactiveScheduler, clock: Clock, count: int) -> int:
    total = 0
    for _ in range(count):
        clock.advance(seconds=30)
        total += await scheduler.tick()
    return total


async def test_baseline_does_not_fire_and_new_stable_file_fires_once(
    tmp_path: Path, downloads: Path
) -> None:
    write(downloads, "eski.pdf")
    clock, panel = Clock(), Panel()
    runtime = make_runtime(tmp_path, trigger(downloads, suggest="Özetle: {path}"))
    scheduler = make_scheduler(runtime, panel, clock)
    assert await run_ticks(scheduler, clock, 3) == 0  # taban çizgisi, eski dosya sessiz
    write(downloads, "rapor.pdf")
    assert await run_ticks(scheduler, clock, 1) == 0  # ilk görülüş: henüz kararlı değil
    assert await run_ticks(scheduler, clock, 1) == 1
    assert await run_ticks(scheduler, clock, 3) == 0
    assert len(panel.items) == 1
    title, body, routine, suggest = panel.items[0]
    assert title == "Tetikleyici · indirilenler"
    assert body == "Yeni dosya geldi: rapor.pdf"
    assert routine == "" and suggest == f"Özetle: {downloads / 'rapor.pdf'}"


async def test_growing_file_waits_until_size_is_stable(tmp_path: Path, downloads: Path) -> None:
    clock, panel = Clock(), Panel()
    scheduler = make_scheduler(make_runtime(tmp_path, trigger(downloads)), panel, clock)
    await run_ticks(scheduler, clock, 1)
    big = write(downloads, "buyuk.iso", 10)
    for size in (20, 30, 40):
        assert await run_ticks(scheduler, clock, 1) == 0
        big.write_bytes(b"x" * size)
    assert await run_ticks(scheduler, clock, 1) == 0  # son büyümeden sonraki ilk yoklama
    assert await run_ticks(scheduler, clock, 1) == 1
    assert len(panel.items) == 1


async def test_temp_extensions_and_other_patterns_are_ignored(
    tmp_path: Path, downloads: Path
) -> None:
    clock, panel = Clock(), Panel()
    runtime = make_runtime(tmp_path, trigger(downloads, pattern="*.pdf"))
    scheduler = make_scheduler(runtime, panel, clock)
    await run_ticks(scheduler, clock, 1)
    for name in ("a.pdf.crdownload", "b.pdf.part", "c.pdf.TMP", "d.txt"):
        write(downloads, name)
    (downloads / "klasor.pdf").mkdir()
    assert await run_ticks(scheduler, clock, 4) == 0
    assert panel.items == []


async def test_restart_does_not_refire(tmp_path: Path, downloads: Path) -> None:
    clock, panel = Clock(), Panel()
    runtime = make_runtime(tmp_path, trigger(downloads))
    first = make_scheduler(runtime, panel, clock)
    await run_ticks(first, clock, 1)
    write(downloads, "rapor.pdf")
    assert await run_ticks(first, clock, 2) == 1
    again = make_scheduler(runtime, panel, clock, owner="b")  # yeniden başlatma
    assert await run_ticks(again, clock, 3) == 0
    assert len(panel.items) == 1


async def test_two_processes_on_one_store_notify_once(tmp_path: Path, downloads: Path) -> None:
    clock, panel = Clock(), Panel()
    runtime = make_runtime(tmp_path, trigger(downloads))
    one = make_scheduler(runtime, panel, clock, owner="a")
    two = make_scheduler(runtime, panel, clock, owner="b")
    await one.tick()
    write(downloads, "rapor.pdf")
    fired = 0
    for _ in range(3):
        clock.advance(seconds=30)
        fired += await one.tick() + await two.tick()
    assert fired == 1
    assert len(panel.items) == 1


async def test_several_files_in_one_tick_merge_into_one_notice(
    tmp_path: Path, downloads: Path
) -> None:
    clock, panel = Clock(), Panel()
    runtime = make_runtime(tmp_path, trigger(downloads, suggest="Bak: {path}"))
    scheduler = make_scheduler(runtime, panel, clock)
    await run_ticks(scheduler, clock, 1)
    for name in ("a.pdf", "b.pdf", "c.pdf", "d.pdf"):
        write(downloads, name)
    assert await run_ticks(scheduler, clock, 2) == 1
    assert len(panel.items) == 1
    assert panel.items[0][1] == "4 yeni dosya: a.pdf, b.pdf, c.pdf ve 1 tane daha"
    assert panel.items[0][3] == f"Bak: {downloads / 'a.pdf'}"


async def test_daily_cap_defers_until_next_day(tmp_path: Path, downloads: Path) -> None:
    other = downloads.parent / "Diger"
    other.mkdir()
    clock, panel = Clock(), Panel()
    runtime = make_runtime(
        tmp_path, trigger(downloads, "bir"), trigger(other, "iki"), max_events_per_day=1
    )
    scheduler = make_scheduler(runtime, panel, clock)
    await run_ticks(scheduler, clock, 1)
    write(downloads, "a.pdf")
    write(other, "b.pdf")
    await run_ticks(scheduler, clock, 3)
    assert [item[0] for item in panel.items] == ["Tetikleyici · bir"]  # ikincisi sınırda bekler
    clock.advance(days=1)
    assert await scheduler.tick() == 1
    assert [item[0] for item in panel.items] == ["Tetikleyici · bir", "Tetikleyici · iki"]


def test_file_names_are_sanitised_and_capped() -> None:
    hostile = "rapor\x1b[31m\x00\u202egnp.exe\n" + "u" * 300
    clean = sanitize_name(hostile)
    assert len(clean) <= 120
    assert not any(ord(ch) < 32 or ch == "\u202e" for ch in clean)
    title, body, prompt = build_notice(
        "t", "C:/x", "Geldi: {file} ({name})", "Oku {path} {file}", [hostile]
    )
    assert "\x1b" not in body + prompt and "\n" not in body + prompt
    assert title == "Tetikleyici · t" and body.startswith("Geldi: rapor[31m")
    # Şablon format() değildir: kullanıcının süslü parantezleri bozulmaz.
    assert build_notice("t", "C:/x", "{ } {x} {file}", None, ["a"])[1] == "{ } {x} a"


async def test_nothing_runs_without_a_click_and_telegram_is_never_used(
    tmp_path: Path, downloads: Path
) -> None:
    sent: list[str] = []

    class Spy:
        def send_message(self, *args: Any, **kwargs: Any) -> None:
            sent.append("send")

    clock, panel = Clock(), Panel()
    runtime = make_runtime(tmp_path, trigger(downloads, routine="işe başla"), routines=True)
    scheduler = make_scheduler(runtime, panel, clock, telegram=Spy())
    await run_ticks(scheduler, clock, 1)
    write(downloads, "rapor.pdf")
    assert await run_ticks(scheduler, clock, 2) == 1
    assert panel.items[0][2] == "işe başla" and panel.items[0][3] == ""
    audit = audit_of(runtime)
    assert not [r for r in audit if r["event"] in {"tool_dispatch", "routine_run_start"}]
    assert sent == []
    delivery = next(r for r in audit if r["event"] == "proactive_delivery")
    assert "telegram" not in delivery["targets"]


async def test_routine_button_is_dropped_when_routines_are_disabled(
    tmp_path: Path, downloads: Path
) -> None:
    clock, panel = Clock(), Panel()
    runtime = make_runtime(tmp_path, trigger(downloads, routine="x"), routines=False)
    scheduler = make_scheduler(runtime, panel, clock)
    await run_ticks(scheduler, clock, 1)
    write(downloads, "a.pdf")
    await run_ticks(scheduler, clock, 3)
    assert [item[2:] for item in panel.items] == [("", "")]  # bildirim var, düğmesi yok


async def test_disabled_flag_scans_nothing(tmp_path: Path, downloads: Path) -> None:
    clock, panel = Clock(), Panel()
    runtime = make_runtime(tmp_path, trigger(downloads), enabled=False)
    scheduler = make_scheduler(runtime, panel, clock)
    await run_ticks(scheduler, clock, 1)
    write(downloads, "a.pdf")
    assert await run_ticks(scheduler, clock, 3) == 0 and panel.items == []
    assert runtime.context.services["proactive_store"].trigger_files("indirilenler") is None


async def test_missing_folder_is_reported_once_and_does_not_stop_others(
    tmp_path: Path, downloads: Path
) -> None:
    clock, panel = Clock(), Panel()
    runtime = make_runtime(
        tmp_path, trigger(tmp_path / "yok", "kayip"), trigger(downloads, "iyi", suggest="a")
    )
    scheduler = make_scheduler(runtime, panel, clock)
    await run_ticks(scheduler, clock, 1)
    write(downloads, "a.pdf")
    await run_ticks(scheduler, clock, 4)
    errors = [r for r in audit_of(runtime) if r["event"] == "trigger_unavailable"]
    assert len(errors) == 1 and errors[0]["trigger"] == "kayip"
    assert len(panel.items) == 1  # sağlam tetikleyici çalıştı


def test_scan_skips_symlinks_and_raises_oserror_for_missing_folder(
    tmp_path: Path, downloads: Path
) -> None:
    real = write(downloads, "gercek.txt")
    try:
        os.symlink(real, downloads / "baglanti.txt")
    except (OSError, NotImplementedError):
        pytest.skip("sembolik bağlantı kurulamadı")
    found, truncated = scan_folder(str(downloads), "*")
    assert found == {"gercek.txt": 10} and not truncated
    with pytest.raises(OSError):
        scan_folder(str(downloads / "yok"), "*")


def test_config_validation(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        TriggerConfig(name="a", folder="goreli/yol")
    with pytest.raises(ValidationError):
        trigger(tmp_path, suggest="x", routine="y")
    with pytest.raises(ValidationError):
        TriggersConfig(items=tuple(trigger(tmp_path, f"t{i}") for i in range(11)))
    with pytest.raises(ValidationError):
        TriggersConfig(items=(trigger(tmp_path, "a"), trigger(tmp_path, "A")))
    assert TriggersConfig().enabled is False
