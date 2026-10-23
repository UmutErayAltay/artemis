"""Pencere/sistem araçları (ADR 0031): risk, onay, gözetimsiz tur, taint, dürüst sonuç.

Windows'a dokunulmaz: servis `services["system_control"]` ile sahtesine değiştirilir.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from artemis.brain.models import ToolCall
from artemis.config.settings import PerceptionConfig, Settings, SystemControlConfig
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.base import ToolContext
from artemis.tools.registry import discover_tools
from artemis.tools.system_control import SystemOutcome, WindowInfo


class FakeService:
    def __init__(self, outcome: SystemOutcome | None = None) -> None:
        self.outcome = outcome or SystemOutcome("ok", "Tamam.")
        self.calls: list[tuple[Any, ...]] = []

    def list_windows(self) -> SystemOutcome:
        self.calls.append(("list",))
        return SystemOutcome(
            "ok",
            "2 pencere.",
            windows=(
                WindowInfo(11, "Gelen kutusu - Gizli konu", "outlook", True),
                WindowInfo(22, "Belge.docx", "winword", False),
            ),
        )

    def control_window(
        self, window_id: int, action: str, screen: int | None = None
    ) -> SystemOutcome:
        self.calls.append(("window", window_id, action))
        return self.outcome

    def set_volume(self, level: int | None, muted: bool | None) -> SystemOutcome:
        self.calls.append(("volume", level, muted))
        return self.outcome

    def set_brightness(self, level: int) -> SystemOutcome:
        self.calls.append(("brightness", level))
        return self.outcome

    def power(self, action: str, *, delay_seconds: int = 60) -> SystemOutcome:
        self.calls.append(("power", action, delay_seconds))
        return self.outcome


def env(tmp_path: Path, service: FakeService, **settings: Any) -> tuple[SafetyGate, ToolContext]:
    config = Settings(workspace=tmp_path, **settings)
    context = ToolContext(tmp_path, config, AuditLog(tmp_path / "audit.jsonl"))
    context.services["system_control"] = service
    return SafetyGate(discover_tools()), context


def never(name: str, arguments: dict[str, Any]) -> bool:
    pytest.fail("Onay sorulmamalıydı.")


def call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id="c", name=name, arguments=arguments)


async def test_window_list_is_read_only_external_and_private(tmp_path: Path) -> None:
    service = FakeService()
    gate, context = env(tmp_path, service)
    state = TurnState()
    result = await gate.dispatch(call("window_list"), context, state, never)
    assert result.success
    assert result.external and result.content_kind == "screen"
    assert [item["id"] for item in result.data["windows"]] == [11, 22]
    assert state.tainted


async def test_window_titles_stay_private_unless_cloud_screen_allowed(tmp_path: Path) -> None:
    # Gizlilik kararı agent döngüsündedir; burada aracın doğru içerik türünü bildirdiği yeter.
    tool = discover_tools().get("window_list")
    assert tool is not None and tool.content_kind == "screen"
    assert Settings(workspace=tmp_path).perception.allow_cloud_screen is False
    allowed = Settings(workspace=tmp_path, perception=PerceptionConfig(allow_cloud_screen=True))
    assert allowed.perception.allow_cloud_screen is True


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("window_control", {"window_id": 22, "action": "left"}, ("window", 22, "left")),
        ("volume_set", {"level": 30}, ("volume", 30, None)),
        ("volume_set", {"muted": True}, ("volume", None, True)),
        ("brightness_set", {"level": 70}, ("brightness", 70)),
        ("power", {"action": "lock"}, ("power", "lock", 60)),
        ("power", {"action": "cancel_shutdown"}, ("power", "cancel_shutdown", 60)),
    ],
)
async def test_reversible_actions_run_without_confirmation_in_clean_turn(
    tmp_path: Path, name: str, arguments: dict[str, Any], expected: tuple[Any, ...]
) -> None:
    service = FakeService()
    gate, context = env(tmp_path, service)
    await gate.dispatch(call(name, **arguments), context, TurnState(), never)
    assert service.calls == [expected]


@pytest.mark.parametrize("action", ["sleep", "shutdown", "restart"])
async def test_disruptive_power_actions_always_ask_and_denial_runs_nothing(
    tmp_path: Path, action: str
) -> None:
    service = FakeService()
    gate, context = env(tmp_path, service)
    asked: list[tuple[str, dict[str, Any]]] = []

    def deny(name: str, arguments: dict[str, Any]) -> bool:
        asked.append((name, arguments))
        return False

    result = await gate.dispatch(call("power", action=action), context, TurnState(), deny)
    assert result.status == "denied"
    assert asked == [("power", {"action": action})]
    assert service.calls == []


async def test_shutdown_uses_configured_delay_and_is_reported_as_sent(tmp_path: Path) -> None:
    service = FakeService(SystemOutcome("sent", "90 sn sonra kapanacak."))
    gate, context = env(
        tmp_path, service, system_control=SystemControlConfig(shutdown_delay_seconds=90)
    )
    result = await gate.dispatch(
        call("power", action="shutdown"), context, TurnState(), lambda *_: True
    )
    assert service.calls == [("power", "shutdown", 90)]
    assert result.status == "sent" and result.success is False


def test_shutdown_delay_has_a_floor(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SystemControlConfig(shutdown_delay_seconds=10)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("window_control", {"window_id": 22, "action": "focus"}),
        ("volume_set", {"level": 0}),
        ("brightness_set", {"level": 0}),
        ("power", {"action": "lock"}),
        ("power", {"action": "shutdown"}),
    ],
)
async def test_unattended_turn_runs_no_system_change(
    tmp_path: Path, name: str, arguments: dict[str, Any]
) -> None:
    service = FakeService()
    gate, context = env(tmp_path, service)
    result = await gate.dispatch(
        call(name, **arguments), context, TurnState(unattended=True), never
    )
    assert result.status == "denied"
    assert service.calls == []


async def test_unattended_turn_may_still_list_windows(tmp_path: Path) -> None:
    service = FakeService()
    gate, context = env(tmp_path, service)
    result = await gate.dispatch(call("window_list"), context, TurnState(unattended=True), never)
    assert result.success


async def test_side_effect_after_window_titles_needs_confirmation(tmp_path: Path) -> None:
    service = FakeService()
    gate, context = env(tmp_path, service)
    state = TurnState()
    await gate.dispatch(call("window_list"), context, state, never)
    asked: list[str] = []

    def deny(name: str, arguments: dict[str, Any]) -> bool:
        asked.append(name)
        return False

    result = await gate.dispatch(
        call("window_control", window_id=11, action="focus"), context, state, deny
    )
    assert asked == ["window_control"]
    assert result.status == "denied"
    assert service.calls == [("list",)]


@pytest.mark.parametrize(
    ("outcome", "success", "status"),
    [
        (SystemOutcome("ok", "Ses 30.", level=30, muted=False), True, "ok"),
        (SystemOutcome("sent", "Kilit gönderildi."), False, "sent"),
        (SystemOutcome("error", "Geri okuma uyuşmadı."), False, "error"),
        (SystemOutcome("unavailable", "Yalnız Windows."), False, "error"),
    ],
)
async def test_outcome_is_mapped_honestly(
    tmp_path: Path, outcome: SystemOutcome, success: bool, status: str
) -> None:
    gate, context = env(tmp_path, FakeService(outcome))
    result = await gate.dispatch(call("volume_set", level=30), context, TurnState(), never)
    assert (result.success, result.status) == (success, status)
    assert result.message == outcome.message


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("volume_set", {}),
        ("volume_set", {"level": 101}),
        ("brightness_set", {"level": -1}),
        ("window_control", {"window_id": 0, "action": "focus"}),
        ("window_control", {"window_id": 5, "action": "close"}),
        ("power", {"action": "sign_out"}),
        ("power", {"action": "lock", "force": True}),
    ],
)
async def test_invalid_arguments_never_reach_the_service(
    tmp_path: Path, name: str, arguments: dict[str, Any]
) -> None:
    service = FakeService()
    gate, context = env(tmp_path, service)
    result = await gate.dispatch(call(name, **arguments), context, TurnState(), never)
    assert not result.success
    assert service.calls == []


async def test_power_actions_are_audited(tmp_path: Path) -> None:
    gate, context = env(tmp_path, FakeService(SystemOutcome("sent", "Kilit gönderildi.")))
    await gate.dispatch(call("power", action="lock"), context, TurnState(), never)
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"event": "power"' in text and '"action": "lock"' in text
