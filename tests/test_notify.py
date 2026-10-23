"""Bildirim araçları: sabit Telegram alıcısı, Windows toast'ı ve güvenlik kapısı.

Telegram yalnız loopback sahte sunucuya gider. Toast testleri PowerShell'i çalıştırmaz;
çalıştırıcı (runner) sahte bir nesnedir ve argv, ortam ve betik sözleşmesini yakalar.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from telegram_fake import CHAT_ID, FAKE_TOKEN, Harness, build_harness, sent_bodies

from artemis.brain.models import ToolCall
from artemis.safety.gate import HISTORY_TAINT_NOTICE, TAINT_NOTICE, SafetyGate, TurnState
from artemis.tools.base import Risk, ToolContext
from artemis.tools.platform import child_environment
from artemis.tools.registry import discover_tools
from artemis.tools.toast import (
    TOAST_BODY_LIMIT,
    TOAST_SCRIPT,
    TOAST_TITLE_LIMIT,
    ToastOutcome,
    clean_toast_text,
    show_toast,
)

TOKEN_FRAGMENT = "TEST-not-a-real"


@pytest.fixture
def make_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Harness]]:
    built: list[Harness] = []

    def factory(**options: Any) -> Harness:
        harness = build_harness(tmp_path / f"case-{len(built)}", monkeypatch, **options)
        built.append(harness)
        return harness

    yield factory
    for harness in built:
        harness.close()


def gate() -> SafetyGate:
    # Tüm kayıtlı araçlar: bildirim araçları gerçek kayıttan keşfedilir.
    return SafetyGate(discover_tools())


def dispatch_context(harness: Harness, services: dict[str, Any] | None = None) -> ToolContext:
    return ToolContext(
        workspace=harness.workspace,
        settings=harness.settings,
        audit=harness.audit,
        services=dict(services or {}),
    )


def call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id="call-notify", name=name, arguments=arguments)


def refuse(name: str, arguments: dict[str, Any]) -> bool:
    pytest.fail("Temiz yan etki için onay istenmez.")


# 1. Keşif -----------------------------------------------------------------------------------


def test_notification_tools_are_discovered_with_honest_metadata() -> None:
    registry = discover_tools()
    telegram = registry.get("notify_telegram")
    desktop = registry.get("notify_desktop")
    assert telegram is not None and desktop is not None
    assert telegram.risk == Risk.SIDE_EFFECT
    assert telegram.outbound is True
    assert telegram.external is False
    assert desktop.risk == Risk.SIDE_EFFECT
    assert desktop.outbound is False
    for tool in (telegram, desktop):
        assert tool.label and tool.done_label


def test_notify_telegram_schema_has_no_recipient_field() -> None:
    definitions = {item.name: item for item in discover_tools().definitions()}
    properties = definitions["notify_telegram"].parameters["properties"]
    assert set(properties) == {"text"}
    assert properties["text"]["maxLength"] == 4000


# 2. notify_telegram -------------------------------------------------------------------------


async def test_clean_turn_sends_to_fixed_chat_without_approval(make_harness) -> None:
    harness = make_harness()
    client = harness.client()
    context = dispatch_context(harness, {"telegram_client": client})

    result = await gate().dispatch(
        call("notify_telegram", text="Toplantı 15:00"), context, TurnState(), refuse
    )

    assert result.success is True and result.status == "ok"
    sent = sent_bodies(harness.fake)
    assert [(body["chat_id"], body["text"]) for body in sent] == [(CHAT_ID, "Toplantı 15:00")]


@pytest.mark.parametrize(
    "arguments",
    [{"text": "Merhaba", "chat_id": 999}, {"text": "x" * 4001}, {"text": ""}],
)
async def test_model_cannot_choose_recipient_or_send_oversized_text(
    make_harness, arguments: dict[str, Any]
) -> None:
    harness = make_harness()
    context = dispatch_context(harness, {"telegram_client": harness.client()})

    result = await gate().dispatch(
        call("notify_telegram", **arguments), context, TurnState(), refuse
    )

    assert result.success is False and result.status == "error"
    assert harness.fake.snapshot() == []


async def test_injected_client_is_not_closed_by_the_tool(make_harness) -> None:
    harness = make_harness()
    client = harness.client()
    context = dispatch_context(harness, {"telegram_client": client})

    first = await gate().dispatch(call("notify_telegram", text="ilk"), context, TurnState(), refuse)
    assert first.success
    # Araç kendi kurmadığı istemciyi kapatmamalı; ikinci doğrudan çağrı çalışmalı.
    client.send_message(CHAT_ID, "ikinci", kind="notification")
    assert [body["text"] for body in sent_bodies(harness.fake)] == ["ilk", "ikinci"]


@pytest.mark.parametrize("answer", [False, True])
async def test_tainted_turn_asks_with_taint_notice(make_harness, answer: bool) -> None:
    harness = make_harness()
    context = dispatch_context(harness, {"telegram_client": harness.client()})
    checker = gate()
    seen: list[tuple[str, dict[str, Any]]] = []

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        assert checker.pending_notice.startswith(TAINT_NOTICE)
        seen.append((name, arguments))
        return answer

    result = await checker.dispatch(
        call("notify_telegram", text="Dış metin"), context, TurnState(tainted=True), approve
    )

    assert seen == [("notify_telegram", {"text": "Dış metin"})]
    if answer:
        assert result.success
        assert [body["text"] for body in sent_bodies(harness.fake)] == ["Dış metin"]
    else:
        assert result.status == "denied"
        assert harness.fake.snapshot() == []


@pytest.mark.parametrize("answer", [False, True])
async def test_outbound_after_external_history_asks_with_history_notice(
    make_harness, answer: bool
) -> None:
    harness = make_harness()
    context = dispatch_context(harness, {"telegram_client": harness.client()})
    checker = gate()
    notices: list[str] = []

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        notices.append(checker.pending_notice)
        return answer

    state = TurnState(history_external=True)
    result = await checker.dispatch(
        call("notify_telegram", text="Geçmiş"), context, state, approve
    )

    assert len(notices) == 1 and notices[0].startswith(HISTORY_TAINT_NOTICE)
    assert result.success is answer
    assert (len(harness.fake.snapshot()) > 0) is answer


@pytest.mark.parametrize(
    ("chat_id", "drop_token"),
    [(None, False), (42, True)],
)
async def test_unconfigured_telegram_fails_without_network(
    make_harness, monkeypatch: pytest.MonkeyPatch, chat_id: int | None, drop_token: bool
) -> None:
    harness = make_harness(chat_id=chat_id)
    if drop_token:
        monkeypatch.delenv("ARTEMIS_TELEGRAM_BOT_TOKEN", raising=False)

    result = await gate().dispatch(
        call("notify_telegram", text="Merhaba"), dispatch_context(harness), TurnState(), refuse
    )

    assert result.success is False and result.status == "error"
    assert "yapılandırılmamış" in result.message
    assert harness.fake.snapshot() == []


@pytest.mark.parametrize(
    ("status", "body"), [(500, None), (200, {"ok": False, "description": "x"})]
)
async def test_telegram_refusal_is_reported_as_failure(
    make_harness, status: int, body: dict[str, Any] | None
) -> None:
    harness = make_harness()
    harness.fake.fail("sendMessage", status=status, body=body)
    context = dispatch_context(harness, {"telegram_client": harness.client()})

    result = await gate().dispatch(
        call("notify_telegram", text="Gitmeyecek"), context, TurnState(), refuse
    )

    assert result.success is False and result.status == "error"
    assert "gönderilemedi" in result.message
    assert FAKE_TOKEN not in result.model_dump_json()
    assert TOKEN_FRAGMENT not in result.model_dump_json()


# 3. Toast ----------------------------------------------------------------------------------


class FakeRunner:
    """PowerShell yerine geçen çalıştırıcı: argv ve ortamı yakalar, sonucu programlanır."""

    def __init__(self, *, returncode: int = 0, error: BaseException | None = None) -> None:
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), dict(kwargs)))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(args, self.returncode, stdout="", stderr="")


def test_toast_is_windows_only_and_never_runs_the_runner_elsewhere() -> None:
    runner = FakeRunner()
    outcome = show_toast("Başlık", "Gövde", runner=runner, platform="linux")
    assert outcome.status == "error"
    assert runner.calls == []


@pytest.mark.parametrize(
    ("returncode", "error", "expected"),
    [
        (0, None, "ok"),
        (3, None, "disabled"),
        (1, None, "error"),
        (0, "timeout", "error"),
        (0, "oserror", "error"),
    ],
)
def test_toast_outcomes_follow_exit_codes_and_failures(
    returncode: int, error: str | None, expected: str
) -> None:
    exception: BaseException | None = None
    if error == "timeout":
        exception = subprocess.TimeoutExpired(cmd="powershell.exe", timeout=15)
    elif error == "oserror":
        exception = OSError("powershell.exe bulunamadı")
    runner = FakeRunner(returncode=returncode, error=exception)
    outcome = show_toast("Başlık", "Gövde", runner=runner, platform="win32")
    assert outcome.status == expected
    assert len(runner.calls) == 1
    if expected == "disabled":
        assert "kapalı" in outcome.message


def test_toast_argv_carries_the_fixed_script_only() -> None:
    runner = FakeRunner()
    show_toast("Başlık-ÖZEL-42", "Gövde-ÖZEL-42", runner=runner, platform="win32")
    args, kwargs = runner.calls[0]
    assert args[:5] == [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
    ]
    assert args[5] == "-EncodedCommand"
    decoded = base64.b64decode(args[6]).decode("utf-16-le")
    assert decoded == TOAST_SCRIPT
    assert "Başlık-ÖZEL-42" not in decoded and "Gövde-ÖZEL-42" not in decoded
    assert "Başlık-ÖZEL-42" not in " ".join(args)
    assert "$env:ARTEMIS_TOAST_TITLE" in TOAST_SCRIPT
    assert "$env:ARTEMIS_TOAST_BODY" in TOAST_SCRIPT
    assert kwargs["timeout"] == 15.0
    assert kwargs["encoding"] == "utf-8"


def test_toast_environment_is_cleaned_and_carries_no_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARTEMIS_TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("ARTEMIS_UNLISTED_SETTING", "1")
    runner = FakeRunner()

    show_toast("A\x00" + "t" * 150, "B\x07" + "g" * 400, runner=runner, platform="win32")

    env = runner.calls[0][1]["env"]
    title = env["ARTEMIS_TOAST_TITLE"]
    body = env["ARTEMIS_TOAST_BODY"]
    assert len(title) <= TOAST_TITLE_LIMIT and title.endswith("…")
    assert len(body) <= TOAST_BODY_LIMIT and body.endswith("…")
    assert "\x00" not in title and "\x07" not in body
    for key in ("ARTEMIS_TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        assert key not in env
    assert "ARTEMIS_UNLISTED_SETTING" not in env
    assert FAKE_TOKEN not in env.values()
    assert set(env) <= set(child_environment()) | {"ARTEMIS_TOAST_TITLE", "ARTEMIS_TOAST_BODY"}


@pytest.mark.parametrize(
    ("raw", "limit"),
    [("a\x00b\x07c", 100), ("Satır\tsonu\n", 100), ("x" * 10, 5)],
)
def test_clean_toast_text_removes_controls_and_shortens(raw: str, limit: int) -> None:
    cleaned = clean_toast_text(raw, limit)
    assert len(cleaned) <= limit
    assert "\x00" not in cleaned and "\x07" not in cleaned
    if raw == "a\x00b\x07c":
        assert cleaned == "abc"
    if raw == "Satır\tsonu\n":
        assert cleaned == raw
    if raw == "x" * 10:
        assert cleaned.endswith("…") and cleaned.startswith("xxx")


# 4. notify_desktop -------------------------------------------------------------------------


async def test_notify_desktop_reports_success_only_when_toast_is_ok(make_harness) -> None:
    harness = make_harness()
    calls: list[tuple[str, str]] = []

    def toast(title: str, body: str) -> ToastOutcome:
        calls.append((title, body))
        return ToastOutcome(status="ok", message="Bildirim gösterildi.")

    result = await gate().dispatch(
        call("notify_desktop", title="Hatırlatma", body="Su iç"),
        dispatch_context(harness, {"toast": toast}),
        TurnState(),
        refuse,
    )

    assert result.success is True and result.status == "ok"
    assert calls == [("Hatırlatma", "Su iç")]
    events = [e for e in harness.audit_events() if e["event"] == "desktop_notification"]
    assert events[-1]["status"] == "ok"
    assert events[-1]["title"] == "Hatırlatma" and events[-1]["body"] == "Su iç"


@pytest.mark.parametrize("status", ["disabled", "error"])
async def test_notify_desktop_failures_are_not_success(make_harness, status: str) -> None:
    harness = make_harness()

    def toast(title: str, body: str) -> ToastOutcome:
        return ToastOutcome(status=status, message="Windows bildirimi gösterilemedi.")

    result = await gate().dispatch(
        call("notify_desktop", title="Başlık", body="Gövde"),
        dispatch_context(harness, {"toast": toast}),
        TurnState(),
        refuse,
    )

    assert result.success is False and result.status == "error"
    events = [e for e in harness.audit_events() if e["event"] == "desktop_notification"]
    assert events[-1]["status"] == status


@pytest.mark.parametrize(
    ("title", "body"),
    [("", "Gövde"), ("t" * 101, "Gövde"), ("Başlık", "b" * 301)],
)
async def test_notify_desktop_schema_limits_are_enforced(
    make_harness, title: str, body: str
) -> None:
    harness = make_harness()
    calls: list[str] = []

    def toast(title_value: str, body_value: str) -> ToastOutcome:
        calls.append(title_value)
        return ToastOutcome(status="ok", message="x")

    result = await gate().dispatch(
        call("notify_desktop", title=title, body=body),
        dispatch_context(harness, {"toast": toast}),
        TurnState(),
        refuse,
    )

    assert result.success is False
    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="Linux'ta varsayılan toast çağrılmaz")
async def test_notify_desktop_without_windows_fails_honestly(make_harness) -> None:
    harness = make_harness()
    result = await gate().dispatch(
        call("notify_desktop", title="Başlık", body="Gövde"),
        dispatch_context(harness),
        TurnState(),
        refuse,
    )
    assert result.success is False and result.status == "error"


@pytest.mark.disruptive
@pytest.mark.skipif(sys.platform != "win32", reason="Gerçek bildirim yalnız Windows'ta gösterilir")
def test_real_windows_toast_shows_notification() -> None:
    # Gerçek masaüstünde bir bildirim gösterir; yalnız `pytest -m disruptive` ile çalışır.
    outcome = show_toast("Artemis testi", "Bu bir gerçek bildirim denemesidir.")
    assert outcome.status == "ok", outcome.message
