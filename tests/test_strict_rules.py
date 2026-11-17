"""Sıkı istem kuralları ve yıkıcı öğe kapısı testleri (ADR 0078)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from artemis.brain.models import Completion, Message
from artemis.brain.strict_rules import destructive_unrequested, strict_rules_on
from artemis.computer.session import ComputerSession, SessionResult
from artemis.config.settings import ComputerUseConfig, ModelConfig

OWN_PID = 4242


@dataclass(frozen=True)
class _UiElement:
    id: int
    kind: str
    name: str
    rect: tuple[int, int, int, int]
    interactive: bool = True
    focused: bool = False


class _UiaBackend:
    def __init__(self, elements: list[_UiElement]) -> None:
        self.elements = elements
        self.calls: list[tuple[Any, ...]] = []
        self.foreground = (r"C:\Program Files\App\app.exe", 100)
        self.window = (7, (0, 0, 800, 600))
        self.monitor_rects = [(0, 0, 800, 600)]

    def monitors(self) -> list[tuple[int, int, int, int]]:
        return list(self.monitor_rects)

    def capture(self, left: int, top: int, width: int, height: int) -> tuple[bytes, int, int]:
        return bytes(width * height * 4), width, height

    def foreground_process(self) -> tuple[str, int]:
        return self.foreground

    def foreground_window(self) -> tuple[int, tuple[int, int, int, int]]:
        return self.window

    def process_at(self, x: int, y: int) -> tuple[str, int]:
        return self.foreground

    def window_at(self, x: int, y: int) -> int:
        return self.window[0]

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        self.calls.append(("click", x, y, button, double))

    def scroll(self, x: int, y: int, notches: int) -> None:
        self.calls.append(("scroll", x, y, notches))

    def type_text(self, text: str, should_continue: Callable[[], bool]) -> int:
        return len(text)

    def press_keys(self, keys: tuple[str, ...]) -> None:
        self.calls.append(("keys", keys))

    def focused_value_contains(self, expected: str, timeout: float) -> bool | None:
        return None

    def ui_elements(self, hwnd: int) -> list[_UiElement]:
        return list(self.elements)


class _ScriptedVision:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.seen: list[list[Message]] = []

    async def complete(
        self, messages: list[Message], tools: list[Any], *, timeout: float
    ) -> Completion:
        self.seen.append(list(messages))
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return Completion(content=content)


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [fields for name, fields in self.events if name == event]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _act(**fields: Any) -> str:
    fields.setdefault("reason", "deneme")
    return json.dumps(fields)


_DONE = _act(type="done", text="Bitti.")
_YES = json.dumps({"verified": True, "evidence": "Tamam."})


async def _run_uia_session(
    goal: str,
    *contents: str,
    elements: list[_UiElement],
    strict: bool = False,
) -> tuple[SessionResult, _UiaBackend, _ScriptedVision, _Audit]:
    backend = _UiaBackend(elements)
    provider = _ScriptedVision(*contents)
    audit = _Audit()
    clock = _Clock()
    config = ComputerUseConfig(
        enabled=True,
        model="m",
        mode="auto",
        observation="uia",
        step_delay_seconds=0,
    )
    session = ComputerSession(
        provider=provider,
        backend=backend,
        approve=lambda _n, _a: True,
        audit=audit,
        config=config,
        own_pid=OWN_PID,
        stop=threading.Event(),
        on_step=lambda _s: None,
        provider_timeout=15.0,
        clock=clock,
        sleep=clock.sleep,
        strict=strict,
    )
    result = await session.run(goal, "t1", None)
    return result, backend, provider, audit


def test_destructive_unrequested_cases() -> None:
    reason = destructive_unrequested("Hesabı sil", "duyuruyu oku")
    assert reason is not None and "yıkıcı" in reason

    assert destructive_unrequested("Hesabı sil", "hesabımı sil") is None

    logout_reason = destructive_unrequested("Çıkış", "dosyayı kaydet")
    assert logout_reason is not None

    assert destructive_unrequested("Kaydet", "duyuruyu oku") is None

    upper_reason = destructive_unrequested("SİL", "duyuruyu oku")
    assert upper_reason is not None


def _model(**fields: object) -> ModelConfig:
    return ModelConfig.model_validate(
        {"input_per_million": 0, "output_per_million": 0, **fields}
    )


def test_strict_rules_on_defaults_and_overrides() -> None:
    assert strict_rules_on(None) is False
    assert (
        strict_rules_on(_model(provider="anthropic", model="claude-sonnet-4-20250514"))
        is False
    )
    assert strict_rules_on(_model(provider="claude_cli", model="sonnet")) is False
    assert (
        strict_rules_on(_model(provider="openrouter", model="anthropic/claude-3.5-sonnet"))
        is False
    )
    assert strict_rules_on(_model(provider="openrouter", model="openai/gpt-4o")) is True
    assert (
        strict_rules_on(
            _model(provider="openrouter", model="openai/gpt-4o", strict_rules=False)
        )
        is False
    )


async def test_session_destructive_gate_blocks_unrequested_in_auto_mode() -> None:
    elements = [_UiElement(id=1, kind="Button", name="Hesabı sil", rect=(10, 10, 80, 30))]
    click_del = _act(type="click_element", element=1, reason="Düğmeye tıkla")

    result, backend, provider, audit = await _run_uia_session(
        "Duyuruyu oku", click_del, _DONE, _YES, elements=elements
    )

    assert result.reason == "done"
    assert backend.calls == []
    second_user_prompt = str(provider.seen[1][1].content)
    assert "ENGELLENDİ (click_element)" in second_user_prompt
    assert "yıkıcı bir öğe" in second_user_prompt
    destructive_events = audit.named("computer_destructive_blocked")
    assert destructive_events == [{"turn_id": "t1", "element_kind": "Button"}]
    assert "Hesabı sil" not in repr(destructive_events)


async def test_session_destructive_gate_allows_when_goal_requests_it() -> None:
    elements = [_UiElement(id=1, kind="Button", name="Hesabı sil", rect=(10, 10, 80, 30))]
    click_del = _act(type="click_element", element=1, reason="Hesabı sil düğmesine tıkla")

    result, backend, _provider, audit = await _run_uia_session(
        "Hesabımı sil", click_del, _DONE, _YES, elements=elements
    )

    assert result.reason == "done"
    assert backend.calls == [("click", 50, 25, "left", False)]
    assert audit.named("computer_destructive_blocked") == []


async def test_session_strict_prompt_toggle() -> None:
    elements = [_UiElement(id=1, kind="Button", name="Tamam", rect=(10, 10, 80, 30))]

    _, _, strict_provider, _ = await _run_uia_session(
        "Tamam'a bas", _DONE, _YES, elements=elements, strict=True
    )
    assert "STRICT RULES" in str(strict_provider.seen[0][0].content)

    _, _, normal_provider, _ = await _run_uia_session(
        "Tamam'a bas", _DONE, _YES, elements=elements, strict=False
    )
    assert "STRICT RULES" not in str(normal_provider.seen[0][0].content)
