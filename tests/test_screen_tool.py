"""Ekran okuma aracı (ADR 0029): kapalı varsayılan, onay, taint, gizlilik, gözetimsiz tur.

Gerçek ekran okunmaz: okuyucu sahte bir işlevdir (`services["screen_reader"]`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import Completion, ToolCall
from artemis.channels.runtime import create_runtime
from artemis.config.settings import ComputerUseConfig, PerceptionConfig, Settings
from artemis.perception.screen import ScreenOutcome
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.base import ToolContext
from artemis.tools.registry import discover_tools


class Reader:
    def __init__(self, outcome: ScreenOutcome | None = None) -> None:
        self.outcome = outcome or ScreenOutcome(
            "ok", "okundu", text="Hata: bağlantı reddedildi", language="tr-TR",
            window_title="Terminal",
        )
        self.calls: list[str] = []

    def __call__(self, scope: str, *, timeout: float) -> ScreenOutcome:
        self.calls.append(scope)
        return self.outcome


def env(
    tmp_path: Path, reader: Reader, *, enabled: bool = True, auto: bool = False
) -> tuple[SafetyGate, ToolContext]:
    settings = Settings(
        workspace=tmp_path,
        perception=PerceptionConfig(screen_enabled=enabled),
        computer_use=ComputerUseConfig(mode="auto" if auto else "accept"),
    )
    context = ToolContext(tmp_path, settings, AuditLog(tmp_path / "audit.jsonl"))
    context.services["screen_reader"] = reader
    return SafetyGate(discover_tools()), context


def never(name: str, arguments: dict[str, Any]) -> bool:
    pytest.fail("Onay sorulmamalıydı.")


async def test_disabled_reads_nothing_and_asks_nothing(tmp_path: Path) -> None:
    reader = Reader()
    gate, context = env(tmp_path, reader, enabled=False)
    result = await gate.dispatch(ToolCall(id="c", name="screen_read", arguments={}), context,
                                 TurnState(), never)
    assert result.success is False and "screen_enabled" in result.message
    assert reader.calls == []


async def test_every_read_asks_and_shows_scope(tmp_path: Path) -> None:
    reader = Reader()
    gate, context = env(tmp_path, reader)
    asked: list[dict[str, Any]] = []

    def deny(name: str, arguments: dict[str, Any]) -> bool:
        asked.append(arguments)
        return False

    result = await gate.dispatch(ToolCall(id="c", name="screen_read", arguments={}), context,
                                 TurnState(), deny)
    assert result.status == "denied"
    assert asked == [{"scope": "active_window"}]
    assert reader.calls == []


async def test_approved_read_returns_text_and_taints(tmp_path: Path) -> None:
    reader = Reader()
    gate, context = env(tmp_path, reader)
    state = TurnState()
    result = await gate.dispatch(
        ToolCall(id="c", name="screen_read", arguments={"scope": "screen"}), context, state,
        lambda *_: True,
    )
    assert result.status == "ok"
    assert result.data["text"] == "Hata: bağlantı reddedildi"
    assert result.data["window_title"] == "Terminal"
    assert result.external is True and result.content_kind == "screen"
    assert state.tainted is True
    assert reader.calls == ["screen"]


async def test_unattended_turn_cannot_read_screen(tmp_path: Path) -> None:
    reader = Reader()
    gate, context = env(tmp_path, reader)
    result = await gate.dispatch(ToolCall(id="c", name="screen_read", arguments={}), context,
                                 TurnState(unattended=True), never)
    assert result.status == "denied" and reader.calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        ScreenOutcome("empty", "Ekranda okunabilir metin bulunamadı."),
        ScreenOutcome("unavailable", "Ekran okuma yalnız Windows'ta çalışır."),
        ScreenOutcome("error", "Ekran okuma zaman aşımına uğradı."),
    ],
)
async def test_failures_are_honest(tmp_path: Path, outcome: ScreenOutcome) -> None:
    gate, context = env(tmp_path, Reader(outcome))
    result = await gate.dispatch(ToolCall(id="c", name="screen_read", arguments={}), context,
                                 TurnState(), lambda *_: True)
    assert result.success is False and result.message == outcome.message


@pytest.mark.parametrize(("allow_cloud", "private"), [(False, True), (True, False)])
async def test_screen_text_is_private_for_cloud_unless_allowed(
    tmp_path: Path, allow_cloud: bool, private: bool
) -> None:
    responses = [
        Completion(tool_calls=[ToolCall(id="s", name="screen_read", arguments={})]),
        Completion(content="Ekranda bağlantı hatası var."),
    ]
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "s",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        perception=PerceptionConfig(screen_enabled=True, allow_cloud_screen=allow_cloud),
    )
    runtime = create_runtime(tmp_path / "x.yaml", lambda *_: True,
                             provider=ScriptedProvider(responses), settings=settings)
    runtime.context.services["screen_reader"] = Reader()
    result = await runtime.runner.run("ekranda ne yazıyor")
    assert result.record is not None
    assert result.record.answer_private is private
    assert result.record.tainted is True


async def test_auto_mode_reads_without_asking_even_on_tainted_turn(tmp_path: Path) -> None:
    import json

    reader = Reader()
    gate, context = env(tmp_path, reader, auto=True)
    result = await gate.dispatch(ToolCall(id="c", name="screen_read", arguments={}), context,
                                 TurnState(tainted=True), never)
    assert result.success is True and reader.calls == ["active_window"]
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "approval_skipped" in events and "approval" not in events


async def test_auto_mode_still_refuses_unattended_read(tmp_path: Path) -> None:
    reader = Reader()
    gate, context = env(tmp_path, reader, auto=True)
    result = await gate.dispatch(ToolCall(id="c", name="screen_read", arguments={}), context,
                                 TurnState(unattended=True), never)
    assert result.status == "denied" and reader.calls == []
