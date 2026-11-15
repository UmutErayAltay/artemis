"""Telegram ses/fotoğraf/dosya girdisi (ADR 0064): sahte istemci dosyaları, sahte STT, sahte OCR.

Ağ yalnız 127.0.0.1 sahte sunucusudur; indirme `get_file`/`download_file` örnek düzeyinde
değiştirilir. Gerçek ses, OCR ya da model çalışmaz.
"""

from __future__ import annotations

import io
import os
import time
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from telegram_fake import (
    FAKE_TOKEN,
    Harness,
    build_harness,
    everything_sent,
    make_message,
    sent_bodies,
)
from test_telegram import (
    ConfirmWriteTool,
    SideEffectWriteTool,
    answer,
    call,
    registry_with,
    scripted,
)

from artemis.channels.telegram.api import TelegramClient, TelegramError
from artemis.channels.telegram.channel import TelegramChannel
from artemis.channels.telegram.media import (
    decode_voice,
    extension_allowed,
    sanitize_filename,
)
from artemis.channels.voice.factory import build_dictation_recognizer
from artemis.config.settings import EndpointConfig, PerceptionConfig, Settings, VoiceConfig
from artemis.perception.screen import ScreenOutcome, read_image
from artemis.safety.audit import AuditLog


class FakeFiles:
    """`get_file`/`download_file` yerine geçer; gerçek istemci gibi okurken de sınır uygular."""

    def __init__(self, files: dict[str, bytes], reported: dict[str, int | None] | None = None):
        self.files = files
        self.reported = reported or {}
        self.downloads: list[str] = []

    def get_file(self, file_id: str) -> tuple[str, int | None]:
        return f"dosya/{file_id}", self.reported.get(file_id, len(self.files[file_id]))

    def download_file(self, path: str, max_bytes: int) -> bytes:
        self.downloads.append(path)
        data = self.files[path.split("/", 1)[1]]
        if len(data) > max_bytes:
            raise TelegramError("sınır", error_type="TooLarge")
        return data


class FakeRecognizer:
    def __init__(self, text: str = "saat kaç") -> None:
        self.text = text
        self.calls: list[bytes] = []

    async def transcribe(self, pcm: bytes) -> str:
        self.calls.append(pcm)
        return self.text

    async def transcribe_partial(self, pcm: bytes) -> str:
        return ""


JPEG = b"\xff\xd8\xff\xe0" + b"x" * 64
ENABLED = {"accept_voice": True, "accept_photo": True, "accept_files": True}


@pytest.fixture
def make_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Harness]]:
    built: list[Harness] = []

    def factory(**options: Any) -> Harness:
        options.setdefault("telegram_fields", ENABLED)
        harness = build_harness(tmp_path / f"case-{len(built)}", monkeypatch, **options)
        built.append(harness)
        return harness

    yield factory
    for harness in built:
        harness.close()


def wire(
    channel: TelegramChannel,
    files: FakeFiles,
    *,
    recognizer: FakeRecognizer | None = None,
    ocr: Callable[..., ScreenOutcome] | None = None,
) -> FakeRecognizer:
    """İstemci indirmesini, çözücüyü, STT'yi ve OCR'ı sahteleriyle değiştirir."""
    channel.client.get_file = files.get_file  # type: ignore[method-assign]
    channel.client.download_file = files.download_file  # type: ignore[method-assign]
    channel.audio_decoder = lambda data, _seconds: b"PCM:" + data
    fake_stt = recognizer or FakeRecognizer()

    async def factory(_runtime: Any) -> FakeRecognizer:
        return fake_stt

    channel.recognizer_factory = factory
    if ocr is not None:
        channel.image_reader = ocr
    return fake_stt


def voice_update(**extra: Any) -> dict[str, Any]:
    voice = {"file_id": "v1", "duration": 3, "file_size": 100}
    return make_message(1, None, date=int(time.time()), voice=voice, **extra)


def photo_update(caption: str | None = None, **extra: Any) -> dict[str, Any]:
    sizes = [
        {"file_id": "p-small", "width": 90, "height": 90, "file_size": 900},
        {"file_id": "p-big", "width": 1280, "height": 960, "file_size": 5000},
    ]
    if caption is not None:
        extra["caption"] = caption
    return make_message(1, None, date=int(time.time()), photo=sizes, **extra)


def document_update(name: object, size: int | None = 10, **extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {"file_id": "d1", "file_name": name}
    if size is not None:
        document["file_size"] = size
    return make_message(1, None, date=int(time.time()), document=document, **extra)


def downloads(harness: Harness) -> list[dict[str, Any]]:
    return [e for e in harness.audit_events() if e["event"] == "telegram_download"]


# 1. Bayraklar ve ortak kapılar ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (voice_update, "voice_disabled"),
        (lambda: photo_update("oku"), "photo_disabled"),
        (lambda: document_update("a.pdf"), "document_disabled"),
    ],
)
async def test_flag_off_refuses_with_a_reply(make_harness, build, reason) -> None:
    update = build()  # tarih çağrı anında alınır: toplama anındaki saat bayatlar
    harness = make_harness(telegram_fields={})
    provider = scripted(answer("Çalışmamalı."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    files = FakeFiles({"v1": b"a", "p-big": JPEG, "d1": b"x"})
    wire(channel, files)

    await channel.handle_update(update)

    assert provider.requests == [] and files.downloads == []
    [notice] = sent_bodies(harness.fake)
    assert "kapalı" in notice["text"]
    rejected = [e for e in harness.audit_events() if e["event"] == "telegram_rejected"]
    assert [e["reason"] for e in rejected] == [reason]


@pytest.mark.parametrize("kind", ["voice", "photo", "document"])
@pytest.mark.parametrize(
    "extra",
    [
        {"forward_date": 1},
        {"date": int(time.time()) - 5000},
        {"chat_id": -100, "chat_type": "group"},
        {"from_id": 7, "chat_id": 7},
    ],
)
async def test_forwarded_stale_group_and_stranger_media_never_run(
    make_harness, kind: str, extra: dict[str, Any]
) -> None:
    harness = make_harness()
    provider = scripted(answer("Çalışmamalı."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    files = FakeFiles({"v1": b"a", "p-big": JPEG, "d1": b"x"})
    stt = wire(channel, files)
    base = {"voice": voice_update, "photo": photo_update, "document": lambda **k: document_update(
        "a.pdf", **k)}[kind]
    update = base(**{k: v for k, v in extra.items() if k != "date"})
    if "date" in extra:
        update["message"]["date"] = extra["date"]

    await channel.handle_update(update)

    assert provider.requests == [] and files.downloads == [] and stt.calls == []
    assert downloads(harness) == []
    assert not (harness.workspace / "Telegram").exists()


# 2. Ses ---------------------------------------------------------------------------------------


async def test_voice_is_echoed_then_run_as_text_command(make_harness) -> None:
    harness = make_harness()
    provider = scripted(answer("Saat on."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    stt = wire(channel, FakeFiles({"v1": b"OGG"}))

    await channel.handle_update(voice_update())

    assert stt.calls == [b"PCM:OGG"]
    assert [body["text"] for body in sent_bodies(harness.fake)] == [
        "Şunu duydum: saat kaç",
        "Saat on.",
    ]
    assert "saat kaç" in provider.requests[0][-1].content
    [event] = downloads(harness)
    assert (event["kind"], event["size"], event["outcome"]) == ("voice", 3, "ok")


async def test_voice_slash_text_is_not_a_channel_command(make_harness) -> None:
    harness = make_harness()
    provider = scripted(answer("Tamam."))
    channel, runtime = harness.channel(provider, registry_with(SideEffectWriteTool))
    wire(channel, FakeFiles({"v1": b"OGG"}), recognizer=FakeRecognizer("/yeni"))

    await channel.handle_update(voice_update())

    # "/yeni" sohbeti sıfırlamaz: sıradan komut olarak modele gider.
    assert len(provider.requests) == 1 and "/yeni" in provider.requests[0][-1].content
    assert len(runtime.runner.conversation.render().messages) > 0


async def test_empty_transcript_says_so_and_runs_nothing(make_harness) -> None:
    harness = make_harness()
    provider = scripted(answer("Çalışmamalı."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    wire(channel, FakeFiles({"v1": b"OGG"}), recognizer=FakeRecognizer("  \n "))

    await channel.handle_update(voice_update())

    assert provider.requests == []
    [notice] = sent_bodies(harness.fake)
    assert "anlaşılamadı" in notice["text"]
    assert downloads(harness)[0]["outcome"] == "empty"


@pytest.mark.parametrize(
    ("file_size", "duration", "payload_size", "outcome"),
    [
        (2_000_000, 3, 1, "too_large"),  # bildirilen boyut: indirmeden önce reddedilir
        (None, 3, 1_000_001, "too_large"),  # bildirim yok: okurken yakalanır
        (100, 61, 1, "too_long"),
    ],
)
async def test_oversize_voice_is_rejected_with_a_reply(
    make_harness, file_size, duration, payload_size, outcome
) -> None:
    harness = make_harness()
    provider = scripted(answer("Çalışmamalı."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    files = FakeFiles({"v1": b"x" * payload_size}, reported={"v1": file_size})
    stt = wire(channel, files)
    voice = {"file_id": "v1", "duration": duration}
    if file_size is not None:
        voice["file_size"] = file_size

    await channel.handle_update(make_message(1, None, date=int(time.time()), voice=voice))

    assert provider.requests == [] and stt.calls == []
    assert downloads(harness)[0]["outcome"] == outcome
    assert len(sent_bodies(harness.fake)) == 1
    if outcome == "too_large" and file_size:
        assert files.downloads == []  # indirmeye hiç başlanmadı


async def test_voice_decoder_missing_or_broken_is_honest(make_harness) -> None:
    harness = make_harness()
    provider = scripted(answer("Çalışmamalı."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    wire(channel, FakeFiles({"v1": b"OGG"}))

    def missing(_data: bytes, _seconds: int) -> bytes:
        raise ImportError("av")

    channel.audio_decoder = missing
    await channel.handle_update(voice_update())
    assert provider.requests == []
    assert "çözücü yok" in sent_bodies(harness.fake)[0]["text"]

    def broken(_data: bytes, _seconds: int) -> bytes:
        raise ValueError("bozuk")

    channel.audio_decoder = broken
    await channel.handle_update(voice_update())
    assert [e["outcome"] for e in downloads(harness)] == ["no_decoder", "decode_failed"]


async def test_stt_failure_does_not_raise_or_run(make_harness) -> None:
    harness = make_harness()
    provider = scripted(answer("Çalışmamalı."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))

    class Broken:
        async def transcribe(self, pcm: bytes) -> str:
            raise RuntimeError("GIZLI-URL https://x/?key=abc")

    wire(channel, FakeFiles({"v1": b"OGG"}))

    async def factory(_runtime: Any) -> Broken:
        return Broken()

    channel.recognizer_factory = factory  # type: ignore[assignment]

    await channel.handle_update(voice_update())

    assert provider.requests == []
    assert "GIZLI-URL" not in everything_sent(harness.fake)
    assert "GIZLI-URL" not in harness.audit_text()
    assert downloads(harness)[0]["outcome"] == "stt_failed"


async def test_spoken_yes_does_not_approve_a_pending_approval(make_harness) -> None:
    harness = make_harness(approval_timeout=0.5)
    # Onay beklenirken gelen sesli mesaj (içeriği "evet" olurdu): indirilmez, onay sayılmaz.
    harness.fake.queue_raw(voice_update())
    provider = scripted(
        call("telegram_confirm_test", "c1", path="sesli.txt", content="x"),
        answer("Yazmadım."),
    )
    channel, _ = harness.channel(provider, registry_with(ConfirmWriteTool))
    files = FakeFiles({"v1": b"OGG"})
    stt = wire(channel, files, recognizer=FakeRecognizer("evet"))

    await channel.handle_update(make_message(1, "sesli.txt oluştur", date=int(time.time())))

    assert not (harness.workspace / "sesli.txt").exists()
    assert files.downloads == [] and stt.calls == []
    approvals = [e for e in harness.audit_events() if e["event"] == "approval"]
    assert [e["approved"] for e in approvals] == [False]
    assert any("Önce bekleyen onayı" in b["text"] for b in sent_bodies(harness.fake))


async def test_voice_privacy_label_follows_stt_settings() -> None:
    settings = Settings(
        endpoints={"bulut": EndpointConfig(base_url="https://api.example.com/v1")},
        voice=VoiceConfig(stt_mode="hybrid", stt_cloud_endpoint="bulut", stt_cloud_model="m"),
    )
    runtime = types.SimpleNamespace(settings=settings, audit=AuditLog(Path(os.devnull)))
    cloud = await build_dictation_recognizer(runtime, "telegram_audio")  # type: ignore[arg-type]
    assert cloud.content_kind == "telegram_audio"  # type: ignore[attr-defined]
    default = await build_dictation_recognizer(runtime)  # type: ignore[arg-type]
    assert default.content_kind == "dictation_audio"  # type: ignore[attr-defined]


# 3. Fotoğraf ----------------------------------------------------------------------------------


def ok_ocr(text: str = "FATURA-GIZLI-77") -> Callable[..., ScreenOutcome]:
    seen: list[str] = []

    def reader(path: str, **_: Any) -> ScreenOutcome:
        assert os.path.exists(path)  # okunurken dosya var
        seen.append(path)
        return ScreenOutcome("ok", "ok", text=text, language="tr")

    reader.seen = seen  # type: ignore[attr-defined]
    return reader


async def test_photo_without_caption_replies_text_and_deletes_image(make_harness) -> None:
    harness = make_harness()
    provider = scripted(answer("Çalışmamalı."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    files = FakeFiles({"p-small": b"k", "p-big": JPEG})
    reader = ok_ocr("Merhaba dünya")
    wire(channel, files, ocr=reader)

    await channel.handle_update(photo_update())

    assert provider.requests == []  # görüntü ve metin modele gitmedi
    assert files.downloads == ["dosya/p-big"]  # en büyük boyut
    assert [b["text"] for b in sent_bodies(harness.fake)] == ["Merhaba dünya"]
    assert not os.path.exists(reader.seen[0])  # type: ignore[attr-defined]
    assert downloads(harness)[0]["outcome"] == "ok"


@pytest.mark.parametrize("mode", ["unavailable", "error", "raise"])
async def test_temp_image_removed_on_every_error_path(make_harness, mode: str) -> None:
    harness = make_harness()
    provider = scripted(answer("Çalışmamalı."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    paths: list[str] = []

    def reader(path: str, **_: Any) -> ScreenOutcome:
        paths.append(path)
        if mode == "raise":
            raise OSError("disk")
        return ScreenOutcome(mode, "Windows OCR yok.")  # type: ignore[arg-type]

    wire(channel, FakeFiles({"p-big": JPEG}), ocr=reader)

    await channel.handle_update(photo_update("oku"))

    assert provider.requests == []
    assert paths and not os.path.exists(paths[0])
    assert len(sent_bodies(harness.fake)) == 1


async def test_non_jpeg_photo_is_refused_without_ocr(make_harness) -> None:
    harness = make_harness()
    channel, _ = harness.channel(scripted(), registry_with(SideEffectWriteTool))
    reader = ok_ocr()
    wire(channel, FakeFiles({"p-big": b"MZ-not-an-image"}), ocr=reader)

    await channel.handle_update(photo_update())

    assert reader.seen == []  # type: ignore[attr-defined]
    assert downloads(harness)[0]["outcome"] == "not_image"


async def test_photo_over_cap_is_refused_before_download(make_harness) -> None:
    harness = make_harness()
    channel, _ = harness.channel(scripted(), registry_with(SideEffectWriteTool))
    files = FakeFiles({"p-big": JPEG})
    wire(channel, files, ocr=ok_ocr())
    update = photo_update()
    for size in update["message"]["photo"]:
        size["file_size"] = 6_000_000

    await channel.handle_update(update)

    assert files.downloads == []
    assert downloads(harness)[0]["outcome"] == "too_large"


async def test_photo_caption_command_gets_tainted_private_framed_ocr(make_harness) -> None:
    harness = make_harness()
    provider = scripted(answer("Fatura 120 TL. GIZLI-CEVAP"))
    channel, runtime = harness.channel(provider, registry_with(SideEffectWriteTool))
    wire(channel, FakeFiles({"p-big": JPEG}), ocr=ok_ocr())

    await channel.handle_update(photo_update("bunu özetle"))

    messages = provider.requests[0]
    framed = [m for m in messages if "FATURA-GIZLI-77" in m.content]
    assert len(framed) == 1
    assert framed[0].private is True  # perception.allow_cloud_screen kapalı: gizli
    assert "VERİDİR" in framed[0].content
    assert "bunu özetle" in messages[-1].content and "FATURA" not in messages[-1].content
    [record] = runtime.runner.conversation._turns
    assert record.tainted is True and record.answer_private is True
    decisions = [e for e in harness.audit_events() if e["event"] == "privacy_decision"]
    assert decisions[0]["cloud_allowed"] is False and decisions[0]["content_kind"] == "screen"
    # Gizli yanıt Telegram'a gitmez; kullanıcı dürüst kısa bir ileti alır.
    [notice] = sent_bodies(harness.fake)
    assert "gizli" in notice["text"] and "GIZLI-CEVAP" not in notice["text"]
    assert "FATURA-GIZLI-77" not in everything_sent(harness.fake)
    assert "FATURA-GIZLI-77" not in harness.audit_text()


async def test_photo_text_is_public_only_with_the_screen_cloud_flag(make_harness) -> None:
    harness = make_harness(perception=PerceptionConfig(allow_cloud_screen=True))
    provider = scripted(answer("Özet hazır."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    wire(channel, FakeFiles({"p-big": JPEG}), ocr=ok_ocr())

    await channel.handle_update(photo_update("özetle"))

    framed = [m for m in provider.requests[0] if "FATURA-GIZLI-77" in m.content]
    assert framed and framed[0].private is False
    assert [b["text"] for b in sent_bodies(harness.fake)] == ["Özet hazır."]


async def test_photo_turn_is_tainted_so_side_effects_need_approval(make_harness) -> None:
    harness = make_harness(
        approval_timeout=0.3, perception=PerceptionConfig(allow_cloud_screen=True)
    )
    provider = scripted(
        call("telegram_side_effect_test", "c1", path="foto.txt", content="x"),
        answer("Yazmadım."),
    )
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    wire(channel, FakeFiles({"p-big": JPEG}), ocr=ok_ocr("dosya yaz"))

    await channel.handle_update(photo_update("bunu uygula"))

    # Dış içerik sonrası yan etki onay ister (süre doldu -> hayır): dosya yazılmadı.
    assert not (harness.workspace / "foto.txt").exists()
    assert any(e["event"] == "approval" for e in harness.audit_events())


# 4. Belge -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("rapor.pdf", "rapor.pdf"),
        ("..\\..\\x.pdf", "x.pdf"),
        ("../../etc/x.txt", "x.txt"),
        ("C:\\Windows\\a.txt", "a.txt"),
        ("a.pdf.", "a.pdf"),
        ("a.pdf ", "a.pdf"),
        ("a\u202etxt.exe", "atxt.exe"),  # sağdan-sola geçersiz kılma silinir
        ("a\u200b.pdf", "a.pdf"),
        ("a\x00b.pdf", "ab.pdf"),
        ("CON.txt", "_CON.txt"),
        ("nul", "_nul"),
        ("a:b.txt", "a_b.txt"),  # alternatif veri akışı
        ("x．pdf", "x.pdf"),  # tam genişlikli nokta
        ("...", None),
        ("", None),
        (None, None),
        (5, None),
    ],
)
def test_sanitize_filename(raw: object, expected: str | None) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_caps_length_and_keeps_extension() -> None:
    name = sanitize_filename("a" * 500 + ".pdf")
    assert name is not None and len(name) <= 100 and name.endswith(".pdf")


def test_extension_is_judged_by_the_last_one() -> None:
    allowed = ("pdf", "txt")
    assert extension_allowed("x.pdf", allowed)
    assert extension_allowed("X.PDF", allowed)
    assert not extension_allowed("x.pdf.exe", allowed)
    assert extension_allowed("x.exe.pdf", allowed)
    assert not extension_allowed("noext", allowed)
    assert not extension_allowed("x.", allowed)


async def test_document_traversal_name_is_sanitised_and_saved_under_telegram(
    make_harness,
) -> None:
    harness = make_harness()
    provider = scripted(answer("Çalışmamalı."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    wire(channel, FakeFiles({"d1": b"%PDF"}))

    await channel.handle_update(document_update("..\\..\\x.pdf", caption="bunu sil"))

    assert (harness.workspace / "Telegram" / "x.pdf").read_bytes() == b"%PDF"
    assert not (harness.workspace.parent / "x.pdf").exists()
    assert [b["text"] for b in sent_bodies(harness.fake)] == ["Kaydedildi: Telegram/x.pdf"]
    assert provider.requests == []  # başlık komut sayılmaz


@pytest.mark.parametrize("name", ["..\\..\\x.exe", "x.pdf.exe", "arac.bat", None, "..."])
async def test_document_bad_name_or_extension_is_refused_without_download(
    make_harness, name: object
) -> None:
    harness = make_harness()
    channel, _ = harness.channel(scripted(), registry_with(SideEffectWriteTool))
    files = FakeFiles({"d1": b"MZ"})
    wire(channel, files)

    await channel.handle_update(document_update(name))

    assert files.downloads == []
    assert not (harness.workspace / "Telegram").exists()
    assert len(sent_bodies(harness.fake)) == 1
    assert downloads(harness)[0]["outcome"] in {"bad_extension", "bad_name"}


async def test_document_never_overwrites(make_harness) -> None:
    harness = make_harness()
    channel, _ = harness.channel(scripted(), registry_with(SideEffectWriteTool))
    wire(channel, FakeFiles({"d1": b"ILK"}))
    await channel.handle_update(document_update("a.txt"))
    channel.client.download_file = FakeFiles({"d1": b"IKINCI"}).download_file  # type: ignore[method-assign]
    await channel.handle_update(document_update("a.txt"))

    folder = harness.workspace / "Telegram"
    assert (folder / "a.txt").read_bytes() == b"ILK"
    assert (folder / "a-2.txt").read_bytes() == b"IKINCI"
    texts = [b["text"] for b in sent_bodies(harness.fake)]
    assert texts == ["Kaydedildi: Telegram/a.txt", "Kaydedildi: Telegram/a-2.txt"]


async def test_document_over_cap_is_refused_before_download(make_harness) -> None:
    harness = make_harness()
    channel, _ = harness.channel(scripted(), registry_with(SideEffectWriteTool))
    files = FakeFiles({"d1": b"x"})
    wire(channel, files)

    await channel.handle_update(document_update("a.pdf", size=21_000_000))

    assert files.downloads == [] and not (harness.workspace / "Telegram").exists()
    assert downloads(harness)[0]["outcome"] == "too_large"


async def test_document_custom_extension_list_is_honoured(make_harness) -> None:
    harness = make_harness(telegram_fields={**ENABLED, "file_extensions": [".PDF"]})
    channel, _ = harness.channel(scripted(), registry_with(SideEffectWriteTool))
    wire(channel, FakeFiles({"d1": b"x"}))

    await channel.handle_update(document_update("a.txt"))
    await channel.handle_update(document_update("b.pdf"))

    assert not (harness.workspace / "Telegram" / "a.txt").exists()
    assert (harness.workspace / "Telegram" / "b.pdf").exists()


async def test_document_into_symlinked_folder_is_refused(make_harness, tmp_path: Path) -> None:
    harness = make_harness()
    channel, _ = harness.channel(scripted(), registry_with(SideEffectWriteTool))
    wire(channel, FakeFiles({"d1": b"x"}))
    outside = tmp_path / "disari"
    outside.mkdir()
    try:
        (harness.workspace / "Telegram").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("sembolik bağlantı oluşturulamıyor")

    await channel.handle_update(document_update("a.txt"))

    assert list(outside.iterdir()) == []
    assert downloads(harness)[0]["outcome"] == "save_failed"


async def test_download_events_carry_no_content(make_harness) -> None:
    harness = make_harness()
    provider = scripted(answer("Tamam."))
    channel, _ = harness.channel(provider, registry_with(SideEffectWriteTool))
    wire(
        channel,
        FakeFiles({"d1": b"GIZLI-DOSYA-ICERIGI", "v1": b"OGG", "p-big": JPEG}),
        recognizer=FakeRecognizer("GIZLI-KONUSMA"),
        ocr=ok_ocr("GIZLI-OCR-METNI"),
    )
    await channel.handle_update(document_update("gizli-ad.pdf"))
    await channel.handle_update(photo_update())

    for event in downloads(harness):
        assert set(event) - {"event", "ts", "time", "timestamp"} <= {"kind", "size", "outcome"}
    text = harness.audit_text()
    assert "GIZLI-DOSYA-ICERIGI" not in text
    assert "GIZLI-OCR-METNI" not in text  # fotoğraf metni kullanıcıya döner, denetime girmez
    assert "GIZLI-OCR-METNI" in everything_sent(harness.fake)


# 5. İstemci: boyut okurken de sınırlanır, token sızmaz -----------------------------------------


def _client(handler: Callable[[httpx.Request], httpx.Response], audit: AuditLog) -> TelegramClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return TelegramClient(FAKE_TOKEN, "https://api.example.test", audit=audit, http=http)


def test_client_download_stops_reading_at_the_cap(tmp_path: Path) -> None:
    sent = {"chunks": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        def body() -> Iterator[bytes]:
            for _ in range(100):
                sent["chunks"] += 1
                yield b"x" * 1000

        return httpx.Response(200, content=body())

    client = _client(handler, AuditLog(tmp_path / "a.jsonl"))
    with pytest.raises(TelegramError) as error:
        client.download_file("voice/file_1.oga", 2500)
    assert error.value.error_type == "TooLarge"
    assert FAKE_TOKEN not in str(error.value)
    assert client.download_file("voice/file_1.oga", 200_000) == b"x" * 100_000


def test_client_get_file_validates_and_hides_token(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "../x", "file_size": 5}}
            )
        return httpx.Response(500)

    client = _client(handler, AuditLog(tmp_path / "a.jsonl"))
    with pytest.raises(TelegramError):
        client.get_file("f")
    with pytest.raises(TelegramError) as error:
        client.download_file("a/../b", 10)
    assert FAKE_TOKEN not in str(error.value)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("https://x/bot" + FAKE_TOKEN)

    failing = _client(boom, AuditLog(tmp_path / "b.jsonl"))
    with pytest.raises(TelegramError) as caught:
        failing.download_file("a/b.jpg", 10)
    assert FAKE_TOKEN not in str(caught.value) and caught.value.error_type == "ConnectError"


# 6. Yerel parçalar: OCR betiği çalıştırıcısı ve OGG/Opus çözücü ----------------------------------


class _Runner:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode, self.stdout = returncode, stdout
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr="")


def test_read_image_passes_path_by_environment_only() -> None:
    runner = _Runner(stdout='{"text":"merhaba\\u0007","language":"tr"}\n')
    outcome = read_image("C:/tmp/a.jpg", runner=runner, platform="win32")
    assert (outcome.status, outcome.text, outcome.language) == ("ok", "merhaba", "tr")
    args, kwargs = runner.calls[0]
    assert "C:/tmp/a.jpg" not in " ".join(args)
    assert kwargs["env"]["ARTEMIS_OCR_IMAGE"] == "C:/tmp/a.jpg"


def test_read_image_honest_outcomes() -> None:
    assert read_image("x", platform="linux").status == "unavailable"
    assert read_image("x", runner=_Runner(3), platform="win32").status == "unavailable"
    assert read_image("x", runner=_Runner(4), platform="win32").status == "error"
    assert read_image("x", runner=_Runner(1), platform="win32").status == "error"
    assert read_image("x", runner=_Runner(0, "{}"), platform="win32").status == "error"
    empty = read_image("x", runner=_Runner(0, '{"text":"  ","language":"tr"}'), platform="win32")
    assert empty.status == "empty"


def _opus_ogg(seconds: float) -> bytes:
    import av
    import numpy

    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=48000)
        stream.layout = "mono"  # type: ignore[attr-defined]
        total = int(48000 * seconds)
        tone = (numpy.sin(numpy.arange(total) * 2 * numpy.pi * 440 / 48000) * 8000).astype(
            numpy.int16
        )
        for start in range(0, total - 960, 960):
            frame = av.AudioFrame.from_ndarray(
                tone[start : start + 960].reshape(1, -1), format="s16", layout="mono"
            )
            frame.sample_rate = 48000
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue()


def test_decode_voice_reads_ogg_opus_in_memory_and_caps_length() -> None:
    pytest.importorskip("av")
    pytest.importorskip("faster_whisper")
    try:
        data = _opus_ogg(1.0)
    except Exception as error:  # noqa: BLE001 - kodlayıcı bu derlemede yoksa test atlanır
        pytest.skip(f"Opus kodlayıcı yok ({type(error).__name__})")
    pcm = decode_voice(data, 60)
    assert 0.8 * 16000 * 2 <= len(pcm) <= 1.2 * 16000 * 2
    with pytest.raises(ValueError):
        decode_voice(data, 0)
    with pytest.raises(ValueError):
        decode_voice(b"bu ses degil", 60)
