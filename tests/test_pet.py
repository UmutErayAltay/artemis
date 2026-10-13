"""Pet testleri offscreen Qt kullanır; gerçek ayar kaydına (registry) yazılmaz."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect, QSettings, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from jarvis.channels.desktop.pet import (
    BODY,
    PetWidget,
    clamp_anchor,
    job_summary,
    level_from_message,
    truncate,
)


@pytest.fixture
def app() -> QApplication:
    current = QApplication.instance()
    instance = current if isinstance(current, QApplication) else QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


class Calls:
    def __init__(self) -> None:
        self.log: list[str] = []

    def cb(self, name: str) -> Callable[[], None]:
        return lambda: self.log.append(name)


@pytest.fixture
def made(app: QApplication, tmp_path: Path) -> Any:
    pets: list[PetWidget] = []

    def make(**kwargs: Any) -> tuple[PetWidget, Calls]:
        calls = Calls()
        pet = PetWidget(
            on_toggle_voice=calls.cb("voice"),
            on_show_panel=calls.cb("panel"),
            on_quick=calls.cb("quick"),
            on_approval=calls.cb("approval"),
            settings=QSettings(str(tmp_path / "pet.ini"), QSettings.Format.IniFormat),
            **kwargs,
        )
        pets.append(pet)
        return pet, calls

    yield make
    for pet in pets:
        pet.close()


def _wait(app: QApplication, condition: Callable[[], bool], timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("zamanında oluşmadı")


def _settle(pet: PetWidget, seconds: float = 1.5) -> None:
    for _ in range(int(seconds / 0.016)):
        pet.tick(0.016)


def apply_state(pet: PetWidget, name: str) -> None:
    """Her görsel durumu gerçek giriş yöntemleriyle kurar (strip betiği de kullanır)."""
    if name == "idle":
        pet.say("merhaba")  # kapalı sesle bile kısa süre uyandırır
    elif name == "listening":
        pet.set_voice("listening")
        pet.set_level(0.7)
    elif name == "thinking":
        pet.set_busy(True)
    elif name == "speaking":
        pet.set_voice("speaking")
    elif name == "approval":
        pet.set_approval(True)
    elif name == "working":
        pet.set_voice("listening")
        pet.code_agent_event("code_agent_started", {"job_id": "a"})
        pet.code_agent_event("code_agent_started", {"job_id": "b"})
    elif name == "happy":
        pet.code_agent_event("code_agent_done", {"job_id": "a", "project": "p", "status": "done"})
    elif name == "sad":
        pet.code_agent_event("code_agent_done", {"job_id": "a", "project": "p", "status": "error"})
    elif name == "error":
        pet.flash_error()


STATES = [
    "off", "idle", "listening", "thinking", "speaking", "approval", "working",
    "happy", "sad", "error",
]


def render(pet: PetWidget) -> QImage:
    image = QImage(pet.size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    pet.render(image)
    return image


def _opaque(image: QImage) -> int:
    return sum(
        1
        for y in range(0, image.height(), 2)
        for x in range(0, image.width(), 2)
        if image.pixelColor(x, y).alpha() > 40
    )


def test_state_precedence(made: Any) -> None:
    pet, _ = made()
    assert pet.state == "off"
    pet.set_voice("listening")
    assert pet.state == "listening"
    pet.set_busy(True)
    assert pet.state == "listening"  # dinleme düşünmeden önce gelir
    pet.set_voice("speaking")
    assert pet.state == "speaking"
    pet.set_approval(True)
    assert pet.state == "approval"
    pet.set_approval(False)
    pet.set_voice("off")
    assert pet.state == "thinking"
    pet.set_busy(False)
    pet.flash_error()
    assert pet.state == "error"
    pet.set_voice("awaiting_command")
    assert pet.state == "listening"


def test_working_badge_never_changes_state(made: Any) -> None:
    pet, _ = made()
    pet.set_voice("speaking")
    pet.code_agent_event("code_agent_started", {"job_id": "x"})
    assert pet.state == "speaking"
    assert pet.jobs == 1


def test_level_mapping() -> None:
    assert level_from_message("0") == 0.0
    assert level_from_message("50") == 0.5
    assert level_from_message("100") == 1.0
    assert level_from_message("250") == 1.0
    assert level_from_message("abc") is None
    assert level_from_message("") is None


def test_level_is_smoothed_and_only_for_listening(made: Any) -> None:
    pet, _ = made()
    pet.set_level(1.0)
    _settle(pet, 0.3)
    assert pet._level == 0.0  # dinlemiyorsa halka seviyesi yok
    pet.set_voice("listening")
    _settle(pet, 0.3)
    assert pet._level > 0.9
    pet.set_level(0.0)
    pet.tick(0.016)
    assert 0.5 < pet._level < 1.0  # iniş yavaş


def test_truncate_and_summaries() -> None:
    assert truncate("a  b\n c") == "a b c"
    long = truncate("x" * 500)
    assert len(long) == 140 and long.endswith("…")
    assert job_summary("jarvis", "done", ["a", "b"]) == "jarvis: bitti, 2 commit"
    assert job_summary("jarvis", "done", 0) == "jarvis: bitti, commit yok"
    assert job_summary("jarvis", "timeout", ["a"]) == "jarvis: hata"


def test_bubble_heard_answer_and_autohide(made: Any) -> None:
    pet, _ = made()
    pet.heard("   ")
    assert pet._bubble_text == ""
    pet.heard("saat kaç")
    assert pet._bubble_text == "saat kaç" and pet.width() > BODY
    pet.answer("y" * 300)
    assert len(pet._bubble_text) == 140
    _settle(pet, 7.5)
    assert pet._bubble_text == "" and pet.width() == BODY


def test_bubble_is_plain_text_not_markup(made: Any) -> None:
    pet, _ = made()
    pet.answer("<b>kalın</b>")
    assert pet._bubble_text == "<b>kalın</b>"
    assert _opaque(render(pet)) > 0


def test_code_agent_events_update_jobs_and_bubble(made: Any) -> None:
    pet, _ = made()
    assert pet.code_agent_event("code_agent_started", {"job_id": "1", "project": "web"}) is None
    pet.code_agent_event("code_agent_started", {"job_id": "2", "project": "api"})
    assert pet.jobs == 2
    text = pet.code_agent_event(
        "code_agent_done",
        {"job_id": "1", "project": "web", "status": "done", "commits": ["a", "b"]},
    )
    assert text == "web: bitti, 2 commit"
    assert pet._bubble_text == text and pet.jobs == 1
    assert pet._reaction == "happy"
    text = pet.code_agent_event(
        "code_agent_done", {"job_id": "2", "project": "api", "status": "timeout"}
    )
    assert text == "api: hata" and pet.jobs == 0 and pet._reaction == "sad"
    assert pet.code_agent_event("tool_result", {}) is None


def test_click_vs_drag(app: QApplication, made: Any) -> None:
    pet, calls = made()
    pet._click_delay_ms = 0
    pet.show()
    pet.move_anchor(QPoint(200, 200))
    start = QPoint(pet.pos())
    center = QPoint(BODY // 2, BODY // 2)
    QTest.mouseClick(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
    _wait(app, lambda: calls.log == ["voice"])
    assert pet.pos() == start

    calls.log.clear()
    QTest.mousePress(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
    QTest.mouseMove(pet, center + QPoint(40, 30))
    QTest.mouseRelease(
        pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center + QPoint(40, 30)
    )
    time.sleep(0.05)
    app.processEvents()
    assert calls.log == []  # sürükleme tık sayılmaz
    assert pet._anchor != QPoint(200, 200)
    assert pet._settings.value("x", type=int) == pet._anchor.x()


def test_double_click_opens_panel_not_voice(app: QApplication, made: Any) -> None:
    pet, calls = made()
    pet.show()
    center = QPoint(BODY // 2, BODY // 2)
    QTest.mouseDClick(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
    time.sleep(0.4)
    app.processEvents()
    assert "panel" in calls.log and "voice" not in calls.log


def test_click_during_approval_focuses_approval(made: Any) -> None:
    pet, calls = made()
    pet.show()
    pet.set_approval(True)
    center = QPoint(BODY // 2, BODY // 2)
    QTest.mouseClick(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
    assert calls.log == ["approval"]


def test_menu_entries_and_hide_is_persisted(made: Any) -> None:
    pet, calls = made()
    pet.show()
    menu = pet.build_menu()
    labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert labels == ["Paneli aç", "Hızlı komut", "Sesi aç", "Peti gizle"]
    pet.set_voice("listening")
    assert "Sesi kapat" in [a.text() for a in pet.build_menu().actions()]
    menu.actions()[0].trigger()
    assert calls.log == ["panel"]
    menu.actions()[-1].trigger()
    assert not pet.isVisible()
    assert pet._settings.value("visible", True, type=bool) is False
    pet.start()
    assert not pet.isVisible()  # kayıtlı gizlilik korunur
    pet.toggle_pet()
    assert pet.isVisible()


def test_position_clamping_and_default_corner(made: Any) -> None:
    area = QRect(0, 0, 800, 600)
    assert clamp_anchor(QPoint(-50, -50), area) == QPoint(0, 0)
    assert clamp_anchor(QPoint(5000, 5000), area) == QPoint(800 - BODY, 600 - BODY)
    assert clamp_anchor(QPoint(100, 100), area) == QPoint(100, 100)
    pet, _ = made()
    screen = pet._area(QPoint(0, 0))
    assert pet._anchor.x() + BODY <= screen.right() + 1
    assert pet._anchor.y() + BODY <= screen.bottom() + 1
    assert pet._anchor.x() > screen.center().x()  # varsayılan sağ alt
    pet.move_anchor(QPoint(-9999, -9999))
    assert pet._anchor == screen.topLeft()
    pet.move_anchor(QPoint(99999, 99999))
    assert pet._anchor == QPoint(screen.right() + 1 - BODY, screen.bottom() + 1 - BODY)


def test_bubble_flips_toward_screen_center(made: Any) -> None:
    pet, _ = made()
    screen = pet._area(QPoint(0, 0))
    pet.move_anchor(screen.topLeft())
    pet.say("sol köşe")
    assert not pet._bubble_side_left and pet.pos().x() == pet._anchor.x()
    pet.move_anchor(QPoint(99999, 0))
    assert pet._bubble_side_left
    assert pet.pos().x() + pet.width() == pet._anchor.x() + BODY


def test_position_restored_from_settings(app: QApplication, tmp_path: Path) -> None:
    settings = QSettings(str(tmp_path / "p.ini"), QSettings.Format.IniFormat)
    settings.setValue("x", 33)
    settings.setValue("y", 44)
    pet = PetWidget(
        on_toggle_voice=lambda: None,
        on_show_panel=lambda: None,
        on_quick=lambda: None,
        on_approval=lambda: None,
        settings=settings,
    )
    assert pet._anchor == QPoint(33, 44)


def test_timer_runs_only_while_visible(made: Any) -> None:
    pet, _ = made()
    assert not pet._timer.isActive()
    pet.show()
    assert pet._timer.isActive()
    pet.hide()
    assert not pet._timer.isActive()


def test_accessibility_follows_state(made: Any) -> None:
    pet, _ = made()
    assert pet.accessibleName() == "Jarvis pet"
    assert "ses kapalı" in pet.accessibleDescription()
    pet.set_approval(True)
    assert "Onay" in pet.accessibleDescription()
    pet.code_agent_event("code_agent_started", {"job_id": "x"})
    assert "1 kod işi" in pet.accessibleDescription()


@pytest.mark.parametrize("name", STATES)
def test_every_state_renders_pixels(made: Any, name: str) -> None:
    pet, _ = made(reduced_motion=False)
    apply_state(pet, name)
    _settle(pet)
    assert _opaque(render(pet)) > 200, name
    reduced, _ = made(reduced_motion=True)
    apply_state(reduced, name)
    _settle(reduced, 0.5)
    assert _opaque(render(reduced)) > 200


def test_reduced_motion_freezes_ambient_motion(made: Any) -> None:
    pet, _ = made(reduced_motion=True)
    pet.set_voice("speaking")
    _settle(pet, 5.0)
    assert pet._offsets("speaking")[1] == 0.0
    assert pet._blink_p is None
