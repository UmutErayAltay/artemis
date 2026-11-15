"""Gün sonu özeti (ADR 0066): sayımlar, boş gün, gecikme, gizlilik, bozuk denetim, okuma aracı.

Gerçek makineye dokunulmaz: denetim dosyası tmp_path'te sentetiktir, saat enjekte edilir,
toast kapalıdır, Telegram sahtedir.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from artemis.brain.fake import ScriptedProvider
from artemis.channels.runtime import Runtime, create_runtime
from artemis.config.settings import DailySummaryConfig, ProactiveConfig, Settings, TelegramConfig
from artemis.proactive import daily_summary
from artemis.proactive.daily_summary import build_summary, parse_day, write_summary
from artemis.proactive.delivery import Deliverer
from artemis.proactive.scheduler import ProactiveScheduler
from artemis.tools.plugins.daily_summary import DailySummaryRead, ReadArguments

# Europe/Istanbul UTC+3: 22:30 yerel = 19:30 UTC.
DAY = date(2026, 10, 15)
ON_TIME = datetime(2026, 10, 15, 19, 31, tzinfo=UTC)
START = datetime(2026, 10, 14, 21, 0, tzinfo=UTC)
END = START + timedelta(days=1)
CHAT_SECRET = "GIZLI-SOHBET-METNI"


def line(event: str, hour: int = 10, **fields: Any) -> str:
    stamp = datetime(2026, 10, 15, hour, 0, tzinfo=UTC).isoformat()
    return json.dumps({"event": event, "timestamp": stamp, **fields}, ensure_ascii=False)


def tool_pair(
    turn: str, tool: str, args: dict[str, Any], *, ok: bool = True, status: str = ""
) -> list[str]:
    result = {"success": ok, "status": status or ("ok" if ok else "error"), "message": CHAT_SECRET}
    return [
        line("tool_requested", turn_id=turn, tool=tool, arguments=args),
        line("tool_result", turn_id=turn, tool=tool, result=result),
    ]


def busy_day() -> list[str]:
    path = "notlar/‮evil\x07.txt"
    return [
        line("turn_start", turn_id="t1", input=CHAT_SECRET),
        *tool_pair("t1", "create_file", {"path": path, "content": CHAT_SECRET}),
        *tool_pair("t1", "read_file", {"path": "yok.txt"}, ok=False),
        *tool_pair("t1", "delete_file", {"path": "eski.txt"}),
        *tool_pair("t1", "move_file", {"source": "a.txt", "destination": "b/a.txt"}),
        line("turn_end", turn_id="t1", stop_reason="completed", usage={"cost_usd": "0.0125"}),
        line("turn_start", turn_id="t2", unattended=True),
        line("turn_end", turn_id="t2", stop_reason="timeout", usage={"cost_usd": "0.0100"}),
        line("turn_start", turn_id="t3", channel="typed_tool"),
        *tool_pair("t3", "vault_append", {"path": "x.md"}, ok=False, status="denied"),
        line("approval", turn_id="t3", tool="vault_append", approved=False),
        line("code_agent_done", job_id="j", project="proje-a", status="completed"),
        line("research_end", job_id="r", status="completed", searches=3),
        line("routine_run_end", turn_id="t4", name="işe başla", completed=True, steps=2),
        line("dictation", chars=12),
        line("dictation", chars=5),
        line("telegram_download", kind="voice", size=3, outcome="ok"),
        # Gürültü: bozuk, yarım, tuhaf şekilli satırlar ve başka güne ait olay.
        "{bozuk json",
        '{"event": "turn_start", "timestamp": "2026-10-15T1',
        json.dumps(["liste"]),
        json.dumps("metin"),
        json.dumps({"event": "turn_start"}),
        json.dumps({"event": "turn_start", "timestamp": "2026-10-15T10:00:00"}),  # saat dilimsiz
        json.dumps({"event": "tool_result", "timestamp": "2026-10-15T11:00:00+00:00",
                    "tool": 5, "result": "x"}),
        line("turn_start").replace("2026-10-15", "2026-10-14"),
    ]


def make_runtime(tmp_path: Path, rows: list[str] | None = None, **summary: Any) -> Runtime:
    workspace = tmp_path / "desktop"
    workspace.mkdir(parents=True, exist_ok=True)
    audit = tmp_path / "audit.jsonl"
    if rows is not None:
        audit.write_text("\n".join(rows) + "\n", encoding="utf-8")
    settings = Settings(
        workspace=workspace,
        state_dir=tmp_path / "state",
        audit_path=audit,
        persist_memory=False,
        proactive=ProactiveConfig(enabled=True),
        daily_summary=DailySummaryConfig(enabled=True, **summary),
        telegram=TelegramConfig(chat_id=42),
    )
    return create_runtime(
        tmp_path / "unused.yaml", lambda n, a: False,
        provider=ScriptedProvider([]), settings=settings,
    )


class Panel:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, str, str]] = []

    def __call__(self, title: str, body: str, routine: str, suggest: str, day: str) -> bool:
        self.items.append((title, body, routine, suggest, day))
        return True


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send_message(self, *args: Any, **kwargs: Any) -> None:
        self.sent.append(args)


def scheduler(
    runtime: Runtime, now: datetime, panel: Panel, telegram: Any = None
) -> ProactiveScheduler:
    deliverer = Deliverer(
        runtime.settings, runtime.audit,
        panel=lambda t, b: panel(t, b, "", "", ""), routine_panel=panel, toast=None,
        telegram=telegram,
    )
    return ProactiveScheduler(
        runtime, runtime.context.services["proactive_store"], deliverer,
        clock=lambda: now, owner="a",
    )


def summary_file(runtime: Runtime, day: date = DAY) -> Path:
    return runtime.settings.state_dir / "gunluk" / f"{day.isoformat()}.md"


def built(tmp_path: Path, rows: list[str], **kwargs: Any) -> str:
    audit = tmp_path / "a.jsonl"
    audit.write_text("\n".join(rows) + "\n", encoding="utf-8")
    text = build_summary(audit, DAY, START, END, **kwargs)
    assert text is not None
    return text


def test_counts_failures_files_jobs_and_spend(tmp_path: Path) -> None:
    text = built(tmp_path, busy_day(), reminders={"delivered": 2, "missed": 1})
    assert "sohbet 1" in text and "gözetimsiz 1" in text and "panel 1" in text
    assert "tamamlanmayan 1" in text
    assert "- read_file: 1 (1 başarısız)" in text
    assert "- create_file: 1" in text
    assert "evil.txt (oluşturuldu)" in text and "eski.txt (silindi)" in text
    assert "a.txt → b/a.txt (taşındı)" in text
    assert "Kod işi proje-a: completed" in text and "Araştırma: completed" in text
    assert "işe başla: tamamlandı" in text
    assert "iletildi 2" in text and "kaçırıldı 1" in text
    assert "vault_append 1" in text  # reddedilen onay
    assert "- vault_append" not in text  # reddedilen çağrı araç/başarısız sayılmaz
    assert "dikte 2" in text and "Telegram'dan indirme 1" in text
    assert "$0.0225" in text


def test_no_conversation_text_and_control_chars_stripped(tmp_path: Path) -> None:
    text = built(tmp_path, busy_day())
    assert CHAT_SECRET not in text
    assert "‮" not in text and "\x07" not in text


def test_day_boundaries_exclude_other_days(tmp_path: Path) -> None:
    rows = [line("turn_start", hour=10), line("turn_start", hour=11)]
    rows.append(line("turn_start").replace("2026-10-15T10", "2026-10-14T20"))  # START öncesi
    rows.append(line("turn_start", hour=21))  # END (21:00Z) dahil değil
    assert "sohbet 2" in built(tmp_path, rows)


def test_hostile_audit_never_raises_and_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = tmp_path / "a.jsonl"
    old = [line("turn_start", hour=9) for _ in range(5)]
    huge = b"x" * (daily_summary.MAX_LINE_BYTES * 2)  # satır sonu olmayan dev satır
    audit.write_bytes(
        ("\n".join(old) + "\n").encode() + b"\xff\xfe\x00garbage\n" + huge + b"\n"
        + (line("turn_start", hour=12) + "\n").encode()
    )
    assert "sohbet 6" in (build_summary(audit, DAY, START, END) or "")
    # Bayt sınırı: yalnız dosyanın sonu taranır, eski satırlar sayılmaz.
    monkeypatch.setattr(daily_summary, "MAX_SCAN_BYTES", len(line("turn_start", hour=12)) + 5)
    assert "sohbet 1" in (build_summary(audit, DAY, START, END) or "")
    # Dizin ya da yok dosya: boş gün, hata yok.
    assert build_summary(tmp_path, DAY, START, END) is None
    assert build_summary(tmp_path / "yok.jsonl", DAY, START, END) is None


def test_empty_day_returns_none_and_overwrite_is_atomic(tmp_path: Path) -> None:
    audit = tmp_path / "a.jsonl"
    audit.write_text(
        line("proactive_started") + "\n" + line("proactive_delivery", kind="reminder") + "\n",
        encoding="utf-8",
    )
    assert build_summary(audit, DAY, START, END) is None
    target = tmp_path / "gunluk" / "2026-10-15.md"
    write_summary(target, "bir\n")
    write_summary(target, "iki\n")
    assert target.read_text(encoding="utf-8") == "iki\n"
    assert [p.name for p in target.parent.iterdir()] == ["2026-10-15.md"]


def test_parse_day_is_strict() -> None:
    assert parse_day("2026-10-15") == DAY
    for bad in ("2026-1-5", "../2026-10-15", "2026-02-30", "2026-10-15\n", "", "²026-10-15"):
        assert parse_day(bad) is None


async def test_tick_writes_file_notifies_privately_and_skips_telegram(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, busy_day())
    panel, telegram = Panel(), FakeTelegram()
    sched = scheduler(runtime, ON_TIME, panel, telegram)
    assert await sched.tick() == 1
    assert await sched.tick() == 0  # aynı oluşum bir kez
    text = summary_file(runtime).read_text(encoding="utf-8")
    assert text.startswith("# Gün özeti · 2026-10-15") and "gecikmeli" not in text
    assert [(i[0], i[4]) for i in panel.items] == [("Günün özeti hazır", "2026-10-15")]
    assert telegram.sent == []
    records = [
        json.loads(x) for x in runtime.audit.path.read_text(encoding="utf-8").splitlines()
        if '"proactive_delivery"' in x and '"daily_summary"' in x
    ]
    assert records[0]["private"] is True and "telegram" not in records[0]["targets"]


async def test_empty_day_writes_nothing_and_sends_no_notice(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, [line("proactive_started")])
    panel = Panel()
    sched = scheduler(runtime, ON_TIME, panel)
    assert await sched.tick() == 0
    assert not summary_file(runtime).exists() and panel.items == []
    assert await sched.tick() == 0  # kapatıldı, yeniden denenmez


async def test_missed_day_is_produced_late_and_labelled_then_expires(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, busy_day())
    panel = Panel()
    late = datetime(2026, 10, 15, 21, 10, tzinfo=UTC)  # 00:10 yerel: ertesi gün, süre payı içinde
    assert await scheduler(runtime, late, panel).tick() == 1
    text = summary_file(runtime).read_text(encoding="utf-8")
    assert "gecikmeli" in text and panel.items[0][4] == "2026-10-15"
    other = make_runtime(tmp_path / "b", busy_day())
    assert await scheduler(other, ON_TIME + timedelta(hours=5), Panel()).tick() == 0
    assert not summary_file(other).exists()


async def test_disabled_flag_does_nothing(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, busy_day())
    object.__setattr__(runtime.settings, "daily_summary", DailySummaryConfig(enabled=False))
    panel = Panel()
    assert await scheduler(runtime, ON_TIME, panel).tick() == 0
    assert panel.items == [] and not summary_file(runtime).exists()


async def test_build_failure_does_not_raise_out_of_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from artemis.proactive import scheduler as module

    def boom(*args: Any, **kwargs: Any) -> str:
        raise OSError("disk")

    monkeypatch.setattr(module, "build_summary", boom)
    runtime = make_runtime(tmp_path, busy_day())
    assert await scheduler(runtime, ON_TIME, Panel()).tick() == 0
    assert "daily_summary_error" in runtime.audit.path.read_text(encoding="utf-8")


async def test_read_tool_reads_past_day_validates_and_respects_flag(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    write_summary(summary_file(runtime, date(2026, 10, 14)), "# dün\n")
    tool = DailySummaryRead()
    ok = await tool.execute(ReadArguments(day="2026-10-14"), runtime.context)
    assert ok.success and ok.private and ok.content_kind == "file"
    assert ok.data["summary"] == "# dün\n"
    assert not (await tool.execute(ReadArguments(day="2026-10-13"), runtime.context)).success
    for bad in ("../audit", "2026-10-1", "dün"):
        result = await tool.execute(ReadArguments(day=bad), runtime.context)
        assert "YYYY-MM-DD" in result.message
    assert tool.refuse(ReadArguments(), runtime.context) is None
    off = make_runtime(tmp_path / "off")
    object.__setattr__(off.settings, "daily_summary", DailySummaryConfig(enabled=False))
    assert tool.refuse(ReadArguments(), off.context)


def test_registry_hides_tool_when_disabled(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    assert runtime.registry.get("daily_summary_read") is not None
    settings = runtime.settings.model_copy(update={"daily_summary": DailySummaryConfig()})
    off = create_runtime(
        tmp_path / "unused.yaml", lambda n, a: False,
        provider=ScriptedProvider([]), settings=settings,
    )
    assert off.registry.get("daily_summary_read") is None


def test_extreme_timestamp_line_is_skipped(tmp_path: Path) -> None:
    """Yıl 1 / 9999 + ofset `astimezone`'da OverflowError verir; satır atlanır, gün okunur."""
    audit = tmp_path / "audit.jsonl"
    hostile = [
        json.dumps({"event": "turn_start", "timestamp": "0001-01-01T00:00:00+23:59"}),
        json.dumps({"event": "turn_start", "timestamp": "9999-12-31T23:59:59-23:59"}),
    ]
    audit.write_text("\n".join(hostile + busy_day()) + "\n", encoding="utf-8")
    assert build_summary(audit, DAY, START, END)


def test_truncated_scan_is_stated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text("\n".join(busy_day()) + "\n", encoding="utf-8")
    assert "eksik olabilir" not in (build_summary(audit, DAY, START, END) or "")
    monkeypatch.setattr(daily_summary, "MAX_SCAN_LINES", 3)
    assert "eksik olabilir" in (build_summary(audit, DAY, START, END) or "")
    monkeypatch.setattr(daily_summary, "MAX_SCAN_LINES", 400_000)
    monkeypatch.setattr(daily_summary, "MAX_SCAN_BYTES", audit.stat().st_size // 2)
    assert "eksik olabilir" in (build_summary(audit, DAY, START, END) or "")
