"""Takvim (ICS) ve posta (IMAP) okuma çekirdeği (ADR 0059). Salt okunur, yalnız stdlib.

Sırlar (ICS adresi içinde jeton taşır, IMAP parolası) YAML'a, argv'ye, log'a, denetime ve hata
iletilerine girmez: ortam değişkeni ya da kimlik kasası. Hata iletileri adresi/parolayı
asla içermez, yalnız hata türünü söyler.

Yinelenme için python-dateutil EKLENMEDİ (bağımlılık eklememek için): DAILY/WEEKLY/MONTHLY/YEARLY
+ INTERVAL/COUNT/UNTIL/EXDATE/BYDAY(yalnız haftalık) desteklenir. Daha karmaşık kural (BYSETPOS,
ayın n. haftası, HOURLY...) sessizce atlanmaz: olay ilk oluşumuyla listelenir ve uyarıda belirtilir.
"""

from __future__ import annotations

import contextlib
import email
import http.client
import imaplib
import os
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from email import policy
from html.parser import HTMLParser
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from artemis.tools.credentials import read_secret

# `_TOKEN`/`PASSWORD` içerdiği için denetim temizleyicisi değerlerini her kayıttan siler.
ICS_ENV = "ARTEMIS_CALENDAR_ICS_TOKEN"
MAIL_ENV = "ARTEMIS_MAIL_PASSWORD"
ICS_TARGET = "Artemis/calendar_ics"
MAIL_TARGET = "Artemis/mail_password"

MAX_ICS_BYTES = 2_000_000
TIMEOUT = 10.0
TOTAL_TIMEOUT = 30.0
_CHUNK_BYTES = 65_536
SNIPPET_CHARS = 300
MAX_EVENTS = 50
_MAX_STEPS = 20_000  # tek olay için oluşum üretme sınırı (çok eski DAILY kural şişmesin)
# Gövdenin yalnız başı çekilir; ek dosya parçaları ayrıştırılmaz/döndürülmez.
_BODY_BYTES = 32_768
_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


class SourceError(Exception):
    """Kullanıcıya olduğu gibi gösterilebilir ileti taşır; sır içermez."""


def resolve_secret(env_name: str, target: str) -> str | None:
    """Önce ortam değişkeni, sonra kimlik kasası (ADR 0051 ile aynı sıra)."""
    return os.environ.get(env_name, "").strip() or (read_secret(target) or "").strip() or None


# ICS ------------------------------------------------------------------------------------------


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any):  # type: ignore[no-untyped-def]  # noqa: E501
        if not str(newurl).lower().startswith("https://"):
            raise SourceError("Takvim adresi https olmayan bir adrese yönlendirdi; reddedildi.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_ics(url: str) -> bytes:
    """HTTPS, 10 sn istek / 30 sn toplam süre, 2 MB sınırı, yönlendirme yalnız https."""
    if not url.lower().startswith("https://"):
        raise SourceError("Takvim adresi https:// ile başlamalı.")
    opener = urllib.request.build_opener(_HttpsOnly)
    deadline = monotonic() + TOTAL_TIMEOUT
    chunks: list[bytes] = []
    total = 0
    try:
        with opener.open(url, timeout=TIMEOUT) as response:  # noqa: S310 - şema yukarıda denetlendi
            while True:
                chunk: bytes = response.read(_CHUNK_BYTES)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_ICS_BYTES:
                    raise SourceError("Takvim dosyası 2 MB sınırını aşıyor; okunmadı.")
                # Yavaş damlatan sunucuya karşı soket zaman aşımı yetmez; toplam bütçe gerekir.
                if monotonic() > deadline:
                    raise SourceError("Takvim çok yavaş yanıt verdi; okunmadı.")
    except SourceError:
        raise
    except urllib.error.HTTPError as error:
        raise SourceError(f"Takvim sunucusu HTTP {error.code} döndürdü.") from None
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        ValueError,
    ) as error:
        raise SourceError(f"Takvime ulaşılamadı ({type(error).__name__}).") from None
    return b"".join(chunks)


@dataclass
class Event:
    title: str
    start: datetime | date
    end: datetime | date
    location: str = ""
    recurring: bool = False
    unsupported_rule: str = ""

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _parse_line(line: str) -> tuple[str, dict[str, str], str] | None:
    head, sep, value = line.partition(":")
    if not sep:
        return None
    name, *params = head.split(";")
    return (
        name.upper(),
        {k.upper(): v.strip('"') for k, _, v in (p.partition("=") for p in params)},
        value,
    )


def _text(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def _when(value: str, params: dict[str, str], local: tzinfo, warnings: set[str]) -> datetime | date:
    """ICS tarih/saati: DATE -> date; `Z` -> UTC; TZID -> o dilim; yüzen saat -> yerel dilim."""
    value = value.strip()
    if "T" not in value:
        return datetime.strptime(value[:8], "%Y%m%d").date()
    naive = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return naive.replace(tzinfo=ZoneInfo("UTC"))
    tzid = params.get("TZID")
    if tzid:
        try:
            return naive.replace(tzinfo=ZoneInfo(tzid))
        except (ZoneInfoNotFoundError, ValueError):
            warnings.add(f"Zaman dilimi tanınmadı ({tzid[:40]}); yerel saat varsayıldı.")
    return naive.replace(tzinfo=local)


def _occurrence(day: date, base: datetime | date) -> datetime | date:
    """Aynı duvar saatini yeni güne taşır (yaz saati geçişinde saat kayması olmaz)."""
    if isinstance(base, datetime):
        return datetime.combine(day, base.timetz().replace(tzinfo=None), tzinfo=base.tzinfo)
    return day


def _expand(
    base: datetime | date, rule: dict[str, str], window_end: date
) -> tuple[list[datetime | date], str]:
    """Pencere sonuna dek oluşumlar; desteklenmeyen kuralda ([], neden)."""
    freq = rule.get("FREQ", "")
    try:
        interval = int(rule.get("INTERVAL", "1"))
        count = int(rule["COUNT"]) if "COUNT" in rule else None
    except ValueError:
        return [], "geçersiz sayı"
    # Düşmanca ya da bozuk takvim: 0/negatif aralık döngüyü boşa yakar, dev aralık taşar.
    if not 1 <= interval <= 1000:
        return [], "geçersiz sayı"
    # Hafta başı Pazartesi varsayılır; başka WKST yalnız aralıklı haftalıkta sonucu değiştirir.
    if freq == "WEEKLY" and interval > 1 and rule.get("WKST", "MO") != "MO":
        return [], "desteklenmeyen hafta başı (WKST)"
    supported = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "WKST"} | (
        {"BYDAY"} if freq == "WEEKLY" else set()
    )
    if freq not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"} or set(rule) - supported:
        return [], "desteklenmeyen yinelenme kuralı"
    byday: list[int] = []
    if "BYDAY" in rule:
        try:
            byday = sorted(_DAYS[d] for d in rule["BYDAY"].split(","))
        except KeyError:
            return [], "desteklenmeyen BYDAY"
    until: date | None = None
    until_moment: datetime | None = None
    if "UNTIL" in rule:
        raw = rule["UNTIL"].strip()
        try:
            until = datetime.strptime(raw[:8], "%Y%m%d").date()
            if raw.endswith("Z") and "T" in raw and isinstance(base, datetime):
                # UTC UNTIL an olarak karşılaştırılır; yalnız tarihe bakmak doğu dilimlerinde
                # gece yarısından sonraki son oluşumu düşürürdü.
                until_moment = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").replace(
                    tzinfo=ZoneInfo("UTC")
                )
        except ValueError:
            return [], "geçersiz UNTIL"
    start = base.date() if isinstance(base, datetime) else base
    first_monday = start - timedelta(days=start.weekday())
    out: list[datetime | date] = []
    produced = 0
    for k in range(_MAX_STEPS):
        if freq == "DAILY":
            days = [start + timedelta(days=k * interval)]
        elif freq == "WEEKLY":
            monday = first_monday + timedelta(weeks=k * interval)
            days = [monday + timedelta(days=d) for d in (byday or [start.weekday()])]
        else:
            months = k * interval * (1 if freq == "MONTHLY" else 12)
            year, month = divmod(start.month - 1 + months, 12)
            try:
                days = [date(start.year + year, month + 1, start.day)]
            except ValueError:
                days = []  # RFC 5545: olmayan gün (31 Şubat) atlanır
        for day in days:
            if day < start:
                continue
            occurrence = _occurrence(day, base)
            if until_moment is not None and isinstance(occurrence, datetime):
                past_until = occurrence > until_moment
            else:
                past_until = until is not None and day > until
            if past_until or day >= window_end:
                return out, ""
            out.append(occurrence)
            produced += 1
            if count is not None and produced >= count:
                return out, ""
        if days and days[0] >= window_end and freq != "WEEKLY":
            return out, ""
    return out, "oluşum sınırı aşıldı"


def parse_events(
    data: bytes, local: tzinfo, window_start: datetime, window_end: datetime
) -> tuple[list[Event], list[str]]:
    """[window_start, window_end) ile kesişen olaylar (yinelenenler açılmış) ve uyarılar."""
    warnings: set[str] = set()
    events: list[Event] = []
    props: dict[str, list[tuple[dict[str, str], str]]] | None = None
    blocks: list[dict[str, list[tuple[dict[str, str], str]]]] = []
    for line in _unfold(data.decode("utf-8", errors="replace")):
        if line == "BEGIN:VEVENT":
            props = {}
        elif line == "END:VEVENT" and props is not None:
            blocks.append(props)
            props = None
        elif props is not None and (parsed := _parse_line(line)):
            props.setdefault(parsed[0], []).append((parsed[1], parsed[2]))
    # Tek bir oluşumu değiştirilmiş yinelenen olayda ana kuralın o oluşumu atlanır.
    overridden: set[tuple[str, date]] = set()
    for block in blocks:
        if "RECURRENCE-ID" in block and "UID" in block:
            params, value = block["RECURRENCE-ID"][0]
            try:
                moment = _when(value, params, local, warnings)
                occ_day = _to_local(moment, local).date()
            except (ValueError, OverflowError):
                # Tek bozuk olay bütün takvimi okunmaz yapmamalı.
                warnings.add("Okunamayan bir olay atlandı.")
                continue
            overridden.add((block["UID"][0][1], occ_day))
    for block in blocks:
        if block.get("STATUS", [({}, "")])[0][1].upper() == "CANCELLED" or "DTSTART" not in block:
            continue
        try:
            events.extend(_event(block, local, window_start, window_end, overridden, warnings))
        except (ValueError, KeyError, OverflowError):
            warnings.add("Okunamayan bir olay atlandı.")
    events.sort(key=lambda e: _to_local(e.start, local))
    for event in events:
        if event.unsupported_rule:
            warnings.add(
                f"'{event.title[:40]}' yinelenen bir olay ({event.unsupported_rule}); "
                "yalnız ilk oluşumu gösterilebildi, sonrakiler eksik olabilir."
            )
    return events, sorted(warnings)


def _to_local(value: datetime | date, local: tzinfo) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(local)
    return datetime.combine(value, time(0), tzinfo=local)


_DURATION_RE = re.compile(
    r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)


def _duration(text: str) -> timedelta | None:
    """RFC 5545 süre metnini (ör. PT2H, P1DT30M, P2W) çözer; geçersizse None."""
    match = _DURATION_RE.match(text.strip())
    if not match or not any(match.groups()):
        return None
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    try:
        return timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    except OverflowError:
        return None


def _event(
    block: dict[str, list[tuple[dict[str, str], str]]],
    local: tzinfo,
    window_start: datetime,
    window_end: datetime,
    overridden: set[tuple[str, date]],
    warnings: set[str],
) -> list[Event]:
    params, value = block["DTSTART"][0]
    start = _when(value, params, local, warnings)
    fallback_length = timedelta(days=1) if not isinstance(start, datetime) else timedelta(0)
    if "DTEND" in block:
        end = _when(block["DTEND"][0][1], block["DTEND"][0][0], local, warnings)
        length = (
            end - start
            if isinstance(start, datetime) == isinstance(end, datetime)
            else timedelta(0)
        )
    elif "DURATION" in block:
        parsed = _duration(block["DURATION"][0][1])
        if parsed is None:
            warnings.add("Okunamayan etkinlik süresi yok sayıldı.")
            length = fallback_length
        else:
            length = parsed
    else:
        length = fallback_length
    if length < timedelta(0):
        # Bitişi başlangıçtan önce gelen bozuk etkinlik olumsuz süre üretmemeli.
        warnings.add("Bitiş saati başlangıçtan önce olan bir etkinlik sıfır süreyle gösterildi.")
        length = timedelta(0)
    title = _text(block.get("SUMMARY", [({}, "")])[0][1]).strip()[:200] or "(başlıksız)"
    where = _text(block.get("LOCATION", [({}, "")])[0][1]).strip()[:200]
    rule_text = block.get("RRULE", [({}, "")])[0][1]
    starts: list[datetime | date] = [start]
    unsupported = ""
    if rule_text:
        rule = dict(p.partition("=")[::2] for p in rule_text.split(";") if p)
        horizon = window_end.astimezone(local).date() + timedelta(days=2)
        starts, unsupported = _expand(start, rule, horizon)
        if unsupported:
            starts = [start]
        excluded: set[date] = set()
        for ex_params, ex_value in block.get("EXDATE", []):
            for item in ex_value.split(","):
                try:
                    moment = _when(item, ex_params, local, warnings)
                    excluded.add(_to_local(moment, local).date())
                except (ValueError, OverflowError):
                    # Tek bozuk EXDATE bütün yinelenen olayı düşürmemeli.
                    warnings.add("Okunamayan bir hariç tutma tarihi atlandı.")
        uid = block.get("UID", [({}, "")])[0][1]
        starts = [
            s for s in starts
            if _to_local(s, local).date() not in excluded
            and (uid, _to_local(s, local).date()) not in overridden
        ]
    result = []
    for occ in starts:
        occ_start = _to_local(occ, local)
        occ_end = occ_start + length if length else occ_start
        # Başlangıcı olan sıfır süreli olay da pencerede sayılır.
        if occ_start < window_end and (occ_end > window_start or occ_start >= window_start):
            shown_end: datetime | date = occ_end
            if not isinstance(occ, datetime):
                shown_end = occ + length
            result.append(
                Event(
                    title, occ, shown_end, where,
                    bool(rule_text), unsupported,
                )
            )
    return result


def day_window(
    local: tzinfo, start_day: str | None, days: int, now: datetime | None = None
) -> tuple[datetime, datetime]:
    first = (
        date.fromisoformat(start_day)
        if start_day
        else (now or datetime.now(local)).astimezone(local).date()
    )
    begin = datetime.combine(first, time(0), tzinfo=local)
    return begin, datetime.combine(first + timedelta(days=days), time(0), tzinfo=local)


def event_data(event: Event, local: tzinfo) -> dict[str, Any]:
    def fmt(value: datetime | date) -> str:
        return value.isoformat() if not isinstance(value, datetime) else (
            value.astimezone(local).isoformat(timespec="minutes")
        )

    data: dict[str, Any] = {
        "title": event.title,
        "start": fmt(event.start),
        "end": fmt(event.end),
        "all_day": event.all_day,
    }
    if event.location:
        data["location"] = event.location
    if event.recurring:
        data["recurring"] = True
    return data


# Posta ------------------------------------------------------------------------------------------


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._skip += tag in {"script", "style"}

    def handle_endtag(self, tag: str) -> None:
        self._skip -= tag in {"script", "style"} and self._skip > 0

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _snippet(message: email.message.EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))  # ekler (attachment) dönmez
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError, UnicodeError):
        return ""
    if part.get_content_subtype() == "html":
        stripper = _Text()
        # Standart HTMLParser bozuk işaretlemede iç AssertionError fırlatabilir;
        # o ana dek toplanan metin korunur.
        with contextlib.suppress(AssertionError):
            stripper.feed(text)
        with contextlib.suppress(AssertionError):
            stripper.close()
        text = " ".join(stripper.parts)
    return " ".join(text.split())[:SNIPPET_CHARS]


def _header(message: email.message.EmailMessage, name: str, limit: int) -> str:
    return " ".join(str(message.get(name, "")).split())[:limit]


def read_unread(
    connect: Callable[[str, int], Any],
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str,
    limit: int,
) -> tuple[list[dict[str, str]], int]:
    """(en yeni `limit` okunmamış ileti, toplam okunmamış). Yalnız `readonly` select + PEEK:
    sunucuda \\Seen bayrağı değişmez, hiçbir şey silinmez/taşınmaz."""
    # Komut enjeksiyonu ve ASCII dışı kodlama hatalarını önlemek için ağdan önce doğrula.
    if not folder.isascii() or any(ord(ch) < 32 or ord(ch) == 127 for ch in folder):
        raise SourceError(
            "Posta klasör adı desteklenmiyor (yalnız ASCII, denetim karakteri yok)."
        )
    quoted = '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
    try:
        imap = connect(host, port)
    except (OSError, imaplib.IMAP4.error) as error:
        raise SourceError(f"Posta sunucusuna bağlanılamadı ({type(error).__name__}).") from None
    try:
        try:
            imap.login(username, password)
        except imaplib.IMAP4.error:
            raise SourceError(
                "Posta girişi reddedildi; kullanıcı adı ve uygulama parolasını kontrol edin."
            ) from None
        except OSError as error:
            raise SourceError(
                f"Posta girişi yanıt vermedi ({type(error).__name__})."
            ) from None
        try:
            status, _ = imap.select(quoted, readonly=True)
            if status != "OK":
                raise SourceError("Posta klasörü açılamadı.")
            status, found = imap.search(None, "UNSEEN")
            ids = found[0].split() if status == "OK" and found and found[0] else []
            items: list[dict[str, str]] = []
            deadline = monotonic() + TOTAL_TIMEOUT
            for number in reversed(ids[-limit:] if limit > 0 else []):
                # Her ileti soket zaman aşımını tetikleyebilir; toplam okuma bütçesini aşma.
                if monotonic() > deadline:
                    raise SourceError(
                        "Posta okuma zaman aşımına uğradı; sonuç gösterilmedi."
                    )
                status, data = imap.fetch(number, f"(BODY.PEEK[]<0.{_BODY_BYTES}>)")
                raw = next((p[1] for p in data or [] if isinstance(p, tuple)), None)
                if status != "OK" or not isinstance(raw, bytes):
                    continue
                message = email.message_from_bytes(raw, policy=policy.default)
                items.append(
                    {
                        "from": _header(message, "From", 200),
                        "subject": _header(message, "Subject", 200),
                        "date": _header(message, "Date", 60),
                        "snippet": _snippet(message),
                    }
                )
        except (OSError, imaplib.IMAP4.error) as error:
            raise SourceError(f"Posta okunamadı ({type(error).__name__}).") from None
        return items, len(ids)
    finally:
        # Bağlantı zaten düşmüş olabilir; okuma sonucu etkilenmez.
        with contextlib.suppress(OSError, imaplib.IMAP4.error):
            imap.logout()


def connect_imap(host: str, port: int) -> imaplib.IMAP4_SSL:
    """TLS + sertifika/ad doğrulaması + zaman aşımı."""
    return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
