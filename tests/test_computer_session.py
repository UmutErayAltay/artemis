"""Bilgisayar kullanımı oturum döngüsü (ADR 0039): yalnız sahte arka uç/sağlayıcı/onay.

Gerçek fare, klavye, ekran ya da `jarvis.computer.windows` girdi işlevleri hiç çağrılmaz.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from jarvis.brain.models import Completion, Message
from jarvis.computer.session import ComputerSession, SessionResult
from jarvis.config.settings import ComputerUseConfig

OWN_PID = 4242
SECRET = "gizli-parola-123"


class FakeBackend:
    def __init__(
        self,
        size: tuple[int, int] = (8, 4),
        foreground: tuple[str, int] = ("C:\\Program Files\\App\\app.exe", 100),
    ) -> None:
        self.size = size
        self.foreground = foreground
        self.at: tuple[str, int] | None = None  # None: noktadaki pencere = ön plan
        self.calls: list[tuple[Any, ...]] = []
        self.captures = 0
        self.fail: Exception | None = None

    def primary_screen_size(self) -> tuple[int, int]:
        return self.size

    def capture_primary(self) -> tuple[bytes, int, int]:
        self.captures += 1
        width, height = self.size
        return bytes(width * height * 4), width, height

    def foreground_process(self) -> tuple[str, int]:
        return self.foreground

    def process_at(self, x: int, y: int) -> tuple[str, int]:
        return self.at if self.at is not None else self.foreground

    def _record(self, *call: Any) -> None:
        if self.fail is not None:
            raise self.fail
        self.calls.append(call)

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        self._record("click", x, y, button, double)

    def scroll(self, x: int, y: int, notches: int) -> None:
        self._record("scroll", x, y, notches)

    def type_text(self, text: str) -> None:
        self._record("type", text)

    def press_keys(self, keys: tuple[str, ...]) -> None:
        self._record("keys", keys)


class ScriptedVision:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.seen: list[list[Message]] = []
        self.timeouts: list[float] = []

    async def complete(self, messages: list[Message], tools: list[Any], *, timeout: float):
        self.seen.append(list(messages))
        self.timeouts.append(timeout)
        # Betik biterse son yanıt tekrarlanır (sınır testleri için).
        content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        return Completion(content=content)


class Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [fields for name, fields in self.events if name == event]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def act(**fields: Any) -> str:
    fields.setdefault("reason", "deneme")
    return json.dumps(fields)


def click(x: int = 2, y: int = 1, **extra: Any) -> str:
    return act(type="click", x=x, y=y, **extra)


DONE = act(type="done", text="Bitti.")


class Env:
    def __init__(
        self,
        *contents: str,
        backend: FakeBackend | None = None,
        approve_result: bool = True,
        config: ComputerUseConfig | None = None,
        deadline: float | None = None,
        esc_after_step: bool = False,
    ) -> None:
        self.esc_after_step = esc_after_step
        self.backend = backend or FakeBackend()
        self.provider = ScriptedVision(*contents)
        self.audit = Audit()
        self.clock = Clock()
        self.stop = threading.Event()
        self.steps: list[str] = []
        self.approvals: list[tuple[str, dict[str, Any]]] = []
        self.approve_result = approve_result
        self.deadline = deadline
        self.config = config or ComputerUseConfig(
            enabled=True, model="m", step_delay_seconds=0.6
        )

    def _approve(self, name: str, arguments: dict[str, Any]) -> bool:
        self.approvals.append((name, arguments))
        return self.approve_result

    def _on_step(self, text: str) -> None:
        self.steps.append(text)
        if self.esc_after_step:
            self.stop.set()  # Esc'e basılmış gibi

    async def run(self, goal: str = "bir şey yap") -> SessionResult:
        session = ComputerSession(
            provider=self.provider,
            backend=self.backend,
            approve=self._approve,
            audit=self.audit,
            config=self.config,
            own_pid=OWN_PID,
            stop=self.stop,
            on_step=self._on_step,
            provider_timeout=15.0,
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        return await session.run(goal, "t1", self.deadline)


async def test_done_ends_without_input() -> None:
    env = Env(DONE)
    result = await env.run()
    assert result == SessionResult("done", "Bitti.", 0)
    assert env.backend.calls == []
    assert env.audit.named("computer_session_end") == [
        {"turn_id": "t1", "reason": "done", "steps": 0}
    ]


async def test_ask_user_returns_question() -> None:
    env = Env(act(type="ask_user", text="Hangi hesap?"))
    result = await env.run()
    assert (result.reason, result.summary) == ("ask_user", "Hangi hesap?")


async def test_click_then_done_sends_only_latest_image_on_user_message() -> None:
    env = Env(click(button="right", double=True), DONE)
    result = await env.run("dosyayı aç")
    assert result.reason == "done" and result.steps == 1
    assert env.backend.calls == [("click", 2, 1, "right", True)]
    assert env.steps == ["Adım 1: deneme"]
    assert env.backend.captures == 2
    for call in env.provider.seen:
        with_image = [m for m in call if m.image_png is not None]
        assert len(with_image) == 1 and with_image[0] is call[-1] and with_image[0].role == "user"
        assert with_image[0].image_png.startswith(b"\x89PNG")  # type: ignore[union-attr]
        assert call[0].role == "system"
    first, second = (call[-1].content for call in env.provider.seen)
    assert "Hedef: dosyayı aç" in first and "8x4 piksel" in first and "Önceki adımlar:\n-" in first
    assert "Adım 1: click - deneme" in second


async def test_invalid_twice_ends_invalid_with_one_capture() -> None:
    env = Env("bu JSON değil", "{}")
    result = await env.run()
    assert result.reason == "invalid" and result.steps == 0
    assert env.backend.captures == 1 and len(env.provider.seen) == 2
    retry = env.provider.seen[1]
    assert [m.role for m in retry] == ["system", "user", "assistant", "user"]
    assert retry[2].content == "bu JSON değil"
    assert retry[3].content == "Yalnız tek bir geçerli JSON eylem döndür."
    assert retry[1].image_png is not None  # yeniden istek aynı görüntüyü kullanır


async def test_invalid_once_then_ok() -> None:
    env = Env("sacma", DONE)
    assert (await env.run()).reason == "done"
    assert env.backend.captures == 1


@pytest.mark.parametrize(
    "foreground",
    [("C:\\Windows\\System32\\cmd.exe", 7), ("C:\\x\\jarvis.exe", OWN_PID), ("", 0)],
)
async def test_blocked_foreground_stops_before_input(foreground: tuple[str, int]) -> None:
    env = Env(click(), DONE, backend=FakeBackend(foreground=foreground))
    result = await env.run()
    assert result.reason == "blocked" and result.steps == 0
    assert env.backend.calls == [] and env.approvals == []


async def test_blocked_key_combo() -> None:
    env = Env(act(type="key", keys="win+r"), DONE)
    result = await env.run()
    assert result.reason == "blocked" and "Windows" in result.summary
    assert env.backend.calls == []


async def test_sensitive_asks_with_details_and_rejection_stops() -> None:
    env = Env(click(sensitive=True, reason="Gönder düğmesi"), DONE, approve_result=False)
    result = await env.run()
    assert result.reason == "rejected"
    assert env.backend.calls == []
    assert env.approvals == [
        (
            "computer_action",
            {
                "eylem": "click",
                "konum": [2, 1],
                "metin": None,
                "tuslar": None,
                "gerekce": "Gönder düğmesi",
                "pencere": "app.exe",
            },
        )
    ]


async def test_sensitive_approved_executes_and_audits() -> None:
    env = Env(click(sensitive=True), DONE)
    assert (await env.run()).reason == "done"
    assert env.backend.calls == [("click", 2, 1, "left", False)]
    step = env.audit.named("computer_step")[0]
    assert step["sensitive"] is True and step["approved"] is True


async def test_alt_f4_is_always_sensitive() -> None:
    env = Env(act(type="key", keys="alt+f4"), DONE, approve_result=False)
    assert (await env.run()).reason == "rejected"
    assert len(env.approvals) == 1 and env.approvals[0][1]["tuslar"] == "alt+f4"
    assert env.backend.calls == []


async def test_stop_after_first_step_prevents_next_action() -> None:
    env = Env(click(), click(), DONE, esc_after_step=True)
    result = await env.run()
    assert result.reason == "stopped" and result.steps == 1
    assert len(env.backend.calls) == 1


async def test_stop_already_set_does_nothing() -> None:
    env = Env(click())
    env.stop.set()
    assert (await env.run()).reason == "stopped"
    assert env.backend.captures == 0 and env.provider.seen == []


async def test_max_steps_limit() -> None:
    env = Env(click(), config=ComputerUseConfig(enabled=True, model="m", max_steps=2))
    result = await env.run()
    assert result.reason == "limit" and result.steps == 2
    assert len(env.backend.calls) == 2


async def test_deadline_timeout_uses_two_second_margin() -> None:
    env = Env(click(), deadline=3.0)  # sınır = 1.0 sn; adım arası 0.6 sn
    result = await env.run()
    assert result.reason == "timeout" and result.steps == 2
    assert max(env.provider.timeouts) <= 1.0


async def test_max_minutes_timeout() -> None:
    config = ComputerUseConfig(enabled=True, model="m", max_minutes=0.5, step_delay_seconds=3)
    env = Env(click(), config=config)
    result = await env.run()
    assert result.reason == "timeout" and result.steps == 10  # 30 sn / 3 sn


async def test_coordinates_are_scaled_to_screen() -> None:
    # 1280x8 ekran 640 piksel genişliğe küçülür (640x4); (320, 2) gerçek (640, 4)'e düşer.
    backend = FakeBackend(size=(1280, 8))
    config = ComputerUseConfig(
        enabled=True, model="m", screenshot_width=640, step_delay_seconds=0
    )
    env = Env(click(320, 2), act(type="scroll", x=320, y=2, dy=3), DONE, backend=backend,
              config=config)
    await env.run()
    assert backend.calls == [("click", 640, 4, "left", False), ("scroll", 640, 4, 3)]
    assert "640x4 piksel" in env.provider.seen[0][-1].content
    assert env.audit.named("computer_step")[0]["x"] == 640


async def test_out_of_bounds_coordinate_is_invalid() -> None:
    env = Env(click(9, 1), DONE)  # görüntü 8x4
    result = await env.run()
    assert result.reason == "invalid" and env.backend.calls == []


async def test_wait_sleeps_without_touching_backend() -> None:
    env = Env(act(type="wait", seconds=2), DONE)
    result = await env.run()
    assert result.reason == "done" and result.steps == 1
    assert env.backend.calls == []
    assert 2 in env.clock.sleeps


async def test_backend_error_is_reported() -> None:
    backend = FakeBackend()
    backend.fail = OSError("girdi engellendi")
    env = Env(click(), DONE, backend=backend)
    result = await env.run()
    assert result.reason == "error" and "girdi engellendi" in result.summary
    assert env.steps == []


async def test_audit_never_holds_typed_text_or_bytes() -> None:
    env = Env(act(type="type", text=SECRET, reason="Alana yaz"), DONE)
    await env.run()
    assert env.backend.calls == [("type", SECRET)]
    step = env.audit.named("computer_step")[0]
    assert step["text_length"] == len(SECRET) and step["action"] == "type"
    assert SECRET not in repr(env.audit.events)
    for _, fields in env.audit.events:
        assert not any(isinstance(value, (bytes, bytearray)) for value in fields.values())
        assert "png" not in repr(fields).lower()


async def test_click_on_blocked_window_under_point_is_blocked_even_if_foreground_is_fine() -> None:
    # Ön planda izinli uygulama var ama tıklama noktasında arkadaki terminal duruyor.
    backend = FakeBackend()
    backend.at = (r"C:\Windows\System32\WindowsTerminal.exe", 300)
    env = Env(click(), DONE, backend=backend)
    result = await env.run()
    assert result.reason == "blocked"
    assert backend.calls == []


async def test_target_is_rechecked_after_approval() -> None:
    # Onay penceresi kapanınca odak Jarvis'e geçti: onaylanmış adım bile çalışmamalı.
    env = Env(act(type="type", text="merhaba", sensitive=True), DONE)

    def approve_and_focus_jarvis(name: str, arguments: dict[str, Any]) -> bool:
        env.approvals.append((name, arguments))
        env.backend.foreground = (r"C:\Jarvis\pythonw.exe", OWN_PID)
        return True

    env._approve = approve_and_focus_jarvis  # type: ignore[method-assign]
    result = await env.run()
    assert len(env.approvals) == 1
    assert result.reason == "blocked"
    assert env.backend.calls == []
