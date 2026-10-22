"""voice_continuous aracı: sahte denetleyiciyle, mikrofon açmadan."""

from pathlib import Path
from typing import Any

import pytest

from jarvis.brain.models import ToolCall
from jarvis.config.settings import Settings
from jarvis.safety.audit import AuditLog
from jarvis.safety.gate import SafetyGate, TurnState
from jarvis.tools.base import Risk, ToolContext
from jarvis.tools.registry import discover_tools


class FakeController:
    def __init__(self, *, stuck: bool = False) -> None:
        self.continuous = False
        self.stuck = stuck

    def set_continuous(self, enabled: bool) -> None:
        if not self.stuck:
            self.continuous = enabled


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    settings = Settings(
        workspace=tmp_path, state_dir=tmp_path / "state", audit_path=tmp_path / "audit.jsonl"
    )
    return ToolContext(workspace=tmp_path, settings=settings, audit=AuditLog(settings.audit_path))


def call(arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id="c1", name="voice_continuous", arguments=arguments)


def never(*_: Any) -> bool:
    pytest.fail("Temiz turda onay istenmemeli.")


def test_discovered_with_strict_schema() -> None:
    tool = discover_tools().get("voice_continuous")
    assert tool is not None
    assert tool.risk is Risk.SIDE_EFFECT
    schema = tool.args_model.model_json_schema()
    assert schema["required"] == ["enabled"] and schema["additionalProperties"] is False
    with pytest.raises(ValueError):
        tool.args_model.model_validate({"enabled": "yes"})
    with pytest.raises(ValueError):
        tool.args_model.model_validate({"enabled": True, "extra": 1})


async def test_toggles_controller_state(context: ToolContext) -> None:
    controller = FakeController()
    context.services["voice_controller"] = controller
    gate = SafetyGate(discover_tools())
    on = await gate.dispatch(call({"enabled": True}), context, TurnState(), never)
    assert on.success and controller.continuous is True
    off = await gate.dispatch(call({"enabled": False}), context, TurnState(), never)
    assert off.success and controller.continuous is False


async def test_no_service_is_honest_failure(context: ToolContext) -> None:
    result = await SafetyGate(discover_tools()).dispatch(
        call({"enabled": True}), context, TurnState(), never
    )
    assert not result.success and "Ses kapalı" in result.message


async def test_unchanged_state_is_not_success(context: ToolContext) -> None:
    context.services["voice_controller"] = FakeController(stuck=True)
    result = await SafetyGate(discover_tools()).dispatch(
        call({"enabled": True}), context, TurnState(), never
    )
    assert not result.success


async def test_tainted_turn_asks_approval(context: ToolContext) -> None:
    controller = FakeController()
    context.services["voice_controller"] = controller
    asked: list[str] = []

    def reject(name: str, arguments: dict[str, Any]) -> bool:
        asked.append(name)
        return False

    result = await SafetyGate(discover_tools()).dispatch(
        call({"enabled": True}), context, TurnState(tainted=True), reject
    )
    assert asked == ["voice_continuous"] and result.status == "denied"
    assert controller.continuous is False
