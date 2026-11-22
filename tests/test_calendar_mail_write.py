"""Onaylı iki yönlü takvim (CalDAV) ve e-posta (SMTP) testleri (ADR 0084)."""

from __future__ import annotations

import base64
import json
import smtplib
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from artemis.brain.models import Completion, Message, ToolCall
from artemis.brain.providers import Provider
from artemis.channels.runtime import create_runtime
from artemis.config.settings import CalendarConfig, ComputerUseConfig, MailConfig, Settings
from artemis.i18n import set_language
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools import calendar_mail as core_read
from artemis.tools import calendar_mail_write as core_write
from artemis.tools.base import Risk, ToolContext, ToolResult
from artemis.tools.plugins.calendar_mail import CalendarEvents, MailUnread
from artemis.tools.plugins.calendar_mail_write import CalendarCreateEvent, MailSend
from artemis.tools.registry import ToolRegistry

TZ = ZoneInfo("Europe/Istanbul")
SECRET_SMTP_PASS = "smtp-super-secret-99"
SECRET_CALDAV_PASS = "caldav-super-secret-88"


@pytest.fixture(autouse=True)
def _clean_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_read, "read_secret", lambda _target: None)
    for env_key in (
        core_read.ICS_ENV,
        core_read.MAIL_ENV,
        core_write.CALDAV_PASSWORD_ENV,
        core_write.SMTP_PASSWORD_ENV,
    ):
        monkeypatch.delenv(env_key, raising=False)
    set_language("tr")


class _StubProvider(Provider):
    async def complete(
        self,
        messages: list[Message],
        tools: list[Any],
        *,
        timeout: float = 30.0,
    ) -> Completion:
        return Completion(content="ok")


def _make_settings(
    tmp_path: Path,
    *,
    calendar: dict[str, Any] | None = None,
    mail: dict[str, Any] | None = None,
    auto_mode: bool = False,
) -> Settings:
    return Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        calendar=CalendarConfig(**(calendar or {})),
        mail=MailConfig(**(mail or {})),
        computer_use=ComputerUseConfig(mode="auto" if auto_mode else "accept"),
    )


class _Harness:
    def __init__(self, tmp_path: Path, settings: Settings) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        registry = ToolRegistry()
        registry.register(CalendarEvents)
        registry.register(MailUnread)
        registry.register(CalendarCreateEvent)
        registry.register(MailSend)
        self.gate = SafetyGate(registry)
        self.context = ToolContext(tmp_path, settings, AuditLog(settings.audit_path))
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self,
        name: str,
        *,
        approve_answer: bool = True,
        state: TurnState | None = None,
        **args: Any,
    ) -> ToolResult:
        def approve(tool: str, arguments: dict[str, Any]) -> bool:
            self.asked.append((tool, dict(arguments)))
            return approve_answer

        return await self.gate.dispatch(
            ToolCall(id="call-1", name=name, arguments=args),
            self.context,
            state or TurnState(),
            approve,
        )

    def audit_records(self) -> list[dict[str, Any]]:
        if not self.settings.audit_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.settings.audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


# Config & Opt-in / Runtime --------------------------------------------------------------------


def test_config_defaults_and_caldav_url_validation() -> None:
    cal = CalendarConfig()
    assert cal.allow_write is False
    assert cal.caldav_url == ""
    assert cal.caldav_username == ""

    mail = MailConfig()
    assert mail.allow_send is False
    assert mail.smtp_host == ""
    assert mail.smtp_port == 465
    assert mail.smtp_username == ""

    with pytest.raises(ValidationError):
        CalendarConfig(caldav_url="http://cal.example/dav")
    with pytest.raises(ValidationError):
        CalendarConfig(caldav_url="https://user:secret@cal.example/dav")
    with pytest.raises(ValidationError):
        CalendarConfig(caldav_url="https://cal.example/dav?token=1")

    valid = CalendarConfig(caldav_url="https://cal.example/dav/")
    assert valid.caldav_url == "https://cal.example/dav"
    loopback = CalendarConfig(caldav_url="http://127.0.0.1:8080/dav/")
    assert loopback.caldav_url == "http://127.0.0.1:8080/dav"


async def test_opt_in_false_refuses_without_approval_and_hides_in_runtime(
    tmp_path: Path,
) -> None:
    settings = _make_settings(
        tmp_path,
        calendar={"enabled": True, "allow_write": False},
        mail={"enabled": True, "allow_send": False},
    )
    h = _Harness(tmp_path, settings)

    res_mail = await h.call(
        "mail_send",
        to="ali@example.com",
        subject="Deneme",
        body="Merhaba",
    )
    res_cal = await h.call(
        "calendar_create_event",
        title="Toplantı",
        start="2026-11-23T10:00",
        end="2026-11-23T11:00",
    )
    assert not res_mail.success and "kapalı" in res_mail.message
    assert not res_cal.success and "kapalı" in res_cal.message
    assert h.asked == []

    runtime = create_runtime(
        tmp_path / "config.yaml",
        lambda *_: False,
        provider=_StubProvider(),
        settings=settings,
        with_code_agent=False,
    )
    try:
        names = {d.name for d in runtime.registry.definitions()}
        assert {"calendar_events", "mail_unread"} <= names
        assert "calendar_create_event" not in names
        assert "mail_send" not in names
    finally:
        runtime.shutdown()

    enabled_settings = _make_settings(
        tmp_path / "on",
        calendar={"enabled": True, "allow_write": True},
        mail={"enabled": True, "allow_send": True},
    )
    runtime_on = create_runtime(
        tmp_path / "on" / "config.yaml",
        lambda *_: False,
        provider=_StubProvider(),
        settings=enabled_settings,
        with_code_agent=False,
    )
    try:
        names_on = {d.name for d in runtime_on.registry.definitions()}
        assert {"calendar_create_event", "mail_send"} <= names_on
    finally:
        runtime_on.shutdown()


# SafetyGate invariants ------------------------------------------------------------------------


async def test_safety_gate_requires_confirm_even_in_auto_mode_and_blocks_unattended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(core_write.SMTP_PASSWORD_ENV, SECRET_SMTP_PASS)
    monkeypatch.setenv(core_write.CALDAV_PASSWORD_ENV, SECRET_CALDAV_PASS)
    assert MailSend.risk is Risk.CONFIRM_REQUIRED
    assert MailSend.outbound is True
    assert MailSend.auto_mode_ok is False
    assert CalendarCreateEvent.risk is Risk.CONFIRM_REQUIRED
    assert CalendarCreateEvent.outbound is True
    assert CalendarCreateEvent.auto_mode_ok is False

    settings = _make_settings(
        tmp_path,
        calendar={
            "enabled": True,
            "allow_write": True,
            "caldav_url": "https://cal.example/dav",
            "caldav_username": "u@example.com",
        },
        mail={
            "enabled": True,
            "allow_send": True,
            "smtp_host": "smtp.example.com",
            "smtp_username": "u@example.com",
        },
        auto_mode=True,
    )
    h = _Harness(tmp_path, settings)
    sent: list[Any] = []
    puts: list[Any] = []
    h.context.services["smtp_connect"] = lambda *_: sent.append("connected")
    h.context.services["calendar_put"] = lambda *args: puts.append(args) or 201

    # computer_use.mode == "auto" kipinde bile onay sorulur ve Hayır'da ağ çağrısı yapılmaz.
    res_mail = await h.call(
        "mail_send",
        approve_answer=False,
        to="ali@example.com",
        subject="Konu",
        body="Gövde",
    )
    res_cal = await h.call(
        "calendar_create_event",
        approve_answer=False,
        title="Toplantı",
        start="2026-11-23T10:00",
        end="2026-11-23T11:00",
    )
    assert not res_mail.success and not res_cal.success
    assert [name for name, _ in h.asked] == ["mail_send", "calendar_create_event"]
    assert sent == [] and puts == []

    # Gözetimsiz turda onay sorulmadan engellenir.
    h.asked.clear()
    unattended_mail = await h.call(
        "mail_send",
        approve_answer=True,
        state=TurnState(unattended=True),
        to="ali@example.com",
        subject="Konu",
        body="Gövde",
    )
    unattended_cal = await h.call(
        "calendar_create_event",
        approve_answer=True,
        state=TurnState(unattended=True),
        title="Toplantı",
        start="2026-11-23T10:00",
        end="2026-11-23T11:00",
    )
    assert not unattended_mail.success and "Gözetimsiz" in unattended_mail.message
    assert not unattended_cal.success and "Gözetimsiz" in unattended_cal.message
    assert h.asked == []
    assert sent == [] and puts == []


# SMTP Transport & Plugin ----------------------------------------------------------------------


class _FakeSmtp:
    def __init__(self, *, refused: dict[str, Any] | None = None, auth_fail: bool = False) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.refused = refused or {}
        self.auth_fail = auth_fail
        self.sent_messages: list[Any] = []

    def ehlo(self) -> tuple[int, bytes]:
        self.calls.append(("ehlo", None))
        return 250, b"ok"

    def starttls(self, context: Any = None) -> tuple[int, bytes]:
        self.calls.append(("starttls", context is not None))
        return 220, b"ready"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.calls.append(("login", user))
        if self.auth_fail:
            raise smtplib.SMTPAuthenticationError(535, b"bad auth")
        return 235, b"ok"

    def send_message(self, msg: Any) -> dict[str, Any]:
        self.sent_messages.append(msg)
        return self.refused

    def quit(self) -> tuple[int, bytes]:
        self.calls.append(("quit", None))
        return 221, b"bye"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"to": "a@b.com\r\nBcc: evil@x.com"},
        {"cc": "c@d.com\nBcc: evil@x.com"},
        {"subject": "Konu\r\nX-Injected: 1"},
        {"username": "u@b.com\0"},
        {"to": "gecersiz-adres"},
        {"to": "a@b.com", "cc": "kotu"},
    ],
)
def test_smtp_rejects_header_injection_and_bad_recipients_before_connect(
    kwargs: dict[str, str],
) -> None:
    called = False

    def connect(_host: str, _port: int) -> Any:
        nonlocal called
        called = True
        return _FakeSmtp()

    params = {
        "host": "smtp.example.com",
        "port": 465,
        "username": "sender@example.com",
        "password": SECRET_SMTP_PASS,
        "to": "ali@example.com",
        "subject": "Konu",
        "body": "Merhaba",
        "cc": "",
    }
    params.update(kwargs)
    with pytest.raises(core_read.SourceError):
        core_write.send_mail(connect, **params)  # type: ignore[arg-type]
    assert not called


def test_connect_smtp_uses_ssl_on_465_and_starttls_on_587(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[str, str, int]] = []

    class FakeSSL(_FakeSmtp):
        def __init__(self, host: str, port: int, context: Any = None, timeout: float = 0) -> None:
            super().__init__()
            created.append(("ssl", host, port))

    class FakePlain(_FakeSmtp):
        def __init__(self, host: str, port: int, timeout: float = 0) -> None:
            super().__init__()
            created.append(("plain", host, port))

    monkeypatch.setattr(core_write.smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setattr(core_write.smtplib, "SMTP", FakePlain)

    ssl_client = core_write.connect_smtp("smtp.example.com", 465)
    assert isinstance(ssl_client, FakeSSL)
    assert created[-1] == ("ssl", "smtp.example.com", 465)

    tls_client = core_write.connect_smtp("smtp.example.com", 587)
    assert isinstance(tls_client, FakePlain)
    assert ("starttls", True) in tls_client.calls
    assert tls_client.calls.count(("ehlo", None)) == 2


async def test_mail_send_success_refused_and_fallback_secret_without_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(core_read.MAIL_ENV, SECRET_SMTP_PASS)  # fallback secret
    settings = _make_settings(
        tmp_path,
        mail={
            "enabled": True,
            "allow_send": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "username": "ben@example.com",
        },
    )
    h = _Harness(tmp_path, settings)
    fake = _FakeSmtp()
    h.context.services["smtp_connect"] = lambda _host, _port: fake

    result = await h.call(
        "mail_send",
        to="ali@example.com",
        cc="veli@example.com",
        subject="Proje toplantısı",
        body="Yarın saat 10:00'da görüşelim.\nİyi çalışmalar.",
    )
    assert result.success, result.message
    assert len(fake.sent_messages) == 1
    msg = fake.sent_messages[0]
    assert msg["From"] == "ben@example.com"
    assert msg["To"] == "ali@example.com"
    assert msg["Cc"] == "veli@example.com"
    assert msg["Subject"] == "Proje toplantısı"
    assert "Yarın saat 10:00'da görüşelim." in msg.get_content()

    # Refused recipient ve auth hatası sır sızdırmaz.
    h.context.services["smtp_connect"] = lambda *_: _FakeSmtp(
        refused={"ali@example.com": (550, b"no")}
    )
    refused_res = await h.call(
        "mail_send", to="ali@example.com", subject="Test", body="Gövde"
    )
    assert not refused_res.success and "reddedildi" in refused_res.message

    h.context.services["smtp_connect"] = lambda *_: _FakeSmtp(auth_fail=True)
    auth_res = await h.call("mail_send", to="ali@example.com", subject="Test", body="Gövde")
    assert not auth_res.success and SECRET_SMTP_PASS not in auth_res.message

    audit_raw = settings.audit_path.read_text(encoding="utf-8")
    assert SECRET_SMTP_PASS not in audit_raw
    assert SECRET_SMTP_PASS not in repr(h.context)


# CalDAV Transport & Loopback Server -----------------------------------------------------------


def test_build_vevent_ics_escaping_bounds_and_roundtrip() -> None:
    with pytest.raises(core_read.SourceError):
        core_write.build_vevent_ics(
            uid="u1", title="Tatil", start="2026-11-23", end="2026-11-23"
        )
    with pytest.raises(core_read.SourceError):
        core_write.build_vevent_ics(
            uid="u2", title="Toplantı", start="2026-11-23T11:00", end="2026-11-23T10:00"
        )
    with pytest.raises(core_read.SourceError):
        core_write.build_vevent_ics(
            uid="u3", title="Karışık", start="2026-11-23", end="2026-11-23T10:00"
        )

    ics = core_write.build_vevent_ics(
        uid="evt-123",
        title="Bütçe, plan; gözden\\geçirme",
        start="2026-11-23T14:00",
        end="2026-11-23T15:30",
        location="Oda A, Kat 2",
        description="1. satır\n2. satır; devam",
        local_tz=TZ,
    )
    assert "SUMMARY:Bütçe\\, plan\\; gözden\\\\geçirme" in ics
    assert "DESCRIPTION:1. satır\\n2. satır\\; devam" in ics

    events, warnings = core_read.parse_events(
        ics.encode("utf-8"),
        TZ,
        datetime(2026, 11, 23, 0, 0, tzinfo=TZ),
        datetime(2026, 11, 24, 0, 0, tzinfo=TZ),
    )
    assert not warnings
    assert len(events) == 1
    assert events[0].title == "Bütçe, plan; gözden\\geçirme"
    assert events[0].location == "Oda A, Kat 2"


async def test_caldav_loopback_http_put_and_error_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(core_write.CALDAV_PASSWORD_ENV, SECRET_CALDAV_PASS)
    received: list[dict[str, Any]] = []
    next_status = 201

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            received.append(
                {
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "if_none_match": self.headers.get("If-None-Match"),
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                }
            )
            self.send_response(next_status)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/calendars/user/default"
        settings = _make_settings(
            tmp_path,
            calendar={
                "enabled": True,
                "allow_write": True,
                "caldav_url": base_url,
                "caldav_username": "caluser",
            },
        )
        h = _Harness(tmp_path, settings)
        res = await h.call(
            "calendar_create_event",
            title="Haftalık eşitleme",
            start="2026-11-23T09:30",
            end="2026-11-23T10:15",
            location="Çevrimiçi",
            description="Gündem:\n- Riskler\n- Teslim",
        )
        assert res.success, res.message
        assert len(received) == 1
        req = received[0]
        assert req["path"].startswith("/calendars/user/default/") and req["path"].endswith(".ics")
        assert req["content_type"] == "text/calendar; charset=utf-8"
        assert req["if_none_match"] == "*"
        expected_auth = "Basic " + base64.b64encode(
            f"caluser:{SECRET_CALDAV_PASS}".encode()
        ).decode("ascii")
        assert req["authorization"] == expected_auth
        assert "BEGIN:VEVENT" in req["body"] and "SUMMARY:Haftalık eşitleme" in req["body"]

        next_status = 403
        err_res = await h.call(
            "calendar_create_event",
            title="Yetkisiz",
            start="2026-11-23T11:00",
            end="2026-11-23T12:00",
        )
        assert not err_res.success
        assert SECRET_CALDAV_PASS not in err_res.message
        assert "127.0.0.1" not in err_res.message
    finally:
        server.shutdown()
        server.server_close()

    with pytest.raises(core_read.SourceError, match="https://"):
        core_write.put_caldav_event("http://cal.example/dav/a.ics", "BEGIN:VCALENDAR")

    audit_raw = settings.audit_path.read_text(encoding="utf-8")
    assert SECRET_CALDAV_PASS not in audit_raw
    assert SECRET_CALDAV_PASS not in repr(h.context)


# ApprovalDialog Full Outbound Preview ---------------------------------------------------------


def test_approval_dialog_shows_untruncated_outbound_preview_and_defaults_to_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication, QDialog, QWidget

    from artemis.channels.desktop.approval_dialog import ApprovalDialog
    from artemis.channels.desktop.bridge import Approval

    QApplication.instance() or QApplication([])
    parent = QWidget()
    long_body = (
        "Merhaba Ali,\n\n"
        + ("Bu metin 80 karakter sınırını fazlasıyla aşan tam e-posta gövdesidir. " * 4)
        + "\nSaygılar."
    )
    mail_item = Approval(
        "req-mail",
        "turn-1",
        "app-1",
        "mail_send",
        {
            "to": "ali@example.com",
            "cc": "ekip@example.com",
            "subject": "Onaylı gönderim testi",
            "body": long_body,
        },
        "Bu adım dış içerik okunduktan sonra istendi. Açıklama",
        None,
        "E-posta gönder",
    )
    decisions: list[bool] = []
    mail_dialog = ApprovalDialog(
        mail_item,
        lambda _item, ok, _site: decisions.append(ok) or ok,
        parent,
    )
    try:
        assert mail_dialog.outbound_preview_box is not None
        assert mail_dialog.outbound_preview_text is not None
        assert mail_dialog.outbound_preview_text.isReadOnly()
        preview_text = mail_dialog.outbound_preview_text.toPlainText()
        assert "Alıcı: ali@example.com" in preview_text
        assert "Bilgi (Cc): ekip@example.com" in preview_text
        assert "Konu: Onaylı gönderim testi" in preview_text
        assert long_body in preview_text
        assert mail_dialog.no_button.isDefault()
        assert not mail_dialog.yes_button.isDefault()

        # Enter tuşu varsayılan Hayır'ı seçer (yanlışlıkla gönderim yok).
        mail_dialog.keyPressEvent(
            QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        )
        assert decisions == [False]
        assert mail_dialog.result() == QDialog.DialogCode.Rejected
    finally:
        mail_dialog.deleteLater()

    long_desc = "Satır 1: Kurulum.\n" + ("Ayrıntı notu " * 15) + "\nSatır 3: Kapanış."
    cal_item = Approval(
        "req-cal",
        "turn-2",
        "app-1",
        "calendar_create_event",
        {
            "title": "Mimari gözden geçirme",
            "start": "2026-11-24T10:00",
            "end": "2026-11-24T11:00",
            "location": "Toplantı Odası B",
            "description": long_desc,
        },
        "Bu işlem onay gerektirir.. Açıklama",
        None,
        "Takvim etkinliği oluştur",
    )
    set_language("en")
    cal_dialog = ApprovalDialog(cal_item, lambda *_: True, parent)
    try:
        assert cal_dialog.outbound_preview_text is not None
        en_preview = cal_dialog.outbound_preview_text.toPlainText()
        assert "Title: Mimari gözden geçirme" in en_preview
        assert "Location: Toplantı Odası B" in en_preview
        assert long_desc in en_preview
    finally:
        cal_dialog._choose(False)
        cal_dialog.deleteLater()
        parent.deleteLater()


# Settings UI ----------------------------------------------------------------------------------


def test_settings_ui_write_toggles_follow_parent_and_secrets_stay_out_of_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QLineEdit

    from artemis.channels.desktop.settings_io import save_choices
    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.config.settings import load_settings
    from tests.test_settings_panel import config

    QApplication.instance() or QApplication([])
    path = config(tmp_path)
    save_choices(
        path,
        None,
        values={
            ("calendar", "enabled"): False,
            ("calendar", "allow_write"): True,
            ("calendar", "caldav_url"): "https://cal.example.com/dav",
            ("calendar", "caldav_username"): "cal-u",
            ("mail", "enabled"): True,
            ("mail", "allow_send"): False,
            ("mail", "smtp_host"): "smtp.example.com",
            ("mail", "smtp_port"): 587,
            ("mail", "smtp_username"): "smtp-u",
        },
    )
    panel = SettingsPanel(path)
    try:
        assert not panel.calendar_allow_write.isEnabled()
        assert panel.calendar_allow_write.isChecked() is True
        assert panel.mail_allow_send.isEnabled()
        assert panel.mail_allow_send.isChecked() is False
        panel.calendar_enabled.setChecked(True)
        assert panel.calendar_allow_write.isEnabled()
        panel.mail_enabled.setChecked(False)
        assert not panel.mail_allow_send.isEnabled()

        assert panel.calendar_mail.caldav_password.echoMode() == QLineEdit.EchoMode.Password
        assert panel.calendar_mail.smtp_password.echoMode() == QLineEdit.EchoMode.Password
        panel.calendar_mail._save_caldav()
        assert "parolasını yaz" in panel.calendar_mail.message.text()
        panel.calendar_mail._save_smtp()
        assert "parolasını yaz" in panel.calendar_mail.message.text()

        saved = load_settings(path)
        assert saved.calendar.caldav_url == "https://cal.example.com/dav"
        assert saved.mail.smtp_port == 587
    finally:
        panel.deleteLater()
