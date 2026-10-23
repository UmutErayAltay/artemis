"""`artemis.proactive.schedule` modülü için testler.

Tüm testler saf fonksiyonları doğrular; ağ, mock, uyku yoktur.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from artemis.proactive.schedule import (
    briefing_occurrence,
    classify_reminder,
    format_local,
    local_day_bounds,
    parse_local,
    resolve_local,
)

IST = ZoneInfo("Europe/Istanbul")  # UTC+3, DST yok
BERLIN = ZoneInfo("Europe/Berlin")  # DST var


class TestResolveLocal:
    """`resolve_local` davranışları."""

    def test_istanbul_no_dst(self) -> None:
        """Europe/Istanbul (UTC+3, DST yok): 2026-10-15 08:30 -> 05:30 UTC."""
        day = date(2026, 10, 15)
        at = time(8, 30)
        result = resolve_local(day, at, IST)
        expected = datetime(2026, 10, 15, 5, 30, tzinfo=UTC)
        assert result == expected
        assert result.tzinfo is UTC

    def test_berlin_spring_gap(self) -> None:
        """Europe/Berlin ileri sarma: 2026-03-29 02:30 yerel yok -> 01:00 UTC (03:00 CEST)."""
        # 2026-03-29'de saat 02:00 -> 03:00 atlar (CEST başlar). 02:30 yoktur.
        day = date(2026, 3, 29)
        at = time(2, 30)
        result = resolve_local(day, at, BERLIN)
        # 03:00 CEST = 01:00 UTC
        expected = datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
        assert result == expected

    def test_berlin_fall_back_ambiguity(self) -> None:
        """Europe/Berlin gerialma: 2026-10-25 02:30 yerel -> 00:30 UTC (ilk, CEST)."""
        # 2026-10-25 03:00 CEST -> 02:00 CET. 02:30 iki kez olur; ilk CEST'tir.
        day = date(2026, 10, 25)
        at = time(2, 30)
        result = resolve_local(day, at, BERLIN)
        # İlk oluşum: 02:30 CEST = 00:30 UTC
        expected = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
        assert result == expected

    def test_naive_time_raises(self) -> None:
        """`at` timezone-aware olursa `ValueError`."""
        with pytest.raises(ValueError, match="timezone-aware olamaz"):
            resolve_local(date(2026, 1, 1), time(12, 0, tzinfo=UTC), IST)


class TestBriefingOccurrence:
    """`briefing_occurrence` davranışları."""

    def test_inside_window(self) -> None:
        """Pencere içindeyse scheduled döner."""
        days = (0, 1, 2, 3, 4)  # Pt-Cuma
        at = time(9, 0)
        # 2026-10-15 Perşembe (3), yerel 09:15 -> UTC 06:15
        now = datetime(2026, 10, 15, 6, 15, tzinfo=UTC)
        grace = timedelta(hours=1)
        scheduled = resolve_local(date(2026, 10, 15), at, IST)
        result = briefing_occurrence(days, at, now, IST, grace)
        assert result == scheduled

    def test_before_scheduled_returns_none(self) -> None:
        """Planlanandan önceyse `None`."""
        days = (3,)  # Perşembe
        at = time(9, 0)
        # 08:00 UTC = yerel 11:00, brifing 09:00 (06:00 UTC) -> geçmiş
        now = datetime(2026, 10, 15, 5, 0, tzinfo=UTC)  # scheduled'dan 1 saat önce
        grace = timedelta(hours=1)
        result = briefing_occurrence(days, at, now, IST, grace)
        assert result is None

    def test_after_window_returns_none(self) -> None:
        """Pencere bitiminden sonra `None`."""
        days = (3,)
        at = time(9, 0)
        # scheduled 06:00 UTC, grace 1h -> window 07:00 UTC'e kadar
        now = datetime(2026, 10, 15, 7, 30, tzinfo=UTC)
        grace = timedelta(hours=1)
        result = briefing_occurrence(days, at, now, IST, grace)
        assert result is None

    def test_weekday_not_in_days_returns_none(self) -> None:
        """Gün `days` içinde değilse `None`."""
        days = (0, 1)  # Pt, Salı
        at = time(9, 0)
        # 2026-10-15 Perşembe
        now = datetime(2026, 10, 15, 6, 15, tzinfo=UTC)
        grace = timedelta(hours=1)
        result = briefing_occurrence(days, at, now, IST, grace)
        assert result is None

    def test_crosses_local_midnight_uses_previous_date(self) -> None:
        """Gece yarısını aşıyan pencere: at=23:30, grace=2h, now=00:30 ertesi gün -> dün."""
        # 2026-10-15 Perşembe 23:30 yerel = 20:30 UTC
        # Grace 2h -> pencere 20:30 - 22:30 UTC
        # Ertesi gün (Cuma) yerel 00:30 = 2026-10-15 21:30 UTC -> pencere içinde
        days = (3, 4)  # Perşembe, Cuma
        at = time(23, 30)
        now = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)  # Cuma yerel 00:30
        grace = timedelta(hours=2)
        # Perşembe'nin scheduled'i 2026-10-15 23:30 yerel = 20:30 UTC
        expected = resolve_local(date(2026, 10, 15), at, IST)
        result = briefing_occurrence(days, at, now, IST, grace)
        assert result == expected

    def test_grace_zero_still_one_minute_window(self) -> None:
        """grace=0 yine de 1 dakikalık pencere verir."""
        days = (3,)
        at = time(9, 0)
        # scheduled 06:00 UTC, pencere [06:00, 06:01)
        now = datetime(2026, 10, 15, 6, 0, 30, tzinfo=UTC)  # 30 sn içinde
        grace = timedelta(0)
        result = briefing_occurrence(days, at, now, IST, grace)
        expected = resolve_local(date(2026, 10, 15), at, IST)
        assert result == expected

        # 06:01:00 -> pencere dışı
        now_late = datetime(2026, 10, 15, 6, 1, tzinfo=UTC)
        result_late = briefing_occurrence(days, at, now_late, IST, grace)
        assert result_late is None

    def test_both_dates_match_returns_later(self) -> None:
        """Her iki yerel tarih de tutuyorsa daha geç scheduled döner."""
        # at=23:30, grace=2h. Perşembe 23:30 -> Cuma 01:30 yerel pencere.
        # Cuma da 23:30 brifing varsa (days her iki gün de), now=Cuma 00:30
        # hem Perşembe'nin (gece yarısını aşkın) hem Cuma'nın (henüz başlamadı)
        # penceresinde değildir. Ama now=Cuma 23:45 ise her ikisi de tutar.
        days = (3, 4)  # Perşembe, Cuma
        at = time(23, 30)
        # Cuma 23:45 yerel = 20:45 UTC
        now = datetime(2026, 10, 16, 20, 45, tzinfo=UTC)
        grace = timedelta(hours=2)
        # Perşembe scheduled: 2026-10-15 23:30 yerel = 20:30 UTC, window bitiş 22:30 UTC
        # Cuma scheduled: 2026-10-16 23:30 yerel = 20:30 UTC, window bitiş 22:30 UTC
        # now=20:45 UTC her ikisinin de penceresindedir -> daha geç (Cuma) döner
        result = briefing_occurrence(days, at, now, IST, grace)
        expected = resolve_local(date(2026, 10, 16), at, IST)
        assert result == expected

    def test_naive_now_raises(self) -> None:
        """Naive `now` `ValueError` fırlatır."""
        with pytest.raises(ValueError, match="timezone-aware olmalı"):
            briefing_occurrence(
                (0,), time(9, 0), datetime(2026, 1, 1, 9, 0), IST, timedelta(hours=1)
            )


class TestClassifyReminder:
    """`classify_reminder` dört sonucu ve sınırları."""

    def test_wait(self) -> None:
        """due > now -> 'wait'."""
        due = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
        now = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
        assert classify_reminder(due, now, timedelta(hours=1)) == "wait"

    def test_deliver_exact_boundary(self) -> None:
        """delay == late_after -> 'deliver'."""
        due = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
        now = datetime(2026, 10, 15, 10, 5, tzinfo=UTC)  # 5 dk gecikme
        assert classify_reminder(due, now, timedelta(hours=1)) == "deliver"

    def test_deliver_within_late_after(self) -> None:
        """delay < late_after -> 'deliver'."""
        due = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
        now = datetime(2026, 10, 15, 10, 3, tzinfo=UTC)
        assert classify_reminder(due, now, timedelta(hours=1)) == "deliver"

    def test_late_exact_boundary(self) -> None:
        """delay == grace -> 'late'."""
        due = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
        now = datetime(2026, 10, 15, 11, 0, tzinfo=UTC)  # 60 dk gecikme
        assert classify_reminder(due, now, timedelta(hours=1)) == "late"

    def test_late_within_grace(self) -> None:
        """late_after < delay < grace -> 'late'."""
        due = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
        now = datetime(2026, 10, 15, 10, 30, tzinfo=UTC)
        assert classify_reminder(due, now, timedelta(hours=1)) == "late"

    def test_missed_exact_boundary(self) -> None:
        """delay > grace -> 'missed' (grace+1 sn)."""
        due = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
        now = datetime(2026, 10, 15, 11, 0, 1, tzinfo=UTC)
        assert classify_reminder(due, now, timedelta(hours=1)) == "missed"

    def test_missed_beyond_grace(self) -> None:
        """delay >> grace -> 'missed'."""
        due = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
        now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
        assert classify_reminder(due, now, timedelta(hours=1)) == "missed"

    def test_naive_args_raise(self) -> None:
        """Naive `due` veya `now` `ValueError`."""
        due = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
        now_naive = datetime(2026, 10, 15, 10, 0)
        with pytest.raises(ValueError, match="timezone-aware olmalı"):
            classify_reminder(due, now_naive, timedelta(hours=1))
        with pytest.raises(ValueError, match="timezone-aware olmalı"):
            classify_reminder(now_naive, due, timedelta(hours=1))


class TestLocalDayBounds:
    """`local_day_bounds` DST günü uzunluklarını doğrular."""

    def test_berlin_spring_23_hours(self) -> None:
        """2026-03-29 Berlin: gün 23 saat (02:00-03:00 atlanır)."""
        # 2026-03-29 12:00 UTC = Berlin 14:00 CEST (DST geçişi o gün)
        now = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
        start, end = local_day_bounds(now, BERLIN)
        # Gün başı: 2026-03-29 00:00 yerel yok -> 01:00 CEST = 23:00 UTC (28 Mart)
        # Gün sonu: 2026-03-30 00:00 yerel = 22:00 UTC (29 Mart)
        assert start == datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
        assert end == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)
        assert (end - start) == timedelta(hours=23)

    def test_berlin_fall_25_hours(self) -> None:
        """2026-10-25 Berlin: gün 25 saat (03:00->02:00 tekrar eder)."""
        now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
        start, end = local_day_bounds(now, BERLIN)
        # Gün başı: 2026-10-25 00:00 CEST = 22:00 UTC (24 Ekim)
        # Gün sonu: 2026-10-26 00:00 CET = 23:00 UTC (25 Ekim)
        assert start == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
        assert end == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)
        assert (end - start) == timedelta(hours=25)

    def test_istanbul_normal_24_hours(self) -> None:
        """Istanbul DST yok: her gün 24 saat."""
        now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
        start, end = local_day_bounds(now, IST)
        assert start == datetime(2026, 10, 14, 21, 0, tzinfo=UTC)  # 00:00+3
        assert end == datetime(2026, 10, 15, 21, 0, tzinfo=UTC)
        assert (end - start) == timedelta(hours=24)

    def test_naive_now_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware olmalı"):
            local_day_bounds(datetime(2026, 1, 1), IST)


class TestParseLocal:
    """`parse_local` format kuralları."""

    def test_space_separator(self) -> None:
        """Boşluk ayracı kabul edilir."""
        result = parse_local("2026-10-15 08:30", IST)
        expected = datetime(2026, 10, 15, 5, 30, tzinfo=UTC)
        assert result == expected

    def test_t_separator(self) -> None:
        """`T` ayracı kabul edilir."""
        result = parse_local("2026-10-15T08:30", IST)
        expected = datetime(2026, 10, 15, 5, 30, tzinfo=UTC)
        assert result == expected

    def test_surrounding_whitespace_stripped(self) -> None:
        """Etrafındaki boşluk atılır."""
        result = parse_local("  2026-10-15 08:30  ", IST)
        expected = datetime(2026, 10, 15, 5, 30, tzinfo=UTC)
        assert result == expected

    def test_rejects_seconds(self) -> None:
        """Saniye içeren format reddedilir."""
        with pytest.raises(ValueError, match="saniye"):
            parse_local("2026-10-15 08:30:00", IST)

    def test_rejects_offset(self) -> None:
        """Offset içeren format reddedilir."""
        with pytest.raises(ValueError, match="formatı"):
            parse_local("2026-10-15T08:30+03:00", IST)

    def test_rejects_z_suffix(self) -> None:
        """`Z` soneki reddedilir."""
        with pytest.raises(ValueError, match="formatı"):
            parse_local("2026-10-15T08:30Z", IST)

    def test_rejects_invalid_words(self) -> None:
        """Sözcük tabanlı giriş reddedilir."""
        with pytest.raises(ValueError, match="formatı"):
            parse_local("dün", IST)

    def test_rejects_empty(self) -> None:
        """Boş string reddedilir."""
        with pytest.raises(ValueError, match="Boş zaman metni"):
            parse_local("", IST)
        with pytest.raises(ValueError, match="Boş zaman metni"):
            parse_local("   ", IST)


class TestFormatLocal:
    """`format_local` biçimlendirme ve round-trip."""

    def test_format_basic(self) -> None:
        """Temel biçimlendirme."""
        moment = datetime(2026, 10, 15, 5, 30, tzinfo=UTC)
        assert format_local(moment, IST) == "2026-10-15 08:30"

    def test_round_trip(self) -> None:
        """`parse_local(format_local(...))` kimliktir."""
        original = datetime(2026, 10, 15, 5, 30, tzinfo=UTC)
        formatted = format_local(original, IST)
        parsed = parse_local(formatted, IST)
        assert parsed == original

    def test_naive_moment_raises(self) -> None:
        """Naive `moment` `ValueError`."""
        with pytest.raises(ValueError, match="timezone-aware olmalı"):
            format_local(datetime(2026, 10, 15, 5, 30), IST)
