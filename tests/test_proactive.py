"""Proaktif olaylar (ADR 0027): gözetimsiz tur kuralı, zamanlayıcı, teslim ve hatırlatmalar.

Gerçek saat, gerçek bildirim veya ağ kullanılmaz: saat enjekte edilir, sağlayıcı senaryoludur,
toast kapalıdır, Telegram yalnız loopback sahte sunucuya gider (CLAUDE.md 8. kural).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from telegram_fake import Harness, build_harness, sent_bodies

from artemis.brain.errors import BudgetExceeded
from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import Completion, Message, ToolCall
from artemis.channels.runtime import Runtime, create_runtime
from artemis.channels.telegram.api import build_telegram_client
from artemis.config.settings import BriefingConfig, ProactiveConfig, Settings
from artemis.proactive.delivery import PRIVATE_WITHHELD, Deliverer
from artemis.proactive.scheduler import BRIEFING_PREFIX, ProactiveScheduler
from artemis.proactive.store import ProactiveStore, reminder_event_id
from artemis.safety.audit import AuditLog
from artemis.safety.gate import UNATTENDED_NOTICE, SafetyGate, TurnState
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolDefinition, ToolResult
from artemis.tools.registry import ToolRegistry, discover_tools

# 2026-10-15 Perşembe 09:00 İstanbul = 06:00 UTC.
NOW = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def read_audit(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# 1. Kapı: gözetimsiz tur ----------------------------------------------------------------


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class Recorder(BaseTool):
    name = "write_probe"
    description = "Yan etkili sahte araç."
    args_model = Args
    risk: ClassVar[Risk] = Risk.SIDE_EFFECT
    ran: ClassVar[list[str]] = []

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        Recorder.ran.append(self.name)
        return ToolResult(success=True, status="ok", message="yazıldı")


class ConfirmProbe(Recorder):
    name = "confirm_probe"
    risk: ClassVar[Risk] = Risk.CONFIRM_REQUIRED


class UnknownRiskProbe(Recorder):
    name = "unknown_probe"
    risk: ClassVar[Risk] = "BELIRSIZ"  # type: ignore[assignment]


class ReadProbe(Recorder):
    name = "read_probe"
    risk: ClassVar[Risk] = Risk.READ_ONLY


@pytest.fixture
def probe_gate(tmp_path: Path) -> tuple[SafetyGate, ToolContext]:
    Recorder.ran = []
    registry = ToolRegistry()
    for tool in (Recorder, ConfirmProbe, UnknownRiskProbe, ReadProbe):
        registry.register(tool)
    context = ToolContext(tmp_path, Settings(workspace=tmp_path), AuditLog(tmp_path / "a.jsonl"))
    return SafetyGate(registry), context


def never(name: str, arguments: dict[str, Any]) -> bool:
    pytest.fail("Gözetimsiz turda onay sorulmamalı.")


@pytest.mark.parametrize("name", ["write_probe", "confirm_probe", "unknown_probe"])
async def test_unattended_turn_denies_every_non_read_only_call_without_asking(
    probe_gate: tuple[SafetyGate, ToolContext], tmp_path: Path, name: str
) -> None:
    gate, context = probe_gate
    state = TurnState(unattended=True)
    result = await gate.dispatch(ToolCall(id="c", name=name, arguments={"path": "x"}), context,
                                 state, never)
    assert result.status == "denied"
    assert UNATTENDED_NOTICE in result.message
    assert Recorder.ran == []
    approvals = [r for r in read_audit(tmp_path / "a.jsonl") if r["event"] == "approval"]
    assert approvals[-1]["approved"] is False
    assert approvals[-1]["unattended"] is True
    assert approvals[-1]["arguments"] == {"path": "x"}


async def test_unattended_turn_still_runs_read_only(
    probe_gate: tuple[SafetyGate, ToolContext],
) -> None:
    gate, context = probe_gate
    result = await gate.dispatch(
        ToolCall(id="c", name="read_probe", arguments={"path": "x"}),
        context,
        TurnState(unattended=True),
        never,
    )
    assert result.status == "ok"
    assert Recorder.ran == ["read_probe"]


async def test_attended_turn_is_unchanged(probe_gate: tuple[SafetyGate, ToolContext]) -> None:
    gate, context = probe_gate
    result = await gate.dispatch(
        ToolCall(id="c", name="write_probe", arguments={"path": "x"}),
        context,
        TurnState(),
        never,
    )
    assert result.status == "ok"


def test_no_tool_can_set_or_change_briefing_prompts() -> None:
    # Brifing istemi yalnız config'tedir (ADR 0027); kayıtta ona dokunan araç yoktur.
    names = [definition.name for definition in discover_tools().definitions()]
    assert not [name for name in names if "brief" in name or "schedule" in name]
    assert {"reminder_create", "reminder_list", "reminder_cancel"} <= set(names)


# 2. Ayarlar ---------------------------------------------------------------------------


def test_proactive_settings_validate_timezone_days_and_names() -> None:
    with pytest.raises(ValidationError):
        ProactiveConfig(timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        BriefingConfig(name="sabah", at=time(8), days=(7,), prompt="x")
    with pytest.raises(ValidationError):
        BriefingConfig(name="Sabah Brifingi", at=time(8), prompt="x")
    duplicate = BriefingConfig(name="sabah", at=time(8), prompt="x")
    with pytest.raises(ValidationError):
        ProactiveConfig(briefings=(duplicate, duplicate))
    assert ProactiveConfig().enabled is False
    assert ProactiveConfig().briefings == ()


# 3. Zamanlayıcı -------------------------------------------------------------------------


class Panel:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.items: list[tuple[str, str]] = []

    def __call__(self, title: str, body: str) -> bool:
        self.items.append((title, body))
        return self.accept


def make_runtime(
    tmp_path: Path,
    responses: list[Completion] | None = None,
    *,
    provider: Any = None,
    telegram: Any = None,
    **proactive: Any,
) -> Runtime:
    workspace = tmp_path / "desktop"
    workspace.mkdir(parents=True, exist_ok=True)
    fields: dict[str, Any] = {
        "workspace": workspace,
        "state_dir": tmp_path / "state",
        "audit_path": tmp_path / "audit.jsonl",
        "persist_memory": False,
        "proactive": ProactiveConfig(enabled=True, **proactive),
    }
    if telegram is not None:
        fields["telegram"] = telegram
    settings = Settings(**fields)
    chosen = provider if provider is not None else ScriptedProvider(list(responses or []))
    return create_runtime(tmp_path / "unused.yaml", never, provider=chosen, settings=settings)


def scheduler_for(
    runtime: Runtime, panel: Panel, clock: Clock, *, owner: str = "a", telegram: Any = None
) -> ProactiveScheduler:
    store = runtime.context.services["proactive_store"]
    deliverer = Deliverer(runtime.settings, runtime.audit, panel=panel, toast=None,
                          telegram=telegram)
    return ProactiveScheduler(runtime, store, deliverer, clock=clock, owner=owner)


def add(runtime: Runtime, text: str, due: datetime, **flags: bool) -> int:
    store: ProactiveStore = runtime.context.services["proactive_store"]
    created = store.add_reminder(
        text,
        due,
        tainted=flags.get("tainted", False),
        private=flags.get("private", False),
        now=due - timedelta(hours=1),
    )
    return created.id


async def test_due_reminder_is_delivered_once(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    clock, panel = Clock(), Panel()
    reminder_id = add(runtime, "Su iç", NOW - timedelta(seconds=10))
    add(runtime, "Henüz değil", NOW + timedelta(minutes=5))
    scheduler = scheduler_for(runtime, panel, clock)
    assert await scheduler.tick() == 1
    assert await scheduler.tick() == 0
    assert panel.items == [("Hatırlatma", "Su iç")]
    store: ProactiveStore = runtime.context.services["proactive_store"]
    assert store.occurrence_state(reminder_event_id(reminder_id), NOW - timedelta(seconds=10)) \
        == "delivered"
    deliveries = [r for r in read_audit(tmp_path / "audit.jsonl")
                  if r["event"] == "proactive_delivery"]
    assert len(deliveries) == 1 and deliveries[0]["targets"] == {"panel": "ok"}


async def test_two_schedulers_on_one_store_deliver_once(tmp_path: Path) -> None:
    # Masaüstü ve Telegram süreci aynı anda açık: oluşum talebi tek teslimi garanti eder.
    runtime = make_runtime(tmp_path)
    clock = Clock()
    add(runtime, "Toplantı", NOW - timedelta(seconds=1))
    first, second = Panel(), Panel()
    other_store = ProactiveStore(runtime.settings.state_dir / "proactive.sqlite3")
    other = ProactiveScheduler(
        runtime,
        other_store,
        Deliverer(runtime.settings, runtime.audit, panel=second, toast=None),
        clock=clock,
        owner="b",
    )
    # İkisi de oluşumu bekliyor görsün: ikinci süreç listeyi talepten ÖNCE okumuş olsun.
    stale = other_store.due_reminders(NOW)
    other_store.due_reminders = lambda now: stale  # type: ignore[method-assign]
    await scheduler_for(runtime, first, clock).tick()
    await other.tick()
    assert len(first.items) + len(second.items) == 1


async def test_late_tainted_and_missed_reminders_are_reported_honestly(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, reminder_grace_hours=12)
    panel = Panel()
    add(runtime, "Geç kalan", NOW - timedelta(hours=1), tainted=True)
    add(runtime, "Çok eski", NOW - timedelta(hours=13))
    await scheduler_for(runtime, panel, Clock()).tick()
    titles = [title for title, _ in panel.items]
    assert titles == ["Hatırlatma", "Kaçırılan 1 hatırlatma"]
    late_body = panel.items[0][1]
    assert "Gecikmeli: 2026-10-15 08:00" in late_body
    assert "dış bir içerik okunduktan sonra" in late_body
    assert "2026-10-14 20:00: Çok eski" in panel.items[1][1]


async def test_failed_delivery_is_not_marked_delivered(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    due = NOW - timedelta(seconds=5)
    reminder_id = add(runtime, "Kimse görmedi", due)
    await scheduler_for(runtime, Panel(accept=False), Clock()).tick()
    store: ProactiveStore = runtime.context.services["proactive_store"]
    assert store.occurrence_state(reminder_event_id(reminder_id), due) == "failed"


async def test_daily_cap_limits_deliveries(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, max_events_per_day=1)
    panel = Panel()
    add(runtime, "bir", NOW - timedelta(seconds=3))
    add(runtime, "iki", NOW - timedelta(seconds=2))
    scheduler = scheduler_for(runtime, panel, Clock())
    await scheduler.tick()
    await scheduler.tick()
    assert [body for _, body in panel.items] == ["bir"]
    capped = [r for r in read_audit(tmp_path / "audit.jsonl") if r["event"] == "proactive_capped"]
    assert len(capped) == 1


async def test_stale_claim_becomes_uncertain_and_is_not_rerun(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    due = NOW - timedelta(minutes=30)
    reminder_id = add(runtime, "Yarım kaldı", due)
    store: ProactiveStore = runtime.context.services["proactive_store"]
    event_id = reminder_event_id(reminder_id)
    assert store.claim(event_id, due, "reminder", "olu-surec", NOW - timedelta(minutes=20))
    panel = Panel()
    await scheduler_for(runtime, panel, Clock()).tick()
    assert store.occurrence_state(event_id, due) == "uncertain"
    assert panel.items == []


def briefing(**fields: Any) -> BriefingConfig:
    values: dict[str, Any] = {"name": "sabah", "at": time(9, 0), "prompt": "Bugünü özetle."}
    values.update(fields)
    return BriefingConfig(**values)


async def test_briefing_runs_unattended_with_fresh_context_and_cannot_side_effect(
    tmp_path: Path,
) -> None:
    responses = [
        Completion(
            tool_calls=[ToolCall(id="t1", name="create_file",
                                 arguments={"path": "not.txt", "content": "x"})]
        ),
        Completion(content="Günaydın, bugün takvim boş."),
    ]
    runtime = make_runtime(tmp_path, responses, briefings=(briefing(),))
    provider: ScriptedProvider = runtime.provider  # type: ignore[assignment]
    panel = Panel()
    clock = Clock(NOW + timedelta(minutes=1))
    assert await scheduler_for(runtime, panel, clock).tick() == 1
    assert panel.items == [("Brifing · sabah", "Günaydın, bugün takvim boş.")]
    assert not (runtime.settings.workspace / "not.txt").exists()
    first_request: list[Message] = provider.requests[0]
    assert first_request[-1].content.startswith(BRIEFING_PREFIX)
    assert "Bugünü özetle." in first_request[-1].content
    # Kullanıcının panel geçmişine eklenmez.
    assert runtime.runner.conversation.render().turns == 0
    starts = [r for r in read_audit(tmp_path / "audit.jsonl") if r["event"] == "turn_start"]
    assert starts[-1]["unattended"] is True
    # Aynı pencerede ikinci tarama brifingi tekrar çalıştırmaz.
    assert await scheduler_for(runtime, panel, clock, owner="b").tick() == 0


async def test_briefing_outside_window_or_day_does_not_run(tmp_path: Path) -> None:
    thursday = 3
    runtime = make_runtime(
        tmp_path,
        briefings=(briefing(days=(thursday + 1,)), briefing(name="erken", at=time(6, 0))),
        briefing_grace_hours=2,
    )
    panel = Panel()
    assert await scheduler_for(runtime, panel, Clock()).tick() == 0
    assert panel.items == []


class OverBudget:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self, messages: list[Message], tools: list[ToolDefinition], *, timeout: float
    ) -> Completion:
        self.calls += 1
        raise BudgetExceeded("Günlük maliyet tavanı bu çağrıya izin vermiyor.")


async def test_briefing_over_budget_is_reported_and_marked_failed(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, provider=OverBudget(), briefings=(briefing(),))
    panel = Panel()
    clock = Clock(NOW + timedelta(minutes=1))
    provider: OverBudget = runtime.provider  # type: ignore[assignment]
    await scheduler_for(runtime, panel, clock).tick()
    # Gerçek bütçe defteri çağrıyı ağdan önce reddeder (test_budget); burada döngünün
    # bu reddi tek denemede dürüstçe raporladığı ve tekrar denemediği sınanır.
    assert provider.calls == 1
    assert panel.items and "maliyet tavanı" in panel.items[0][1]
    store: ProactiveStore = runtime.context.services["proactive_store"]
    scheduled = NOW
    assert store.occurrence_state("briefing:sabah", scheduled) == "failed"


@pytest.fixture
def telegram_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    harness = build_harness(tmp_path / "tg", monkeypatch)
    yield harness
    harness.close()


async def test_private_briefing_is_withheld_from_telegram(
    tmp_path: Path, telegram_harness: Harness
) -> None:
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "gizli.txt").write_text("maaş bordrosu", encoding="utf-8")
    responses = [
        Completion(tool_calls=[ToolCall(id="r", name="read_file",
                                        arguments={"path": "gizli.txt"})]),
        Completion(content="Bordro: maaş bordrosu"),
    ]
    runtime = make_runtime(
        tmp_path, responses, briefings=(briefing(),), telegram=telegram_harness.settings.telegram
    )
    client = build_telegram_client(runtime.settings, runtime.audit)
    assert client is not None
    try:
        panel = Panel()
        clock = Clock(NOW + timedelta(minutes=1))
        await scheduler_for(runtime, panel, clock, telegram=client).tick()
    finally:
        client.close()
    texts = [body.get("text") for body in sent_bodies(telegram_harness.fake)]
    assert texts == [PRIVATE_WITHHELD]
    assert panel.items[0][1] == "Bordro: maaş bordrosu"


async def test_reminder_reaches_telegram_when_not_private(
    tmp_path: Path, telegram_harness: Harness
) -> None:
    runtime = make_runtime(tmp_path, telegram=telegram_harness.settings.telegram)
    add(runtime, "İlacını al", NOW - timedelta(seconds=1))
    add(runtime, "Dosyadan gelen", NOW - timedelta(seconds=2), private=True)
    client = build_telegram_client(runtime.settings, runtime.audit)
    assert client is not None
    try:
        await scheduler_for(runtime, Panel(), Clock(), telegram=client).tick()
    finally:
        client.close()
    texts = [body.get("text") for body in sent_bodies(telegram_harness.fake)]
    assert PRIVATE_WITHHELD in texts
    assert "Hatırlatma\n\nİlacını al" in texts
    assert not [text for text in texts if "Dosyadan gelen" in str(text)]


async def test_serve_stops_promptly(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, poll_seconds=300)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler_for(runtime, Panel(), Clock()).serve(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


# 4. Hatırlatma araçları -----------------------------------------------------------------


def tool_env(tmp_path: Path, enabled: bool = True) -> tuple[SafetyGate, ToolContext]:
    settings = Settings(
        workspace=tmp_path, state_dir=tmp_path, proactive=ProactiveConfig(enabled=enabled)
    )
    context = ToolContext(tmp_path, settings, AuditLog(tmp_path / "audit.jsonl"))
    if enabled:
        context.services["proactive_store"] = ProactiveStore(tmp_path / "proactive.sqlite3")
    context.services["proactive_clock"] = Clock()
    return SafetyGate(discover_tools()), context


async def dispatch(
    env: tuple[SafetyGate, ToolContext],
    name: str,
    state: TurnState | None = None,
    approve: Any = never,
    **arguments: Any,
) -> ToolResult:
    gate, context = env
    return await gate.dispatch(
        ToolCall(id="c", name=name, arguments=arguments), context, state or TurnState(), approve
    )


async def test_reminder_create_in_clean_turn_needs_no_approval(tmp_path: Path) -> None:
    env = tool_env(tmp_path)
    result = await dispatch(env, "reminder_create", text="Annemi ara", at="2026-10-15 18:30")
    assert result.status == "ok"
    assert result.data["due_local"] == "2026-10-15 18:30"
    listed = await dispatch(env, "reminder_list")
    assert listed.data["reminders"][0]["text"] == "Annemi ara"
    assert listed.external is False
    in_minutes = await dispatch(env, "reminder_create", text="Çay", in_minutes=15)
    assert in_minutes.data["due_local"] == "2026-10-15 09:15"


async def test_reminder_create_in_tainted_turn_asks_and_records_taint(tmp_path: Path) -> None:
    env = tool_env(tmp_path)
    asked: list[dict[str, Any]] = []

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        asked.append(arguments)
        return True

    state = TurnState(tainted=True)
    result = await dispatch(env, "reminder_create", state, approve, text="Linke tıkla",
                            in_minutes=5)
    assert result.status == "ok"
    assert asked == [{"text": "Linke tıkla", "at": None, "in_minutes": 5}]
    listed = await dispatch(env, "reminder_list")
    assert listed.data["reminders"][0]["created_after_external_content"] is True
    # Dış içerikten kurulan metni okumak turu yeniden lekeler.
    assert listed.external is True

    denied = await dispatch(env, "reminder_create", TurnState(tainted=True), lambda *_: False,
                            text="Reddedilen", in_minutes=5)
    assert denied.status == "denied"


async def test_reminder_from_private_turn_is_stored_private(tmp_path: Path) -> None:
    env = tool_env(tmp_path)
    env[1].turn_private = True
    await dispatch(env, "reminder_create", text="Bordrodaki tutar", in_minutes=5)
    listed = await dispatch(env, "reminder_list")
    assert listed.content_kind == "file"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"text": "geçmiş", "at": "2026-10-15 08:00"}, "geçmişte"),
        ({"text": "biçim", "at": "yarın sabah"}, "biçiminde"),
        ({"text": "uzak", "at": "2028-01-01 09:00"}, "bir yıl"),
    ],
)
async def test_reminder_create_rejects_bad_times(
    tmp_path: Path, arguments: dict[str, Any], message: str
) -> None:
    result = await dispatch(tool_env(tmp_path), "reminder_create", **arguments)
    assert result.status == "error" and message in result.message


async def test_reminder_create_requires_exactly_one_time_field(tmp_path: Path) -> None:
    env = tool_env(tmp_path)
    both = await dispatch(env, "reminder_create", text="x", at="2026-10-15 10:00", in_minutes=5)
    neither = await dispatch(env, "reminder_create", text="x")
    assert both.status == neither.status == "error"
    assert "şemaya uymuyor" in both.message


async def test_reminder_tools_report_disabled_honestly(tmp_path: Path) -> None:
    env = tool_env(tmp_path, enabled=False)
    result = await dispatch(env, "reminder_create", text="x", in_minutes=5)
    assert result.status == "error" and "proactive.enabled" in result.message


async def test_reminder_cancel(tmp_path: Path) -> None:
    env = tool_env(tmp_path)
    created = await dispatch(env, "reminder_create", text="İptal edilecek", in_minutes=5)
    reminder_id = created.data["reminder_id"]
    assert (await dispatch(env, "reminder_cancel", reminder_id=reminder_id)).status == "ok"
    again = await dispatch(env, "reminder_cancel", reminder_id=reminder_id)
    assert again.status == "error"
    assert (await dispatch(env, "reminder_list")).data["reminders"] == []


def test_runtime_creates_store_only_when_enabled(tmp_path: Path) -> None:
    enabled = make_runtime(tmp_path / "on")
    assert isinstance(enabled.context.services.get("proactive_store"), ProactiveStore)
    settings = Settings(workspace=tmp_path / "off", state_dir=tmp_path / "off-state",
                        audit_path=tmp_path / "off.jsonl", persist_memory=False)
    disabled = create_runtime(tmp_path / "x.yaml", never, provider=ScriptedProvider([]),
                              settings=settings)
    assert "proactive_store" not in disabled.context.services
    assert not (tmp_path / "off-state" / "proactive.sqlite3").exists()


# 5. İnceleme bulguları -----------------------------------------------------------------


async def test_agent_loop_marks_reminder_private_after_reading_file(tmp_path: Path) -> None:
    # turn_private'ı döngü doldurur; aynı adımdaki ikinci çağrı birincinin gizliliğini görür.
    workspace = tmp_path / "desktop"
    workspace.mkdir()
    (workspace / "bordro.txt").write_text("net 50.000", encoding="utf-8")
    responses = [
        Completion(
            tool_calls=[
                ToolCall(id="r", name="read_file", arguments={"path": "bordro.txt"}),
                ToolCall(id="c", name="reminder_create",
                         arguments={"text": "net 50.000", "in_minutes": 5}),
            ]
        ),
        Completion(content="Kuruldu."),
    ]
    settings = Settings(
        workspace=workspace,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        proactive=ProactiveConfig(enabled=True),
    )
    runtime = create_runtime(tmp_path / "x.yaml", lambda *_: True,
                             provider=ScriptedProvider(responses), settings=settings)
    await runtime.runner.run("bordrodaki tutarı hatırlat")
    store: ProactiveStore = runtime.context.services["proactive_store"]
    [reminder] = store.pending_reminders()
    assert reminder.created_private is True
    assert reminder.created_tainted is True


async def test_reminder_from_previous_external_turn_is_marked_tainted(tmp_path: Path) -> None:
    workspace = tmp_path / "desktop"
    workspace.mkdir()
    (workspace / "not.txt").write_text("yarın faturayı öde", encoding="utf-8")
    responses = [
        Completion(tool_calls=[ToolCall(id="r", name="read_file",
                                        arguments={"path": "not.txt"})]),
        Completion(content="Okudum."),
        Completion(tool_calls=[ToolCall(id="c", name="reminder_create",
                                        arguments={"text": "faturayı öde", "in_minutes": 5})]),
        Completion(content="Kuruldu."),
    ]
    settings = Settings(
        workspace=workspace,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        allow_cloud_file_content=True,
        proactive=ProactiveConfig(enabled=True),
    )
    runtime = create_runtime(tmp_path / "x.yaml", never,
                             provider=ScriptedProvider(responses), settings=settings)
    await runtime.runner.run("notu oku")
    await runtime.runner.run("bunu hatırlat")
    store: ProactiveStore = runtime.context.services["proactive_store"]
    [reminder] = store.pending_reminders()
    assert reminder.created_tainted is True


async def test_missed_summary_with_any_private_item_is_withheld_from_telegram(
    tmp_path: Path, telegram_harness: Harness
) -> None:
    runtime = make_runtime(tmp_path, telegram=telegram_harness.settings.telegram)
    add(runtime, "açık metin", NOW - timedelta(hours=20))
    add(runtime, "gizli metin", NOW - timedelta(hours=21), private=True)
    client = build_telegram_client(runtime.settings, runtime.audit)
    assert client is not None
    panel = Panel()
    try:
        await scheduler_for(runtime, panel, Clock(), telegram=client).tick()
    finally:
        client.close()
    texts = [body.get("text") for body in sent_bodies(telegram_harness.fake)]
    assert texts == [PRIVATE_WITHHELD]
    assert "gizli metin" in panel.items[0][1]


class Exploding(ProactiveScheduler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    async def tick(self) -> int:
        self.calls += 1
        if self.calls == 1:
            raise ValueError("saat dilimi çözülemedi")
        return 0


async def test_scheduler_survives_unexpected_error_and_audits_it(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, poll_seconds=1)
    store = runtime.context.services["proactive_store"]
    scheduler = Exploding(runtime, store, Deliverer(runtime.settings, runtime.audit, toast=None),
                          clock=Clock())
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.serve(stop))

    async def second_tick() -> None:
        while scheduler.calls < 2 and not task.done():
            await asyncio.sleep(0.05)

    # Sınırlı bekleme: görev ölürse test takılmaz, kırılır.
    await asyncio.wait_for(second_tick(), timeout=5)
    assert not task.done(), "Zamanlayıcı beklenmeyen hatada durdu"
    stop.set()
    await asyncio.wait_for(task, timeout=3)
    errors = [r for r in read_audit(tmp_path / "audit.jsonl") if r["event"] == "proactive_error"]
    assert [error["error_type"] for error in errors] == ["ValueError"]


def test_telegram_privacy_record_survives_interleaved_briefing_turn(tmp_path: Path) -> None:
    from artemis.channels.telegram.channel import TelegramChannel

    channel = TelegramChannel(
        object(),  # type: ignore[arg-type]
        1,
        audit=AuditLog(tmp_path / "a.jsonl"),
        approval_timeout=5,
        max_message_age=60,
        poll_timeout=0,
    )
    channel.observe({"event": "turn_start", "turn_id": "kullanici"})
    channel.observe({"event": "privacy_decision", "turn_id": "kullanici", "cloud_allowed": False})
    channel.observe({"event": "turn_start", "turn_id": "brifing", "unattended": True})
    assert "kullanici" in channel._private_turns
    channel.observe({"event": "turn_start", "turn_id": "yeni", "unattended": False})
    assert channel._private_turns == set()
