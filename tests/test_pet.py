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

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSettings, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from artemis.channels.desktop.pet import (
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
        self.sent: list[str] = []
        self.accept = True

    def submit(self, text: str) -> bool:
        self.sent.append(text)
        return self.accept

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
            on_quit=calls.cb("quit"),
            on_submit=calls.submit,
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
    assert job_summary("artemis", "done", ["a", "b"]) == "artemis: bitti, 2 commit"
    assert job_summary("artemis", "done", 0) == "artemis: bitti, commit yok"
    assert job_summary("artemis", "timeout", ["a"]) == "artemis: hata"


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


def test_live_bubble_shows_latest_words(made: Any) -> None:
    pet, _ = made()
    pet.live("  ")
    assert pet._bubble_text == ""
    pet.live("artemis saat")
    assert pet._bubble_text == "artemis saat" and pet._bubble_kind == "heard"
    pet.live("baş " + "x" * 300 + " son")
    assert len(pet._bubble_text) == 140
    assert pet._bubble_text.startswith("…") and pet._bubble_text.endswith(" son")


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


def _mouse_event(kind: QEvent.Type, pos: QPoint) -> QMouseEvent:
    point = QPointF(pos)
    return QMouseEvent(
        kind, point, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _click(pet: PetWidget, times: int = 1) -> None:
    center = QPoint(BODY // 2, BODY // 2)
    for _ in range(times):
        QTest.mouseClick(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)


def test_one_click_opens_quick_box_below_pet(app: QApplication, made: Any) -> None:
    pet, calls = made()
    pet._click_delay_ms = 0
    pet.show()
    pet.move_anchor(QPoint(200, 200))
    start = QPoint(pet.pos())
    _click(pet)
    _wait(app, lambda: pet._quick.isVisible())
    assert calls.log == [] and pet.pos() == start
    assert pet._quick.y() >= 200 + BODY  # gövdenin altında
    pet.move_anchor(QPoint(300, 220))  # pet taşınınca kutu izler
    assert pet._quick.y() >= 220 + BODY and pet._quick.x() != 200
    _click(pet)  # ikinci tek tık kapatır
    _wait(app, lambda: not pet._quick.isVisible())


def test_quick_box_flips_above_near_screen_bottom(made: Any) -> None:
    pet, _ = made()
    pet.show()
    area = pet._area(pet._anchor)
    pet.move_anchor(QPoint(300, area.bottom()))
    pet._quick.open()
    pet._place_quick()
    assert pet._quick.y() + pet._quick.height() <= pet._anchor.y()


def test_two_clicks_toggle_voice_three_open_menu(
    app: QApplication, made: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pet, calls = made()
    pet._click_delay_ms = 0
    pet.show()
    menus: list[QPoint] = []
    monkeypatch.setattr(pet, "_open_menu", menus.append)
    # Qt'nin gerçek çift-tık dizisi: basış, bırakış, çift-tık olayı (ikinci basış), bırakış.
    center = QPoint(BODY // 2, BODY // 2)
    _click(pet)
    QApplication.sendEvent(pet, _mouse_event(QEvent.Type.MouseButtonDblClick, center))
    QTest.mouseRelease(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
    _wait(app, lambda: calls.log == ["voice"])
    assert not pet._quick.isVisible() and menus == []

    pet._click_delay_ms = 10_000  # 3. tık zamanlayıcıyı beklemeden gönderilir
    _click(pet, 3)
    assert len(menus) == 1 and calls.log == ["voice"] and pet._clicks == 0
    assert not pet._quick.isVisible()


def test_click_vs_drag(app: QApplication, made: Any) -> None:
    pet, calls = made()
    pet._click_delay_ms = 0
    pet.show()
    pet.move_anchor(QPoint(200, 200))
    center = QPoint(BODY // 2, BODY // 2)
    QTest.mousePress(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
    QTest.mouseMove(pet, center + QPoint(40, 30))
    QTest.mouseRelease(
        pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center + QPoint(40, 30)
    )
    time.sleep(0.05)
    app.processEvents()
    assert calls.log == [] and pet._clicks == 0 and not pet._quick.isVisible()  # tık sayılmaz
    assert pet._anchor != QPoint(200, 200)
    assert pet._settings.value("x", type=int) == pet._anchor.x()


def test_quick_box_submit_outcomes(made: Any) -> None:
    pet, calls = made()
    box = pet._quick
    box.open()
    box.input.setText("   ")
    box.send()
    assert calls.sent == [] and box.isVisible()  # boş metin bir şey yapmaz

    calls.accept = False
    box.input.setText("merhaba")
    box.send()
    assert calls.sent == ["merhaba"] and box.input.text() == "merhaba"
    assert not box.hint.isHidden() and box.isVisible()  # metin korunur, ipucu görünür

    calls.accept = True
    box.send()
    assert box.input.text() == "" and not box.isVisible() and box.hint.isHidden()

    box.open()
    QTest.keyClick(box, Qt.Key.Key_Escape)
    assert not box.isVisible()
    assert box.input.maxLength() == 2000 and box.accessibleName()


def test_quick_box_hides_with_pet_and_never_gives_pet_command_logic(made: Any) -> None:
    pet, calls = made()
    pet.show()
    pet._quick.open()
    pet.hide()
    assert not pet._quick.isVisible() and calls.log == []


def test_job_list_states_hover_and_drop(made: Any) -> None:
    pet, _ = made()
    pet.show()
    pet.code_agent_event("code_agent_started", {"job_id": "1", "project": "web"})
    assert pet._bubble_text == "web · çalışıyor"
    pet.code_agent_event("code_agent_started", {"job_id": "2", "project": "api"})
    pet.code_agent_event("code_agent_waiting", {"job_id": "2", "project": "api"})
    assert pet._bubble_text == "web · çalışıyor\napi · girdi bekliyor"
    assert pet.jobs == 2  # dişli sayısı = etkin işler (bekleyen de etkin)
    pet._bubble_text = ""
    gear = pet._body_rect().topLeft() + QPoint(BODY // 2 + 32, 60 - 30)
    QTest.mouseMove(pet, QPoint(5, 5))
    QTest.mouseMove(pet, gear)
    assert "api · girdi bekliyor" in pet._bubble_text  # rozet üstüne gelme listeyi açar
    text = pet.code_agent_event(
        "code_agent_done", {"job_id": "1", "project": "web", "status": "done"}
    )
    assert text == "web: bitti, commit yok" and pet.jobs == 1
    pet.code_agent_event("code_agent_done", {"job_id": "2", "project": "api", "status": "error"})
    assert pet.jobs == 0 and pet.job_lines() == []


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
    assert labels == ["Paneli aç", "Hızlı komut", "Sesi aç", "Peti gizle", "Kapat"]
    pet.set_voice("listening")
    assert "Sesi kapat" in [a.text() for a in pet.build_menu().actions()]
    menu.actions()[0].trigger()
    assert calls.log == ["panel"]
    menu.actions()[-1].trigger()
    assert calls.log == ["panel", "quit"]
    next(a for a in menu.actions() if a.text() == "Peti gizle").trigger()
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
        on_quit=lambda: None,
        on_submit=lambda text: True,
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
    assert pet.accessibleName() == "Artemis pet"
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


def test_particles_sleep_z_and_success_burst(made: Any) -> None:
    pet, _ = made(reduced_motion=False)
    for _ in range(60):
        pet.tick(0.05)  # ses kapalı: uyku "z"leri çıkar, ömrü dolanlar silinir
    assert {pt.kind for pt in pet._particles} == {"z"} and len(pet._particles) <= 3
    pet.code_agent_event("code_agent_done", {"job_id": "1", "status": "done"})
    assert sum(pt.kind == "spark" for pt in pet._particles) == 10
    for _ in range(30):
        pet.tick(0.05)
    assert not any(pt.kind == "spark" for pt in pet._particles)
    quiet, _ = made(reduced_motion=True)
    for _ in range(60):
        quiet.tick(0.05)
    quiet.code_agent_event("code_agent_done", {"job_id": "1", "status": "done"})
    assert quiet._particles == []


# ------------------------------------------------------------- sprite pet (ADR 0048)


def _pet_widget(made: Any, tmp_path: Path, **kwargs: Any) -> tuple[PetWidget, Calls]:
    from tests.test_pet_sprite import make_pet

    pets = tmp_path / "pets"
    make_pet(pets, "blob")
    return made(pets_dir=pets, **kwargs)


def _center(pet: PetWidget) -> tuple[int, int, int]:
    color = render(pet).pixelColor(BODY // 2, BODY // 2)
    return color.red(), color.green(), color.alpha()


@pytest.mark.parametrize("name", STATES)
def test_sprite_renders_pixels_in_every_state(made: Any, tmp_path: Path, name: str) -> None:
    pet, _ = _pet_widget(made, tmp_path, reduced_motion=False)
    assert pet.set_skin("blob") is None and pet.skin == "blob"
    apply_state(pet, name)
    _settle(pet, 0.2)
    assert _opaque(render(pet)) > 200, name


def test_sprite_rows_follow_state_and_reduced_motion_is_frame_zero(
    made: Any, tmp_path: Path
) -> None:
    from artemis.channels.desktop import pet_sprite as ps

    pet, _ = _pet_widget(made, tmp_path, reduced_motion=True)
    pet.set_skin("blob")
    for name, row in (("listening", ps.REVIEW), ("thinking", ps.RUNNING),
                      ("speaking", ps.WAVING), ("approval", ps.WAITING), ("error", ps.FAILED)):
        fresh, _ = _pet_widget_reuse(made, pet)
        apply_state(fresh, name)
        _settle(fresh, 0.3)
        assert fresh._sprite_row == row, name
        # Hücre rengi R=satır*25, G=sütun*25: azaltılmış harekette hep sütun 0
        assert _center(fresh)[:2] == (row * 25, 0), name


def _pet_widget_reuse(made: Any, template: PetWidget) -> tuple[PetWidget, Calls]:
    fresh, calls = made(pets_dir=template._pets_dir, reduced_motion=template._reduced)
    fresh.set_skin("blob")
    return fresh, calls


def test_sprite_animates_when_motion_allowed(made: Any, tmp_path: Path) -> None:
    pet, _ = _pet_widget(made, tmp_path, reduced_motion=False)
    pet.set_skin("blob")
    pet.say("x")
    pet.tick(0.016)
    first = _center(pet)
    _settle(pet, 0.2)  # >1 kare ilerler
    assert _center(pet)[1] != first[1]


def test_sprite_jump_once_and_drag_direction(made: Any, tmp_path: Path) -> None:
    from artemis.channels.desktop import pet_sprite as ps

    pet, _ = _pet_widget(made, tmp_path, reduced_motion=False)
    pet.set_skin("blob")
    apply_state(pet, "happy")
    pet.tick(0.016)
    assert pet._sprite_row == ps.JUMPING
    _settle(pet, 5 / ps.FPS + 0.3)  # zıplamanın 5 karesi bitince idle
    assert pet._sprite_row == ps.IDLE
    pet._dragging, pet._run_right = True, False
    pet.tick(0.016)
    assert pet._sprite_row == ps.RUN_LEFT
    pet._run_right = True
    pet.tick(0.016)
    assert pet._sprite_row == ps.RUN_RIGHT


def test_broken_or_missing_pet_falls_back_to_orb(made: Any, tmp_path: Path) -> None:
    pet, _ = _pet_widget(made, tmp_path)
    assert "bulunamadı" in (pet.set_skin("yok") or "") and pet.skin == "orb"
    (tmp_path / "pets" / "blob" / "spritesheet.png").write_bytes(b"bozuk")
    assert pet.set_skin("blob") is not None and pet._sprite is None
    assert _opaque(render(pet)) > 200  # küre çizilmeye devam eder
    assert pet.set_skin(None) is None and pet.skin == "orb"


def test_skin_is_persisted_and_loaded_on_start(made: Any, tmp_path: Path) -> None:
    pet, _ = _pet_widget(made, tmp_path)
    pet.set_skin("blob")
    assert pet._settings.value("skin") == "blob"
    again, _ = made(pets_dir=tmp_path / "pets")  # aynı pet.ini
    assert again.skin == "orb"  # start() öncesi
    again.start()
    assert again.skin == "blob"
    (tmp_path / "pets" / "blob" / "pet.json").unlink()
    broken, _ = made(pets_dir=tmp_path / "pets")
    broken.start()
    assert broken.skin == "orb" and pet._settings.value("skin") == "blob"  # kayıt silinmez


def test_os_reduced_motion_used_only_without_explicit_setting(
    made: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from artemis.channels.desktop import pet as pet_module

    monkeypatch.setattr(pet_module, "os_reduced_motion", lambda: True)
    assert made()[0]._reduced is True
    explicit, _ = made()
    explicit._settings.setValue("reduced_motion", False)
    again, _ = made()
    assert again._reduced is False  # açık ayar işletim sistemini yener
    monkeypatch.setattr(pet_module, "os_reduced_motion", lambda: False)
    again._settings.remove("reduced_motion")
    assert made()[0]._reduced is False
