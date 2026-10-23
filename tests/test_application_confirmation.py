"""Uygulama istisnası yalnız temiz turu etkiler; dış içerik onayı korunur."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from artemis.brain.models import ToolCall
from artemis.config.settings import Settings
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.base import ToolContext
from artemis.tools.registry import discover_tools


class Desktop:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def close_application(self, executable: str) -> bool:
        self.closed.append(executable)
        return True


@pytest.mark.parametrize(
    ("name", "tainted", "expected"),
    [("brave", False, "ok"), ("brave", True, "denied"), ("notepad", False, "denied")],
)
async def test_per_application_confirmation(
    tmp_path: Path, name: str, tainted: bool, expected: str
) -> None:
    settings = Settings(
        applications={"brave": "brave.exe", "notepad": "notepad.exe"},
        close_without_confirmation=["brave"],
    )
    desktop = Desktop()
    context = ToolContext(tmp_path, settings, AuditLog(tmp_path / "audit.jsonl"))
    context.services["desktop"] = desktop
    approvals: list[str] = []

    def deny(tool: str, arguments: dict[str, object]) -> bool:
        approvals.append(tool)
        return False

    result = await SafetyGate(discover_tools()).dispatch(
        ToolCall(id="close", name="close_application", arguments={"name": name}),
        context, TurnState(tainted=tainted), deny,
    )
    assert result.status == expected
    assert approvals == ([] if expected == "ok" else ["close_application"])
    assert desktop.closed == (["brave.exe"] if expected == "ok" else [])


def test_unlisted_confirmation_exception_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(applications={}, close_without_confirmation=["brave"])


@pytest.mark.parametrize(("name", "expected"), [("calculator", "ok"), ("notepad", "denied")])
async def test_all_apps_mode_only_confirms_sensitive_close(
    tmp_path: Path, name: str, expected: str
) -> None:
    settings = Settings(
        allow_all_applications=True,
        applications={"calculator": "calc.exe", "notepad": "notepad.exe"},
    )
    desktop = Desktop()
    context = ToolContext(tmp_path, settings, AuditLog(tmp_path / "audit.jsonl"))
    context.services["desktop"] = desktop
    result = await SafetyGate(discover_tools()).dispatch(
        ToolCall(id="close", name="close_application", arguments={"name": name}),
        context, TurnState(), lambda *_: False,
    )
    assert result.status == expected
    assert desktop.closed == (["calc.exe"] if expected == "ok" else [])
