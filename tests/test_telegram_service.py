"""Masaüstü içi Telegram kanalı (ADR 0052): servis, gömülü kip ve CLI uyumu.

Ağ yalnız loopback sahte Telegram sunucusudur; sağlayıcı senaryoludur; gerçek token, bot ya da
Windows kimlik kasası yoktur (kasa okuması `stored_telegram_token` yamasıyla kapatılır).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from telegram_fake import (
    CHAT_ID,
    FAKE_TOKEN,
    Harness,
    build_harness,
    closed_port,
    method_bodies,
    sent_bodies,
)
from test_telegram import (
    ConfirmWriteTool,
    answer,
    approval_requests,
    call,
    registry_with,
    scripted,
)

from artemis.channels.desktop.telegram_service import TelegramService, should_run
from artemis.channels.runtime import Runtime, create_runtime
from artemis.channels.telegram import api
from artemis.channels.telegram import channel as channel_module
from artemis.channels.telegram.channel import (
    STATUS_CONFLICT,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_STOPPED,
    run_telegram,
    serve_telegram,
)
from artemis.config.settings import Settings, TelegramConfig

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    built = build_harness(tmp_path / "case", monkeypatch)
    yield built
    built.close()


def write_config(harness: Harness, **telegram: Any) -> Path:
    path = harness.root / "config.yaml"
    section = {
        "chat_id": CHAT_ID,
        "api_base": harness.fake.api_base,
        "poll_timeout_seconds": 0,
        "approval_timeout_seconds": 5,
        "run_in_desktop": True,
        **telegram,
    }
    section = {key: value for key, value in section.items() if value is not None}
    path.write_text(
        yaml.safe_dump({
            "workspace": str(harness.workspace),
            "state_dir": str(harness.root / "state"),
            "audit_path": str(harness.audit_path),
            "telegram": section,
        }),
        encoding="utf-8",
    )
    return path


def runtime_factory(provider: Any, registry: Any) -> Callable[..., Runtime]:
    def make(config_path: Path, approve: Any, *, settings: Settings, audit: Any) -> Runtime:
        return create_runtime(
            config_path, approve, provider=provider, registry=registry, audit=audit,
            settings=settings, with_code_agent=False,
        )

    return make


def wait_until(predicate: Callable[[], bool], seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def service_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "artemis-telegram"]


def polls(harness: Harness) -> int:
    return len(method_bodies(harness.fake, "getUpdates"))


def running(harness: Harness, statuses: list[str]) -> bool:
    return STATUS_RUNNING in statuses and any(
        "Komut yazabilirsiniz" in body["text"] for body in sent_bodies(harness.fake)
    )


# Yaşam döngüsü ---------------------------------------------------------------------------


def test_service_handles_message_with_buttons_and_stops_cleanly(harness: Harness) -> None:
    statuses: list[str] = []
    provider = scripted(
        call("telegram_confirm_test", "c1", path="onay.txt", content="Onaylı içerik"),
        answer("Dosya yazıldı."),
    )
    service = TelegramService(
        statuses.append, runtime_factory=runtime_factory(provider, registry_with(ConfirmWriteTool))
    )
    try:
        assert service.start(write_config(harness)) is True
        assert wait_until(lambda: running(harness, statuses)), statuses
        # İkinci başlatma yeni iş parçacığı açmaz; kontrol kanal açıldıktan sonra yapılır
        # (yavaş koşucuda iş parçacığı bitmişse neden `statuses` ile görünür, iş parçacığı sızmaz).
        assert service.start(write_config(harness)) is False, statuses
        assert statuses[0] == STATUS_STARTING
        # Kanal açılırken biriken komutlar atlanır; mesaj ve düğme açılıştan SONRA kuyruğa girer.
        harness.fake.queue_message("onay.txt oluştur")
        harness.fake.queue_press("ok")
        assert wait_until(lambda: (harness.workspace / "onay.txt").exists())
        assert wait_until(
            lambda: any(b["text"] == "Dosya yazıldı." for b in sent_bodies(harness.fake))
        )
    finally:
        service.stop()
    assert (harness.workspace / "onay.txt").read_text(encoding="utf-8") == "Onaylı içerik"
    [approval] = approval_requests(harness.fake)
    assert approval["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("ok:")
    assert [e["approved"] for e in harness.audit_events() if e["event"] == "approval"] == [True]
    assert not service.is_running and service_threads() == []
    assert statuses[-1] == STATUS_STOPPED
    service.stop()  # idempotent


def test_stop_before_thread_is_up_still_ends_the_thread(harness: Harness) -> None:
    service = TelegramService(
        lambda _text: None, runtime_factory=runtime_factory(scripted(answer("x")), registry_with())
    )
    service.start(write_config(harness))
    service.stop()
    assert service_threads() == []


def test_conflict_at_start_reports_once_and_never_retries(harness: Harness) -> None:
    harness.fake.fail("getUpdates", status=409)
    statuses: list[str] = []
    service = TelegramService(
        statuses.append, runtime_factory=runtime_factory(scripted(answer("x")), registry_with())
    )
    service.start(write_config(harness))
    assert wait_until(lambda: not service.is_running)
    assert statuses[-1] == STATUS_CONFLICT
    seen = polls(harness)
    time.sleep(0.5)
    assert polls(harness) == seen == 1  # yeniden deneme döngüsü yok
    assert service_threads() == []


def test_conflict_while_serving_stops_without_retry(harness: Harness) -> None:
    statuses: list[str] = []
    service = TelegramService(
        statuses.append, runtime_factory=runtime_factory(scripted(answer("x")), registry_with())
    )
    service.start(write_config(harness))
    try:
        assert wait_until(lambda: running(harness, statuses))
        harness.fake.fail("getUpdates", status=409)
        assert wait_until(lambda: not service.is_running)
    finally:
        service.stop()
    assert statuses[-1] == STATUS_CONFLICT
    seen = polls(harness)
    time.sleep(0.5)
    assert polls(harness) == seen


def test_setup_failure_reports_error_type_only(harness: Harness) -> None:
    statuses: list[str] = []

    def broken(*_args: Any, **_kwargs: Any) -> Runtime:
        raise ValueError(f"gizli {FAKE_TOKEN}")

    service = TelegramService(statuses.append, runtime_factory=broken)
    service.start(write_config(harness))
    assert wait_until(lambda: not service.is_running)
    assert statuses[-1] == "Telegram hatası (ValueError)"
    assert all(FAKE_TOKEN not in text for text in statuses)


def test_unexpected_error_type_is_reported_not_swallowed_into_stopped(harness: Harness) -> None:
    statuses: list[str] = []

    def broken(*_args: Any, **_kwargs: Any) -> Runtime:
        raise KeyError(f"gizli {FAKE_TOKEN}")  # beklenen türler dışında bir arıza

    service = TelegramService(statuses.append, runtime_factory=broken)
    service.start(write_config(harness))
    assert wait_until(lambda: not service.is_running)
    assert statuses[-1] == "Telegram hatası (KeyError)"
    assert all(FAKE_TOKEN not in text for text in statuses) and service_threads() == []


def test_start_during_unfinished_stop_says_so_instead_of_vanishing(harness: Harness) -> None:
    from artemis.channels.desktop.telegram_service import STOPPING_TEXT

    statuses: list[str] = []
    service = TelegramService(statuses.append, runtime_factory=broken_never_called)
    release = threading.Event()
    stuck = threading.Thread(target=release.wait, daemon=True)
    stuck.start()
    service._thread = stuck  # önceki durma süresinde bitmedi: tur sürüyor
    service._stop_requested.set()
    try:
        assert service.start(write_config(harness)) is False
        assert statuses[-1] == STOPPING_TEXT
    finally:
        release.set()
        stuck.join(5)


def broken_never_called(*_args: Any, **_kwargs: Any) -> Runtime:
    raise AssertionError("bu test runtime kurmamalı")


def test_embedded_asks_no_code_agent_but_cli_does(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    def capture(*_args: Any, with_code_agent: bool = True, **_kwargs: Any) -> Runtime:
        seen.append(with_code_agent)
        raise OSError("durdur")

    path = write_config(harness)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update({
        "primary_model": "first",
        "openrouter_base_url": "http://127.0.0.1:8317/v1",
        "models": {"first": {"provider": "openrouter", "model": "first-model",
                             "input_per_million": "0", "output_per_million": "0"}},
    })
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(channel_module, "create_runtime", capture)
    for embedded in (True, False):
        with pytest.raises(OSError):
            asyncio.run(serve_telegram(path, asyncio.Event(), embedded=embedded, status=print))
    assert seen == [False, True]


def test_exit_stops_the_telegram_service() -> None:
    pytest.importorskip("PySide6")
    from unittest.mock import MagicMock

    from artemis.channels.desktop.app import DesktopWindow

    window = MagicMock()
    window._exiting = True
    window._hotkey = None
    window._tray = None
    window._temporary = None
    DesktopWindow._finish_exit(window)
    window._telegram_service.stop.assert_called_once_with()


# Başlatma koşulu ---------------------------------------------------------------------------


def test_run_in_desktop_defaults_to_false() -> None:
    assert TelegramConfig().run_in_desktop is False
    assert TelegramConfig(chat_id=CHAT_ID).run_in_desktop is False


def test_service_condition_needs_flag_chat_id_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "stored_telegram_token", lambda: None)  # gerçek kasaya bakma
    monkeypatch.delenv("ARTEMIS_TELEGRAM_BOT_TOKEN", raising=False)
    assert not should_run(TelegramConfig(chat_id=CHAT_ID, run_in_desktop=True))  # token yok
    monkeypatch.setenv("ARTEMIS_TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    assert not should_run(TelegramConfig(run_in_desktop=True))  # chat_id yok
    assert not should_run(TelegramConfig(chat_id=CHAT_ID))  # bayrak kapalı
    assert should_run(TelegramConfig(chat_id=CHAT_ID, run_in_desktop=True))


def test_old_config_without_key_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("workspace: desktop\ntelegram:\n  chat_id: 42\n", encoding="utf-8")
    from artemis.config.settings import load_settings

    assert load_settings(path).telegram.run_in_desktop is False


# Gömülü kip ve CLI -------------------------------------------------------------------------


async def test_embedded_mode_never_starts_proactive_but_cli_does(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(
        channel_module, "_start_proactive", lambda *_args: started.append(True)
    )
    factory = runtime_factory(scripted(answer("x")), registry_with())
    for embedded in (True, False):
        stop = asyncio.Event()
        statuses: list[str] = []
        task = asyncio.create_task(serve_telegram(
            write_config(harness), stop, embedded=embedded, status=statuses.append,
            runtime_factory=factory,
        ))
        for _ in range(500):
            if STATUS_RUNNING in statuses or any("Ctrl+C" in s for s in statuses) or task.done():
                break
            await asyncio.sleep(0.02)
        stop.set()
        assert await task == 0
        assert started == ([] if embedded else [True])
    # CLI metni aynen korunur.
    assert "Artemis Telegram kanalı açık. Durdurmak için Ctrl+C." in statuses


async def test_cli_return_codes_and_messages(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(api, "stored_telegram_token", lambda: None)
    assert await run_telegram(write_config(harness, chat_id=None)) == 2
    assert "telegram.chat_id" in capsys.readouterr().out
    monkeypatch.delenv("ARTEMIS_TELEGRAM_BOT_TOKEN")
    assert await run_telegram(write_config(harness)) == 2
    assert "ARTEMIS_TELEGRAM_BOT_TOKEN" in capsys.readouterr().out
    monkeypatch.setenv("ARTEMIS_TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    assert await run_telegram(write_config(harness)) == 2  # primary_model yok
    assert "primary_model" in capsys.readouterr().out
    # Bağlantı kurulamazsa 1.
    assert await _connect_failure(harness, monkeypatch, capsys) == 1


async def _connect_failure(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> int:
    path = write_config(harness, api_base=f"http://127.0.0.1:{closed_port()}")
    statuses: list[str] = []
    code = await serve_telegram(
        path, asyncio.Event(), embedded=False, status=statuses.append,
        runtime_factory=runtime_factory(scripted(answer("x")), registry_with()),
    )
    assert statuses and statuses[0].startswith("Telegram'a bağlanılamadı (")
    return code


async def test_embedded_codes_and_fixed_texts(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "stored_telegram_token", lambda: None)
    statuses: list[str] = []
    assert await serve_telegram(
        write_config(harness, chat_id=None), asyncio.Event(), embedded=True, status=statuses.append
    ) == 2
    monkeypatch.delenv("ARTEMIS_TELEGRAM_BOT_TOKEN")
    assert await serve_telegram(
        write_config(harness), asyncio.Event(), embedded=True, status=statuses.append
    ) == 2
    assert statuses == ["Telegram hatası", "Telegram hatası (token yok)"]


# Uygulama bağlantısı -----------------------------------------------------------------------


def test_app_sync_starts_stops_and_respects_demo_and_exit(harness: Harness) -> None:
    pytest.importorskip("PySide6")
    from artemis.channels.desktop.app import DesktopWindow

    path = write_config(harness)
    service = TelegramService(
        lambda _text: None,
        runtime_factory=runtime_factory(scripted(answer("x")), registry_with()),
    )
    window = SimpleNamespace(
        _telegram_service=service, _demo=False, _exiting=False, _config_path=path
    )
    sync = DesktopWindow._sync_telegram_service
    try:
        sync(window)  # type: ignore[arg-type]
        assert service.is_running
        path.write_text(path.read_text(encoding="utf-8").replace("true", "false"), "utf-8")
        sync(window)  # type: ignore[arg-type]
        assert not service.is_running and service_threads() == []
        path.write_text(path.read_text(encoding="utf-8").replace("false", "true"), "utf-8")
        window._demo = True
        sync(window)  # type: ignore[arg-type]
        assert not service.is_running
        window._demo, window._exiting = False, True
        sync(window)  # type: ignore[arg-type]
        assert not service.is_running
    finally:
        service.stop()
    assert service_threads() == []
