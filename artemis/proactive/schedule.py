"""Proaktif zamanlama yardımcıları.

Tüm fonksiyonlar saf (pure) fonksiyonlardır: I/O yok, global durum yok, uyku yok.
Zaman dilimi işlemleri için `zoneinfo` kullanılır; tüm `datetime` nesneleri
UTC'de timezone-aware olmalıdır. Naive datetime argümanı `ValueError` fırlatır.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo


def _ensure_aware_utc(dt: datetime, param_name: str) -> None:
    """`dt` timezone-aware ve UTC ise sessizce döner, aksi halde `ValueError` fırlatır.

    Neden: Proje genelinde zaman dilimi hatalarını erken yakalamak için merkezi bir
    doğrulama noktası sağlar; çağrı yerlerinde tekrar eden kontrolleri önler.
    """
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{param_name} timezone-aware olmalı")
    if dt.utcoffset() != timedelta(0):
        raise ValueError(f"{param_name} UTC olmalı")


def resolve_local(day: date, at: time, tz: ZoneInfo) -> datetime:
    """`day` + `at` duvar saatini `tz` içinde UTC `datetime` olarak çözümler.

    - Olmayan yerel saat (DST ileri sarma boşluğu): dakika dakika ileri sararak
      (en fazla 24*60 adım) yerel->UTC->yerel turu aynı duvar saatini verene
      kadar denenir; o an döndürülür.
    - Belirsiz yerel saat (gerialma): İLK oluşum (fold=0) döndürülür.

    Neden: `zoneinfo` doğrudan "olmayan saat" hatası vermez; round-trip yöntemi
    hem boşluğu hem de belirsizliği tek bir mantıkla çözer.
    """
    if at.tzinfo is not None:
        raise ValueError("at timezone-aware olamaz")

    naive_base = datetime.combine(day, at)
    # En fazla bir günlük boşluk olabilir; 1440 dakika yeterli güvenlik payıdır.
    for offset_minutes in range(1440):
        candidate_naive = naive_base + timedelta(minutes=offset_minutes)
        # fold=0 varsayılan; belirsiz saatte ilk oluşumu verir.
        candidate_aware = candidate_naive.replace(tzinfo=tz)
        utc_candidate = candidate_aware.astimezone(UTC)
        # UTC'ye çevirip tekrar yerel saatine döndür; duvar saati korunmuş mu?
        back = utc_candidate.astimezone(tz)
        if back.replace(tzinfo=None) == candidate_naive:
            return utc_candidate
    # Teorik olarak asla buraya düşmemeli (maks 1 gün boşluk).
    raise ValueError(f"{day} {at} için {tz} içinde geçerli bir an bulunamadı")


def briefing_occurrence(
    days: tuple[int, ...],
    at: time,
    now: datetime,
    tz: ZoneInfo,
    grace: timedelta,
) -> datetime | None:
    """`now` anının hangi brifing penceresine düştüğünü belirler.

    `days`: `date.weekday()` değerleri (0=Pazartesi). `now` un `tz`deki yerel
    takvim tarihi VE bir önceki yerel tarihi kontrol edilir. Her birinin haftanın
    günü `days` içindeyse `scheduled = resolve_local(tarih, at, tz)` hesaplanır.
    Pencere `[scheduled, scheduled + max(grace, 1dk))`. `now` bir pencerede ise
    o `scheduled` döndürülür (her ikisi de tutuyorsa daha geç olan).

    Neden: Gece yarısı sıfırlaması ve DST geçişleri sırasında "önceki gün"ün
    brifing'inin ertesi günün erken saatlerine sarkmasını doğru yakalamak için
    her iki yerel tarihi de kontrol ederiz.
    """
    _ensure_aware_utc(now, "now")
    if at.tzinfo is not None:
        raise ValueError("at timezone-aware olamaz")

    local_now = now.astimezone(tz)
    local_date = local_now.date()
    prev_date = local_date - timedelta(days=1)

    window_min = max(grace, timedelta(minutes=1))
    matched: datetime | None = None

    for check_date in (prev_date, local_date):
        if check_date.weekday() not in days:
            continue
        scheduled = resolve_local(check_date, at, tz)
        window_end = scheduled + window_min
        if scheduled <= now < window_end:
            # İki tarih de tutarsa daha geç olan (local_date) kazanır.
            matched = scheduled

    return matched


def classify_reminder(
    due: datetime,
    now: datetime,
    grace: timedelta,
    late_after: timedelta = timedelta(minutes=5),
) -> Literal["wait", "deliver", "late", "missed"]:
    """Hatırlatma durumunu sınıflandırır.

    `delay = now - due`.
    - `delay < 0`          -> "wait"   (henüz zaman gelmedi)
    - `delay <= late_after` -> "deliver" (zamanında, normal teslim)
    - `delay <= grace`      -> "late"    (geç ama grace içinde)
    - aksi halde            -> "missed"  (grace aşıldı)

    Neden: `late_after` ve `grace` ayrı parametrelerdir; "zamanında" ile "geç
    ama kabul edilebilir" ayrımı arayüz/ bildirim stratejilerini değiştirir.
    """
    _ensure_aware_utc(due, "due")
    _ensure_aware_utc(now, "now")

    delay = now - due
    if delay < timedelta(0):
        return "wait"
    if delay <= late_after:
        return "deliver"
    if delay <= grace:
        return "late"
    return "missed"


def local_day_bounds(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """`now` un `tz`deki yerel takvim gününün UTC başlangıç (dahil) ve bitiş (hariç) anları.

    `resolve_local(tarih, 00:00, tz)` ve bir sonraki gün için aynı hesaplama.

    Neden: DST geçiş günlerinde gün 23 veya 25 saat olabilir; `resolve_local`
    bu geçişleri doğru işler (ör. ileri sarma günü 00:00 yoksa 01:00 UTC
    olur, yani gün 23 saat sürer).
    """
    _ensure_aware_utc(now, "now")

    local_now = now.astimezone(tz)
    local_date = local_now.date()
    next_date = local_date + timedelta(days=1)

    start = resolve_local(local_date, time(0, 0), tz)
    end = resolve_local(next_date, time(0, 0), tz)
    return (start, end)


def parse_local(text: str, tz: ZoneInfo) -> datetime:
    """`YYYY-MM-DD HH:MM` veya `YYYY-MM-DDTHH:MM` biçimini `tz`deki UTC anına çevirir.

    Etrafındaki boşluk atılır. Saniye, offset, `Z`, diğer biçimler `ValueError`
    (Türkçe mesaj) fırlatır. Sonuç `resolve_local` ile elde edilir.

    Neden: Kullanıcı girdisi katı bir şablona zorlanır; esnek parse edilmemesi
    yanlış alarm/brifing zamanlarını önler.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("Boş zaman metni geçersizdir")

    # Saniye, offset, Z gibi kabul edilmeyen kalıpları erken yakala.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            datetime.strptime(stripped, fmt)
        except ValueError:
            continue
        raise ValueError("saniye içeren format desteklenmez")

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            naive = datetime.strptime(stripped, fmt)
        except ValueError:
            continue
        # Saniye/mikrosaniye sıfır olmalı (strptime zaten öyle verir ama garanti).
        if naive.second != 0 or naive.microsecond != 0:
            raise ValueError("Saniye veya milisaniye içeren format desteklenmez")
        day = naive.date()
        at = naive.time()
        return resolve_local(day, at, tz)

    raise ValueError(
        "Zaman formatı 'YYYY-MM-DD HH:MM' veya 'YYYY-MM-DDTHH:MM' olmalıdır"
    )


def format_local(moment: datetime, tz: ZoneInfo) -> str:
    """UTC `moment` u `tz`deki yerel saate çevirip `YYYY-MM-DD HH:MM` olarak biçimler.

    Neden: Kullanıcıya gösterim ve loglama için tutarlı, saat dilimi bilincili
    bir dize standardı sağlar; `parse_local` ile round-trip güvenlidir.
    """
    _ensure_aware_utc(moment, "moment")
    local = moment.astimezone(tz)
    return local.strftime("%Y-%m-%d %H:%M")
