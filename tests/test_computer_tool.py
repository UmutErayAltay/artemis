"""computer_use aracı (ADR 0039): risk, ayar kapıları, gizlilik, sonuç dürüstlüğü, masaüstü host'u.

Gerçek fare/klavye/ekran yok; host, sağlayıcı ve arka uç sahtedir. Esc kısayolu da sahtedir.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError

import jarvis.channels.desktop.computer_host as host_module
from jarvis.brain.models import ToolCall
from jarvis.channels.desktop.computer_host import ComputerHost, SessionHandles
from jarvis.config.settings import ComputerUseConfig, ModelConfig, PerceptionConfig, Settings
from jarvis.safety.audit import AuditLog
from jarvis.safety.gate import SafetyGate, TurnState
from jarvis.tools.base import Risk, ToolContext
from jarvis.tools.registry import discover_tools
from tests.test_computer_session import DONE, FakeBackend, ScriptedVision, click

CALL = ToolCall(id="c", name="computer_use", arguments={"goal": "Not Defteri'ni aç"})


class FakeHost:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.steps: list[str] = []
        self.approvals: list[str] = []
        self.sessions = 0

    @contextmanager
    def session(self) -> Iterator[SessionHandles]:
        self.sessions += 1
        yield self.stop, self.steps.append, self._approve

    def _approve(self, name: str, arguments: dict[str, Any]) -> bool:
        self.approvals.append(name)
        return True


def model(provider: str = "ollama") -> ModelConfig:
    return ModelConfig(provider=provider, model="vision-1", input_per_million=0,  # type: ignore[arg-type]
                       output_per_million=0)


def env(
    tmp_path: Path,
    *,
    enabled: bool = True,
    provider: str = "ollama",
    allow_cloud: bool = False,
    host: bool = True,
    replies: tuple[str, ...] = (click(), DONE),
    mode: Literal["accept", "auto"] = "accept",
) -> tuple[SafetyGate, ToolContext, FakeHost, FakeBackend, ScriptedVision]:
    settings = Settings(
        workspace=tmp_path,
        models={"vision": model(provider)},
        computer_use=ComputerUseConfig(
            enabled=enabled, model="vision" if enabled else "", step_delay_seconds=0, mode=mode
        ),
        perception=PerceptionConfig(allow_cloud_screen=allow_cloud),
    )
    context = ToolContext(tmp_path, settings, AuditLog(tmp_path / "audit.jsonl"), turn_id="t1")
    fake_host, backend, vision = FakeHost(), FakeBackend(), ScriptedVision(*replies)
    if host:
        context.services["computer_host"] = fake_host
    context.services["computer_backend"] = backend
    context.services["computer_provider"] = vision
    return SafetyGate(discover_tools()), context, fake_host, backend, vision


def never(name: str, arguments: dict[str, Any]) -> bool:
    pytest.fail("Onay sorulmamalıydı.")


def test_registry_discovers_tool_with_expected_traits() -> None:
    tool = discover_tools().get("computer_use")
    assert tool is not None
    assert tool.external is True and tool.outbound is True and tool.content_kind == "screen"
    assert tool.risk is Risk.CONFIRM_REQUIRED


def test_risk_follows_enabled_flag(tmp_path: Path) -> None:
    gate, context, *_ = env(tmp_path, enabled=False)
    tool = discover_tools().get("computer_use")
    assert tool is not None
    args = tool.args_model(goal="x")
    assert tool.risk_for(args, context) is Risk.READ_ONLY
    gate, context, *_ = env(tmp_path, enabled=True)
    assert tool.risk_for(args, context) is Risk.CONFIRM_REQUIRED
    gate, context, *_ = env(tmp_path, enabled=True, mode="auto")
    assert tool.risk_for(args, context) is Risk.SIDE_EFFECT


async def test_auto_mode_skips_start_only_on_clean_turn(tmp_path: Path) -> None:
    gate, context, *_ = env(tmp_path, mode="auto")
    result = await gate.dispatch(CALL, context, TurnState(), never)
    assert result.status == "sent"
    # Kapı kuralları otomatik kipte de geçerli: dış içerik sonrası ve önceki turlarda dış
    # içerik varken (ADR 0013) başlangıç yine sorulur; ret çağrıyı çalıştırmaz.
    asked: list[str] = []

    def refuse(name: str, arguments: dict[str, Any]) -> bool:
        asked.append(name)
        return False

    for state in (TurnState(tainted=True), TurnState(history_external=True)):
        gate, context, _, backend, _ = env(tmp_path, mode="auto")
        asked.clear()
        await gate.dispatch(CALL, context, state, refuse)
        assert asked == ["computer_use"]
        assert backend.calls == []


@pytest.mark.parametrize("goal", ["", "x" * 501])
def test_goal_length_is_validated(goal: str) -> None:
    tool = discover_tools().get("computer_use")
    assert tool is not None
    with pytest.raises(ValidationError):
        tool.args_model(goal=goal)


async def test_disabled_says_off_and_asks_nothing(tmp_path: Path) -> None:
    gate, context, host, backend, vision = env(tmp_path, enabled=False)
    result = await gate.dispatch(CALL, context, TurnState(), never)
    assert result.success is False and "kapalı" in result.message
    assert host.sessions == 0 and backend.captures == 0 and vision.seen == []


async def test_missing_host_is_honest(tmp_path: Path) -> None:
    gate, context, _, backend, _ = env(tmp_path, host=False)
    result = await gate.dispatch(CALL, context, TurnState(), lambda *_: True)
    assert result.success is False and "masaüstü" in result.message
    assert backend.captures == 0


async def test_cloud_screen_refused_before_capture(tmp_path: Path) -> None:
    gate, context, host, backend, vision = env(tmp_path, provider="openrouter")
    result = await gate.dispatch(CALL, context, TurnState(), lambda *_: True)
    assert result.success is False and "allow_cloud_screen" in result.message
    assert host.sessions == 0 and backend.captures == 0 and vision.seen == []


async def test_cloud_allowed_with_permission(tmp_path: Path) -> None:
    gate, context, *_ = env(tmp_path, provider="openrouter", allow_cloud=True)
    result = await gate.dispatch(CALL, context, TurnState(), lambda *_: True)
    assert result.status == "sent"


async def test_happy_path_is_sent_never_success(tmp_path: Path) -> None:
    gate, context, host, backend, _ = env(tmp_path)
    asked: list[dict[str, Any]] = []

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        asked.append(arguments)
        return True

    state = TurnState()
    result = await gate.dispatch(CALL, context, state, approve)
    assert asked == [{"goal": "Not Defteri'ni aç"}]
    assert result.status == "sent" and result.success is False
    assert result.external is True and result.content_kind == "screen"
    assert result.data == {"reason": "done", "steps": 1}
    assert "Bitti." in result.message and "doğrulanmadı" in result.message
    assert backend.calls == [("click", 2, 1, "left", False)]
    assert host.sessions == 1 and host.steps == ["Adım 1: deneme"]
    assert state.tainted is True


async def test_cancelled_context_stops_before_next_action(tmp_path: Path) -> None:
    gate, context, host, backend, _ = env(tmp_path)
    context.cancelled.set()
    tool = gate.registry.get("computer_use")  # kapı iptal edilmiş bağlamı kendisi keser
    assert tool is not None
    result = await tool.execute(tool.args_model(goal="x"), context)
    assert result.data["reason"] == "stopped"
    assert backend.calls == []


# --- ComputerHost (Esc kısayolu sahte) -----------------------------------------------


class FakeHotkey:
    registered = True
    instances: list[FakeHotkey] = []

    def __init__(self, text: str, on_press: Callable[[], None]) -> None:
        self.text, self.on_press, self.stopped = text, on_press, False
        FakeHotkey.instances.append(self)

    def start(self) -> bool:
        return FakeHotkey.registered

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_hotkey(monkeypatch: pytest.MonkeyPatch) -> type[FakeHotkey]:
    FakeHotkey.instances = []
    FakeHotkey.registered = True
    monkeypatch.setattr(host_module, "GlobalHotkey", FakeHotkey)
    return FakeHotkey


def test_host_esc_sets_stop_and_status_is_cleared(fake_hotkey: type[FakeHotkey]) -> None:
    statuses: list[str] = []
    host = ComputerHost(lambda *_: True, statuses.append)
    with host.session() as (stop, on_step, approve):
        hotkey = fake_hotkey.instances[0]
        assert hotkey.text == "escape" and not stop.is_set()
        hotkey.on_press()
        assert stop.is_set()
        on_step("Adım 1: x")
        assert approve("computer_action", {}) is True
    assert hotkey.stopped and statuses == ["Adım 1: x", ""]


def test_host_reports_missing_esc_and_still_runs(fake_hotkey: type[FakeHotkey]) -> None:
    fake_hotkey.registered = False
    statuses: list[str] = []
    with ComputerHost(lambda *_: True, statuses.append).session():
        pass
    assert "Esc" in statuses[0] and statuses[-1] == ""


def test_host_allows_one_session_at_a_time(fake_hotkey: type[FakeHotkey]) -> None:
    host = ComputerHost(lambda *_: True, lambda _: None)
    with host.session(), pytest.raises(RuntimeError), host.session():
        pass
    with host.session():  # bitince yeni oturum açılabilir
        pass


async def test_host_session_works_inside_event_loop(fake_hotkey: type[FakeHotkey]) -> None:
    host = ComputerHost(lambda *_: True, lambda _: None)
    with host.session() as (stop, _, _):
        await asyncio.sleep(0)
        assert not stop.is_set()


def test_settings_enabled_requires_known_model() -> None:
    with pytest.raises(ValidationError):
        Settings(computer_use=ComputerUseConfig(enabled=True))
    with pytest.raises(ValidationError):
        Settings(models={"a": model()}, computer_use=ComputerUseConfig(enabled=True, model="b"))
    Settings(models={"a": model()}, computer_use=ComputerUseConfig(enabled=True, model="a"))
    Settings()  # kapalıyken model gerekmez


@pytest.mark.parametrize(
    "field,value",
    [("max_steps", 0), ("max_steps", 101), ("max_minutes", 0.1), ("screenshot_width", 100),
     ("step_delay_seconds", 4), ("bogus", 1)],
)
def test_config_bounds(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        ComputerUseConfig(**{field: value})


async def test_model_chain_falls_through_only_on_retryable_errors() -> None:
    # 429/hız sınırı geçicidir: ilk görsel model takılınca ikincisi aynı görüntüyle çalışır.
    from jarvis.brain.errors import ProviderError, RetryableProviderError
    from jarvis.brain.models import Completion
    from jarvis.tools.plugins.computer import _ModelChain

    class Fixed:
        def __init__(self, name: str, error: ProviderError | None = None) -> None:
            self.name, self.error = name, error

        async def complete(self, messages: Any, tools: Any, *, timeout: float) -> Completion:
            if self.error is not None:
                raise self.error
            return Completion(content=self.name)

    chain = _ModelChain([Fixed("a", RetryableProviderError("429")), Fixed("b")])  # type: ignore[list-item]
    assert (await chain.complete([], [], timeout=1)).content == "b"

    # Kalıcı hata zinciri durdurur: yedek model onu gizlemez.
    chain = _ModelChain([Fixed("a", ProviderError("401")), Fixed("b")])
    with pytest.raises(ProviderError, match="401"):
        await chain.complete([], [], timeout=1)
