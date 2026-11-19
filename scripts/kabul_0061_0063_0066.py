"""ADR 0061 tetikleyiciler, ADR 0063 araştırma işi, ADR 0066 gün özeti kabul doğrulaması.

Gerçek dosya sistemi ve SQLite; saat enjekte edilir, toast/Telegram kapalıdır, model yerine
ScriptedProvider kullanılır. Ağ ve gerçek Windows bildirimi yoktur. Elle kabul maddeleri
(`docs/YENI_OZELLIKLER_KABUL.md`) burada yalnız headless karşılığıyla kontrol edilir; gerçek
süreç öldürme, panel tıklaması ve toast görünümü bu betiğin kapsamı dışındadır.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from artemis.brain.fake import ScriptedProvider  # noqa: E402
from artemis.channels.runtime import Runtime, create_runtime  # noqa: E402
from artemis.config.settings import (  # noqa: E402
    DailySummaryConfig,
    ProactiveConfig,
    ResearchConfig,
    RoutinesConfig,
    Settings,
    TriggerConfig,
    TriggersConfig,
)
from artemis.proactive.daily_summary import summary_path  # noqa: E402
from artemis.proactive.delivery import Deliverer  # noqa: E402
from artemis.proactive.scheduler import ProactiveScheduler  # noqa: E402
from artemis.proactive.triggers import TEMP_SUFFIXES  # noqa: E402
from artemis.safety.audit import AuditLog  # noqa: E402
from artemis.tools.plugins.daily_summary import DailySummaryRead, ReadArguments  # noqa: E402
from artemis.tools.plugins.information import WebSearch  # noqa: E402
from artemis.tools.plugins.research import ResearchStart  # noqa: E402
from artemis.tools.registry import ToolRegistry  # noqa: E402
from artemis.tools.research_jobs import ResearchJobs  # noqa: E402

# 2026-10-15 09:00 İstanbul = 06:00 UTC (tetikleyici/özet testleriyle aynı taban).
NOW = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)
# Europe/Istanbul UTC+3: 22:31 yerel, varsayılan özet saati 22:30'dan sonra.
SUMMARY_ON_TIME = datetime(2026, 10, 15, 19, 31, tzinfo=UTC)
DAY = date(2026, 10, 15)
CHAT_SECRET = "GIZLI-SOHBET-METNI-0066"


# Ortak yardımcılar -------------------------------------------------------------------


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class Panel:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, str]] = []

    def __call__(self, title: str, body: str, routine: str, suggest: str, day: str = "") -> bool:
        self.items.append((title, body, routine, suggest))
        return True


def _trigger(folder: Path, name: str = "indirilenler", **fields: Any) -> TriggerConfig:
    return TriggerConfig(name=name, folder=str(folder), **fields)


def _runtime(
    base: Path,
    *,
    triggers: tuple[TriggerConfig, ...] = (),
    triggers_enabled: bool = True,
    **proactive: Any,
) -> Runtime:
    workspace = base / "desktop"
    workspace.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        workspace=workspace,
        state_dir=base / "state",
        audit_path=base / "audit.jsonl",
        persist_memory=False,
        proactive=ProactiveConfig(enabled=True, **proactive),
        triggers=TriggersConfig(enabled=triggers_enabled, items=triggers),
        routines=RoutinesConfig(enabled=False),
    )
    return create_runtime(
        base / "unused.yaml",
        lambda name, arguments: False,
        provider=ScriptedProvider([]),
        settings=settings,
    )


def _scheduler(
    runtime: Runtime, panel: Panel, clock: Clock, owner: str = "a"
) -> ProactiveScheduler:
    deliverer = Deliverer(
        runtime.settings,
        runtime.audit,
        panel=lambda title, body: panel(title, body, "", ""),
        routine_panel=panel,
        toast=None,
        telegram=None,
    )
    return ProactiveScheduler(
        runtime,
        runtime.context.services["proactive_store"],
        deliverer,
        clock=clock,
        owner=owner,
    )


def _write(folder: Path, name: str, size: int = 10) -> Path:
    path = folder / name
    path.write_bytes(b"x" * size)
    return path


def _audit_events(path: Path, event: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    return [row for row in rows if row.get("event") == event]


async def _ticks(scheduler: ProactiveScheduler, clock: Clock, count: int) -> int:
    total = 0
    for _ in range(count):
        clock.advance(seconds=30)
        total += await scheduler.tick()
    return total


def _remove_tree(root: Path) -> None:
    if not root.exists():
        return
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            shutil.rmtree(root)
            return
        except OSError as error:
            last_error = error
            time.sleep(0.1 * (attempt + 1))
    print(f"Temizleme uyarisi ({root}): {last_error}", file=sys.stderr)


# ADR 0061 tetikleyiciler -----------------------------------------------------------------


def check_0061_108_no_startup_notice(base: Path) -> tuple[bool, str]:
    """Açılışta klasörde var olan PDF için bildirim yok (ilk tarama taban çizgisidir)."""
    downloads = base / "Downloads"
    downloads.mkdir()
    _write(downloads, "onceden.pdf")
    runtime = _runtime(base, triggers=(_trigger(downloads, pattern="*.pdf"),))
    try:
        clock, panel = Clock(), Panel()
        fired = asyncio.run(_ticks(_scheduler(runtime, panel, clock), clock, 4))
        known = runtime.context.services["proactive_store"].trigger_files("indirilenler")
        ok = fired == 0 and panel.items == [] and known is not None and "onceden.pdf" in known
        return ok, f"fired={fired}, notices={len(panel.items)}, baseline_known={known is not None}"
    finally:
        runtime.shutdown()


def check_0061_111_crdownload_ignored(base: Path) -> tuple[bool, str]:
    """Yarım indirme (TEMP_SUFFIXES) varsayılan '*' deseninde bildirilmez; bitince tek bildirim."""
    downloads = base / "Downloads"
    downloads.mkdir()
    other_suffix = next(s for s in TEMP_SUFFIXES if s != ".crdownload")
    runtime = _runtime(base, triggers=(_trigger(downloads),))
    try:
        clock, panel = Clock(), Panel()
        scheduler = _scheduler(runtime, panel, clock)
        asyncio.run(_ticks(scheduler, clock, 1))  # taban çizgisi
        partial = _write(downloads, "buyuk.pdf.crdownload", 4096)
        other_temp = _write(downloads, f"diger.bin{other_suffix}", 2048)
        during = asyncio.run(_ticks(scheduler, clock, 4))
        notices_during = len(panel.items)
        partial.rename(downloads / "buyuk.pdf")
        after = asyncio.run(_ticks(scheduler, clock, 3))
        notices_after = len(panel.items)
        body = panel.items[0][1] if panel.items else ""
        no_temp_in_body = all(s not in body for s in TEMP_SUFFIXES) and "diger.bin" not in body
        ok = (
            other_temp.exists()
            and during == 0
            and notices_during == 0
            and after == 1
            and notices_after == 1
            and "buyuk.pdf" in body
            and no_temp_in_body
        )
        detail = (
            f"during_download_notices={notices_during}, after_rename_fired={after}, "
            f"total_notices={notices_after}, other_suffix={other_suffix}"
        )
        return ok, detail
    finally:
        runtime.shutdown()


def check_0061_112_batch_one_notice(base: Path) -> tuple[bool, str]:
    """Aynı taramada gelen 3 dosya tek birleşik bildirim olur."""
    downloads = base / "Downloads"
    downloads.mkdir()
    runtime = _runtime(base, triggers=(_trigger(downloads, pattern="*.pdf"),))
    try:
        clock, panel = Clock(), Panel()
        scheduler = _scheduler(runtime, panel, clock)
        asyncio.run(_ticks(scheduler, clock, 1))
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            _write(downloads, name)
        fired = asyncio.run(_ticks(scheduler, clock, 2))
        expected = "3 yeni dosya: a.pdf, b.pdf, c.pdf"
        ok = fired == 1 and len(panel.items) == 1 and panel.items[0][1] == expected
        body = panel.items[0][1] if panel.items else ""
        return ok, f"fired={fired}, notices={len(panel.items)}, body={body!r}"
    finally:
        runtime.shutdown()


def check_0061_113_restart_dedupe(base: Path) -> tuple[bool, str]:
    """Artemis yeniden başlatılınca aynı dosya için ikinci bildirim gelmez (aynı state_dir)."""
    downloads = base / "Downloads"
    downloads.mkdir()
    trigger = _trigger(downloads, pattern="*.pdf")
    first_rt = _runtime(base, triggers=(trigger,))
    first_panel, clock = Panel(), Clock()
    try:
        asyncio.run(_ticks(_scheduler(first_rt, first_panel, clock), clock, 1))
        _write(downloads, "rapor.pdf")
        fired_first = asyncio.run(_ticks(_scheduler(first_rt, first_panel, clock), clock, 2))
    finally:
        first_rt.shutdown()
    # Yeniden başlatma: yeni çalışma zamanı, aynı state_dir ve proactive.sqlite3.
    second_rt = _runtime(base, triggers=(trigger,))
    second_panel = Panel()
    try:
        fired_second = asyncio.run(
            _ticks(_scheduler(second_rt, second_panel, clock, owner="b"), clock, 3)
        )
    finally:
        second_rt.shutdown()
    ok = fired_first == 1 and fired_second == 0 and second_panel.items == []
    return ok, (
        f"before_restart={fired_first}, after_restart={fired_second}, "
        f"post_restart_notices={len(second_panel.items)}"
    )


def check_0061_116_missing_folder_once(base: Path) -> tuple[bool, str]:
    """Olmayan klasör denetimde bir kez raporlanır; diğer tetikleyici çalışmaya devam eder."""
    healthy = base / "Downloads"
    healthy.mkdir()
    missing = base / "yok"
    runtime = _runtime(
        base,
        triggers=(
            _trigger(missing, "kayip"),
            _trigger(healthy, "iyi", suggest="Bak: {path}"),
        ),
    )
    try:
        clock, panel = Clock(), Panel()
        scheduler = _scheduler(runtime, panel, clock)
        asyncio.run(_ticks(scheduler, clock, 1))
        _write(healthy, "a.pdf")
        asyncio.run(_ticks(scheduler, clock, 4))
        events = _audit_events(runtime.audit.path, "trigger_unavailable")
        unavailable = [row for row in events if row.get("trigger") == "kayip"]
        ok = (
            len(unavailable) == 1
            and len(panel.items) == 1
            and panel.items[0][0] == "Tetikleyici · iyi"
        )
        detail = (
            f"trigger_unavailable_count={len(unavailable)}, healthy_notices={len(panel.items)}"
        )
        return ok, detail
    finally:
        runtime.shutdown()


def check_0061_117_max_per_day(base: Path) -> tuple[bool, str]:
    """max_events_per_day=1: ikinci bildirim ertesi güne kalır."""
    first_dir = base / "bir"
    second_dir = base / "iki"
    first_dir.mkdir()
    second_dir.mkdir()
    runtime = _runtime(
        base,
        triggers=(_trigger(first_dir, "bir"), _trigger(second_dir, "iki")),
        max_events_per_day=1,
    )
    try:
        clock, panel = Clock(), Panel()
        scheduler = _scheduler(runtime, panel, clock)
        asyncio.run(_ticks(scheduler, clock, 1))
        _write(first_dir, "a.pdf")
        _write(second_dir, "b.pdf")
        asyncio.run(_ticks(scheduler, clock, 3))
        same_day = [item[0] for item in panel.items]
        clock.advance(days=1)
        next_day = asyncio.run(scheduler.tick())
        all_titles = [item[0] for item in panel.items]
        ok = same_day == ["Tetikleyici · bir"] and next_day == 1 and all_titles == [
            "Tetikleyici · bir",
            "Tetikleyici · iki",
        ]
        return ok, f"same_day={same_day}, next_day_fired={next_day}, all={all_titles}"
    finally:
        runtime.shutdown()


def check_0061_118_disabled_no_notice(base: Path) -> tuple[bool, str]:
    """Tetikleyiciler kapalıyken yeni dosyada bildirim yok ve klasör taranmaz."""
    downloads = base / "Downloads"
    downloads.mkdir()
    runtime = _runtime(
        base, triggers=(_trigger(downloads, pattern="*.pdf"),), triggers_enabled=False
    )
    try:
        clock, panel = Clock(), Panel()
        scheduler = _scheduler(runtime, panel, clock)
        asyncio.run(_ticks(scheduler, clock, 1))
        _write(downloads, "yeni.pdf")
        fired = asyncio.run(_ticks(scheduler, clock, 3))
        known = runtime.context.services["proactive_store"].trigger_files("indirilenler")
        ok = fired == 0 and panel.items == [] and known is None
        return ok, f"fired={fired}, notices={len(panel.items)}, scanned={known is not None}"
    finally:
        runtime.shutdown()


# ADR 0063 araştırma işi -------------------------------------------------------------------


def _research_jobs(base: Path) -> ResearchJobs:
    settings = Settings(
        workspace=base,
        state_dir=base / "state",
        audit_path=base / "audit.jsonl",
        persist_memory=False,
        research=ResearchConfig(enabled=True),
    )
    registry = ToolRegistry()
    registry.register(WebSearch)
    audit = AuditLog(settings.audit_path)
    return ResearchJobs(settings, audit, ScriptedProvider([]), registry, {})


def check_0063_151_restart_uncertain(base: Path) -> tuple[bool, str]:
    """Çalışan iş sırasında süreç ölürse yeniden açılışta `uncertain`, yeniden başlamaz.

    Gerçek süreç öldürme yerine: çalışan bir satır yazılır (ölen süreçten kalmış gibi) ve
    ResearchJobs yeniden kurulur. Gerçek kesinti elle kabul maddesidir.
    """
    first = _research_jobs(base)
    job_id = "kabul0063ab01"
    with closing(first._connect()) as db, db:
        db.execute(
            "INSERT INTO research_jobs (id, topic, status, max_searches, created) "
            "VALUES (?, 'kabul konusu', 'running', 3, 0)",
            (job_id,),
        )
    revived = _research_jobs(base)
    job = revived.get(job_id)
    time.sleep(0.3)
    still = revived.get(job_id)
    searches = _audit_events(revived.audit.path, "research_search")
    ok = (
        job is not None
        and job["status"] == "uncertain"
        and still is not None
        and still["status"] == "uncertain"
        and revived._thread is None
        and searches == []
    )
    status = job["status"] if job else None
    thread_started = revived._thread is not None
    return ok, f"status={status}, thread_started={thread_started}, searches={len(searches)}"


def check_0063_152_disabled_hides_tool(base: Path) -> tuple[bool, str]:
    """research.enabled kapalıyken araç modele görünmez, servis kurulmaz."""
    registry = ToolRegistry()
    registry.register(ResearchStart)
    registry.register(WebSearch)
    runtime = create_runtime(
        base / "x.yaml",
        lambda *_: True,
        provider=ScriptedProvider([]),
        registry=registry,
        settings=Settings(
            workspace=base,
            state_dir=base / "state",
            audit_path=base / "a.jsonl",
            persist_memory=False,
        ),
    )
    try:
        hidden = "research_start" not in runtime.registry
        web_kept = "web_search" in runtime.registry
        no_service = "research_jobs" not in runtime.context.services
    finally:
        runtime.shutdown()
    ok = hidden and web_kept and no_service
    return ok, (
        f"research_start_hidden={hidden}, web_search_kept={web_kept}, "
        f"no_service={no_service}"
    )


# ADR 0066 gün özeti -----------------------------------------------------------------------


def _line(event: str, hour: int = 10, **fields: Any) -> str:
    stamp = datetime(2026, 10, 15, hour, 0, tzinfo=UTC).isoformat()
    return json.dumps({"event": event, "timestamp": stamp, **fields}, ensure_ascii=False)


def _busy_rows() -> list[str]:
    """Konuşma metni içeren, gerçek bir etkinlik günü (gizlilik kontrolü için sır taşır)."""
    return [
        _line("turn_start", turn_id="t1", input=CHAT_SECRET),
        _line("tool_requested", turn_id="t1", tool="create_file",
              arguments={"path": "notlar/ozet.txt", "content": CHAT_SECRET}),
        _line("tool_result", turn_id="t1", tool="create_file",
              result={"success": True, "status": "ok", "message": CHAT_SECRET}),
        _line("turn_end", turn_id="t1", stop_reason="completed", usage={"cost_usd": "0.01"}),
        _line("code_agent_done", job_id="j", project="proje-a", status="completed"),
    ]


def _summary_runtime(base: Path, rows: list[str], *, enabled: bool) -> Runtime:
    workspace = base / "desktop"
    workspace.mkdir(parents=True, exist_ok=True)
    audit = base / "audit.jsonl"
    audit.write_text("\n".join(rows) + "\n", encoding="utf-8")
    settings = Settings(
        workspace=workspace,
        state_dir=base / "state",
        audit_path=audit,
        persist_memory=False,
        proactive=ProactiveConfig(enabled=True),
        daily_summary=DailySummaryConfig(enabled=enabled),
        triggers=TriggersConfig(enabled=False),
    )
    return create_runtime(
        base / "unused.yaml",
        lambda name, arguments: False,
        provider=ScriptedProvider([]),
        settings=settings,
    )


def check_0066_199_empty_day_silent(base: Path) -> tuple[bool, str]:
    """Etkinlik olmayan günde ne bildirim ne dosya."""
    runtime = _summary_runtime(base, [_line("proactive_started")], enabled=True)
    try:
        panel = Panel()
        clock = Clock(SUMMARY_ON_TIME)
        fired = asyncio.run(_scheduler(runtime, panel, clock).tick())
        file_exists = summary_path(runtime.settings.state_dir, DAY).exists()
        ok = fired == 0 and panel.items == [] and not file_exists
        return ok, f"fired={fired}, notices={len(panel.items)}, file_exists={file_exists}"
    finally:
        runtime.shutdown()


def check_0066_201_read_tool(base: Path) -> tuple[bool, str]:
    """`daily_summary_read` bugünkü özeti okur (gizlilik korunur); kayıt yoksa dürüstçe söyler."""
    runtime = _summary_runtime(base, _busy_rows(), enabled=True)
    try:
        panel = Panel()
        fired = asyncio.run(_scheduler(runtime, panel, Clock(SUMMARY_ON_TIME)).tick())
        tool = DailySummaryRead()
        read = asyncio.run(tool.execute(ReadArguments(day="2026-10-15"), runtime.context))
        summary = str(read.data.get("summary", "")) if read.data else ""
        missing = asyncio.run(tool.execute(ReadArguments(day="2026-10-13"), runtime.context))
        in_registry = "daily_summary_read" in runtime.registry
        ok = (
            fired == 1
            and in_registry
            and read.success
            and "# Gün özeti · 2026-10-15" in summary
            and CHAT_SECRET not in summary
            and not missing.success
            and "kayıtlı özet yok" in missing.message
        )
        detail = (
            f"fired={fired}, read_ok={read.success}, secret_leaked={CHAT_SECRET in summary}, "
            f"missing_msg={missing.message!r}"
        )
        return ok, detail
    finally:
        runtime.shutdown()


def check_0066_202_disabled(base: Path) -> tuple[bool, str]:
    """daily_summary.enabled kapalıyken araç modelden gizli, bildirim ve dosya yok."""
    runtime = _summary_runtime(base, _busy_rows(), enabled=False)
    try:
        panel = Panel()
        fired = asyncio.run(_scheduler(runtime, panel, Clock(SUMMARY_ON_TIME)).tick())
        hidden = "daily_summary_read" not in runtime.registry
        refusal = DailySummaryRead().refuse(ReadArguments(), runtime.context)
        file_exists = summary_path(runtime.settings.state_dir, DAY).exists()
        ok = hidden and fired == 0 and panel.items == [] and not file_exists and bool(refusal)
        return ok, (
            f"tool_hidden={hidden}, fired={fired}, notices={len(panel.items)}, "
            f"file_exists={file_exists}, refuse_message={bool(refusal)}"
        )
    finally:
        runtime.shutdown()


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    temp_root = Path(tempfile.mkdtemp(prefix="artemis_kabul_0061_0063_0066_"))
    any_fail = False
    checks: list[tuple[str, Callable[[Path], tuple[bool, str]]]] = [
        ("0061-108", check_0061_108_no_startup_notice),
        ("0061-111", check_0061_111_crdownload_ignored),
        ("0061-112", check_0061_112_batch_one_notice),
        ("0061-113", check_0061_113_restart_dedupe),
        ("0061-116", check_0061_116_missing_folder_once),
        ("0061-117", check_0061_117_max_per_day),
        ("0061-118", check_0061_118_disabled_no_notice),
        ("0063-151", check_0063_151_restart_uncertain),
        ("0063-152", check_0063_152_disabled_hides_tool),
        ("0066-199", check_0066_199_empty_day_silent),
        ("0066-201", check_0066_201_read_tool),
        ("0066-202", check_0066_202_disabled),
    ]
    try:
        for item_id, fn in checks:
            item_dir = temp_root / item_id
            item_dir.mkdir(parents=True, exist_ok=True)
            try:
                ok, detail = fn(item_dir)
            except Exception as error:  # noqa: BLE001 - madde siniri
                ok = False
                detail = f"{type(error).__name__}: {error}"
            clean_detail = " ".join(str(detail).splitlines()).strip()
            status = "PASS" if ok else "FAIL"
            if not ok:
                any_fail = True
            print(f"{status} {item_id} {clean_detail}")
    finally:
        _remove_tree(temp_root)
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
