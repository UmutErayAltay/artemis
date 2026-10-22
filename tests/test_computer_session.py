"""Bilgisayar kullanımı oturum döngüsü (ADR 0039): yalnız sahte arka uç/sağlayıcı/onay.

Gerçek fare, klavye, ekran ya da `jarvis.computer.windows` girdi işlevleri hiç çağrılmaz.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

import pytest

from jarvis.brain.models import Completion, Message
from jarvis.computer.session import ComputerSession, SessionResult
from jarvis.config.settings import ComputerUseConfig

OWN_PID = 4242
TYPED_TEXT = "yazilan-ornek-metin-123"  # denetime sızmaması gereken yazılan metin


class FakeBackend:
    def __init__(
        self,
        size: tuple[int, int] = (8, 4),
        foreground: tuple[str, int] = ("C:\\Program Files\\App\\app.exe", 100),
        monitor_rects: list[tuple[int, int, int, int]] | None = None,
    ) -> None:
        self.size = size
        self.foreground = foreground
        self.monitor_rects = (
            monitor_rects
            if monitor_rects is not None
            else [(0, 0, size[0], size[1])]
        )
        self.at: tuple[str, int] | None = None  # None: noktadaki pencere = ön plan
        self.calls: list[tuple[Any, ...]] = []
        self.captured_rects: list[tuple[int, int, int, int]] = []
        self.captures = 0
        self.fail: Exception | None = None

    def monitors(self) -> list[tuple[int, int, int, int]]:
        return list(self.monitor_rects)

    def capture(
        self, left: int, top: int, width: int, height: int
    ) -> tuple[bytes, int, int]:
        self.captures += 1
        self.captured_rects.append((left, top, width, height))
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
        mode: Callable[[], str] | None = None,
    ) -> None:
        self.esc_after_step = esc_after_step
        self.mode = mode
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
            mode=self.mode,
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
    first, second = (call[1].content for call in env.provider.seen)
    assert "Hedef: dosyayı aç" in first
    assert "Ekran sayısı: 1" in first
    assert "8x4 piksel" in first
    assert "Önceki adımlar:\n-" in first
    assert "Adım 1: click - deneme" in second
    assert env.provider.seen[0][-1].content == "Ekran 1 (8x4 piksel)"


async def test_click_uses_selected_monitor_and_provider_receives_each_screen() -> None:
    backend = FakeBackend(monitor_rects=[(0, 0, 8, 4), (8, 0, 8, 4)])
    config = ComputerUseConfig(
        # En küçük izinli genişlik; 8x4 görüntü küçültülmez → ölçek 1:1.
        enabled=True, model="m", screenshot_width=640, step_delay_seconds=0
    )
    env = Env(click(1, 1, screen=2), DONE, backend=backend, config=config)

    result = await env.run()

    assert result.reason == "done"
    assert backend.calls == [("click", 9, 1, "left", False)]
    assert backend.captured_rects[:2] == [(0, 0, 8, 4), (8, 0, 8, 4)]
    messages = env.provider.seen[0]
    images = [message for message in messages if message.image_png is not None]
    assert len(images) == 2
    assert [message.content for message in images] == [
        "Ekran 1 (8x4 piksel)",
        "Ekran 2 (8x4 piksel)",
    ]


async def test_invalid_screen_is_fed_back_as_block_not_session_end() -> None:
    backend = FakeBackend(monitor_rects=[(0, 0, 8, 4), (8, 0, 8, 4)])
    env = Env(click(screen=3), DONE, backend=backend)

    result = await env.run()

    assert result.reason == "done"
    assert backend.calls == []
    blocked = env.audit.named("computer_blocked")
    assert len(blocked) == 1 and "Ekran 3 yok" in blocked[0]["reason"]
    assert "Ekran 3 yok" in env.provider.seen[1][1].content  # model geri bildirimi gördü


async def test_screen_out_of_range_does_not_block_type_action() -> None:
    env = Env(act(type="type", text="merhaba", screen=3), DONE)

    result = await env.run()

    assert result.reason == "done"
    assert env.backend.calls == [("type", "merhaba")]


async def test_max_blocks_ends_session_only_on_third_block() -> None:
    blocked = (r"C:\Windows\System32\cmd.exe", 7)
    backend = FakeBackend(foreground=blocked)
    backend.at = blocked
    two = Env(click(), click(), DONE, backend=backend)
    assert (await two.run()).reason == "done"  # iki engel oturumu bitirmez
    assert len(two.audit.named("computer_blocked")) == 2

    three = Env(click(), click(), click(), DONE, backend=backend)
    assert (await three.run()).reason == "blocked"  # üçüncüde biter
    assert len(three.audit.named("computer_blocked")) == 3


async def test_mode_is_read_live_each_sensitive_step() -> None:
    mode = ["auto"]
    env = Env(click(sensitive=True), click(sensitive=True), DONE, approve_result=False,
              mode=lambda: mode[0])
    original = env._on_step

    def step_then_switch(text: str) -> None:
        original(text)
        mode[0] = "accept"  # panelden oto onay oturum sürerken kapatıldı

    env._on_step = step_then_switch  # type: ignore[method-assign]
    result = await env.run()

    assert result.reason == "rejected"  # ikinci hassas adım artık soruldu
    assert len(env.approvals) == 1
    assert len(env.backend.calls) == 1


async def test_auto_mode_executes_sensitive_action_without_approval() -> None:
    config = ComputerUseConfig(enabled=True, model="m", mode="auto")
    env = Env(click(sensitive=True), DONE, config=config)

    result = await env.run()

    assert result.reason == "done"
    assert env.approvals == []
    assert env.backend.calls == [("click", 2, 1, "left", False)]
    step = env.audit.named("computer_step")[0]
    assert step["auto_approved"] is True
    assert step["approved"] is None  # kimse sorulmadı; uydurma onay yazılmaz


async def test_esc_during_approval_prevents_the_action() -> None:
    env = Env(click(sensitive=True), DONE)
    original = env._approve

    def approve_then_esc(name: str, arguments: dict[str, Any]) -> bool:
        env.stop.set()  # onay penceresi açıkken Esc'e basılmış gibi
        return original(name, arguments)

    env._approve = approve_then_esc  # type: ignore[method-assign]
    result = await env.run()

    assert result.reason == "stopped"
    assert env.backend.calls == []


async def test_auto_mode_still_blocks_key_without_approval() -> None:
    config = ComputerUseConfig(enabled=True, model="m", mode="auto")
    env = Env(act(type="key", keys="win+r"), config=config)

    result = await env.run()

    assert result.reason == "blocked"
    assert env.approvals == []
    assert env.backend.calls == []


async def test_auto_mode_still_blocks_process_without_approval() -> None:
    blocked = (r"C:\Windows\System32\cmd.exe", 7)
    backend = FakeBackend(foreground=blocked)
    backend.at = blocked  # tıklama cmd.exe penceresine düşer
    config = ComputerUseConfig(enabled=True, model="m", mode="auto")
    # Engel modele söylenir; model inatla tekrarlarsa üçüncü engelde oturum biter.
    env = Env(click(sensitive=True), click(sensitive=True), click(sensitive=True),
              DONE, backend=backend, config=config)

    result = await env.run()

    assert result.reason == "blocked"
    assert env.approvals == []
    assert env.backend.calls == []


async def test_invalid_twice_ends_invalid_with_one_capture() -> None:
    env = Env("bu JSON değil", "{}")
    result = await env.run()
    assert result.reason == "invalid" and result.steps == 0
    assert env.backend.captures == 1 and len(env.provider.seen) == 2
    retry = env.provider.seen[1]
    assert [m.role for m in retry] == ["system", "user", "user", "assistant", "user"]
    assert retry[3].content == "bu JSON değil"
    # Neden + talimat: model hatasını görmeden aynı biçimi tekrarlıyordu.
    assert retry[4].content.startswith("Model geçerli bir eylem döndürmedi")
    assert retry[4].content.endswith("Yalnız tek bir geçerli JSON eylem döndür.")
    assert retry[2].image_png is not None  # yeniden istek aynı görüntüyü kullanır


async def test_invalid_once_then_ok() -> None:
    env = Env("sacma", DONE)
    assert (await env.run()).reason == "done"
    assert env.backend.captures == 1


@pytest.mark.parametrize(
    "foreground",
    [("C:\\Windows\\System32\\cmd.exe", 7), ("C:\\x\\jarvis.exe", OWN_PID), ("", 0)],
)
async def test_blocked_window_never_gets_input_and_third_block_ends(
    foreground: tuple[str, int],
) -> None:
    # Noktadaki pencere varsayılan olarak ön planla aynı (FakeBackend.at = None).
    env = Env(click(), click(), click(), DONE, backend=FakeBackend(foreground=foreground))
    result = await env.run()
    assert result.reason == "blocked" and result.steps == 0
    assert env.backend.calls == [] and env.approvals == []
    assert len(env.audit.named("computer_blocked")) == 3


async def test_single_block_is_reported_to_model_and_session_continues() -> None:
    backend = FakeBackend(foreground=(r"C:\Windows\System32\cmd.exe", 7))
    env = Env(click(), DONE, backend=backend)
    result = await env.run()
    assert result.reason == "done" and backend.calls == []
    # Son ileti ekran görüntüsüdür; metin istemi görüntülerden önce gelir.
    second_prompt = env.provider.seen[1][1].content
    assert "ENGELLENDİ (click)" in second_prompt


async def test_click_on_app_runs_even_when_jarvis_is_foreground() -> None:
    # Canlı hata: komut panelden verilince ön plan Jarvis'ti, Brave'e tıklama engelleniyordu.
    backend = FakeBackend(foreground=(r"C:\Jarvis\pythonw.exe", OWN_PID))
    backend.at = (r"C:\Program Files\BraveSoftware\brave.exe", 500)
    env = Env(click(), DONE, backend=backend)
    assert (await env.run()).reason == "done"
    assert backend.calls == [("click", 2, 1, "left", False)]


async def test_typing_goes_to_foreground_so_jarvis_foreground_blocks_it() -> None:
    backend = FakeBackend(foreground=(r"C:\Jarvis\pythonw.exe", OWN_PID))
    backend.at = (r"C:\Program Files\BraveSoftware\brave.exe", 500)
    env = Env(act(type="type", text="merhaba", reason="yaz"), DONE, backend=backend)
    assert (await env.run()).reason == "done"
    assert backend.calls == []
    assert env.audit.named("computer_blocked")[0]["action"] == "type"


async def test_blocked_key_combo() -> None:
    keys = act(type="key", keys="win+r")
    env = Env(keys, keys, keys, DONE)
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
    assert step["auto_approved"] is False


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
    env = Env(act(type="type", text=TYPED_TEXT, reason="Alana yaz"), DONE)
    await env.run()
    assert env.backend.calls == [("type", TYPED_TEXT)]
    step = env.audit.named("computer_step")[0]
    assert step["text_length"] == len(TYPED_TEXT) and step["action"] == "type"
    assert TYPED_TEXT not in repr(env.audit.events)
    for _, fields in env.audit.events:
        assert not any(isinstance(value, (bytes, bytearray)) for value in fields.values())
        assert "png" not in repr(fields).lower()


async def test_click_on_blocked_window_under_point_is_blocked_even_if_foreground_is_fine() -> None:
    # Ön planda izinli uygulama var ama tıklama noktasında arkadaki terminal duruyor.
    backend = FakeBackend()
    backend.at = (r"C:\Windows\System32\WindowsTerminal.exe", 300)
    env = Env(click(), DONE, backend=backend)
    await env.run()
    assert backend.calls == []
    assert "windowsterminal" in env.audit.named("computer_blocked")[0]["reason"]


async def test_target_is_rechecked_after_approval() -> None:
    # Onay penceresi kapanınca odak Jarvis'e geçti: onaylanmış adım bile çalışmamalı.
    env = Env(act(type="type", text="merhaba", sensitive=True), DONE)

    def approve_and_focus_jarvis(name: str, arguments: dict[str, Any]) -> bool:
        env.approvals.append((name, arguments))
        env.backend.foreground = (r"C:\Jarvis\pythonw.exe", OWN_PID)
        return True

    env._approve = approve_and_focus_jarvis  # type: ignore[method-assign]
    await env.run()
    assert len(env.approvals) == 1
    assert env.backend.calls == []
    assert env.audit.named("computer_blocked")[0]["action"] == "type"


async def test_typing_with_harmless_coordinates_still_checks_foreground() -> None:
    # Güvenlik taraması: x/y taşıyan type eylemi noktadaki zararsız pencereyle ön plandaki
    # terminali denetimden kaçırabiliyordu. Klavye girdisi her zaman ön plana göre denetlenir.
    backend = FakeBackend(foreground=(r"C:\Windows\System32\cmd.exe", 7))
    backend.at = (r"C:\Program Files\BraveSoftware\brave.exe", 500)
    sneaky = act(type="type", text="calc", x=2, y=1, reason="yaz")
    keys = act(type="key", keys="ctrl+v", x=2, y=1, reason="yapıştır")
    env = Env(sneaky, keys, sneaky, DONE, backend=backend)
    result = await env.run()
    assert result.reason == "blocked"
    assert backend.calls == []


async def test_zero_monitors_ends_error_without_backend_input() -> None:
    # Gap 1: hiç monitör yoksa oturum "error" ile biter, backend girdi almaz.
    env = Env(DONE, backend=FakeBackend(monitor_rects=[]))
    result = await env.run()
    assert result.reason == "error" and "Ekran" in result.summary
    assert env.backend.calls == []


class _CaptureFailingBackend(FakeBackend):
    """İkinci monitörün capture'ı OSError fırlatan sahte arka uç (Gap 2)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._capture_count = 0

    def capture(self, left: int, top: int, width: int, height: int) -> tuple[bytes, int, int]:
        self._capture_count += 1
        if self._capture_count == 2:
            raise OSError("alım başarısız")
        return super().capture(left, top, width, height)


async def test_capture_failure_on_second_monitor_is_honest_error() -> None:
    # Gap 2: ikinci monitörde görüntü alınamazsa oturum "error" ile biter.
    backend = _CaptureFailingBackend(monitor_rects=[(0, 0, 8, 4), (8, 0, 8, 4)])
    env = Env(DONE, backend=backend)
    result = await env.run()
    assert result.reason == "error"
    assert "Ekran görüntüsü alınamadı" in result.summary
    assert env.backend.calls == []
