"""Takvim ve posta (ADR 0059): sahte getirici ve sahte IMAP; ağ ve gerçek hesap yoktur."""

from __future__ import annotations

import asyncio
import email
import http.client
import imaplib
import json
from datetime import datetime, timedelta
from email import policy
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from artemis.brain.models import ToolCall
from artemis.config.settings import CalendarConfig, MailConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools import calendar_mail as core
from artemis.tools.base import ToolContext, ToolResult
from artemis.tools.plugins.calendar_mail import CalendarEvents, MailUnread
from artemis.tools.registry import ToolRegistry

TZ = ZoneInfo("Europe/Istanbul")
SECRET_URL = "https://calendar.example/private/abc123SECRETxyz/basic.ics"
SECRET_PASSWORD = "hunter2-app-password"

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:weekly
SUMMARY:Haftalık toplantı
DTSTART;TZID=Europe/Istanbul:20261102T100000
DTEND;TZID=Europe/Istanbul:20261102T110000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE
EXDATE;TZID=Europe/Istanbul:20261111T100000
END:VEVENT
BEGIN:VEVENT
UID:weekly2
SUMMARY:Her çarşamba\\, spor
DTSTART;TZID=Europe/Istanbul:20261104T190000
DTEND;TZID=Europe/Istanbul:20261104T200000
RRULE:FREQ=WEEKLY;COUNT=10
LOCATION:Salon
END:VEVENT
BEGIN:VEVENT
UID:allday
SUMMARY:Tatil
DTSTART;VALUE=DATE:20261111
DTEND;VALUE=DATE:20261112
END:VEVENT
BEGIN:VEVENT
UID:utc
SUMMARY:UTC görüşme
DTSTART:20261111T060000Z
DTEND:20261111T070000Z
END:VEVENT
BEGIN:VEVENT
UID:odd
SUMMARY:Ayın ikinci salısı
DTSTART;TZID=Europe/Istanbul:20261110T120000
DTEND;TZID=Europe/Istanbul:20261110T130000
RRULE:FREQ=MONTHLY;BYDAY=2TU
END:VEVENT
BEGIN:VEVENT
UID:gone
SUMMARY:İptal
STATUS:CANCELLED
DTSTART;TZID=Europe/Istanbul:20261111T150000
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "read_secret", lambda target: None)  # gerçek kasaya dokunma
    monkeypatch.delenv(core.ICS_ENV, raising=False)
    monkeypatch.delenv(core.MAIL_ENV, raising=False)


def make_settings(tmp_path: Path, calendar: dict[str, Any], mail: dict[str, Any]) -> Settings:
    return Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        persist_memory=False,
        calendar=CalendarConfig(enabled=True, **calendar),
        mail=MailConfig(enabled=True, host="imap.example", username="u@example", **mail),
    )


class Env:
    def __init__(
        self,
        tmp_path: Path,
        *,
        calendar: dict[str, Any] | None = None,
        mail: dict[str, Any] | None = None,
        settings: Settings | None = None,
    ) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.settings = settings or make_settings(tmp_path, calendar or {}, mail or {})
        registry = ToolRegistry()
        registry.register(CalendarEvents)
        registry.register(MailUnread)
        self.gate = SafetyGate(registry)
        self.context = ToolContext(tmp_path, self.settings, AuditLog(self.settings.audit_path))
        self.asked: list[str] = []

    async def call(self, name: str, state: TurnState | None = None, **args: Any) -> ToolResult:
        def approve(tool: str, arguments: dict[str, Any]) -> bool:
            self.asked.append(tool)
            return False

        return await self.gate.dispatch(
            ToolCall(id="c", name=name, arguments=args), self.context, state or TurnState(), approve
        )

    def audit_text(self) -> str:
        return self.settings.audit_path.read_text(encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setenv(core.ICS_ENV, SECRET_URL)
    monkeypatch.setenv(core.MAIL_ENV, SECRET_PASSWORD)
    e = Env(tmp_path)
    e.context.services["calendar_fetch"] = lambda url: ICS.encode()
    return e


def titles(result: ToolResult) -> list[str]:
    return [event["title"] for event in result.data["events"]]


# Takvim ---------------------------------------------------------------------------------------


async def test_today_with_recurrence_exdate_allday_and_timezone(env: Env) -> None:
    result = await env.call("calendar_events", start_day="2026-11-11")
    assert result.success, result.message
    # Pazartesi/Çarşamba toplantısının 11 Kasım oluşumu EXDATE ile düşer.
    assert titles(result) == ["Tatil", "UTC görüşme", "Her çarşamba, spor"]
    by_title = {e["title"]: e for e in result.data["events"]}
    assert by_title["Tatil"]["all_day"] is True
    assert by_title["UTC görüşme"]["start"] == "2026-11-11T09:00+03:00"  # 06:00Z -> İstanbul
    assert by_title["Her çarşamba, spor"]["location"] == "Salon"
    assert by_title["Her çarşamba, spor"]["recurring"] is True


async def test_range_includes_other_weekly_occurrences(env: Env) -> None:
    result = await env.call("calendar_events", start_day="2026-11-09", days=3)
    starts = [e["start"] for e in result.data["events"] if e["title"] == "Haftalık toplantı"]
    assert starts == ["2026-11-09T10:00+03:00"]  # 11'i EXDATE'li


async def test_unsupported_rule_is_flagged_not_dropped(env: Env) -> None:
    result = await env.call("calendar_events", start_day="2026-11-10")
    assert "Ayın ikinci salısı" in titles(result)  # ilk oluşum gösterilir
    assert any("yinelenen" in w and "Ayın ikinci" in w for w in result.data["warnings"])


def test_override_replaces_occurrence() -> None:
    ics = """BEGIN:VEVENT
UID:a
SUMMARY:Ana
DTSTART:20261109T070000Z
DTEND:20261109T080000Z
RRULE:FREQ=DAILY;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:a
RECURRENCE-ID:20261110T070000Z
SUMMARY:Kaydırılmış
DTSTART:20261110T120000Z
DTEND:20261110T130000Z
END:VEVENT"""
    begin = datetime(2026, 11, 10, tzinfo=TZ)
    events, _ = core.parse_events(ics.encode(), TZ, begin, datetime(2026, 11, 11, tzinfo=TZ))
    assert [e.title for e in events] == ["Kaydırılmış"]


def test_http_url_and_oversize_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(core.SourceError, match="https"):
        core.fetch_ics("http://calendar.example/x.ics")

    class Resp:
        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self, n: int) -> bytes:
            return b"x" * n

    class Opener:
        def open(self, url: str, timeout: float) -> Resp:
            return Resp()

    monkeypatch.setattr(core.urllib.request, "build_opener", lambda *h: Opener())
    with pytest.raises(core.SourceError, match="2 MB"):
        core.fetch_ics(SECRET_URL)


def test_redirect_to_http_is_refused() -> None:
    handler = core._HttpsOnly()
    with pytest.raises(core.SourceError, match="https olmayan"):
        handler.redirect_request(None, None, 302, "", {}, "http://evil.example/")


async def test_fetch_error_message_has_no_url(env: Env) -> None:
    def boom(url: str) -> bytes:
        raise core.SourceError("Takvime ulaşılamadı (URLError).")

    env.context.services["calendar_fetch"] = boom
    result = await env.call("calendar_events")
    assert not result.success and "abc123" not in result.message


# Posta ----------------------------------------------------------------------------------------

PLAIN = (
    b"From: Ali <ali@example>\r\nSubject: =?utf-8?q?Toplant=C4=B1?=\r\n"
    b"Date: Tue, 10 Nov 2026 10:00:00 +0300\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
    + ("Merhaba " * 100).encode()
)
HTML = (
    b"From: b@example\r\nSubject: Html\r\nDate: x\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    b"<html><style>p{}</style><body><p>Selam <b>dunya</b></p><script>evil()</script></body></html>"
)


class FakeImap:
    def __init__(self, messages: list[bytes]) -> None:
        self.messages = messages
        self.calls: list[tuple[str, Any]] = []

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", user))
        return "OK", []

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", readonly))
        return "OK", [b"1"]

    def search(self, charset: Any, criterion: str) -> tuple[str, list[bytes]]:
        self.calls.append(("search", criterion))
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, number: bytes, spec: str) -> tuple[str, list[Any]]:
        self.calls.append(("fetch", spec))
        return "OK", [(b"1 (BODY[]", self.messages[int(number) - 1]), b")"]

    def store(self, *args: Any) -> None:
        self.calls.append(("store", args))

    def copy(self, *args: Any) -> None:
        self.calls.append(("copy", args))

    def expunge(self) -> None:
        self.calls.append(("expunge", None))

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout", None))
        return "BYE", []


async def test_mail_is_readonly_peek_with_snippet_cap_and_html_strip(env: Env) -> None:
    fake = FakeImap([PLAIN, HTML])
    env.context.services["mail_connect"] = lambda host, port: fake
    result = await env.call("mail_unread")
    assert result.success, result.message
    assert ("select", True) in fake.calls
    assert all("BODY.PEEK" in spec for kind, spec in fake.calls if kind == "fetch")
    assert not any(kind in {"store", "copy", "expunge"} for kind, _ in fake.calls)
    assert not any("Seen" in str(args) for _, args in fake.calls)
    html, plain = result.data["messages"]  # en yeni önce
    assert html["snippet"] == "Selam dunya"
    assert plain["subject"] == "Toplantı" and len(plain["snippet"]) == 300
    assert plain["from"].startswith("Ali")
    assert result.data["unread_total"] == 2


async def test_mail_skips_attachment_parts(env: Env) -> None:
    raw = (
        b"From: a@x\r\nSubject: Ek\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; "
        b"filename=a.pdf\r\n\r\nGIZLI-EK\r\n--B\r\nContent-Type: text/plain\r\n\r\n"
        b"govde\r\n--B--\r\n"
    )
    env.context.services["mail_connect"] = lambda host, port: FakeImap([raw])
    result = await env.call("mail_unread")
    assert result.data["messages"][0]["snippet"] == "govde"


async def test_mail_login_failure_is_honest(env: Env) -> None:
    class Bad(FakeImap):
        def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    env.context.services["mail_connect"] = lambda host, port: Bad([])
    result = await env.call("mail_unread")
    assert not result.success and "reddedildi" in result.message
    assert SECRET_PASSWORD not in result.message


# Gizlilik, sır, kapı --------------------------------------------------------------------------


async def test_results_are_external_and_private_unless_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(core.ICS_ENV, SECRET_URL)
    monkeypatch.setenv(core.MAIL_ENV, SECRET_PASSWORD)
    for allow in (False, True):
        e = Env(
            tmp_path / str(allow), calendar={"allow_cloud": allow}, mail={"allow_cloud": allow}
        )
        e.context.services["calendar_fetch"] = lambda url: ICS.encode()
        e.context.services["mail_connect"] = lambda host, port: FakeImap([PLAIN])
        for tool in ("calendar_events", "mail_unread"):
            result = await e.call(tool)
            assert result.success and result.external
            assert result.private is (not allow)


async def test_secrets_never_reach_audit_or_repr(env: Env) -> None:
    env.context.services["mail_connect"] = lambda host, port: FakeImap([PLAIN])
    await env.call("calendar_events")
    await env.call("mail_unread")
    log = env.audit_text()
    assert "abc123" not in log and SECRET_PASSWORD not in log
    assert "abc123" not in repr(env.context) and SECRET_PASSWORD not in repr(env.context)
    assert "abc123" not in json.dumps(env.settings.model_dump(mode="json"))


async def test_unattended_turn_runs_both_without_asking(env: Env) -> None:
    env.context.services["mail_connect"] = lambda host, port: FakeImap([PLAIN])
    state = TurnState(unattended=True)
    assert (await env.call("calendar_events", state)).success
    assert (await env.call("mail_unread", state)).success
    assert env.asked == []


async def test_disabled_is_refused_without_approval_and_hidden(tmp_path: Path) -> None:
    off = Settings(
        workspace=tmp_path, state_dir=tmp_path / "s", audit_path=tmp_path / "a.jsonl",
        persist_memory=False,
    )
    e = Env(tmp_path, settings=off)
    for tool in ("calendar_events", "mail_unread"):
        result = await e.call(tool)
        assert not result.success and "kapalı" in result.message
    assert e.asked == []
    registry = ToolRegistry()
    registry.register(CalendarEvents)
    registry.register(MailUnread)
    assert registry.without_prefix("calendar_").without_prefix("mail_").definitions() == []


def test_missing_secret_is_reported(tmp_path: Path) -> None:
    result = asyncio.run(Env(tmp_path).call("calendar_events"))
    assert not result.success and "adresi yok" in result.message


def _ics(*events: str) -> bytes:
    return ("BEGIN:VCALENDAR\n" + "\n".join(events) + "\nEND:VCALENDAR\n").encode()


def _event(uid: str, *lines: str) -> str:
    return "\n".join(["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{uid}", *lines, "END:VEVENT"])


def test_hostile_rules_do_not_raise_or_hide_other_events() -> None:
    """Bozuk tek olay (dev aralık, bozuk RECURRENCE-ID) takvimin kalanını okunmaz yapmaz."""
    data = _ics(
        _event("dev", "DTSTART:20261101T090000", "RRULE:FREQ=DAILY;INTERVAL=99999999999"),
        _event("sifir", "DTSTART:20261101T090000", "RRULE:FREQ=DAILY;INTERVAL=0"),
        _event("bozuk", "DTSTART:20261110T090000", "RECURRENCE-ID:bad"),
        _event("saglam", "DTSTART:20261110T100000"),
    )
    begin = datetime(2026, 11, 10, tzinfo=TZ)
    events, warnings = core.parse_events(data, TZ, begin, datetime(2026, 11, 11, tzinfo=TZ))
    assert "saglam" in [event.title for event in events]
    assert warnings


def test_utc_until_keeps_last_local_occurrence_after_midnight() -> None:
    data = _ics(
        _event(
            "gece",
            "DTSTART;TZID=Europe/Istanbul:20261020T003000",
            "RRULE:FREQ=DAILY;UNTIL=20261021T213000Z",
        )
    )
    begin = datetime(2026, 10, 22, tzinfo=TZ)
    events, _ = core.parse_events(data, TZ, begin, datetime(2026, 10, 24, tzinfo=TZ))
    # 22 Ekim 00:30 İstanbul = 21 Ekim 21:30Z: UNTIL dahildir; 23 Ekim dışarıda kalır.
    assert len(events) == 1


def _calendar_mail_section(tmp_path: Path) -> Any:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QVBoxLayout

    from artemis.channels.desktop.calendar_mail_section import CalendarMailSection

    app = QApplication.instance() or QApplication([])
    section = CalendarMailSection(
        tmp_path / "config.yaml", calendar=QVBoxLayout(), mail=QVBoxLayout()
    )
    return app, section


def test_section_invalid_timezone_reports_error_without_crash(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QWidget

    from artemis.channels.desktop.calendar_mail_section import _Job, check_calendar

    _app, section = _calendar_mail_section(tmp_path)
    parent = QWidget()
    message = check_calendar("https://example.invalid/a.ics", "Bilinmeyen/Bolge")
    assert message is not None
    job = _Job(lambda: check_calendar("https://example.invalid/a.ics", "Bilinmeyen/Bolge"), parent)
    job.run()
    assert job.error is not None and not job.verified
    section.deleteLater()
    parent.deleteLater()


def test_section_source_error_in_job_sets_error_and_never_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from artemis.channels.desktop import calendar_mail_section as mod

    app, section = _calendar_mail_section(tmp_path)
    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "write_secret", lambda s, t: saved.append((s, t)))

    def boom() -> str | None:
        raise core.SourceError("sunucu hatası")

    section._start(boom, "gizli-deger", core.ICS_TARGET, "Takvim")
    assert section._job is not None
    assert section._job.wait(3000)
    app.processEvents()
    assert section._job.error == "sunucu hatası"
    assert not section._job.verified
    assert saved == []
    assert "Kaydedilmedi" in section.message.text()
    section.deleteLater()


def test_section_missing_result_never_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from artemis.channels.desktop import calendar_mail_section as mod

    app, section = _calendar_mail_section(tmp_path)
    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "write_secret", lambda s, t: saved.append((s, t)))
    # run() sonuç üretmeden biterse verified=False, error=None kalır; yine de kaydedilmez.
    monkeypatch.setattr(mod._Job, "run", lambda self: None)

    section._start(lambda: None, "gizli-deger", core.ICS_TARGET, "Takvim")
    assert section._job is not None
    assert section._job.wait(3000)
    app.processEvents()
    assert section._job.error is None and not section._job.verified
    assert saved == []
    assert "Kaydedilmedi" in section.message.text()
    section.deleteLater()


def test_invalid_until_keeps_first_occurrence_with_warning() -> None:
    data = _ics(
        _event("bozuk-until", "DTSTART:20261110T090000Z", "RRULE:FREQ=DAILY;UNTIL=2026XX01")
    )
    begin = datetime(2026, 11, 10, tzinfo=TZ)
    events, warnings = core.parse_events(data, TZ, begin, datetime(2026, 11, 13, tzinfo=TZ))
    assert [e.title for e in events] == ["bozuk-until"]
    assert any("UNTIL" in w for w in warnings)


def test_broken_exdate_item_warns_and_keeps_other_occurrences() -> None:
    data = _ics(
        _event(
            "ex",
            "DTSTART:20260105T090000Z",
            "RRULE:FREQ=DAILY;COUNT=3",
            "EXDATE:20260106T000000Z,bozuk",
        )
    )
    begin = datetime(2026, 1, 5, tzinfo=TZ)
    events, warnings = core.parse_events(data, TZ, begin, datetime(2026, 1, 8, tzinfo=TZ))
    assert len(events) == 2
    assert any("hariç tutma" in w for w in warnings)


def test_negative_length_clamped_to_zero_with_warning() -> None:
    data = _ics(_event("ters", "DTSTART:20261110T100000Z", "DTEND:20261110T080000Z"))
    begin = datetime(2026, 11, 10, tzinfo=TZ)
    events, warnings = core.parse_events(data, TZ, begin, datetime(2026, 11, 11, tzinfo=TZ))
    assert len(events) == 1
    assert events[0].end == events[0].start
    assert any("önce" in w for w in warnings)


def test_duration_sets_end_when_dtend_absent() -> None:
    data = _ics(
        _event("sureli", "DTSTART:20261110T100000Z", "DURATION:PT2H"),
        _event("bozuk-sure", "DTSTART:20261110T130000Z", "DURATION:BOZUK"),
    )
    begin = datetime(2026, 11, 10, tzinfo=TZ)
    events, warnings = core.parse_events(data, TZ, begin, datetime(2026, 11, 11, tzinfo=TZ))
    by_title = {e.title: e for e in events}
    assert by_title["sureli"].end == by_title["sureli"].start + timedelta(hours=2)
    assert by_title["bozuk-sure"].end == by_title["bozuk-sure"].start
    assert any("süresi" in w for w in warnings)


def test_recurrence_id_overflow_does_not_raise() -> None:
    data = _ics(
        _event("tasma", "RECURRENCE-ID:99991231T235959Z", "DTSTART:20261110T090000Z"),
        _event("normal", "DTSTART:20261110T100000Z"),
    )
    begin = datetime(2026, 11, 10, tzinfo=TZ)
    events, warnings = core.parse_events(data, TZ, begin, datetime(2026, 11, 11, tzinfo=TZ))
    assert "normal" in [e.title for e in events]
    assert warnings


def test_fetch_ics_http_exception_raises_source_error_without_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Opener:
        def open(self, url: str, timeout: float) -> Any:
            raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(core.urllib.request, "build_opener", lambda *h: Opener())
    with pytest.raises(core.SourceError) as exc_info:
        core.fetch_ics(SECRET_URL)
    assert "abc123" not in str(exc_info.value)
    assert "calendar.example" not in str(exc_info.value)


def test_fetch_ics_slow_drip_hits_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, core.TOTAL_TIMEOUT + 1.0])
    monkeypatch.setattr(core, "monotonic", lambda: next(ticks))

    class Resp:
        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self, n: int) -> bytes:
            return b"BEGIN:VCALENDAR\n"

    class Opener:
        def open(self, url: str, timeout: float) -> Resp:
            return Resp()

    monkeypatch.setattr(core.urllib.request, "build_opener", lambda *h: Opener())
    with pytest.raises(core.SourceError, match="yavaş"):
        core.fetch_ics(SECRET_URL)


def test_snippet_malformed_html_does_not_raise() -> None:
    raw = (
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "a<]&-script&ascript><![CDATA[ <=<?<![&<<![CDATA[=-/"
    )
    message = email.message_from_string(raw, policy=policy.default)
    result = core._snippet(message)
    assert isinstance(result, str)


def test_snippet_html_closes_parser_and_keeps_trailing_text() -> None:
    raw = "Content-Type: text/html; charset=utf-8\r\n\r\n<p>x</p>tail &amp"
    message = email.message_from_string(raw, policy=policy.default)
    assert "tail &" in core._snippet(message)


def test_read_unread_limit_zero_returns_empty_without_fetch() -> None:
    fake = FakeImap([PLAIN, HTML, PLAIN])
    items, total = core.read_unread(
        lambda host, port: fake,
        host="imap.example",
        port=993,
        username="u@example",
        password=SECRET_PASSWORD,
        folder="INBOX",
        limit=0,
    )
    assert items == []
    assert total == 3
    assert not any(kind == "fetch" for kind, _ in fake.calls)


def test_read_unread_login_timeout_raises_source_error_without_secret() -> None:
    class TimeoutLoginImap(FakeImap):
        def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
            raise TimeoutError("boom")

    with pytest.raises(core.SourceError, match="yanıt vermedi") as exc_info:
        core.read_unread(
            lambda host, port: TimeoutLoginImap([]),
            host="imap.example",
            port=993,
            username="u@example",
            password=SECRET_PASSWORD,
            folder="INBOX",
            limit=5,
        )
    assert SECRET_PASSWORD not in str(exc_info.value)


class _SelectImap(FakeImap):
    def __init__(self, messages: list[bytes]) -> None:
        super().__init__(messages)
        self.selected_folders: list[str] = []

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.selected_folders.append(folder)
        return super().select(folder, readonly=readonly)


def test_read_unread_quotes_folder_with_spaces() -> None:
    fake = _SelectImap([PLAIN])
    items, total = core.read_unread(
        lambda host, port: fake,
        host="imap.example",
        port=993,
        username="u@example",
        password=SECRET_PASSWORD,
        folder="Gelen Kutusu/Alt",
        limit=5,
    )
    assert total == 1 and len(items) == 1
    assert fake.selected_folders == ['"Gelen Kutusu/Alt"']


@pytest.mark.parametrize("bad_folder", ["INBOX\r\nNOOP", "Gönderilmiş"])
def test_read_unread_rejects_invalid_folder_before_connect(bad_folder: str) -> None:
    called = False

    def connect(host: str, port: int) -> Any:
        nonlocal called
        called = True
        return FakeImap([])

    with pytest.raises(core.SourceError):
        core.read_unread(
            connect,
            host="imap.example",
            port=993,
            username="u@example",
            password=SECRET_PASSWORD,
            folder=bad_folder,
            limit=5,
        )
    assert not called


def test_read_unread_hits_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, core.TOTAL_TIMEOUT + 1.0])
    monkeypatch.setattr(core, "monotonic", lambda: next(ticks))
    fake = FakeImap([PLAIN, HTML, PLAIN])
    with pytest.raises(core.SourceError, match="zaman aşımı"):
        core.read_unread(
            lambda host, port: fake,
            host="imap.example",
            port=993,
            username="u@example",
            password=SECRET_PASSWORD,
            folder="INBOX",
            limit=5,
        )
