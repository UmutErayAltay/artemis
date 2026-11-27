"""Hafif kip niyet yönlendirmesi ve araç grupları (ADR 0092)."""

from __future__ import annotations

import pytest

from artemis.brain.light_mode import (
    ALWAYS_TOOLS,
    MAX_NARROWED_TOOLS,
    TOOL_GROUPS,
    route_groups,
    route_tools,
)
from artemis.tools.registry import discover_tools


def test_every_registered_tool_belongs_to_exactly_one_group() -> None:
    registered = {d.name for d in discover_tools().definitions()}
    grouped = [name for names in TOOL_GROUPS.values() for name in names]
    assert len(grouped) == len(set(grouped)), "bir araç iki grupta"
    assert set(grouped) == registered, (
        f"grupsuz: {registered - set(grouped)}, kayıtsız: {set(grouped) - registered}"
    )


@pytest.mark.parametrize(
    ("text", "group"),
    [
        ("notlar.txt dosyasını oku", "files"),
        ("masaüstündeki klasörü listele", "files"),
        ("rename the file report.pdf", "files"),
        ("Spotify'ı aç", "apps_windows"),
        ("Chrome penceresini küçült", "apps_windows"),
        ("yarın 9'da beni hatırlat", "reminders_calendar_mail"),
        ("do I have a meeting today", "reminders_calendar_mail"),
        ("okunmamış mailleri göster", "reminders_calendar_mail"),
        ("sesi kıs", "system_media"),
        ("bilgisayarı kapat", "system_media"),
        ("what time is it", "system_media"),
        ("notlarımda ara", "notes_memory"),
        ("bundan sonra kısa yanıt ver", "memory_admin"),
        ("ekranda ne yazıyor", "screen_computer"),
        ("artemis projesine iş yaptır", "code_agent"),
        ("bu konuyu araştır", "routines_research"),
        ("telegram'dan bildirim gönder", "messages"),
        ("search the web for cats", "web"),
        ("İNDİRİLENLER klasörü", "files"),  # Türkçe büyük İ
        ("OPEN the FILE", "files"),  # İngilizce büyük I
    ],
)
def test_routing_examples(text: str, group: str) -> None:
    assert group in route_groups(text)


def test_routing_narrows_and_adds_current_time() -> None:
    names = route_tools("notlar.txt dosyasını oku")
    assert names is not None
    assert names == TOOL_GROUPS["files"] | ALWAYS_TOOLS
    assert len(names) <= MAX_NARROWED_TOOLS


@pytest.mark.parametrize("text", ["merhaba nasılsın", "tell me a joke", "", "nasıl gidiyor"])
def test_no_match_means_full_set(text: str) -> None:
    assert route_tools(text) is None


def test_big_union_means_full_set() -> None:
    text = "dosyayı mail ile gönder, takvime ekle, kodu commit et, telegram'a bildir, sesi kıs"
    assert len(route_groups(text)) >= 4
    assert route_tools(text) is None
