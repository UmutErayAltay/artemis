"""Telegram'dan bilgisayar kullanımı (ADR 0053): kip, /dur, ekran kilidi, kare gizliliği.

Ağ yalnız loopback sahte Telegram sunucusudur; ekran, fare ve görsel model sahtedir, Esc
kısayolu kaydı yamalıdır. Gerçek token, bot, ekran ya da girdi yoktur.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml
from telegram_fake import (
    CHAT_ID,
    FAKE_TOKEN,
    Harness,
    build_harness,
    method_bodies,
    sent_bodies,
)
from test_telegram import answer, approval_requests, call, registry_with, scripted

import artemis.channels.desktop.computer_host as host_module
from artemis.brain.models import Completion, Message
from artemis.channels.desktop.computer_host import ComputerHost
from artemis.channels.runtime import Runtime
from artemis.channels.telegram.api import TelegramError
from artemis.channels.telegram.channel import TelegramChannel
from artemis.computer.session import SCREEN_LOCK, ScreenBusyError
from artemis.config.settings import ComputerUseConfig, ModelConfig, PerceptionConfig
from artemis.tools.plugins.computer import ComputerUse
from artemis.tools.plugins.filesystem import ReadFile
from tests.test_computer_session import DONE, YES, FakeBackend, ScriptedVision, click

Mode = Literal["accept", "auto"]
GOAL = "Not Defteri'ni aç"


class _NoHotkey:
    def __init__(self, text: str, on_press: Callable[[], None]) -> None:
        pass

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def no_real_hotkey(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_module, "GlobalHotkey", _NoHotkey)


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Harness]]:
    built: list[Harness] = []

    def factory(
        *, mode: Mode = "accept", allow_cloud: bool = True, flag: bool = True
    ) -> Harness:
        harness = build_harness(
            tmp_path / f"case-{len(built)}",
            monkeypatch,
            send_computer_screenshot=flag,
            models={
                "vision": ModelConfig(
                    provider="ollama", model="vision-1", input_per_million=0, output_per_million=0
                )
            },
            computer_use=ComputerUseConfig(
                enabled=True, model="vision", step_delay_seconds=0, mode=mode
            ),
            perception=PerceptionConfig(allow_cloud_screen=allow_cloud),
        )
        built.append(harness)
        return harness

    yield factory
    for harness in built:
        harness.close()
    assert not SCREEN_LOCK.locked()  # hiçbir test ekran kilidini sızdırmamalı


def write_mode(harness: Harness, mode: str) -> Path:
    """Panelin yazdığı config: kip her turdan önce buradan okunur."""
    path = harness.root / "config.yaml"
    path.write_text(
        yaml.safe_dump({
            "workspace": str(harness.workspace),
            "state_dir": str(harness.root / "state"),
            "audit_path": str(harness.audit_path),
            "models": {"vision": {"provider": "ollama", "model": "vision-1",
                                  "input_per_million": 0, "output_per_million": 0}},
            "computer_use": {"enabled": True, "model": "vision", "mode": mode},
        }),
        encoding="utf-8",
    )
    return path


def build(
    harness: Harness,
    main: Completion | None = None,
    *,
    mode: Mode = "accept",
    vision: ScriptedVision | None = None,
    extra: tuple[Any, ...] = (),
    replies: tuple[Completion, ...] | None = None,
) -> tuple[TelegramChannel, Runtime, FakeBackend, ScriptedVision]:
    script = replies or (call("computer_use", "c1", goal=GOAL), answer("İş bitti."))
    channel, runtime = harness.channel(scripted(*script), registry_with(ComputerUse, *extra))
    backend, vision = FakeBackend(), vision or ScriptedVision(click(), DONE, YES)
    runtime.context.services["computer_backend"] = backend
    runtime.context.services["computer_provider"] = vision
    channel.enable_computer_use(write_mode(harness, mode))
    return channel, runtime, backend, vision


def say(text: str) -> dict[str, Any]:
    from telegram_fake import make_message

    return make_message(1, text, date=int(time.time()))


def photos(harness: Harness) -> list[dict[str, Any]]:
    return method_bodies(harness.fake, "sendPhoto")


def audit_photos(harness: Harness) -> list[dict[str, Any]]:
    return [
        event
        for event in harness.audit_events()
        if event["event"] == "telegram_out" and event.get("kind") == "photo"
    ]


# Kip: masaüstüyle aynı ----------------------------------------------------------------------


async def test_auto_mode_runs_without_asking_and_sends_last_frame(make) -> None:
    harness = make(mode="auto")
    channel, _, backend, _ = build(harness, mode="auto")

    await channel.handle_update(say("not defterini aç"))

    assert approval_requests(harness.fake) == []  # düğme sorulmadı
    assert backend.calls  # tıklama gerçekten uygulandı (sahte arka uçta)
    assert [b["text"] for b in sent_bodies(harness.fake)] == ["İş bitti."]
    assert len(photos(harness)) == 1
    assert photos(harness)[0]["photo_is_png"] is True and photos(harness)[0]["chat_id"] == CHAT_ID
    assert harness.fake.photos[0].startswith(b"\x89PNG")
    events = [e["event"] for e in harness.audit_events()]
    assert "approval" not in events  # temiz turda kapı SIDE_EFFECT'i sormaz (mevcut davranış)


async def test_accept_mode_asks_with_telegram_buttons_not_the_desktop(make) -> None:
    harness = make(mode="accept")
    channel, _, backend, _ = build(harness, mode="accept")
    harness.fake.queue_press("ok")

    await channel.handle_update(say("not defterini aç"))

    asked = approval_requests(harness.fake)
    assert len(asked) == 1 and "computer_use" in asked[0]["text"]
    assert backend.calls


async def test_denied_start_never_touches_the_screen(make) -> None:
    harness = make(mode="accept")
    channel, _, backend, _ = build(harness, mode="accept")
    harness.fake.queue_press("no")

    await channel.handle_update(say("not defterini aç"))

    assert backend.captures == 0 and backend.calls == []
    assert photos(harness) == []


def test_mode_is_reread_from_config_each_turn_and_fails_closed(make) -> None:
    harness = make(mode="auto")
    channel, runtime, _, _ = build(harness, mode="auto")
    path = harness.root / "config.yaml"

    channel._sync_computer_mode(runtime)
    assert runtime.context.settings.computer_use.mode == "auto"
    write_mode(harness, "accept")  # kullanıcı panelden oto onayı kapattı
    channel._sync_computer_mode(runtime)
    assert runtime.context.settings.computer_use.mode == "accept"
    write_mode(harness, "auto")
    channel._sync_computer_mode(runtime)
    assert runtime.context.settings.computer_use.mode == "auto"
    path.write_text("computer_use: [bozuk", encoding="utf-8")  # okunamaz => sor
    channel._sync_computer_mode(runtime)
    assert runtime.context.settings.computer_use.mode == "accept"


# /dur ---------------------------------------------------------------------------------------


class StopMidSession(ScriptedVision):
    """İlk model çağrısında yetkili/yetkisiz bir /dur kuyruğa koyar ve yoklamayı bekler."""

    def __init__(self, harness: Harness, *contents: str, **options: Any) -> None:
        super().__init__(*contents)
        self.harness, self.options = harness, options

    async def complete(
        self, messages: list[Message], tools: list[Any], *, timeout: float
    ) -> Completion:
        if not self.seen:
            self.harness.fake.queue_message("/dur", **self.options)
            await asyncio.sleep(0.6)  # yoklama aralığı küçük; /dur duyulur
        return await super().complete(messages, tools, timeout=timeout)


async def test_dur_stops_the_session_before_the_next_action(make) -> None:
    harness = make(mode="auto")
    vision = StopMidSession(harness, click(), DONE, YES)
    channel, _, backend, _ = build(harness, mode="auto", vision=vision)
    channel._stop_poll_seconds = 0.02

    await channel.handle_update(say("not defterini aç"))

    assert backend.calls == []  # durdurma, bekleyen tıklamadan önce geldi
    texts = [b["text"] for b in sent_bodies(harness.fake)]
    assert any("durduruluyor" in t for t in texts)
    ends = [e for e in harness.audit_events() if e["event"] == "computer_session_end"]
    assert ends and ends[-1]["reason"] == "stopped"
    assert not SCREEN_LOCK.locked()


async def test_dur_from_a_stranger_is_ignored(make) -> None:
    harness = make(mode="auto")
    vision = StopMidSession(harness, click(), DONE, YES, from_id=999, chat_id=999)
    channel, _, backend, _ = build(harness, mode="auto", vision=vision)
    channel._stop_poll_seconds = 0.02

    await channel.handle_update(say("not defterini aç"))

    assert backend.calls  # oturum durmadı
    assert not any("durduruluyor" in b["text"] for b in sent_bodies(harness.fake))


async def test_dur_while_approval_is_pending_denies_and_stops(make) -> None:
    harness = make(mode="accept")
    channel, runtime, backend, _ = build(harness, mode="accept")
    harness.fake.queue_message("/dur")  # onay sorulurken gelir

    await channel.handle_update(say("not defterini aç"))

    assert backend.calls == []
    edits = method_bodies(harness.fake, "editMessageText")
    assert edits and "Durduruldu" in edits[-1]["text"]


async def test_dur_without_a_session_says_so(make) -> None:
    harness = make(mode="auto")
    channel, _, _, _ = build(harness, mode="auto")

    await channel.handle_update(say("/dur"))

    texts = [b["text"] for b in sent_bodies(harness.fake)]
    assert texts == ["Çalışan bir bilgisayar kullanımı oturumu yok."]


async def test_dur_is_not_run_in_a_group(make) -> None:
    harness = make(mode="auto")
    channel, _, _, _ = build(harness, mode="auto")
    from telegram_fake import make_message

    await channel.handle_update(
        make_message(1, "/dur", chat_type="group", date=int(time.time()))
    )
    assert sent_bodies(harness.fake) == []


# Ortak ekran kilidi -------------------------------------------------------------------------


def test_two_hosts_never_hold_the_screen_together() -> None:
    desktop = ComputerHost(lambda *_: True, lambda _: None)
    telegram = ComputerHost(lambda *_: True, lambda _: None)
    with desktop.session():
        with pytest.raises(ScreenBusyError), telegram.session():
            pass
        assert not telegram.active
    with telegram.session():  # bitince diğeri alabilir
        assert telegram.active and not desktop.active


async def test_second_session_gets_a_clear_refusal_not_a_crash(make) -> None:
    harness = make(mode="auto")
    channel, _, backend, vision = build(harness, mode="auto")
    desktop = ComputerHost(lambda *_: True, lambda _: None)

    with desktop.session():  # masaüstü oturumu sürerken Telegram'dan istek
        await channel.handle_update(say("not defterini aç"))

    assert backend.captures == 0 and vision.seen == []
    assert photos(harness) == []
    results = [e for e in harness.audit_events() if e["event"] == "tool_result"]
    assert any("ekranı kullanıyor" in str(e) for e in results)


# Ekran karesi gizliliği ---------------------------------------------------------------------


async def test_flag_off_keeps_no_frame_and_sends_no_photo(make) -> None:
    harness = make(mode="auto", flag=False)
    channel, runtime, _, _ = build(harness, mode="auto")

    await channel.handle_update(say("not defterini aç"))

    assert photos(harness) == [] and audit_photos(harness) == []
    assert "computer_frame_sink" not in runtime.context.services
    assert channel._frame is None


async def test_private_history_blocks_the_photo(make) -> None:
    harness = make(mode="auto")
    (harness.workspace / "kaynak.txt").write_text("GIZLI-ICERIK-7731", encoding="utf-8")
    replies = (
        call("read_file", "r1", path="kaynak.txt"),
        call("computer_use", "c1", goal=GOAL),
        answer("Tamam."),
    )
    channel, _, backend, _ = build(harness, mode="auto", extra=(ReadFile,), replies=replies)

    await channel.handle_update(say("dosyayı oku sonra not defterini aç"))

    assert backend.calls  # oturum çalıştı, kare üretildi
    assert photos(harness) == [] and audit_photos(harness) == []
    assert harness.fake.photos == []


def test_cloud_screen_off_blocks_the_photo(make) -> None:
    harness = make(mode="auto", allow_cloud=False)
    channel, runtime, _, _ = build(harness, mode="auto")

    channel._send_frame(runtime, b"\x89PNG-fake", False)
    assert photos(harness) == []


def test_all_gates_open_sends_the_photo_control(make) -> None:
    harness = make(mode="auto", allow_cloud=True)
    channel, runtime, _, _ = build(harness, mode="auto")

    channel._send_frame(runtime, b"\x89PNG-fake", False)
    assert len(photos(harness)) == 1
    channel._send_frame(runtime, b"\x89PNG-fake", True)  # gizli yanıt
    assert len(photos(harness)) == 1


async def test_photo_audit_has_no_image_bytes_and_frame_is_dropped(make) -> None:
    harness = make(mode="auto")
    channel, _, _, _ = build(harness, mode="auto")

    await channel.handle_update(say("not defterini aç"))

    events = audit_photos(harness)
    assert len(events) == 1 and events[0]["ok"] is True and events[0]["text"] == ""
    assert "photo" not in events[0] and "PNG" not in harness.audit_text()
    assert channel._frame is None
    assert not list(harness.root.rglob("*.png"))  # diske hiç yazılmadı


# Telegram istemcisi: send_photo ----------------------------------------------------------


def test_send_photo_is_multipart_and_audited_without_bytes(make) -> None:
    harness = make()
    client = harness.client()
    png = b"\x89PNG\r\n" + bytes(range(256))

    client.send_photo(CHAT_ID, png, kind="photo")

    assert harness.fake.photos == [png]
    body = photos(harness)[0]
    assert body == {"chat_id": CHAT_ID, "photo_len": len(png), "photo_is_png": True}
    out = audit_photos(harness)
    assert out[0]["method"] == "sendPhoto" and out[0]["ok"] is True and out[0]["text"] == ""
    assert FAKE_TOKEN not in harness.audit_text()


def test_send_photo_failure_is_audited_and_raised(make) -> None:
    harness = make()
    harness.fake.fail("sendPhoto", status=500)
    with pytest.raises(TelegramError):
        harness.client().send_photo(CHAT_ID, b"\x89PNG", kind="photo")
    out = audit_photos(harness)
    assert out[0]["ok"] is False and FAKE_TOKEN not in harness.audit_text()


# Gömülü / CLI -------------------------------------------------------------------------------


@pytest.mark.parametrize("embedded", [True, False])
async def test_only_embedded_mode_gets_the_computer_host(make, embedded: bool) -> None:
    from artemis.channels.runtime import create_runtime
    from artemis.channels.telegram.channel import serve_telegram

    harness = make()
    path = write_mode(harness, "auto")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["telegram"] = {
        "chat_id": CHAT_ID, "api_base": harness.fake.api_base, "poll_timeout_seconds": 0,
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    seen: list[Runtime] = []

    def factory(config_path: Path, approve: Any, *, settings: Any, audit: Any) -> Runtime:
        runtime = create_runtime(
            config_path, approve, provider=scripted(), registry=registry_with(ComputerUse),
            audit=audit, settings=settings, with_code_agent=False,
        )
        seen.append(runtime)
        return runtime

    stop = asyncio.Event()
    task = asyncio.create_task(serve_telegram(
        path, stop, embedded=embedded, status=lambda _t: None, runtime_factory=factory,
    ))
    for _ in range(500):
        if any("kanalı açık" in b["text"] for b in sent_bodies(harness.fake)) or task.done():
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 10)

    assert seen and ("computer_host" in seen[0].context.services) is embedded
