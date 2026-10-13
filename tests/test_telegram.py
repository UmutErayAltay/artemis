"""Telegram kanalı: yetki, onay düğmesi, gizlilik, denetim ve token hijyeni.

Ağ yalnız 127.0.0.1 üzerindeki sahte Telegram sunucusudur (tests/telegram_fake.py); gerçek
token, gerçek bot veya gerçek ağ yoktur. Dosya işlemleri tmp_path altında gerçek dosyalarla
yapılır. Her test kısa sürer: sunucu uzun bekleme yapmaz, onay zaman aşımları küçüktür.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from telegram_fake import (
    CHAT_ID,
    FAKE_TOKEN,
    OUT_METHODS,
    FakeTelegram,
    Harness,
    build_harness,
    closed_port,
    everything_sent,
    make_callback,
    make_message,
    method_bodies,
    sent_bodies,
)

from jarvis.brain.fake import ScriptedProvider
from jarvis.brain.models import Completion, ToolCall
from jarvis.channels import cli
from jarvis.channels.telegram.api import (
    TELEGRAM_TEXT_LIMIT,
    TelegramClient,
    TelegramError,
    build_telegram_client,
)
from jarvis.channels.telegram.channel import TelegramChannel, check_message
from jarvis.config.settings import Settings, TelegramConfig, load_settings
from jarvis.safety.audit import AuditLog
from jarvis.safety.gate import TAINT_NOTICE
from jarvis.safety.paths import safe_join
from jarvis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from jarvis.tools.platform import child_environment
from jarvis.tools.plugins.filesystem import ReadFile
from jarvis.tools.registry import ToolRegistry

NOW = 1_800_000_000.0  # check_message testlerinde sabit saat (tam saniye, sınır kesin)
TOKEN_FRAGMENT = "TEST-not-a-real"
LONG_CONTENT = "x" * 4096 + "y" * 4096 + "z" * 10


# Yalnız bu dosyada kullanılan, global kayda girmeyen sahte araçlar ------------------------


class WriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(default="", max_length=8000)


class ConfirmWriteTool(BaseTool):
    name = "telegram_confirm_test"
    label = "Test yazma"
    description = "Telegram onay akışını sınayan sahte yazma aracı."
    args_model = WriteArgs
    risk: ClassVar[Risk] = Risk.CONFIRM_REQUIRED

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        args = WriteArgs.model_validate(arguments)
        safe_join(context.workspace, args.path).write_text(args.content, encoding="utf-8")
        return ToolResult(success=True, status="ok", message="Dosya yazıldı.")


class SideEffectWriteTool(ConfirmWriteTool):
    name = "telegram_side_effect_test"
    label = "Yan etki testi"
    description = "Dış içerik sonrası yan etkiyi sınayan sahte yazma aracı."
    risk: ClassVar[Risk] = Risk.SIDE_EFFECT


def registry_with(*classes: type[BaseTool]) -> ToolRegistry:
    registry = ToolRegistry()
    for cls in classes:
        registry.register(cls)
    return registry


def scripted(*responses: Completion) -> ScriptedProvider:
    return ScriptedProvider(list(responses))


def call(name: str, call_id: str, **arguments: Any) -> Completion:
    return Completion(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def answer(content: str) -> Completion:
    return Completion(content=content)


def approval_requests(fake: FakeTelegram) -> list[dict[str, Any]]:
    return [body for body in sent_bodies(fake) if body.get("reply_markup")]


def observed_channel(
    harness: Harness, provider: Any, registry: ToolRegistry, **options: Any
) -> tuple[TelegramChannel, Any]:
    """AuditLog kayıtları kanala gider; run_telegram'ın kurduğu bağlantının aynısı."""
    holder: list[TelegramChannel] = []
    harness.audit = AuditLog(
        harness.audit_path, on_record=lambda record: holder[0].observe(record)
    )
    channel, runtime = harness.channel(provider, registry, **options)
    holder.append(channel)
    return channel, runtime


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


# 1. Yetki kontrolü (saf fonksiyon) --------------------------------------------------------


def test_check_message_accepts_owner_private_text() -> None:
    message = make_message(1, "Merhaba", date=int(NOW))["message"]
    assert check_message(message, CHAT_ID, NOW, 120) is None


@pytest.mark.parametrize(
    ("chat_id", "chat_type", "from_id", "expected"),
    [
        (43, "private", CHAT_ID, {"unauthorized_chat"}),
        (CHAT_ID, "private", 43, {"unauthorized_chat"}),
        (CHAT_ID, "group", CHAT_ID, {"not_private", "unauthorized_chat"}),
        (CHAT_ID, "supergroup", CHAT_ID, {"not_private", "unauthorized_chat"}),
        (True, "private", True, {"unauthorized_chat"}),
    ],
)
def test_check_message_rejects_foreign_or_group_chats(
    chat_id: Any, chat_type: str, from_id: Any, expected: set[str]
) -> None:
    message = make_message(
        1, "Merhaba", chat_id=chat_id, chat_type=chat_type, from_id=from_id, date=int(NOW)
    )["message"]
    rejection = check_message(message, CHAT_ID, NOW, 120)
    assert rejection is not None
    assert rejection.reason in expected


def test_boolean_ids_never_pass_even_when_they_equal_one() -> None:
    # True == 1 olduğu için bool kontrolü olmadan bu mesaj geçerdi.
    message = make_message(1, "Merhaba", chat_id=True, from_id=True, date=int(NOW))["message"]
    rejection = check_message(message, 1, NOW, 120)
    assert rejection is not None
    assert rejection.reason in {"unauthorized_chat", "not_private"}


@pytest.mark.parametrize(
    "key",
    ["forward_origin", "forward_from", "forward_from_chat", "forward_sender_name", "forward_date"],
)
def test_forwarded_messages_are_rejected_for_every_forward_key(key: str) -> None:
    value: Any = int(NOW) if key == "forward_date" else "Başka kişi"
    if key in {"forward_origin", "forward_from", "forward_from_chat"}:
        value = {"type": "user"}
    message = make_message(1, "Başkasının metni", date=int(NOW), **{key: value})["message"]
    rejection = check_message(message, CHAT_ID, NOW, 120)
    assert rejection is not None and rejection.reason == "forwarded"


def test_identity_is_checked_before_forwarding() -> None:
    message = make_message(
        1, "Yabancı", chat_id=43, from_id=43, date=int(NOW), forward_date=int(NOW)
    )["message"]
    rejection = check_message(message, CHAT_ID, NOW, 120)
    assert rejection is not None and rejection.reason == "unauthorized_chat"


@pytest.mark.parametrize("message_text", [None, "", "   \n"])
def test_non_text_messages_are_rejected(message_text: str | None) -> None:
    extra: dict[str, Any] = {}
    if message_text is None:
        extra["photo"] = [{"file_id": "fotoğraf-test"}]
    message = make_message(1, message_text, date=int(NOW), **extra)["message"]
    rejection = check_message(message, CHAT_ID, NOW, 120)
    assert rejection is not None and rejection.reason == "not_text"


def test_old_or_undated_messages_are_stale() -> None:
    old = make_message(1, "eski", date=int(NOW) - 121)["message"]
    rejection = check_message(old, CHAT_ID, NOW, 120)
    assert rejection is not None and rejection.reason == "stale"

    undated = make_message(2, "tarihsiz", date=int(NOW))["message"]
    del undated["date"]
    rejection = check_message(undated, CHAT_ID, NOW, 120)
    assert rejection is not None and rejection.reason == "stale"

    text_dated = make_message(3, "metin tarih", date=int(NOW))["message"]
    text_dated["date"] = str(int(NOW))
    rejection = check_message(text_dated, CHAT_ID, NOW, 120)
    assert rejection is not None and rejection.reason == "stale"


def test_message_exactly_at_max_age_is_accepted() -> None:
    edge = make_message(1, "sınırda", date=int(NOW) - 120)["message"]
    assert check_message(edge, CHAT_ID, NOW, 120) is None


# 2. Güncelleme işleme ---------------------------------------------------------------------


async def test_unauthorized_message_never_reaches_provider_or_telegram(make_harness) -> None:
    harness = make_harness()
    provider = scripted(
        call("telegram_side_effect_test", "c1", path="yetkisiz.txt", content="x"),
        answer("Bitti."),
    )
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))

    await channel.handle_update(make_message(1, "GIZLI-KOMUT-42 dosya yaz", chat_id=99, from_id=99))

    assert provider.requests == []
    assert not (harness.workspace / "yetkisiz.txt").exists()
    assert harness.fake.snapshot() == []
    events = [event["event"] for event in harness.audit_events()]
    assert "telegram_rejected" in events
    assert "turn_start" not in events and "tool_requested" not in events
    rejected = [event for event in harness.audit_events() if event["event"] == "telegram_rejected"]
    assert rejected[0]["reason"] == "unauthorized_chat"
    assert "GIZLI-KOMUT-42" not in harness.audit_text()


@pytest.mark.parametrize(
    ("message_text", "extra"),
    [
        ("Başkasının metni", {"forward_date": int(NOW)}),
        ("Eski mesaj", {"date": int(time.time()) - 1000}),
        (None, {"photo": [{"file_id": "fotoğraf-test"}]}),
    ],
)
async def test_forwarded_stale_or_non_text_from_owner_gets_one_notice(
    make_harness, message_text: str | None, extra: dict[str, Any]
) -> None:
    harness = make_harness()
    provider = scripted(answer("Çalışmamalı."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))

    await channel.handle_update(make_message(1, message_text, **extra))

    assert provider.requests == []
    notices = sent_bodies(harness.fake)
    assert len(notices) == 1
    assert notices[0]["chat_id"] == CHAT_ID
    assert "Çalışmamalı." not in notices[0]["text"]
    assert len(harness.fake.snapshot()) == 1


async def test_authorized_text_runs_one_turn_and_replies_in_plain_text(make_harness) -> None:
    harness = make_harness()
    provider = scripted(answer("Merhaba, nasılsın?"))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))

    await channel.handle_update(make_message(1, "Merhaba", date=int(time.time())))

    assert len(provider.requests) == 1
    sent = sent_bodies(harness.fake)
    assert [(body["chat_id"], body["text"]) for body in sent] == [
        (CHAT_ID, "Merhaba, nasılsın?")
    ]
    assert "parse_mode" not in sent[0]
    assert sent[0]["disable_web_page_preview"] is True
    outs = [event for event in harness.audit_events() if event["event"] == "telegram_out"]
    assert len(outs) == 1
    assert outs[0]["kind"] == "reply"
    assert outs[0]["ok"] is True
    assert outs[0]["text"] == "Merhaba, nasılsın?"


async def test_long_reply_is_split_and_every_chunk_is_audited(make_harness) -> None:
    harness = make_harness()
    provider = scripted(answer(LONG_CONTENT))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))

    await channel.handle_update(make_message(1, "uzun cevap", date=int(time.time())))

    chunks = [body["text"] for body in sent_bodies(harness.fake)]
    assert len(chunks) >= 3
    assert all(len(chunk) <= TELEGRAM_TEXT_LIMIT for chunk in chunks)
    assert "".join(chunks) == LONG_CONTENT
    outs = [event for event in harness.audit_events() if event["event"] == "telegram_out"]
    assert len(outs) == len(chunks)
    assert all(event["kind"] == "reply" and event["ok"] is True for event in outs)


async def test_private_reply_is_withheld_and_content_never_leaves(make_harness) -> None:
    harness = make_harness()
    (harness.workspace / "kaynak.txt").write_text("GIZLI-ICERIK-7731", encoding="utf-8")
    provider = scripted(
        call("read_file", "r1", path="kaynak.txt"),
        answer("Dosyada yazan: GIZLI-ICERIK-7731"),
    )
    channel, _ = harness.channel(provider, registry_with(ReadFile))

    await channel.handle_update(make_message(1, "kaynak.txt dosyasını oku", date=int(time.time())))

    assert "GIZLI-ICERIK-7731" not in everything_sent(harness.fake)
    sent = sent_bodies(harness.fake)
    assert len(sent) == 1
    assert "gönderilmedi" in sent[0]["text"]


async def test_owner_callback_outside_approval_gets_stale_answer(make_harness) -> None:
    harness = make_harness()
    channel, _ = harness.channel(scripted(), registry_with(SideEffectWriteTool))

    await channel.handle_update(make_callback(1, "ok:0123456789abcdef"))

    answers = method_bodies(harness.fake, "answerCallbackQuery")
    assert len(answers) == 1
    assert answers[0]["text"] == "Bu onay artık geçerli değil."


async def test_unauthorized_callback_outside_approval_is_audited_only(make_harness) -> None:
    harness = make_harness()
    channel, _ = harness.channel(scripted(), registry_with(SideEffectWriteTool))

    await channel.handle_update(make_callback(1, "ok:0123456789abcdef", from_id=99, chat_id=99))

    assert method_bodies(harness.fake, "answerCallbackQuery") == []
    rejected = [event for event in harness.audit_events() if event["event"] == "telegram_rejected"]
    assert [event["reason"] for event in rejected] == ["unauthorized_callback"]


# 3. Onay düğmesi, gizlilik ve süre ---------------------------------------------------------


async def test_approved_button_runs_tool_and_shows_full_arguments(make_harness) -> None:
    harness = make_harness()
    harness.fake.queue_press("ok")
    provider = scripted(
        call("telegram_confirm_test", "c1", path="onay.txt", content="Onaylı içerik"),
        answer("Dosya yazıldı."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))

    await channel.handle_update(make_message(1, "onay.txt oluştur", date=int(time.time())))

    assert (harness.workspace / "onay.txt").read_text(encoding="utf-8") == "Onaylı içerik"
    [approval] = approval_requests(harness.fake)
    full_arguments = json.dumps(
        {"path": "onay.txt", "content": "Onaylı içerik"}, ensure_ascii=False, indent=2
    )
    assert full_arguments in approval["text"]
    assert "Varsayılan cevap hayırdır." in approval["text"]
    assert "sn içinde yanıtlanmazsa reddedilir" in approval["text"]
    buttons = approval["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"].startswith("ok:")
    assert buttons[1]["callback_data"].startswith("no:")
    answers = method_bodies(harness.fake, "answerCallbackQuery")
    assert any(item["text"] == "Onaylandı" for item in answers)
    edits = method_bodies(harness.fake, "editMessageText")
    assert len(edits) == 1
    assert "Karar: Onaylandı" in edits[0]["text"]
    assert "reply_markup" not in edits[0]
    approvals = [e for e in harness.audit_events() if e["event"] == "approval"]
    assert [e["approved"] for e in approvals] == [True]


async def test_deny_button_refuses_and_never_runs_tool(make_harness) -> None:
    harness = make_harness()
    harness.fake.queue_press("no")
    provider = scripted(
        call("telegram_confirm_test", "c1", path="red.txt", content="x"),
        answer("Yazmadım."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))

    await channel.handle_update(make_message(1, "red.txt oluştur", date=int(time.time())))

    assert not (harness.workspace / "red.txt").exists()
    edits = method_bodies(harness.fake, "editMessageText")
    assert len(edits) == 1 and "Karar: Reddedildi" in edits[0]["text"]
    assert any(
        item["text"] == "Reddedildi"
        for item in method_bodies(harness.fake, "answerCallbackQuery")
    )
    approvals = [e for e in harness.audit_events() if e["event"] == "approval"]
    assert [e["approved"] for e in approvals] == [False]


async def test_callback_from_another_user_is_ignored_and_unanswered(make_harness) -> None:
    harness = make_harness(approval_timeout=0.3)
    harness.fake.queue_press("ok", from_id=999)
    provider = scripted(
        call("telegram_confirm_test", "c1", path="baskasi.txt", content="x"),
        answer("Yazmadım."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))

    await channel.handle_update(make_message(1, "baskasi.txt oluştur", date=int(time.time())))

    assert not (harness.workspace / "baskasi.txt").exists()
    assert method_bodies(harness.fake, "answerCallbackQuery") == []
    rejected = [e for e in harness.audit_events() if e["event"] == "telegram_rejected"]
    assert "unauthorized_callback" in [e["reason"] for e in rejected]
    edits = method_bodies(harness.fake, "editMessageText")
    assert len(edits) == 1 and "Süre doldu" in edits[0]["text"]


async def test_stale_approval_id_is_refused_while_waiting(make_harness) -> None:
    harness = make_harness(approval_timeout=0.3)
    harness.fake.queue_press("ok", data="ok:0000000000000000")
    provider = scripted(
        call("telegram_confirm_test", "c1", path="eski.txt", content="x"),
        answer("Yazmadım."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))

    await channel.handle_update(make_message(1, "eski.txt oluştur", date=int(time.time())))

    assert not (harness.workspace / "eski.txt").exists()
    texts = [item["text"] for item in method_bodies(harness.fake, "answerCallbackQuery")]
    assert texts == ["Bu onay artık geçerli değil."]
    assert not any(e.get("approved") for e in harness.audit_events() if e["event"] == "approval")


async def test_approval_timeout_denies_and_edits_message(make_harness) -> None:
    harness = make_harness(approval_timeout=0.3)
    provider = scripted(
        call("telegram_confirm_test", "c1", path="zaman.txt", content="x"),
        answer("Yazmadım."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))

    started = time.monotonic()
    await channel.handle_update(make_message(1, "zaman.txt oluştur", date=int(time.time())))

    assert time.monotonic() - started < 2.0
    assert not (harness.workspace / "zaman.txt").exists()
    edits = method_bodies(harness.fake, "editMessageText")
    assert len(edits) == 1 and "Süre doldu" in edits[0]["text"]
    assert method_bodies(harness.fake, "answerCallbackQuery") == []


async def test_approval_timeout_without_turn_pressure_denies(make_harness) -> None:
    # 0,3 sn testi tur-bitimi yolunu sınar; bu test gerçek onay zaman aşımını (>1 sn) sınar.
    harness = make_harness(approval_timeout=1.1)
    provider = scripted(
        call("telegram_confirm_test", "c1", path="uzun.txt", content="x"),
        answer("Yazmadım."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))

    await channel.handle_update(make_message(1, "uzun.txt oluştur", date=int(time.time())))

    assert not (harness.workspace / "uzun.txt").exists()
    assert len(approval_requests(harness.fake)) == 1
    edits = method_bodies(harness.fake, "editMessageText")
    assert len(edits) == 1 and "Süre doldu" in edits[0]["text"]


async def test_polling_error_during_approval_denies(make_harness) -> None:
    harness = make_harness(approval_timeout=5.0)
    harness.fake.fail("getUpdates", status=500)
    provider = scripted(
        call("telegram_confirm_test", "c1", path="hata.txt", content="x"),
        answer("Yazmadım."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))

    await channel.handle_update(make_message(1, "hata.txt oluştur", date=int(time.time())))

    assert not (harness.workspace / "hata.txt").exists()
    events = [event["event"] for event in harness.audit_events()]
    assert "approval_error" in events
    assert "telegram_poll_error" in events


async def test_oversized_arguments_are_refused_without_buttons(make_harness) -> None:
    harness = make_harness()
    channel, _ = harness.channel(scripted(), registry_with(ConfirmWriteTool))

    arguments = {"path": "buyuk.txt", "content": "x" * 4200}
    approved = channel.approve("telegram_confirm_test", arguments)

    assert approved is False
    assert not (harness.workspace / "buyuk.txt").exists()
    sent = sent_bodies(harness.fake)
    assert len(sent) == 1
    assert "reply_markup" not in sent[0]
    assert "sığmadığı" in sent[0]["text"]


async def test_message_during_approval_is_not_run_and_is_reminded(make_harness) -> None:
    harness = make_harness()
    harness.fake.queue_message("Şimdi başka şey yap", date=int(time.time()))
    harness.fake.queue_press("ok")
    provider = scripted(
        call("telegram_confirm_test", "c1", path="bekle.txt", content="x"),
        answer("Yazıldı."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))

    await channel.handle_update(make_message(1, "bekle.txt yaz", date=int(time.time())))

    assert len(provider.requests) == 2
    reminders = [body for body in sent_bodies(harness.fake) if "bekleyen onayı" in body["text"]]
    assert len(reminders) == 1
    assert "reply_markup" not in reminders[0]
    turns = [e["input"] for e in harness.audit_events() if e["event"] == "turn_start"]
    assert turns == ["bekle.txt yaz"]
    assert (harness.workspace / "bekle.txt").exists()


async def test_taint_notice_is_shown_in_approval_message(make_harness) -> None:
    harness = make_harness(allow_cloud_file_content=True)
    (harness.workspace / "kaynak.txt").write_text("Dış içerik", encoding="utf-8")
    harness.fake.queue_press("ok")
    provider = scripted(
        call("read_file", "r1", path="kaynak.txt"),
        call("telegram_side_effect_test", "c2", path="yan.txt", content="x"),
        answer("Bitti."),
    )
    channel, _ = harness.channel(provider, registry_with(ReadFile, SideEffectWriteTool))

    await channel.handle_update(make_message(1, "kaynak.txt sonrası yaz", date=int(time.time())))

    [approval] = approval_requests(harness.fake)
    assert TAINT_NOTICE in approval["text"]


async def test_private_turn_withholds_approval(make_harness) -> None:
    harness = make_harness(allow_cloud_file_content=False)
    (harness.workspace / "gizli.txt").write_text("GIZLI-ICERIK-9913", encoding="utf-8")
    harness.fake.queue_press("ok")
    provider = scripted(
        call("read_file", "r1", path="gizli.txt"),
        call("telegram_confirm_test", "c2", path="cikti.txt", content="x"),
        answer("Yazamadım."),
    )
    channel, _ = observed_channel(harness, provider, registry_with(ReadFile, ConfirmWriteTool))

    await channel.handle_update(make_message(1, "gizli.txt sonrası yaz", date=int(time.time())))

    assert not (harness.workspace / "cikti.txt").exists()
    assert approval_requests(harness.fake) == []
    assert method_bodies(harness.fake, "getUpdates") == []
    assert "GIZLI-ICERIK-9913" not in everything_sent(harness.fake)
    notices = [body["text"].casefold() for body in sent_bodies(harness.fake)]
    assert any("gizli içerik" in text for text in notices)
    withheld = [e for e in harness.audit_events() if e["event"] == "approval_withheld"]
    assert [e["reason"] for e in withheld] == ["private_turn"]


async def test_private_history_withholds_approval(make_harness) -> None:
    harness = make_harness(allow_cloud_file_content=False)
    (harness.workspace / "gecmis.txt").write_text("GIZLI-GECMIS-5521", encoding="utf-8")
    harness.fake.queue_press("ok")
    provider = scripted(
        call("read_file", "r1", path="gecmis.txt"),
        answer("Dosyayı okudum."),
        call("telegram_confirm_test", "c2", path="sonraki.txt", content="x"),
        answer("Yazamadım."),
    )
    channel, _ = observed_channel(harness, provider, registry_with(ReadFile, ConfirmWriteTool))

    await channel.handle_update(make_message(1, "gecmis.txt oku", date=int(time.time())))
    await channel.handle_update(make_message(2, "şimdi sonraki.txt yaz", date=int(time.time())))

    assert not (harness.workspace / "sonraki.txt").exists()
    assert approval_requests(harness.fake) == []
    assert "GIZLI-GECMIS-5521" not in everything_sent(harness.fake)
    withheld = [e for e in harness.audit_events() if e["event"] == "approval_withheld"]
    assert [e["reason"] for e in withheld] == ["private_history"]


async def test_approval_is_bounded_by_turn_deadline(make_harness) -> None:
    # Tur 1,5 sn sonra biter; onay penceresi 60 sn olsa bile bu süreyi aşamaz.
    harness = make_harness(approval_timeout=60.0, max_seconds=1.5)
    provider = scripted(
        call("telegram_confirm_test", "c1", path="sure.txt", content="x"),
        answer("Yazmadım."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))

    started = time.monotonic()
    await channel.handle_update(make_message(1, "sure.txt yaz", date=int(time.time())))
    elapsed = time.monotonic() - started

    assert elapsed < 1.5 + 0.5
    assert not (harness.workspace / "sure.txt").exists()
    [approval] = approval_requests(harness.fake)
    assert "sn içinde yanıtlanmazsa reddedilir" in approval["text"]
    edits = method_bodies(harness.fake, "editMessageText")
    assert len(edits) == 1 and "Süre doldu" in edits[0]["text"]


async def test_turn_without_a_second_left_asks_nothing(make_harness) -> None:
    harness = make_harness(approval_timeout=60.0, max_seconds=1.0)
    provider = scripted(
        call("telegram_confirm_test", "c1", path="kalmadi.txt", content="x"),
        answer("Yazmadım."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))

    await channel.handle_update(make_message(1, "kalmadı yaz", date=int(time.time())))

    assert not (harness.workspace / "kalmadi.txt").exists()
    assert approval_requests(harness.fake) == []
    assert method_bodies(harness.fake, "getUpdates") == []
    notices = [body["text"] for body in sent_bodies(harness.fake)]
    assert any("Turun süresi doldu" in text for text in notices)


# 4. Başlangıç birikimi ---------------------------------------------------------------------


async def test_skip_backlog_drops_old_updates(make_harness) -> None:
    harness = make_harness()
    queued = [
        harness.fake.queue_message(f"eski {index}", date=int(time.time()) - 600)
        for index in range(3)
    ]
    provider = scripted(answer("Çalışmamalı."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))

    channel.skip_backlog()

    skip = [item.body for item in harness.fake.snapshot() if item.method == "getUpdates"]
    assert skip and skip[-1]["offset"] == -1 and skip[-1]["timeout"] == 0

    stop = asyncio.Event()
    task = asyncio.create_task(channel.serve(stop))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        polls = [item.body for item in harness.fake.snapshot() if item.method == "getUpdates"]
        if any(body.get("offset") not in (None, -1) for body in polls):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    polls = [item.body for item in harness.fake.snapshot() if item.method == "getUpdates"]
    offsets = [body.get("offset") for body in polls if body.get("offset") not in (None, -1)]
    # Son eski güncelleme kimliğinden bir sonrası: eski güncellemeler bir daha gelmez.
    assert offsets and offsets[0] == queued[-1] + 1
    assert provider.requests == []
    assert sent_bodies(harness.fake) == []


# 5. Token hijyeni --------------------------------------------------------------------------


def test_client_repr_hides_token(make_harness) -> None:
    harness = make_harness()
    rendered = repr(harness.client())
    assert FAKE_TOKEN not in rendered
    assert TOKEN_FRAGMENT not in rendered


async def test_wrong_token_is_refused_and_recorded_without_the_token(make_harness) -> None:
    harness = make_harness()
    wrong = "999999:WRONG-not-a-real-token-zzzzzzzzzzzzzzz"
    client = harness.client(wrong)
    with pytest.raises(TelegramError) as info:
        client.send_message(CHAT_ID, "Merhaba", kind="reply")
    assert [item.token_ok for item in harness.fake.snapshot()] == [False]
    assert wrong not in str(info.value)
    assert "WRONG" not in harness.audit_text()


@pytest.mark.parametrize("failure", ["http_500", "connection_refused"])
def test_errors_hide_token_and_url(make_harness, failure: str) -> None:
    harness = make_harness()
    if failure == "http_500":
        harness.fake.fail("sendMessage")
        client = harness.client()
    else:
        client = harness.client(api_base=f"http://127.0.0.1:{closed_port()}", timeout=2.0)
    with pytest.raises(TelegramError) as info:
        client.send_message(CHAT_ID, "Merhaba", kind="reply")
    rendered = str(info.value)
    assert FAKE_TOKEN not in rendered and TOKEN_FRAGMENT not in rendered
    assert "bot" not in rendered.casefold()
    assert "sendMessage" in rendered
    chain = "".join(traceback.format_exception(info.value))
    assert FAKE_TOKEN not in chain and TOKEN_FRAGMENT not in chain


def test_debug_logs_never_contain_the_token(make_harness, caplog: pytest.LogCaptureFixture) -> None:
    harness = make_harness()
    caplog.set_level(logging.DEBUG)
    client = harness.client()
    client.send_message(CHAT_ID, "Merhaba", kind="reply")
    harness.fake.fail("editMessageText")
    with pytest.raises(TelegramError):
        client.edit_message_text(CHAT_ID, 1, "değişiklik", kind="approval_result")
    client.get_updates(None, 0)
    # Kök DEBUG iken bile httpx istek satırı (URL'de token var) yazılmamalı.
    assert FAKE_TOKEN not in caplog.text
    assert TOKEN_FRAGMENT not in caplog.text
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


def test_httpx_request_log_is_capped_at_warning() -> None:
    # Guard'ın varlığı açıkça ölçülür: INFO istek satırı URL'deki token'ı taşır.
    assert logging.getLogger("httpx").level == logging.WARNING


async def test_audit_never_contains_the_token(make_harness) -> None:
    harness = make_harness()
    client = harness.client()
    client.send_message(CHAT_ID, "Merhaba", kind="reply")
    harness.fake.fail("sendMessage", once=True)
    with pytest.raises(TelegramError):
        client.send_message(CHAT_ID, "Başarısız", kind="reply")
    client.get_updates(None, 0)
    harness.fake.fail("getUpdates", status=500, once=True)
    with pytest.raises(TelegramError):
        client.get_updates(None, 0)
    assert harness.audit_events()
    assert FAKE_TOKEN not in harness.audit_text()
    assert TOKEN_FRAGMENT not in harness.audit_text()


def test_child_environment_excludes_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    env = child_environment()
    assert "JARVIS_TELEGRAM_BOT_TOKEN" not in env
    assert FAKE_TOKEN not in env.values()


def test_settings_refuse_any_token_field(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"telegram": {"token": "x"}})
    config = tmp_path / "config.yaml"
    config.write_text('telegram:\n  chat_id: 42\n  token: "x"\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(config)


@pytest.mark.parametrize(
    "token_env", ["TELEGRAM_BOT", "JARVIS_TELEGRAM_BOT", "jarvis_telegram_token", "JARVIS-TOKEN"]
)
def test_token_env_must_be_an_upper_case_token_name(token_env: str) -> None:
    with pytest.raises(ValidationError):
        TelegramConfig(token_env=token_env)


def test_token_env_default_and_valid_name_are_accepted() -> None:
    assert TelegramConfig().token_env == "JARVIS_TELEGRAM_BOT_TOKEN"
    assert TelegramConfig(token_env="MY_BOT_TOKEN").token_env == "MY_BOT_TOKEN"
    assert TelegramConfig().chat_id is None


@pytest.mark.parametrize(
    "api_base",
    [
        "http://example.com",
        "ftp://127.0.0.1:8080",
        "https://user:secret@api.telegram.org",
        "https://api.telegram.org/?x=1",
    ],
)
def test_api_base_follows_endpoint_rules(api_base: str) -> None:
    with pytest.raises(ValidationError):
        TelegramConfig(api_base=api_base)


def test_api_base_is_normalized_for_loopback() -> None:
    assert TelegramConfig(api_base="http://127.0.0.1:8080/").api_base == "http://127.0.0.1:8080"


@pytest.mark.parametrize(
    ("chat_id", "env"),
    [
        (42, {}),
        (42, {"JARVIS_TELEGRAM_BOT_TOKEN": "   "}),
        (None, {"JARVIS_TELEGRAM_BOT_TOKEN": FAKE_TOKEN}),
    ],
)
def test_build_client_is_none_when_telegram_is_off(
    tmp_path: Path, chat_id: int | None, env: dict[str, str]
) -> None:
    settings = Settings(
        workspace=tmp_path / "desktop",
        telegram=TelegramConfig(chat_id=chat_id, api_base="http://127.0.0.1:9"),
    )
    audit = AuditLog(tmp_path / "audit.jsonl")
    assert build_telegram_client(settings, audit, env=env) is None


def test_build_client_with_token_hides_it(tmp_path: Path) -> None:
    settings = Settings(
        workspace=tmp_path / "desktop",
        telegram=TelegramConfig(chat_id=42, api_base="http://127.0.0.1:9"),
    )
    audit = AuditLog(tmp_path / "audit.jsonl")
    client = build_telegram_client(settings, audit, env={"JARVIS_TELEGRAM_BOT_TOKEN": FAKE_TOKEN})
    assert isinstance(client, TelegramClient)
    try:
        assert FAKE_TOKEN not in repr(client)
    finally:
        client.close()


# 6. Her dışarı çağrının denetimi ----------------------------------------------------------


async def test_every_outbound_call_is_audited(make_harness) -> None:
    harness = make_harness()
    provider = scripted(
        answer("İlk yanıt."),
        call("telegram_confirm_test", "c1", path="karma.txt", content="x"),
        answer("Son yanıt."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))

    await channel.handle_update(make_callback(1, "ok:0123456789abcdef"))  # cevap
    await channel.handle_update(make_message(2, "Yönlendirilmiş", forward_date=int(time.time())))
    harness.fake.fail("sendMessage", once=True)
    await channel.handle_update(make_message(3, "ilk soru", date=int(time.time())))  # başarısız
    harness.fake.queue_press("ok")
    await channel.handle_update(make_message(4, "karma.txt yaz", date=int(time.time())))

    outbound = [item for item in harness.fake.snapshot() if item.method in OUT_METHODS]
    events = [e for e in harness.audit_events() if e["event"] == "telegram_out"]
    assert len(outbound) == 7
    assert len(events) == len(outbound)
    assert any(event["ok"] is False for event in events)
    assert all({"method", "kind", "chat_id", "text", "ok"} <= set(event) for event in events)


# 7. CLI -----------------------------------------------------------------------------------


def _run_telegram_cli(monkeypatch: pytest.MonkeyPatch, config: Path) -> int | None:
    monkeypatch.setattr(sys, "argv", ["jarvis", "telegram", "--config", str(config)])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


def test_telegram_command_without_chat_id_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("workspace: desktop\n", encoding="utf-8")
    assert _run_telegram_cli(monkeypatch, config) == 2
    captured = capsys.readouterr()
    assert "telegram.chat_id" in captured.out + captured.err


def test_telegram_command_names_missing_token_variable_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("workspace: desktop\ntelegram:\n  chat_id: 42\n", encoding="utf-8")
    monkeypatch.delenv("JARVIS_TELEGRAM_BOT_TOKEN", raising=False)
    assert _run_telegram_cli(monkeypatch, config) == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "JARVIS_TELEGRAM_BOT_TOKEN" in output
    assert FAKE_TOKEN not in output and TOKEN_FRAGMENT not in output

