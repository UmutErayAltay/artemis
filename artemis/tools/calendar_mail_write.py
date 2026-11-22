"""Takvim (CalDAV PUT) ve posta (SMTP) yazma çekirdeği (ADR 0084). Yalnız stdlib.

Sırlar (CalDAV ve SMTP parolası) YAML'a, argv'ye, log'a, denetime ve hata iletilerine
girmez: ortam değişkeni ya da kimlik kasası. Hata iletileri adresi ve parolayı içermez.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import re
import smtplib
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime, tzinfo
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from artemis.tools.calendar_mail import (
    MAIL_ENV,
    MAIL_TARGET,
    TIMEOUT,
    SourceError,
    resolve_secret,
)

# `_PASSWORD` içerdiği için denetim temizleyicisi değerlerini her kayıttan siler.
CALDAV_PASSWORD_ENV = "ARTEMIS_CALENDAR_CALDAV_PASSWORD"
CALDAV_PASSWORD_TARGET = "Artemis/calendar_caldav_password"
SMTP_PASSWORD_ENV = "ARTEMIS_MAIL_SMTP_PASSWORD"
SMTP_PASSWORD_TARGET = "Artemis/mail_smtp_password"

_CTL_CHARS = ("\r", "\n", "\0")
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def resolve_smtp_password() -> str | None:
    """Önce SMTP sırrını, yoksa okuma tarafındaki MAIL sırrını okur."""
    return resolve_secret(SMTP_PASSWORD_ENV, SMTP_PASSWORD_TARGET) or resolve_secret(
        MAIL_ENV, MAIL_TARGET
    )


def resolve_caldav_password() -> str | None:
    """CalDAV parolasını ortam değişkeni ya da kimlik kasasından okur."""
    return resolve_secret(CALDAV_PASSWORD_ENV, CALDAV_PASSWORD_TARGET)


def _reject_ctl(value: str, label: str) -> None:
    if any(ch in value for ch in _CTL_CHARS):
        raise SourceError(f"{label} satır sonu veya denetim karakteri içeremez.")


def _parse_recipients(raw: str, *, required: bool, label: str) -> list[str]:
    _reject_ctl(raw, label)
    stripped = raw.strip()
    if not stripped:
        if required:
            raise SourceError("Geçerli bir alıcı e-posta adresi gerekir.")
        return []
    addresses = [part.strip() for part in stripped.split(",")]
    if any(not part or not _EMAIL_RE.fullmatch(part) for part in addresses):
        raise SourceError(f"{label} geçerli bir e-posta adresi olmalıdır.")
    return addresses


# SMTP -----------------------------------------------------------------------------------------


def connect_smtp(host: str, port: int) -> smtplib.SMTP:
    """465'te örtük TLS (SMTP_SSL), diğer portlarda zorunlu STARTTLS kurar."""
    context = ssl.create_default_context()
    if port == 465:
        return smtplib.SMTP_SSL(host, port, context=context, timeout=TIMEOUT)
    client = smtplib.SMTP(host, port, timeout=TIMEOUT)
    try:
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
    except (smtplib.SMTPException, OSError):
        with contextlib.suppress(smtplib.SMTPException, OSError):
            client.close()
        raise
    return client


def send_mail(
    connect: Callable[[str, int], Any],
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
) -> dict[str, Any]:
    """Doğrulanmış başlıklarla e-posta gönderir; refused boş değilse hata yükseltir."""
    _reject_ctl(username, "Gönderen")
    _reject_ctl(subject, "Konu")
    sender = username.strip()
    if not sender:
        raise SourceError("Gönderen kullanıcı adı boş olamaz.")
    clean_subject = subject.strip()
    if not clean_subject:
        raise SourceError("E-posta konusu boş olamaz.")
    to_addrs = _parse_recipients(to, required=True, label="Alıcı")
    cc_addrs = _parse_recipients(cc, required=False, label="Bilgi (Cc)")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to_addrs)
    if cc_addrs:
        message["Cc"] = ", ".join(cc_addrs)
    message["Subject"] = clean_subject
    message.set_content(body)

    try:
        smtp = connect(host, port)
    except (smtplib.SMTPException, OSError) as error:
        raise SourceError(f"SMTP sunucusuna bağlanılamadı ({type(error).__name__}).") from None

    try:
        try:
            smtp.login(sender, password)
        except smtplib.SMTPAuthenticationError:
            raise SourceError(
                "SMTP girişi reddedildi; kullanıcı adı ve uygulama parolasını kontrol edin."
            ) from None
        except (smtplib.SMTPException, OSError) as error:
            raise SourceError(
                f"SMTP girişi yanıt vermedi ({type(error).__name__})."
            ) from None
        try:
            refused = smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as error:
            raise SourceError(f"E-posta gönderilemedi ({type(error).__name__}).") from None
        if refused:
            raise SourceError("E-posta alıcılarından bazıları sunucu tarafından reddedildi.")
    finally:
        with contextlib.suppress(smtplib.SMTPException, OSError):
            smtp.quit()

    return {
        "to": to_addrs,
        "cc": cc_addrs,
        "subject": clean_subject,
    }


# CalDAV ---------------------------------------------------------------------------------------


def escape_ics_text(value: str) -> str:
    """RFC 5545 metin kaçışı: ters eğik çizgi, noktalı virgül, virgül ve satır sonu."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return (
        normalized.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def parse_event_bounds(
    start: str, end: str, *, local_tz: tzinfo | None = None
) -> tuple[date | datetime, date | datetime, bool]:
    """ISO tarih/tarih-saat sınırlarını doğrular; (start, end, all_day) döndürür."""
    _reject_ctl(start, "Başlangıç")
    _reject_ctl(end, "Bitiş")
    start_s = start.strip()
    end_s = end.strip()
    start_is_date = bool(_DATE_RE.fullmatch(start_s))
    end_is_date = bool(_DATE_RE.fullmatch(end_s))
    if start_is_date != end_is_date:
        raise SourceError(
            "Başlangıç ve bitiş biçimi uyumlu olmalıdır (ikisi de tarih veya ikisi de saatli)."
        )
    if start_is_date:
        try:
            start_d = date.fromisoformat(start_s)
            end_d = date.fromisoformat(end_s)
        except ValueError:
            raise SourceError("Geçersiz etkinlik tarihi (YYYY-AA-GG beklenir).") from None
        if end_d <= start_d:
            raise SourceError("Tüm gün etkinlikte bitiş tarihi başlangıçtan sonra olmalıdır.")
        return start_d, end_d, True

    try:
        start_dt = datetime.fromisoformat(start_s)
        end_dt = datetime.fromisoformat(end_s)
    except ValueError:
        raise SourceError("Geçersiz etkinlik tarih/saati (ISO biçimi beklenir).") from None
    if (start_dt.tzinfo is None) != (end_dt.tzinfo is None):
        if local_tz is None:
            raise SourceError("Başlangıç ve bitiş saat dilimi tutarlı olmalıdır.")
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=local_tz)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=local_tz)
    elif start_dt.tzinfo is None and local_tz is not None:
        start_dt = start_dt.replace(tzinfo=local_tz)
        end_dt = end_dt.replace(tzinfo=local_tz)
    if end_dt < start_dt:
        raise SourceError("Etkinlik bitiş zamanı başlangıçtan önce olamaz.")
    return start_dt, end_dt, False


def _format_ics_bound(value: date | datetime, *, all_day: bool) -> tuple[str, str]:
    if all_day:
        return ";VALUE=DATE", value.strftime("%Y%m%d")
    assert isinstance(value, datetime)
    if value.tzinfo is not None:
        return "", value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return "", value.strftime("%Y%m%dT%H%M%S")


def build_vevent_ics(
    *,
    uid: str,
    title: str,
    start: str,
    end: str,
    location: str = "",
    description: str = "",
    local_tz: tzinfo | None = None,
) -> str:
    """Tek bir VEVENT içeren geçerli RFC 5545 VCALENDAR gövdesi üretir."""
    _reject_ctl(uid, "Etkinlik kimliği")
    _reject_ctl(title, "Başlık")
    _reject_ctl(location, "Konum")
    if "\0" in description:
        raise SourceError("Açıklama denetim karakteri içeremez.")
    clean_title = title.strip()
    if not clean_title:
        raise SourceError("Etkinlik başlığı boş olamaz.")

    start_val, end_val, all_day = parse_event_bounds(start, end, local_tz=local_tz)
    start_param, start_text = _format_ics_bound(start_val, all_day=all_day)
    end_param, end_text = _format_ics_bound(end_val, all_day=all_day)
    dtstamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Artemis//Calendar//TR",
        "BEGIN:VEVENT",
        f"UID:{uid.strip()}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART{start_param}:{start_text}",
        f"DTEND{end_param}:{end_text}",
        f"SUMMARY:{escape_ics_text(clean_title)}",
    ]
    if location.strip():
        lines.append(f"LOCATION:{escape_ics_text(location.strip())}")
    if description.strip():
        lines.append(f"DESCRIPTION:{escape_ics_text(description.strip())}")
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return "\r\n".join(lines) + "\r\n"


def _is_allowed_caldav_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "https" and bool(parsed.hostname):
        return True
    return parsed.scheme == "http" and (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


class _CalDavRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[no-untyped-def]
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ):
        if not _is_allowed_caldav_url(str(newurl)):
            raise SourceError("CalDAV adresi güvenli olmayan bir adrese yönlendirdi; reddedildi.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _auth_headers(username: str, password: str) -> dict[str, str]:
    if not (username or password):
        return {}
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def put_caldav_event(
    url: str,
    ics_body: str,
    username: str = "",
    password: str = "",
) -> int:
    """CalDAV sunucusuna If-None-Match: * ile tek etkinlik PUT eder; 200/201/204 döner."""
    if not _is_allowed_caldav_url(url):
        raise SourceError("CalDAV adresi https:// ile başlamalıdır.")
    headers = {
        "Content-Type": "text/calendar; charset=utf-8",
        "If-None-Match": "*",
        **_auth_headers(username, password),
    }
    request = urllib.request.Request(
        url,
        data=ics_body.encode("utf-8"),
        headers=headers,
        method="PUT",
    )
    opener = urllib.request.build_opener(_CalDavRedirectHandler)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:  # noqa: S310 - şema doğrulandı
            status = int(getattr(response, "status", 200))
    except SourceError:
        raise
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise SourceError(
                "CalDAV girişi reddedildi; kullanıcı adı ve parolayı kontrol edin."
            ) from None
        raise SourceError(f"CalDAV sunucusu HTTP {error.code} döndürdü.") from None
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        ValueError,
    ) as error:
        raise SourceError(f"CalDAV sunucusuna ulaşılamadı ({type(error).__name__}).") from None
    if status not in {200, 201, 204}:
        raise SourceError(f"CalDAV sunucusu beklenmeyen HTTP {status} döndürdü.")
    return status


def check_caldav(url: str, username: str = "", password: str = "") -> str | None:
    """CalDAV adresini OPTIONS ile yoklar; uygunsa None, değilse kullanıcıya hata döndürür."""
    if not _is_allowed_caldav_url(url):
        return "CalDAV adresi https:// ile başlamalıdır."
    request = urllib.request.Request(
        url,
        headers=_auth_headers(username, password),
        method="OPTIONS",
    )
    opener = urllib.request.build_opener(_CalDavRedirectHandler)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:  # noqa: S310 - şema doğrulandı
            status = int(getattr(response, "status", 200))
            if status >= 400:
                return f"CalDAV sunucusu HTTP {status} döndürdü."
    except SourceError as error:
        return str(error)
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            return "CalDAV girişi reddedildi; kullanıcı adı ve parolayı kontrol edin."
        return f"CalDAV sunucusu HTTP {error.code} döndürdü."
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        ValueError,
    ) as error:
        return f"CalDAV sunucusuna ulaşılamadı ({type(error).__name__})."
    return None


def create_calendar_event(
    put: Callable[[str, str, str, str], int],
    *,
    caldav_url: str,
    username: str,
    password: str,
    title: str,
    start: str,
    end: str,
    location: str = "",
    description: str = "",
    local_tz: tzinfo | None = None,
    uid: str | None = None,
) -> dict[str, Any]:
    """VEVENT ICS oluşturur ve CalDAV koleksiyonuna benzersiz UID.ics ile yazar."""
    base_url = caldav_url.strip().rstrip("/")
    if not base_url:
        raise SourceError("CalDAV adresi tanımlı değil.")
    event_uid = uid or str(uuid4())
    ics_body = build_vevent_ics(
        uid=event_uid,
        title=title,
        start=start,
        end=end,
        location=location,
        description=description,
        local_tz=local_tz,
    )
    target_url = f"{base_url}/{event_uid}.ics"
    status = put(target_url, ics_body, username, password)
    return {
        "uid": event_uid,
        "status": status,
        "title": title.strip(),
        "start": start.strip(),
        "end": end.strip(),
    }
