"""P2 kanal ve kalan masaüstü bileşenleri için Türkçe/İngilizce yerelleştirme testleri."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from telegram_fake import CHAT_ID, FAKE_TOKEN, build_harness, method_bodies, sent_bodies
from test_telegram import (
    ConfirmWriteTool,
    SentTool,
    answer,
    approval_requests,
    call,
    registry_with,
    scripted,
)

from artemis.brain.tiering import REASONS
from artemis.channels import cli
from artemis.channels.autostart import VALUE_NAME, Autostart
from artemis.channels.desktop.bridge import State
from artemis.channels.desktop.computer_host import ComputerHost
from artemis.channels.desktop.connect_wizard import OpenRouterPkce
from artemis.channels.desktop.steps import StepLine, step_line
from artemis.channels.desktop.telegram_service import STOPPING_TEXT
from artemis.channels.desktop.window_autostart import toggle_autostart
from artemis.channels.desktop.window_events import handle_state
from artemis.channels.runner import CommandRunner
from artemis.channels.telegram import api as tg_api
from artemis.channels.telegram.channel import (
    STATUS_CONFLICT,
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_STOPPED,
    serve_telegram,
)
from artemis.channels.voice.contracts import AudioFrame, AudioSegment
from artemis.channels.voice.core import VoiceController
from artemis.computer.session import ScreenBusyError
from artemis.config.settings import VoiceConfig
from artemis.i18n import current_language, set_language, t
from artemis.tools.base import ToolResult
from artemis.tools.plugins.filesystem import ReadFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _restore_language() -> Iterator[None]:
    """Her testten sonra dili varsayılan Türkçeye döndürür."""
    set_language("tr")
    try:
        yield
    finally:
        set_language("tr")


def _ensure_app() -> Any:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_cli_approval_prompt_and_evet_acceptance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI onay istemi EN/TR çevrilir; onay sözcüğü her iki dilde de `evet` kalır."""
    prompts: list[str] = []
    answers = iter(["evet", "yes", "evet"])
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or next(answers))

    set_language("en")
    assert cli.request_approval("write_file", {"path": "a.txt"}) is True
    out_en = capsys.readouterr().out
    assert "Approval requested: write_file" in out_en
    assert prompts[-1] == "Do you approve? [evet/No]: "
    # İngilizcede "yes" yazmak onay sayılmaz; sözleşme yalnız "evet" kabul eder.
    assert cli.request_approval("write_file", {"path": "a.txt"}) is False
    capsys.readouterr()

    set_language("tr")
    assert cli.request_approval("write_file", {"path": "a.txt"}) is True
    out_tr = capsys.readouterr().out
    assert "Onay isteniyor: write_file" in out_tr
    assert prompts[-1] == "Onaylıyor musunuz? [evet/Hayır]: "


def test_cli_main_applies_config_language_before_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`cli.main()` komut çalıştırmadan önce config'teki `language` değerini uygular."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("workspace: desktop\nlanguage: en\n", encoding="utf-8")
    monkeypatch.setattr("artemis.channels.autostart.Autostart._windows_store", lambda _self: None)
    monkeypatch.setattr(sys, "argv", ["artemis", "autostart", "durum", "--config", str(cfg)])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    assert current_language() == "en"
    assert "Autostart is only available on Windows." in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["artemis", "telegram", "--config", str(cfg)])
    with pytest.raises(SystemExit) as tg_exc:
        cli.main()
    assert tg_exc.value.code == 2
    assert "Telegram is disabled: define telegram.chat_id in config." in capsys.readouterr().out

    # Türkçe config tekrar Türkçeye döner.
    cfg.write_text("workspace: desktop\nlanguage: tr\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["artemis", "autostart", "durum", "--config", str(cfg)])
    with pytest.raises(SystemExit) as tr_exc:
        cli.main()
    assert tr_exc.value.code == 1
    assert current_language() == "tr"
    assert "Açılışta başlama yalnız Windows'ta var." in capsys.readouterr().out


async def test_telegram_approval_footer_and_embedded_sentinels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram onay/altbilgi EN'de çevrilir; `embedded=True` durumları Türkçe kalır."""
    harness = build_harness(
        tmp_path / "tg", monkeypatch, allow_cloud_file_content=True, approval_timeout=2.0
    )
    try:
        (harness.workspace / "kaynak.txt").write_text("Dış veri", encoding="utf-8")
        harness.fake.queue_press("ok")
        harness.fake.queue_press("ok")
        provider = scripted(
            call("read_file", "r1", path="kaynak.txt"),
            call("telegram_confirm_test", "c1", path="onay.txt", content="tamam"),
            call("telegram_sent_test", "s1", path="gonder.txt"),
            answer("Done."),
        )
        channel, _ = harness.channel(
            provider, registry_with(ReadFile, ConfirmWriteTool, SentTool)
        )
        set_language("en")
        await channel.handle_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "date": int(__import__("time").time()),
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "from": {"id": CHAT_ID, "is_bot": False},
                    "text": "run",
                },
            }
        )
        approvals = approval_requests(harness.fake)
        assert len(approvals) == 2
        approval = approvals[0]
        assert "Approval required:" in approval["text"]
        assert "This step was requested after reading external content." in approval["text"]
        assert "The default answer is no." in approval["text"]
        buttons = approval["reply_markup"]["inline_keyboard"][0]
        assert [b["text"] for b in buttons] == ["Approve", "Deny"]
        assert buttons[0]["callback_data"].startswith("ok:")
        assert buttons[1]["callback_data"].startswith("no:")
        cb_answers = method_bodies(harness.fake, "answerCallbackQuery")
        assert sum(a["text"] == "Approved" for a in cb_answers) == 2
        edits = method_bodies(harness.fake, "editMessageText")
        assert len(edits) == 2 and all("Decision: Approved" in e["text"] for e in edits)
        replies = [
            b["text"] for b in sent_bodies(harness.fake) if b.get("text", "").startswith("Done.")
        ]
        assert len(replies) == 1
        assert "Sent, result unverified: Gönderim testi" in replies[0]

        # Gömülü kip durum çağrıları İngilizce modda bile Türkçe sabitleri korur.
        monkeypatch.setattr(tg_api, "stored_telegram_token", lambda: None)
        monkeypatch.delenv("ARTEMIS_TELEGRAM_BOT_TOKEN", raising=False)
        cfg = tmp_path / "tg-cfg.yaml"
        cfg.write_text(
            yaml.safe_dump({
                "workspace": str(harness.workspace),
                "state_dir": str(tmp_path / "state"),
                "audit_path": str(harness.audit_path),
                "telegram": {"chat_id": CHAT_ID, "api_base": harness.fake.api_base},
            }),
            encoding="utf-8",
        )
        embedded_statuses: list[str] = []
        assert (
            await serve_telegram(
                cfg, asyncio.Event(), embedded=True, status=embedded_statuses.append
            )
            == 2
        )
        assert embedded_statuses == [f"{STATUS_ERROR} (token yok)"]

        monkeypatch.setenv("ARTEMIS_TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
        cli_statuses: list[str] = []
        assert (
            await serve_telegram(cfg, asyncio.Event(), embedded=False, status=cli_statuses.append)
            == 2
        )
        assert cli_statuses == [
            "Define a verified primary_model from the official catalog in config."
        ]
    finally:
        harness.close()


def test_telegram_section_displays_translated_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TelegramSection dahili Türkçe sabitleri korur, yalnız gösterimde çevirir."""
    _ensure_app()
    from artemis.channels.desktop import telegram_section as section_module
    from artemis.channels.desktop.telegram_section import TelegramSection
    from artemis.channels.telegram import api

    monkeypatch.setattr(api, "read_secret", lambda _target: FAKE_TOKEN)
    monkeypatch.delenv("ARTEMIS_TELEGRAM_BOT_TOKEN", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "workspace": "desktop",
            "state_dir": "state",
            "audit_path": "audit.jsonl",
            "telegram": {"chat_id": CHAT_ID, "run_in_desktop": True},
        }),
        encoding="utf-8",
    )
    set_language("en")
    panel = TelegramSection(cfg)
    try:
        assert "Connected: chat" in panel.status.text()
        panel.set_channel_status(STATUS_RUNNING)
        assert panel.channel_active() is True
        assert "Running" in panel.channel_label.text()
        panel.set_channel_status(STATUS_STARTING)
        assert panel.channel_active() is True
        assert "Starting…" in panel.channel_label.text()
        panel.set_channel_status(STATUS_STOPPED)
        assert "Stopped" in panel.channel_label.text()
        panel.set_channel_status(STATUS_CONFLICT)
        assert "Conflict:" in panel.channel_label.text()
        panel.set_channel_status(f"{STATUS_ERROR} (token yok)")
        assert panel.channel_label.text() == "Channel: Telegram error (no token)"
        panel.set_channel_status(f"{STATUS_ERROR} (model yok)")
        assert panel.channel_label.text() == "Channel: Telegram error (no model)"
        panel.set_channel_status(f"{STATUS_ERROR} (ValueError)")
        assert panel.channel_label.text() == "Channel: Telegram error (ValueError)"
        panel.set_channel_status(STOPPING_TEXT)
        assert "Channel is closing" in panel.channel_label.text()

        # Türkçe gösterim de bozulmaz.
        set_language("tr")
        panel.set_channel_status(f"{STATUS_ERROR} (token yok)")
        assert panel.channel_label.text() == "Kanal: Telegram hatası (token yok)"
    finally:
        panel.finish()
        panel.close()
        _ = section_module


@pytest.mark.parametrize(
    ("code", "tr_reason", "en_reason"),
    [
        ("deep", "derin düşünme istendi", "deep thinking requested"),
        ("multi_step", "çok adımlı istek", "multi-step request"),
        ("code_job", "kod işi taslağı", "code job spec"),
    ],
)
def test_steps_model_choice_translates_reasons(
    code: str, tr_reason: str, en_reason: str
) -> None:
    """`tiering.REASONS` Türkçe kalır; `step_line` gösterimde çevirir."""
    assert REASONS[code] == tr_reason
    set_language("en")
    en_line = step_line("model_choice", {"model": "sonnet", "reason": code}, {})
    assert en_line == StepLine(f"Complex tasks model: sonnet ({en_reason})", "info", False)

    set_language("tr")
    tr_line = step_line("model_choice", {"model": "sonnet", "reason": code}, {})
    assert tr_line == StepLine(f"Zor işler modeli: sonnet ({tr_reason})", "info", False)


def test_window_events_ready_updates_model_in_en_and_tr() -> None:
    """`window_events.handle_state` hem `"Ready · <model>"` hem `"Hazır · <model>"` kabul eder."""
    shown_models: list[str] = []
    statuses: list[tuple[str, str]] = []
    fake_window = SimpleNamespace(
        _active_request=None,
        _busy=False,
        _worker_ready=False,
        _voice_on_ready=False,
        _show_model=shown_models.append,
        _set_status=lambda text, tone: statuses.append((text, tone)),
        _set_controls=lambda: None,
        overlay=SimpleNamespace(set_busy=lambda *_a: None),
        bridge=SimpleNamespace(start_voice=lambda: None),
    )

    set_language("en")
    handle_state(fake_window, State("ready", t("Hazır · {model}").format(model="gpt-4o")))  # type: ignore[arg-type]
    assert shown_models[-1] == "gpt-4o"
    assert statuses[-1] == ("Ready", "ready")

    handle_state(fake_window, State("ready", "Hazır · legacy-model"))  # type: ignore[arg-type]
    assert shown_models[-1] == "legacy-model"

    set_language("tr")
    handle_state(fake_window, State("ready", t("Hazır · {model}").format(model="claude")))  # type: ignore[arg-type]
    assert shown_models[-1] == "claude"
    assert statuses[-1] == ("Hazır", "ready")


def test_data_panel_reminders_and_empty_translate() -> None:
    """DataPanel hatırlatma satırları ve boş durum metinleri EN/TR çevrilir."""
    _ensure_app()
    from artemis.channels.desktop.data_panel import DataPanel

    set_language("en")
    en_panel = DataPanel()
    try:
        en_panel.accept_result(
            "reminder_list",
            ToolResult(
                success=True,
                status="ok",
                message="ok",
                data={
                    "reminders": [
                        {
                            "reminder_id": 1,
                            "state": "uncertain",
                            "due_local": "2026-10-27 12:00",
                            "text": "Call",
                        }
                    ]
                },
            ),
        )
        assert en_panel.items.item(0).text() == "Delivery unverified · 2026-10-27 12:00 · Call"

        empty_res = ToolResult(
            success=True, status="ok", message="ok", data={"reminders": []}
        )
        en_panel.accept_result("reminder_list", empty_res)
        assert en_panel.items_empty.title_label.text() == "No records"
        assert en_panel.items_empty.body_label.text() == "No reminders match this filter."
    finally:
        en_panel.close()

    set_language("tr")
    tr_panel = DataPanel()
    try:
        tr_panel.accept_result(
            "reminder_list",
            ToolResult(success=True, status="ok", message="ok", data={"reminders": []}),
        )
        assert tr_panel.items_empty.title_label.text() == "Kayıt yok"
        assert tr_panel.items_empty.body_label.text() == "Bu filtreye uyan hatırlatma yok."
    finally:
        tr_panel.close()


def test_autostart_and_window_autostart_translate(tmp_path: Path) -> None:
    """Autostart ve pencere tepsi akışı EN/TR çevrilir."""

    class FakeStore:
        def __init__(self) -> None:
            self.value: str | None = None

        def read(self, name: str = VALUE_NAME) -> str | None:
            return self.value

        def write(self, command: str) -> None:
            self.value = command

        def delete(self, name: str = VALUE_NAME) -> None:
            self.value = None

    python_exe = tmp_path / "pythonw.exe"
    python_exe.write_text("", encoding="utf-8")

    def broken_emit(_event: str, **_fields: Any) -> None:
        raise OSError("disk full")

    set_language("en")
    linux_mgr = Autostart(tmp_path / "c.yaml", store=FakeStore(), platform="linux")
    assert linux_mgr.status().message == "Autostart is only available on Windows."

    win_mgr = Autostart(
        tmp_path / "c.yaml",
        store=FakeStore(),
        platform="win32",
        python=str(python_exe),
        emit=broken_emit,
    )
    enabled = win_mgr.enable()
    assert enabled.message == (
        "Artemis will start in the tray at Windows login. (Could not write audit log.)"
    )
    assert win_mgr.status().message == "Autostart is on."
    assert win_mgr.disable().message == "Autostart turned off. (Could not write audit log.)"

    tray_messages: list[str] = []
    fake_window = SimpleNamespace(
        _autostart=lambda: None,
        _autostart_action=None,
        _tray=SimpleNamespace(showMessage=lambda _title, msg, *_a: tray_messages.append(msg)),
    )
    toggle_autostart(fake_window, True)  # type: ignore[arg-type]
    assert tray_messages[-1] == "Could not read settings; autostart entry was not changed."

    set_language("tr")
    toggle_autostart(fake_window, True)  # type: ignore[arg-type]
    assert tray_messages[-1] == "Ayarlar okunamadı; açılış kaydı değiştirilmedi."


def test_connect_wizard_callback_html_translates() -> None:
    """OAuth tarayıcı geri dönüş HTML'i etkin dile göre üretilir."""
    for lang, expected in (("en", "You can return to Artemis"), ("tr", "Artemis'e dönebilirsin")):
        set_language(lang)
        pkce = OpenRouterPkce(http=object())
        auth_url = pkce.start()
        try:
            callback_url = parse_qs(urlsplit(auth_url).query)["callback_url"][0]
            with urllib.request.urlopen(f"{callback_url}&code=abc", timeout=5) as response:
                html_body = response.read().decode("utf-8")
            assert expected in html_body
        finally:
            pkce.close()


async def test_voice_core_notices_translate(tmp_path: Path) -> None:
    """VoiceController kullanıcı bildirimleri EN/TR çevrilir."""

    class FakeMic:
        dropped_frames = 0

        async def frames(self):
            if False:
                yield AudioFrame(b"", 0.0)

        def discard_pending(self) -> None:
            pass

        def pause(self) -> None:
            pass

        def resume(self) -> None:
            pass

        async def close(self) -> None:
            pass

    class FakeVAD:
        def is_speech(self, _pcm: bytes) -> bool:
            return False

    class FakeRecognizer:
        async def transcribe(self, _pcm: bytes) -> str:
            return ""

    class FakeRemoteSynth:
        async def stream(self, _text: str, _cancelled: asyncio.Event):
            if False:
                yield None

        async def close(self) -> None:
            pass

    class FakeSink:
        async def play(self, chunks, _cancelled, _first_pcm) -> None:
            async for _ in chunks:
                pass

        async def close(self) -> None:
            pass

    from artemis.channels.voice.adapters import EdgeTTSynthesizer

    class FakeEdgeSynth(EdgeTTSynthesizer):
        def __init__(self) -> None:
            pass

        async def stream(self, _text: str, _cancelled: asyncio.Event):
            if False:
                yield None

        async def close(self) -> None:
            pass

    notices: list[str] = []
    runner = SimpleNamespace(
        run=lambda *_a, **_kw: None,
        cancel_current=lambda: False,
    )
    controller = VoiceController(
        runner,  # type: ignore[arg-type]
        VoiceConfig(tts_backend="edge"),
        FakeMic(),  # type: ignore[arg-type]
        FakeVAD(),  # type: ignore[arg-type]
        FakeRecognizer(),  # type: ignore[arg-type]
        FakeEdgeSynth(),
        FakeSink(),  # type: ignore[arg-type]
        on_transcript=notices.append,
    )
    try:
        set_language("en")
        async for _ in controller._synth_stream("secret", asyncio.Event(), private=True):
            pass
        assert notices[-1] == (
            "This response was not sent to cloud speech because it contains private content."
        )

        await controller._handle_utterance(AudioSegment(b"\x00\x00", 0.0, 0.5))
        assert notices[-1] == "No clear speech detected; audio was not used as a command."

        set_language("tr")
        async for _ in controller._synth_stream("gizli", asyncio.Event(), private=True):
            pass
        assert notices[-1] == "Bu yanıt gizli içerik taşıdığı için bulut sesine gönderilmedi."
    finally:
        await controller.close()


async def test_runner_and_computer_host_translate(monkeypatch: pytest.MonkeyPatch) -> None:
    """CommandRunner meşgul mesajı ve ComputerHost uyarıları EN/TR çevrilir."""
    from artemis.channels.desktop import computer_host as host_module

    class FakeHotkey:
        def __init__(self, _key: str, _cb: Any) -> None:
            pass

        def start(self) -> bool:
            return False

        def stop(self) -> None:
            pass

    monkeypatch.setattr(host_module, "GlobalHotkey", FakeHotkey)

    fake_loop = SimpleNamespace(
        cleanup_pending=True,
        context=SimpleNamespace(
            settings=SimpleNamespace(context_turns=4, context_token_budget=4000)
        ),
    )
    runner = CommandRunner(fake_loop)  # type: ignore[arg-type]
    set_language("en")
    busy_res = await runner.dispatch_tool("history", SimpleNamespace(model_dump=lambda **_kw: {}))  # type: ignore[arg-type]
    assert busy_res.message == "Another task is running; tool was not executed."
    set_language("tr")
    tr_res = await runner.dispatch_tool("history", SimpleNamespace(model_dump=lambda **_kw: {}))  # type: ignore[arg-type]
    assert tr_res.message == "Başka bir işlem sürüyor; araç çalıştırılmadı."

    statuses: list[str] = []
    host = ComputerHost(lambda *_: True, statuses.append)
    set_language("en")
    with host.session():
        assert statuses[0] == "Esc shortcut is unavailable; use the Cancel button to stop"
        with (
            pytest.raises(ScreenBusyError, match="A computer-use session is already running."),
            host.session(),
        ):
            pass
    _ = json.dumps({"ok": True})
