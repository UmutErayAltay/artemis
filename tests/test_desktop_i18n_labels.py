"""Demo akışı, hızlı komut durumu ve pet durum metinleri için yerelleştirme testleri."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(autouse=True)
def _restore_language() -> Iterator[None]:
    from artemis.i18n import set_language

    set_language("tr")
    try:
        yield
    finally:
        set_language("tr")


def _ensure_app(monkeypatch: pytest.MonkeyPatch) -> object:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
        app = QApplication([])
    return app


def test_demo_triggers_for_english_and_turkish() -> None:
    from artemis.brain.models import Message
    from artemis.channels.desktop.demo import DemoProvider

    provider = DemoProvider()
    for prompt in ("sample flow", "SAMPLE FLOW", "örnek akış"):
        completion = asyncio.run(
            provider.complete([Message(role="user", content=prompt)], None, timeout=1)
        )
        assert [call.name for call in completion.tool_calls] == [
            "demo_read_sample",
            "demo_write_memory",
        ]


def test_demo_write_arguments_follow_language() -> None:
    from artemis.brain.models import Message
    from artemis.channels.desktop.demo import DemoProvider
    from artemis.i18n import set_language

    provider = DemoProvider()

    set_language("en")
    en_completion = asyncio.run(
        provider.complete([Message(role="user", content="sample flow")], None, timeout=1)
    )
    assert en_completion.tool_calls[1].arguments == {
        "key": "last_sample",
        "value": "temporary demo record",
    }

    set_language("tr")
    tr_completion = asyncio.run(
        provider.complete([Message(role="user", content="örnek akış")], None, timeout=1)
    )
    assert tr_completion.tool_calls[1].arguments == {
        "key": "son_örnek",
        "value": "geçici demo kaydı",
    }


def test_demo_english_hint_mentions_sample_flow() -> None:
    from artemis.brain.models import Message
    from artemis.channels.desktop.demo import DemoProvider
    from artemis.i18n import set_language

    set_language("en")
    completion = asyncio.run(
        DemoProvider().complete([Message(role="user", content="merhaba")], None, timeout=1)
    )
    assert completion.content == (
        "DEMO · This input only reached the offline demo provider; no real action was taken. "
        "Type ‘sample flow’ for a safe approval example."
    )
    assert "sample flow" in completion.content
    assert "örnek akış" not in completion.content


def test_quick_overlay_status_retranslates(monkeypatch: pytest.MonkeyPatch) -> None:
    _ensure_app(monkeypatch)
    from artemis.channels.desktop.chat_widgets import QuickOverlay
    from artemis.channels.desktop.retranslate import retranslate_widgets
    from artemis.i18n import set_language, t

    set_language("tr")
    overlay = QuickOverlay(lambda: None, lambda: None, lambda: None)
    try:
        overlay.set_busy(False, t("Hazır"))
        assert overlay.status.text() == "Hazır"

        set_language("en")
        retranslate_widgets([overlay], "en")
        assert overlay.status.text() == "Ready"

        set_language("tr")
        retranslate_widgets([overlay], "tr")
        assert overlay.status.text() == "Hazır"
    finally:
        overlay.close()


def test_pet_status_description_translates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ensure_app(monkeypatch)
    from PySide6.QtCore import QSettings

    from artemis.channels.desktop.pet import PetWidget
    from artemis.channels.desktop.retranslate import retranslate_widgets
    from artemis.i18n import set_language

    ini_path = tmp_path / "pet.ini"
    settings = QSettings(str(ini_path), QSettings.Format.IniFormat)
    pet = PetWidget(
        on_toggle_voice=lambda: None,
        on_show_panel=lambda: None,
        on_quick=lambda: None,
        on_approval=lambda: None,
        on_quit=lambda: None,
        on_submit=lambda _text: True,
        settings=settings,
        reduced_motion=True,
    )
    try:
        set_language("en")
        pet.set_voice("idle")
        assert pet.toolTip() == "Ready"
        assert pet.accessibleDescription() == "Ready"

        set_language("tr")
        retranslate_widgets([pet], "tr")
        assert pet.toolTip() == "Hazır"
        assert pet.accessibleDescription() == "Hazır"
    finally:
        pet.close()
