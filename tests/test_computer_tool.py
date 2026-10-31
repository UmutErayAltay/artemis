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

import artemis.channels.desktop.computer_host as host_module
from artemis.brain.models import ToolCall
from artemis.channels.desktop.computer_host import ComputerHost, SessionHandles
from artemis.config.settings import ComputerUseConfig, ModelConfig, PerceptionConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.base import Risk, ToolContext
from artemis.tools.registry import discover_tools
from tests.test_computer_session import DONE, NO, YES, FakeBackend, ScriptedVision, click

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
    replies: tuple[str, ...] = (click(), DONE, YES),
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


async def test_auto_mode_skips_the_start_and_its_taint(tmp_path: Path) -> None:
    # Kullanıcının bilerek açtığı kipte bilgisayar kullanımı başlangıcı sorulmaz. Ekran
    # adımları her tur dış içerik ürettiği için tur içi/önceki tur lekesi de bu aracı
    # durdurmaz (ADR 0039 ek karar); başka araçların kuralı değişmez.
    for state in (TurnState(), TurnState(tainted=True), TurnState(history_external=True)):
        gate, context, *_ = env(tmp_path, mode="auto")
        result = await gate.dispatch(CALL, context, state, never)
        assert result.status == "ok"


async def test_auto_mode_does_not_relax_other_tools(tmp_path: Path) -> None:
    # Aynı kipte başka bir yan etkili araç dış içerikten sonra yine sorulur.
    gate, context, *_ = env(tmp_path, mode="auto")
    asked: list[str] = []

    def refuse(name: str, arguments: dict[str, Any]) -> bool:
        asked.append(name)
        return False

    # Otomatik kipin kapsamı bilgisayarını kontrol eden iki araçtır; diğerleri kural 4'te.
    for name, arguments in (
        ("volume_set", {"level": 10}),
        ("window_list", {}),
    ):
        gate, context, *_ = env(tmp_path, mode="auto")
        asked.clear()
        await gate.dispatch(
            ToolCall(id="t", name=name, arguments=arguments),
            context,
            TurnState(tainted=True),
            refuse,
        )
        if arguments:  # window_list READ_ONLY: onay sorulmaz, yalnız yan etkililer sorar
            assert asked == [name]


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
    assert result.status == "ok"


async def test_happy_path_is_verified_on_screen(tmp_path: Path) -> None:
    gate, context, host, backend, _ = env(tmp_path)
    asked: list[dict[str, Any]] = []

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        asked.append(arguments)
        return True

    state = TurnState()
    result = await gate.dispatch(CALL, context, state, approve)
    assert asked == [{"goal": "Not Defteri'ni aç"}]
    assert result.status == "ok" and result.success is True
    assert result.external is True and result.content_kind == "screen"
    assert result.data == {"reason": "done", "steps": 1, "verified": True}
    assert "Bitti." in result.message and "Ekranda doğrulandı" in result.message
    assert backend.calls == [("click", 2, 1, "left", False)]
    assert host.sessions == 1 and host.steps == ["Adım 1: deneme"]
    assert state.tainted is True


async def test_unverified_done_stays_sent(tmp_path: Path) -> None:
    for replies in ((click(), DONE, NO), (click(), DONE, "sacma", "sacma")):
        gate, context, *_ = env(tmp_path, replies=replies)
        result = await gate.dispatch(CALL, context, TurnState(), lambda *_: True)
        assert result.status == "sent" and result.success is False
        assert result.data == {"reason": "done", "steps": 1, "verified": False}
        assert "ekranda doğrulanamadı" in result.message
        assert result.external is True and result.content_kind == "screen"


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


def test_host_esc_also_cancels_turn(fake_hotkey: type[FakeHotkey]) -> None:
    calls: list[bool] = []
    host = ComputerHost(lambda *_: True, lambda _: None, on_escape=lambda: calls.append(True))
    with host.session() as (stop, _, _):
        fake_hotkey.instances[0].on_press()
        # Bayrak önce kalkar: tur iptali yetişmese de sıradaki eylem çalışmaz.
        assert stop.is_set() and calls == [True]


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
    from artemis.brain.errors import ProviderError, RetryableProviderError
    from artemis.brain.models import Completion
    from artemis.tools.plugins.computer import _ModelChain

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


async def test_runtime_mode_switch_reaches_the_tool_context(tmp_path: Path) -> None:
    # Üst çubuktaki düğme tur başlamadan runtime ayarını değiştirir: sonraki araç çağrısı
    # otomatik kipte çalışmalı, Uygula beklememeli.
    settings = Settings(
        workspace=tmp_path,
        models={"vision": model("ollama")},
        computer_use=ComputerUseConfig(
            enabled=True, model="vision", step_delay_seconds=0, mode="accept"
        ),
    )
    context = ToolContext(tmp_path, settings, AuditLog(tmp_path / "audit.jsonl"), turn_id="t")
    tool = discover_tools().get("computer_use")
    assert tool is not None
    args = tool.args_model(goal="x")
    assert tool.risk_for(args, context) is Risk.CONFIRM_REQUIRED

    computer_use = settings.computer_use.model_copy(update={"mode": "auto"})
    context.settings = settings.model_copy(update={"computer_use": computer_use})

    assert tool.risk_for(args, context) is Risk.SIDE_EFFECT
    assert context.settings.computer_use.mode == "auto"


async def test_model_chain_skips_a_rate_limited_model_for_a_while() -> None:
    # Ölçüldü: haiku dakikalarca 429 verirken her adımda denemek boşa istek harcıyordu.
    from artemis.brain.errors import ProviderError, RateLimitedError
    from artemis.brain.models import Completion
    from artemis.tools.plugins.computer import _ModelChain

    class Fixed:
        def __init__(self, name: str, error: ProviderError | None = None) -> None:
            self.name, self.error, self.calls = name, error, 0

        async def complete(self, messages: Any, tools: Any, *, timeout: float) -> Completion:
            self.calls += 1
            if self.error is not None:
                raise self.error
            return Completion(content=self.name)

    slow = Fixed("a", RateLimitedError("429"))
    fast = Fixed("b")
    chain = _ModelChain([slow, fast])  # type: ignore[list-item]
    for _ in range(3):
        assert (await chain.complete([], [], timeout=1)).content == "b"
    assert slow.calls == 1 and fast.calls == 3


async def test_auto_mode_skips_window_move_on_tainted_turn(tmp_path: Path) -> None:
    # "2. ekrana taşı" / tam ekran adımları da otomatik kipte sorulmaz.
    gate, context, *_ = env(tmp_path, mode="auto")
    call = ToolCall(
        id="m",
        name="window_control",
        arguments={"window_id": 5, "action": "move_to_screen", "screen": 2},
    )
    result = await gate.dispatch(call, context, TurnState(tainted=True), never)
    assert result.status != "denied"
    import json

    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "approval_skipped" in events and "approval" not in events


def test_empty_computer_model_uses_the_main_brain(tmp_path: Path) -> None:
    # Boş `computer_use.model` = ana beyin: görüntü o an seçili ana modele gider.
    from artemis.tools.plugins.computer import vision_models

    base = Settings(
        workspace=tmp_path,
        models={"main": model("ollama"), "other": model("ollama")},
        primary_model="main",
    )
    follow = base.model_copy(
        update={"computer_use": ComputerUseConfig(enabled=True, model="")}
    )
    assert vision_models(follow) == ("main",)

    pinned = base.model_copy(
        update={
            "computer_use": ComputerUseConfig(
                enabled=True, model="other", fallback_models=("main", "other")
            )
        }
    )
    # Sabit model seçilirse o önce gelir; yedekler yinelenmeden eklenir.
    assert vision_models(pinned) == ("other", "main")


class _Fixed:
    """Zincir testleri için: her çağrıda verilen hatayı atar ya da adını döndürür."""

    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name, self.error, self.calls = name, error, 0

    async def complete(self, messages: Any, tools: Any, *, timeout: float) -> Any:
        from artemis.brain.models import Completion

        self.calls += 1
        if self.error is not None:
            raise self.error
        return Completion(content=self.name)


async def test_model_chain_does_not_cool_down_on_non_429_retryable_errors() -> None:
    from artemis.brain.errors import RetryableProviderError
    from artemis.tools.plugins.computer import _ModelChain

    flaky = _Fixed("a", RetryableProviderError("5xx"))
    chain = _ModelChain([flaky, _Fixed("b")])  # type: ignore[list-item]
    for _ in range(3):
        assert (await chain.complete([], [], timeout=1)).content == "b"
    assert flaky.calls == 3  # bekleme yok: her adımda yeniden denenir


async def test_model_chain_all_rate_limited_then_cooled_down_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from artemis.brain.errors import ProviderError, RateLimitedError
    from artemis.tools.plugins import computer as module

    now = [1000.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    first, second = _Fixed("a", RateLimitedError("429")), _Fixed("b", RateLimitedError("429"))
    chain = module._ModelChain([first, second])  # type: ignore[list-item]

    with pytest.raises(RateLimitedError):  # ilk tur: gerçek son hata yükselir
        await chain.complete([], [], timeout=1)
    with pytest.raises(ProviderError, match="hız sınırında") as info:  # sonra hiç istek yok
        await chain.complete([], [], timeout=1)
    assert not isinstance(info.value, RateLimitedError)
    assert (first.calls, second.calls) == (1, 1)

    now[0] += module._COOLDOWN_SECONDS + 1  # bekleme bitince yeniden denenir
    with pytest.raises(RateLimitedError):
        await chain.complete([], [], timeout=1)
    assert (first.calls, second.calls) == (2, 2)


async def test_model_chain_cooldown_counts_from_the_failure_not_the_loop_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from artemis.brain.errors import RateLimitedError
    from artemis.tools.plugins import computer as module

    now = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])

    class Slow(_Fixed):
        async def complete(self, messages: Any, tools: Any, *, timeout: float) -> Any:
            now[0] += 50.0  # yavaş istek, sonra 429
            return await super().complete(messages, tools, timeout=timeout)

    slow = Slow("a", RateLimitedError("429"))
    chain = module._ModelChain([slow, _Fixed("b")])  # type: ignore[list-item]
    await chain.complete([], [], timeout=1)  # hata t=50'de
    now[0] = module._COOLDOWN_SECONDS + 10  # döngü başından 130, hatadan 80 sn
    await chain.complete([], [], timeout=1)
    assert slow.calls == 1  # hâlâ beklemede


def _chain_env(
    tmp_path: Path, providers: dict[str, str], allow_cloud: bool
) -> tuple[SafetyGate, ToolContext, FakeHost, FakeBackend]:
    names = tuple(providers)
    settings = Settings(
        workspace=tmp_path,
        models={name: model(kind) for name, kind in providers.items()},
        primary_model=names[0],
        computer_use=ComputerUseConfig(
            enabled=True, model=names[0], fallback_models=names[1:], step_delay_seconds=0
        ),
        perception=PerceptionConfig(allow_cloud_screen=allow_cloud),
    )
    context = ToolContext(tmp_path, settings, AuditLog(tmp_path / "audit.jsonl"), turn_id="t1")
    host, backend = FakeHost(), FakeBackend()
    context.services["computer_host"] = host
    context.services["computer_backend"] = backend
    return SafetyGate(discover_tools()), context, host, backend


async def test_cloud_fallback_is_dropped_from_chain_without_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Yalnız ilk model denetlenseydi yerel ilk + bulut yedek izinsiz görüntü alırdı.
    from artemis.tools.plugins import computer as module

    gate, context, host, _ = _chain_env(
        tmp_path, {"local": "ollama", "cloud": "openrouter"}, allow_cloud=False
    )
    built: list[str] = []

    class Recording:
        def __init__(self, settings: Settings, audit: Any) -> None:
            built.append(settings.primary_model)

        async def complete(self, messages: Any, tools: Any, *, timeout: float) -> Any:
            from artemis.brain.models import Completion

            return Completion(content=DONE)

    monkeypatch.setattr(module, "ProviderRouter", Recording)
    tool = gate.registry.get("computer_use")
    assert tool is not None
    result = await tool.execute(tool.args_model(goal="x"), context)
    assert built == ["local"]  # bulut model zincire hiç girmedi
    assert host.sessions == 1 and result.data["reason"] == "done"


async def test_all_cloud_models_without_permission_refuse_before_capture(tmp_path: Path) -> None:
    gate, context, host, backend = _chain_env(
        tmp_path, {"a": "openrouter", "b": "openrouter"}, allow_cloud=False
    )
    tool = gate.registry.get("computer_use")
    assert tool is not None
    result = await tool.execute(tool.args_model(goal="x"), context)
    assert "allow_cloud_screen" in result.message
    assert host.sessions == 0 and backend.captures == 0


async def test_approval_skipped_only_when_approval_was_actually_needed(tmp_path: Path) -> None:
    import json

    def events() -> list[str]:
        path = tmp_path / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["event"] for line in path.read_text("utf-8").splitlines()]

    # Temiz turda computer_use otomatik kipte zaten SIDE_EFFECT: sorulacak onay yoktu.
    gate, context, *_ = env(tmp_path, mode="auto")
    result = await gate.dispatch(CALL, context, TurnState(), never)
    assert result.status == "ok"
    assert "approval_skipped" not in events()

    # Lekeli turda onay gerekirdi: atlama denetime yazılır.
    gate, context, *_ = env(tmp_path, mode="auto")
    await gate.dispatch(CALL, context, TurnState(tainted=True), never)
    assert "approval_skipped" in events()


async def test_memory_is_shared_within_same_turn_and_reset_for_new_turn(tmp_path: Path) -> None:
    gate, context, _host, _backend, vision = env(
        tmp_path, mode="auto", replies=(click(), DONE, YES, DONE, YES, DONE, YES)
    )
    state = TurnState()  # aynı kullanıcı mesajı = aynı turn_id
    await gate.dispatch(CALL, context, state, never)  # ilk çağrı: tık + done
    await gate.dispatch(CALL, context, state, never)  # aynı mesaj: bellek taşınır
    seen = [str(call[1].content) for call in vision.seen]
    first_prompt_of_second_call = next(
        text for text in seen if "Aynı mesajdaki önceki computer_use çağrısı" in text
    )
    assert "Adım 1: click" in first_prompt_of_second_call
    before = len(vision.seen)
    await gate.dispatch(CALL, context, TurnState(), never)  # yeni mesaj: bellek düşer
    fresh = str(vision.seen[before][1].content)
    assert "Aynı mesajdaki önceki" not in fresh and "Adım 1" not in fresh
