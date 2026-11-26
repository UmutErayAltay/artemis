"""Rutinler (ADR 0058): kayıt kuralları, kapıdan geçen çalıştırma, zamanlı bildirim, bozuk dosya.

Gerçek uygulama ya da makine yoktur: adımlar sahte araçlardır, saat enjekte edilir, toast kapalıdır.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from pydantic import BaseModel, ConfigDict

from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import ToolCall
from artemis.channels.runtime import create_runtime
from artemis.config.settings import (
    ComputerUseConfig,
    ProactiveConfig,
    RoutinesConfig,
    Settings,
)
from artemis.proactive.delivery import Deliverer
from artemis.proactive.routines import (
    Routine,
    RoutineSchedule,
    RoutinesError,
    RoutineStep,
    RoutineStore,
)
from artemis.proactive.scheduler import ProactiveScheduler
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.base import BaseTool, GateHandle, Risk, ToolContext, ToolResult
from artemis.tools.plugins.routines import RoutineDelete, RoutineList, RoutineRun, RoutineSave
from artemis.tools.registry import ToolRegistry

# 2026-10-15 Perşembe 09:00 İstanbul = 06:00 UTC.
NOW = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int = 0


class Probe(BaseTool):
    name = "probe_write"
    description = "Yan etkili sahte araç."
    args_model = Args
    risk: ClassVar[Risk] = Risk.SIDE_EFFECT
    ran: ClassVar[list[str]] = []

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        Probe.ran.append(self.name)
        return ToolResult(success=True, status="ok", message="yapıldı")


class ConfirmProbe(Probe):
    name = "probe_confirm"
    risk: ClassVar[Risk] = Risk.CONFIRM_REQUIRED


class ReadProbe(Probe):
    name = "probe_read"
    risk: ClassVar[Risk] = Risk.READ_ONLY


class WebProbe(ReadProbe):
    name = "probe_web"
    external: ClassVar[bool] = True


class FailProbe(Probe):
    name = "probe_fail"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        Probe.ran.append(self.name)
        return ToolResult(message="olmadı")


class SentProbe(Probe):
    name = "probe_sent"

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        Probe.ran.append(self.name)
        return ToolResult(status="sent", message="gönderildi")


def step(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments}


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        Probe, ConfirmProbe, ReadProbe, WebProbe, FailProbe, SentProbe,
        RoutineList, RoutineRun, RoutineSave, RoutineDelete,
    ):
        registry.register(tool)
    return registry


def make_settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    return Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        routines=RoutinesConfig(enabled=enabled),
    )


class Env:
    """Kapı, bağlam ve onay kaydı; testler `approvals` ile sorulanları görür."""

    def __init__(
        self, tmp_path: Path, *, enabled: bool = True, settings: Settings | None = None
    ) -> None:
        Probe.ran = []
        self.tmp_path = tmp_path
        self.settings = settings or make_settings(tmp_path, enabled=enabled)
        self.gate = SafetyGate(build_registry())
        self.context = ToolContext(tmp_path, self.settings, AuditLog(self.settings.audit_path))
        self.approvals: list[str] = []
        self.answer = True
        self.store = RoutineStore(self.settings.state_dir / "routines.json")

    def approve(self, name: str, arguments: dict[str, Any]) -> bool:
        self.approvals.append(name)
        return self.answer

    async def call(
        self, tool_name: str, state: TurnState | None = None, /, **arguments: Any
    ) -> ToolResult:
        return await self.gate.dispatch(
            ToolCall(id="c", name=tool_name, arguments=arguments),
            self.context,
            state or TurnState(),
            self.approve,
        )

    def seed(self, name: str, steps: list[dict[str, Any]], **extra: Any) -> None:
        routines = self.store.load()
        routines.append(Routine(name=name, steps=[RoutineStep(**s) for s in steps], **extra))
        self.store.save(routines)

    def events(self, event: str) -> list[dict[str, Any]]:
        lines = self.settings.audit_path.read_text(encoding="utf-8").splitlines()
        return [r for r in map(json.loads, lines) if r["event"] == event]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# 1. Kaydetme ----------------------------------------------------------------------------


async def test_save_always_asks_and_denial_writes_nothing(env: Env) -> None:
    env.answer = False
    result = await env.call("routine_save", name="işe başla", steps=[step("probe_read")])
    assert env.approvals == ["routine_save"]
    assert result.status == "denied"
    assert env.store.load() == []
    env.answer = True
    result = await env.call("routine_save", name="işe başla", steps=[step("probe_read")])
    assert result.success, result.message
    assert env.approvals == ["routine_save", "routine_save"]
    assert [s.tool for s in env.store.load()[0].steps] == ["probe_read"]


async def test_save_after_external_read_asks_confirmation_in_accept_mode(env: Env) -> None:
    env.answer = False
    denied = await env.call(
        "routine_save", TurnState(tainted=True), name="x", steps=[step("probe_read")]
    )
    assert denied.status == "denied"
    assert env.approvals == ["routine_save"]
    assert env.store.load() == []

    state = TurnState()
    await env.call("probe_web", state)
    assert state.tainted
    env.answer = True
    saved = await env.call("routine_save", state, name="web-ozet", steps=[step("probe_read")])
    assert saved.success, saved.message
    assert env.approvals == ["routine_save", "routine_save"]
    assert [r.name for r in env.store.load()] == ["web-ozet"]


async def test_save_after_prior_external_read_confirms_in_accept_and_auto(
    tmp_path: Path,
) -> None:
    """ADR 0090 (#37): dış içerik bağlamdayken otomatik kip de onay sorar; kayıtlı rutin
    sonra temiz turda onaysız çalışacağı için enjekte talimat kalıcılaşmamalı."""
    for mode in ("accept", "auto"):
        settings = make_settings(tmp_path).model_copy(
            update={"computer_use": ComputerUseConfig(mode=mode)}
        )
        local_env = Env(tmp_path / mode, settings=settings)
        local_env.answer = True
        for name, state in (
            ("x-tainted", TurnState(tainted=True)),
            ("x-history", TurnState(history_external=True)),
        ):
            result = await local_env.call(
                "routine_save", state, name=name, steps=[step("probe_read")]
            )
            assert result.success, result.message
        assert local_env.approvals == ["routine_save", "routine_save"]
        assert local_env.events("approval_skipped") == []


async def test_save_in_clean_turn_skips_approval_only_in_auto(tmp_path: Path) -> None:
    """Temiz turda 2026-11-21 kararı sürer: otomatik kipte onaysız, normal kipte onaylı."""
    for mode in ("accept", "auto"):
        settings = make_settings(tmp_path).model_copy(
            update={"computer_use": ComputerUseConfig(mode=mode)}
        )
        local_env = Env(tmp_path / mode, settings=settings)
        local_env.answer = True
        result = await local_env.call(
            "routine_save", TurnState(), name="temiz", steps=[step("probe_read")]
        )
        assert result.success, result.message
        if mode == "accept":
            assert local_env.approvals == ["routine_save"]
        else:
            assert local_env.approvals == []
            assert len(local_env.events("approval_skipped")) == 1


@pytest.mark.parametrize(
    ("steps", "needle"),
    [
        ([step("yok_boyle_arac")], "araç yok"),
        ([step("probe_write", value="kotu")], "şemaya uymuyor"),
        ([step("probe_write", ekstra=1)], "şemaya uymuyor"),
        ([step("routine_run", name="x")], "rutin içinden rutin"),
        # Gizlenmiş ad çözülünce rutin aracına çıkıyorsa da yasak (önek denetimi çözülmüş adda).
        ([step("mcp__organ_enhance__clip_routine_run", name="x")], "rutin içinden rutin"),
        ([step("clip_routine_run", name="x")], "böyle bir araç yok"),
        ([step("mcp__organ_enhance__clip_yok_boyle")], "böyle bir araç yok"),
        ([step("routine_save", name="x", steps=[step("probe_read")])], "rutin içinden rutin"),
    ],
)
async def test_invalid_steps_are_rejected_at_save(
    env: Env, steps: list[dict[str, Any]], needle: str
) -> None:
    result = await env.call("routine_save", name="r", steps=steps)
    assert not result.success and needle in result.message
    assert env.store.load() == []


async def test_proxy_obfuscated_step_names_resolve_to_registered_tools(env: Env) -> None:
    """Antigravity vekili araç adlarını gizler (`mcp__organ_enhance__clip_<ad>`); kayıt gerçek adı
    yazar. Yalnız kayıtlı bir araca çıkan ad kabul edilir (2026-11-15 canlı kabul bulgusu)."""
    result = await env.call(
        "routine_save",
        name="r",
        steps=[
            step("mcp__organ_enhance__clip_probe_write", value=1),
            step("mcp__organ_enhance__lake_probe_read"),
        ],
    )
    assert result.success
    [routine] = env.store.load()
    assert [s.tool for s in routine.steps] == ["probe_write", "probe_read"]


def test_resolve_tool_name_foreign_prefix() -> None:
    from artemis.proactive.routines import resolve_tool_name

    registry = build_registry()
    assert resolve_tool_name("not_delete_file", registry) is None
    assert resolve_tool_name("not_probe_read", registry) is None

    class AppTool(Probe):
        name = "open_application"

    registry.register(AppTool)
    assert (
        resolve_tool_name("mcp__organ_enhance__clip_open_application", registry)
        == "open_application"
    )
    assert resolve_tool_name("open_application", registry) == "open_application"
    assert resolve_tool_name("mcp__proxy__open_application", registry) == "open_application"


async def test_save_schema_limits_steps_and_forbids_schedule(env: Env) -> None:
    too_many = await env.call("routine_save", name="r", steps=[step("probe_read")] * 13)
    assert "şemaya uymuyor" in too_many.message
    scheduled = await env.call(
        "routine_save", name="r", steps=[step("probe_read")], schedule={"at": "08:00"}
    )
    assert "şemaya uymuyor" in scheduled.message
    assert env.store.load() == [] and env.approvals == []


async def test_resave_replaces_by_name_and_keeps_user_schedule(env: Env) -> None:
    env.seed("İşe Başla", [step("probe_read")], schedule=RoutineSchedule(at="08:30"))
    result = await env.call("routine_save", name="işe  başla", steps=[step("probe_write")])
    assert result.success
    [routine] = env.store.load()
    assert [s.tool for s in routine.steps] == ["probe_write"]
    assert routine.schedule is not None and routine.schedule.at == "08:30"


async def test_routine_count_is_capped(env: Env) -> None:
    for number in range(30):
        env.seed(f"r{number}", [step("probe_read")])
    result = await env.call("routine_save", name="fazla", steps=[step("probe_read")])
    assert not result.success and "30" in result.message


async def test_delete_always_asks(env: Env) -> None:
    env.seed("a", [step("probe_read")])
    env.answer = False
    assert (await env.call("routine_delete", name="a")).status == "denied"
    assert len(env.store.load()) == 1
    env.answer = True
    assert (await env.call("routine_delete", name="a")).success
    assert env.store.load() == []
    assert env.approvals == ["routine_delete", "routine_delete"]


# 2. Çalıştırma kapıdan geçer ------------------------------------------------------------


async def test_run_dispatches_every_step_through_the_gate(env: Env) -> None:
    env.seed("sabah", [step("probe_write"), step("probe_confirm"), step("probe_read")])
    result = await env.call("routine_run", name="Sabah")
    assert result.success, result.message
    assert Probe.ran == ["probe_write", "probe_confirm", "probe_read"]
    # CONFIRM_REQUIRED adım yine sorulur; temiz turdaki SIDE_EFFECT sorulmaz.
    assert env.approvals == ["probe_confirm"]
    dispatched = [r["tool"] for r in env.events("tool_dispatch")]
    assert dispatched == ["routine_run", "probe_write", "probe_confirm", "probe_read"]
    [start] = env.events("routine_run_start")
    [end] = env.events("routine_run_end")
    assert start["steps"] == 3 and end["steps_completed"] == 3 and end["completed"] is True


async def test_denied_step_stops_the_routine_and_is_reported(env: Env) -> None:
    env.seed("sabah", [step("probe_write"), step("probe_confirm"), step("probe_write")])
    env.answer = False
    result = await env.call("routine_run", name="sabah")
    assert result.status == "denied" and not result.success
    assert Probe.ran == ["probe_write"]
    assert "1/3" in result.message and "probe_confirm" in result.message
    assert [s["success"] for s in result.data["steps"]] == [True, False]
    [end] = env.events("routine_run_end")
    assert end["steps_completed"] == 1 and end["completed"] is False


async def test_failed_step_stops_and_is_not_success(env: Env) -> None:
    env.seed("r", [step("probe_fail"), step("probe_write")])
    result = await env.call("routine_run", name="r")
    assert not result.success and result.status == "error"
    assert Probe.ran == ["probe_fail"]


async def test_sent_step_does_not_stop_but_routine_is_not_success(env: Env) -> None:
    env.seed("r", [step("probe_sent"), step("probe_write")])
    result = await env.call("routine_run", name="r")
    assert Probe.ran == ["probe_sent", "probe_write"]
    assert result.status == "sent" and not result.success
    assert "doğrulanmadı" in result.message


async def test_non_utf8_file_counts_as_corrupt(env: Env) -> None:
    env.store.path.parent.mkdir(parents=True)
    env.store.path.write_text('{"routines": []}', encoding="utf-16")
    result = await env.call("routine_list")
    assert not result.success and "bozuk" in result.message


async def test_taint_from_one_step_makes_later_side_effects_ask(env: Env) -> None:
    env.seed("r", [step("probe_web"), step("probe_write")])
    result = await env.call("routine_run", name="r")
    assert result.success and result.external
    assert env.approvals == ["probe_write"]


async def test_unattended_turn_cannot_run_a_routine(env: Env) -> None:
    env.seed("r", [step("probe_write")])
    result = await env.call("routine_run", TurnState(unattended=True), name="r")
    assert result.status == "denied" and Probe.ran == [] and env.approvals == []


async def test_unattended_state_denies_non_read_only_steps(env: Env) -> None:
    env.seed("r", [step("probe_read"), step("probe_write"), step("probe_read")])
    state = TurnState(unattended=True)
    env.context.gate_handle = GateHandle(
        env.gate.registry, lambda c: env.gate.dispatch(c, env.context, state, env.approve)
    )
    result = await RoutineRun().execute(
        RoutineRun.args_model(name="r"), env.context  # type: ignore[call-arg]
    )
    assert Probe.ran == ["probe_read"] and env.approvals == []
    assert result.status == "denied" and "2." in result.message


async def test_run_revalidates_steps_before_running_any(env: Env) -> None:
    # Dosya elle bozulmuş: ikinci adımın aracı yok. Birinci adım da çalışmamalı.
    env.seed("r", [step("probe_write"), step("probe_write")])
    path = env.store.path
    path.write_text(path.read_text(encoding="utf-8").replace("probe_write", "silinmis", 1))
    result = await env.call("routine_run", name="r")
    assert not result.success and "araç yok" in result.message and Probe.ran == []
    env.seed("s", [step("probe_write")])
    text = env.store.path.read_text(encoding="utf-8")
    text = text.replace('"arguments": {}', '"arguments": {"value": "x"}')
    env.store.path.write_text(text)
    assert "şemaya uymuyor" in (await env.call("routine_run", name="s")).message


async def test_run_without_gate_handle_runs_nothing(env: Env) -> None:
    env.seed("r", [step("probe_write")])
    env.context.gate_handle = None
    result = await RoutineRun().execute(RoutineRun.args_model(name="r"), env.context)  # type: ignore[call-arg]
    assert not result.success and Probe.ran == []


async def test_unknown_routine_name(env: Env) -> None:
    result = await env.call("routine_run", name="yok")
    assert not result.success and "yok" in result.message


async def test_list_reports_steps_and_schedule(env: Env) -> None:
    env.seed("r", [step("probe_read")], schedule=RoutineSchedule(at="08:30", days=(0, 2)))
    result = await env.call("routine_list")
    assert result.data["routines"] == [
        {"name": "r", "steps": ["probe_read"], "schedule": "08:30 [0, 2]"}
    ]


# 3. Bozuk dosya, kapalı bayrak ----------------------------------------------------------


async def test_corrupt_file_disables_routines_and_is_never_reset(env: Env) -> None:
    env.store.path.parent.mkdir(parents=True)
    env.store.path.write_text("{ bozuk", encoding="utf-8")
    for name, arguments in (
        ("routine_list", {}),
        ("routine_run", {"name": "a"}),
        ("routine_save", {"name": "a", "steps": [step("probe_read")]}),
        ("routine_delete", {"name": "a"}),
    ):
        result = await env.call(name, **arguments)
        assert not result.success and "bozuk" in result.message, name
    assert env.store.path.read_text(encoding="utf-8") == "{ bozuk"


async def test_schema_invalid_file_counts_as_corrupt(env: Env) -> None:
    env.store.path.parent.mkdir(parents=True)
    env.store.path.write_text(json.dumps({"routines": [{"name": "a", "steps": []}]}))
    assert "bozuk" in (await env.call("routine_list")).message


async def test_disabled_flag_refuses_without_asking(tmp_path: Path) -> None:
    env = Env(tmp_path, enabled=False)
    for name, arguments in (
        ("routine_list", {}),
        ("routine_run", {"name": "a"}),
        ("routine_save", {"name": "a", "steps": [step("probe_read")]}),
        ("routine_delete", {"name": "a"}),
    ):
        result = await env.call(name, **arguments)
        assert not result.success and "kapalı" in result.message, name
    assert env.approvals == [] and not env.store.path.exists()


def runtime_for(tmp_path: Path, *, enabled: bool, proactive: bool = False) -> Any:
    settings = make_settings(tmp_path, enabled=enabled)
    if proactive:
        settings = settings.model_copy(update={"proactive": ProactiveConfig(enabled=True)})
    return create_runtime(
        tmp_path / "unused.yaml",
        lambda name, arguments: False,
        provider=ScriptedProvider([]),
        registry=build_registry(),
        settings=settings,
    )


def test_disabled_flag_hides_the_tools_from_the_model(tmp_path: Path) -> None:
    hidden = runtime_for(tmp_path, enabled=False)
    assert not [d.name for d in hidden.registry.definitions() if d.name.startswith("routine_")]
    assert hidden.registry.get("probe_read") is not None
    shown = runtime_for(tmp_path, enabled=True)
    names = {d.name for d in shown.registry.definitions()}
    assert {"routine_list", "routine_run", "routine_save", "routine_delete"} <= names


def test_no_tool_can_set_a_schedule() -> None:
    for tool in (RoutineSave, RoutineRun, RoutineDelete, RoutineList):
        assert "schedule" not in tool.args_model.model_json_schema()["properties"]


# 4. Zamanlı bildirim: çalıştırmaz, sorar -------------------------------------------------


class Panel:
    def __init__(self) -> None:
        self.routine_items: list[tuple[str, str, str]] = []

    def __call__(
        self, title: str, body: str, routine: str, suggest: str = "", day: str = "",
        _tainted: bool = False,
    ) -> bool:
        self.routine_items.append((title, body, routine))
        return True


async def test_scheduled_occurrence_produces_notice_and_does_not_execute(tmp_path: Path) -> None:
    Probe.ran = []
    runtime = runtime_for(tmp_path, enabled=True, proactive=True)
    RoutineStore(runtime.settings.state_dir / "routines.json").save(
        [
            Routine(
                name="işe başla",
                steps=[RoutineStep(tool="probe_write")],
                schedule=RoutineSchedule(at="09:00", days=(3,)),  # Perşembe
            ),
            Routine(name="zamansız", steps=[RoutineStep(tool="probe_write")]),
        ]
    )
    panel = Panel()
    deliverer = Deliverer(runtime.settings, runtime.audit, routine_panel=panel, toast=None)
    scheduler = ProactiveScheduler(
        runtime,
        runtime.context.services["proactive_store"],
        deliverer,
        clock=lambda: NOW,
        owner="a",
    )
    assert await scheduler.tick() == 1
    assert await scheduler.tick() == 0  # aynı oluşum bir kez
    assert panel.routine_items == [
        ("Rutin zamanı", "işe başla rutininin zamanı. Çalıştırılsın mı?", "işe başla")
    ]
    assert Probe.ran == []
    audit = [json.loads(x) for x in runtime.audit.path.read_text(encoding="utf-8").splitlines()]
    assert not [r for r in audit if r["event"] in {"tool_dispatch", "routine_run_start"}]


async def test_scheduler_ignores_schedules_when_routines_disabled_or_wrong_day(
    tmp_path: Path,
) -> None:
    runtime = runtime_for(tmp_path, enabled=False, proactive=True)
    RoutineStore(runtime.settings.state_dir / "routines.json").save(
        [Routine(name="r", steps=[RoutineStep(tool="probe_write")],
                 schedule=RoutineSchedule(at="09:00"))]
    )
    panel = Panel()
    scheduler = ProactiveScheduler(
        runtime,
        runtime.context.services["proactive_store"],
        Deliverer(runtime.settings, runtime.audit, routine_panel=panel, toast=None),
        clock=lambda: NOW,
    )
    assert await scheduler.tick() == 0 and panel.routine_items == []


async def test_scheduler_survives_corrupt_routine_file(tmp_path: Path) -> None:
    runtime = runtime_for(tmp_path, enabled=True, proactive=True)
    path = runtime.settings.state_dir / "routines.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    scheduler = ProactiveScheduler(
        runtime,
        runtime.context.services["proactive_store"],
        Deliverer(runtime.settings, runtime.audit, toast=None),
        clock=lambda: NOW,
    )
    assert await scheduler.tick() == 0
    assert path.read_text(encoding="utf-8") == "{"


# 5. Arayüz bölümü -----------------------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_routines_section_lists_runs_schedules_and_deletes_via_tools(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QPushButton

    from artemis.channels.desktop.routines_section import RoutinesSection

    QApplication.instance() or QApplication([])
    state = tmp_path / "state"
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({
            "workspace": str(tmp_path), "state_dir": str(state), "routines": {"enabled": True}
        }),
        encoding="utf-8",
    )
    store = RoutineStore(state / "routines.json")
    store.save([Routine(name="işe başla", steps=[RoutineStep(tool="probe_read")])])
    section = RoutinesSection(config)
    requested: list[tuple[str, Any]] = []
    section.tool_requested.connect(lambda name, args: requested.append((name, args)))
    assert section.routine_list.count() == 1
    assert "Çalıştır" not in [button.text() for button in section.findChildren(QPushButton)]
    assert section.run_hint.text() == "Rutinleri Otomasyon sayfasından çalıştırırsın."
    assert section.action_row.isVisibleTo(section)
    assert not section.delete_button.isEnabled()  # seçim yok
    section.routine_list.setCurrentRow(0)
    section._confirm = lambda text: False  # type: ignore[method-assign]
    section.delete_button.click()  # panel silmesi onay ister; Vazgeç hiçbir şeyi silmez
    assert requested == [] and len(store.load()) == 1
    assert store.load()[0].schedule is None
    section.time_edit.setText("25:99")
    section.save_time_button.click()
    assert store.load()[0].schedule is None and "SS:DD" in section.notice.text()
    section.time_edit.setText("08:30")
    section.day_boxes[6].setChecked(False)
    section.save_time_button.click()
    schedule = store.load()[0].schedule
    assert schedule is not None and schedule.at == "08:30" and schedule.days == (0, 1, 2, 3, 4, 5)
    section.clear_time_button.click()
    assert store.load()[0].schedule is None


def test_routines_section_is_inert_when_disabled_or_corrupt(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.routines_section import RoutinesSection

    QApplication.instance() or QApplication([])
    config = tmp_path / "config.yaml"
    state = tmp_path / "state"
    config.write_text(yaml.safe_dump({"workspace": str(tmp_path), "state_dir": str(state)}))
    section = RoutinesSection(config)
    assert section.routine_area.empty.title_label.text() == "Rutinler kapalı"
    assert not section.action_row.isVisibleTo(section)  # kapalıyken eylem satırı yok
    assert not section.run_hint.isVisibleTo(section)
    config.write_text(yaml.safe_dump({
        "workspace": str(tmp_path), "state_dir": str(state), "routines": {"enabled": True}
    }))
    state.mkdir()
    (state / "routines.json").write_text("{", encoding="utf-8")
    section.refresh()
    assert "bozuk" in section.notice.text() and section.routine_list.count() == 0
    assert (state / "routines.json").read_text(encoding="utf-8") == "{"
    assert section.action_row.isVisibleTo(section)


def test_save_wraps_os_error_when_parent_is_a_file(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("dosya", encoding="utf-8")
    store = RoutineStore(blocker / "routines.json")
    with pytest.raises(RoutinesError):
        store.save([])
