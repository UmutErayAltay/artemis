"""Sohbet sayfası ve kabuk sunumu: yoldaş karşılaması, balonlar, yazma alanı, üst çubuk,
kenar çubuğu ikonları, işlem sayfası. Yalnız offscreen demo pencere; güvenlik mantığı yok."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from artemis.channels.desktop.app import (
    ChatTranscript,
    DesktopWindow,
    OperationCard,
    PetFace,
    build_window,
)
from artemis.channels.desktop.bridge import Activity
from artemis.channels.desktop.shell_assets import NAV_ICONS, greeting_for_hour
from artemis.channels.desktop.themes import THEMES


@pytest.fixture
def app() -> QApplication:
    current = QApplication.instance()
    instance = current if isinstance(current, QApplication) else QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def _wait(app: QApplication, condition: Any, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("GUI durumu zamanında oluşmadı")


@pytest.fixture
def window(app: QApplication, tmp_path: Path) -> Any:
    win = build_window(tmp_path / "ignored.yaml", demo=True, app=app)
    win.show()
    _wait(app, lambda: win.status_label.text() == "Hazır")
    yield win
    if not win.bridge.stopped:
        win._request_exit()
    _wait(app, lambda: win.bridge.stopped)
    _wait(app, lambda: win._temporary is None)


def _card_events(window: DesktopWindow, request: str) -> None:
    window._begin_card(request, "mesaj " + request)
    for seq, (event, fields) in enumerate((
        ("turn_start", {}),
        ("tool_requested", {"tool": "demo_write_memory", "arguments": {"key": "k"}}),
        ("tool_result", {"tool": "demo_write_memory", "elapsed_seconds": 0.1,
                         "result": {"status": "ok", "success": True, "message": "tamam"}}),
        ("turn_end", {"stop_reason": "completed"}),
    )):
        window._handle_event(Activity(request, "t-" + request, seq, event, fields))


def test_rerendering_a_card_leaves_no_ghost_widgets(window: DesktopWindow) -> None:
    """Kök neden: `deleteLater` olay döngüsüne kalır; eski kart o ana dek çizilir.

    Olay döngüsü ÇALIŞTIRILMADAN sayılır; böylece geciken silme gizli kalamaz.
    """
    window.resize(1280, 800)
    _card_events(window, "m1")
    host = window.activity.widget()
    one_card_labels = len(host.findChildren(QLabel))
    for _ in range(6):
        window._render_card("m1")
    assert len(host.findChildren(OperationCard)) == 1
    assert len(host.findChildren(QLabel)) == one_card_labels
    # İkinci istek: iki kart, iki kez yeniden çizilse de ikiden fazla çocuk yok.
    _card_events(window, "m2")
    window._render_card("m1")
    assert len(host.findChildren(OperationCard)) == 2


@pytest.mark.parametrize(
    ("hour", "text"),
    [(0, "İyi geceler"), (4, "İyi geceler"), (5, "Günaydın"), (11, "Günaydın"),
     (12, "İyi günler"), (17, "İyi günler"), (18, "İyi akşamlar"), (22, "İyi akşamlar"),
     (23, "İyi geceler")],
)
def test_greeting_follows_injected_hour(app: QApplication, hour: int, text: str) -> None:
    assert greeting_for_hour(hour) == text
    chat = ChatTranscript(False, lambda _t: None, hour_of_day=lambda: hour)
    assert chat.greeting.text() == text
    assert chat.greeting.objectName() == "greeting"


def test_welcome_chips_fill_composer_without_sending(window: DesktopWindow) -> None:
    assert window.chat.welcome.isVisible()
    assert [b.text() for b in window.chat.suggestion_buttons] == ["Merhaba", "örnek akış"]
    assert all(b.objectName() == "chip" for b in window.chat.suggestion_buttons)
    window.chat.suggestion_buttons[1].click()
    assert window.prompt.toPlainText() == "örnek akış"
    assert not window._busy and window.chat.toPlainText() == ""


def test_real_mode_examples_map_to_existing_tools(app: QApplication) -> None:
    chat = ChatTranscript(False, lambda _t: None)
    assert [b.text() for b in chat.suggestion_buttons] == [
        "Saat kaç?", "Yarın 09:00 için hatırlatma kur", "Not defterini aç",
    ]
    from artemis.tools.registry import discover_tools

    registry = discover_tools()
    assert all(name in registry for name in ("current_time", "reminder_create", "open_application"))


def test_cancel_is_visible_only_while_busy(window: DesktopWindow) -> None:
    assert not window.cancel_button.isVisible()
    window._busy = True
    window._set_controls()
    assert window.cancel_button.isVisible() and window.cancel_button.isEnabled()
    window._busy = False
    window._set_controls()
    assert not window.cancel_button.isVisible()
    assert window.send_button.objectName() == "primary"


def test_composer_starts_single_row_grows_to_four_lines_and_keeps_keys(
    window: DesktopWindow,
) -> None:
    assert window.prompt.height() == 44
    window.prompt.setPlainText("\n".join(f"satır {i}" for i in range(12)))
    QApplication.processEvents()
    grown = window.prompt.height()
    assert 44 < grown <= 4 * window.prompt.fontMetrics().lineSpacing() + 40
    window.prompt.clear()
    QApplication.processEvents()
    assert window.prompt.height() == 44
    # Shift+Enter yeni satır ekler, gönderme olmaz.
    window.prompt.setPlainText("a")
    window.prompt.moveCursor(window.prompt.textCursor().MoveOperation.End)
    QTest.keyClick(window.prompt, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert "\n" in window.prompt.toPlainText() and not window._busy
    assert window.prompt.accessibleName() == "Komut metni"
    assert window.send_button.text() == "Gönder" and window.cancel_button.text() == "İptal"


def test_icon_toggles_expose_checked_state_and_names(window: DesktopWindow) -> None:
    for button in (window.voice_button, window.composer_voice, window.auto_button):
        assert button.isCheckable() and button.objectName() == "iconToggle"
        assert button.accessibleName() and button.toolTip() and not button.icon().isNull()
        assert button.text() == ""
    assert window.voice_button.accessibleName() == "Sesi aç veya kapat"
    window._show_voice_state("listening")
    assert window.voice_button.isChecked() and window.composer_voice.isChecked()
    assert window.voice_button.accessibleDescription() == "Dinliyor"
    window._show_voice_state("off")
    assert not window.voice_button.isChecked()
    window.auto_button.setChecked(True)
    assert window.auto_button.accessibleDescription() == "Oto onay açık"
    assert "Oto onay açık" in window.auto_button.toolTip()
    window.auto_button.setChecked(False)
    assert window.auto_button.accessibleDescription() == "Oto onay kapalı"


def test_topbar_has_one_status_pill_and_demo_chip(window: DesktopWindow) -> None:
    assert window.demo_chip is not None and window.demo_chip.text() == "Demo"
    assert window.model_value.objectName() == "chip"
    assert window.brand_pet.width() == 32


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_sidebar_icons_follow_nav_text_color(window: DesktopWindow, theme: str) -> None:
    window._apply_theme(theme)
    assert len(window.nav_buttons) == len(NAV_ICONS) == 5
    target = QColor(THEMES[theme]["nav_text"])
    for button in window.nav_buttons.values():
        assert not button.icon().isNull() and button.text() in NAV_ICONS
        image = button.icon().pixmap(20, 20).toImage().convertToFormat(
            button.icon().pixmap(20, 20).toImage().Format.Format_ARGB32
        )
        solid = [
            QColor(image.pixel(x, y))
            for x in range(image.width()) for y in range(image.height())
            if (image.pixel(x, y) >> 24) & 0xFF >= 200
        ]
        assert solid, button.text()
        assert all(
            abs(c.red() - target.red()) < 12 and abs(c.green() - target.green()) < 12
            and abs(c.blue() - target.blue()) < 12 for c in solid
        ), (theme, button.text())


def test_operations_drawer_opens_and_closes_below_1100px(window: DesktopWindow) -> None:
    window.resize(1280, 800)
    QApplication.processEvents()
    _card_events(window, "m1")
    assert window.activity_panel.isVisible() and not window.operations_button.isVisible()
    window.resize(900, 700)
    QApplication.processEvents()
    assert window.operations_button.isVisible()
    assert not window.activity_panel.isVisible() and not window.drawer.isVisible()
    window.operations_button.click()
    assert window.drawer.isVisible() and window.activity_panel.isVisible()
    host = window.activity.widget()
    assert len(host.findChildren(OperationCard)) == 1  # veri kaybı yok
    QTest.keyClick(window.drawer, Qt.Key.Key_Escape)
    assert not window.drawer.isVisible()
    window.operations_button.click()
    assert window.drawer.isVisible()
    window.activity_close.click()
    assert not window.drawer.isVisible()
    window.operations_button.click()
    window.resize(1280, 800)
    QApplication.processEvents()
    assert not window.drawer.isVisible() and window.activity_panel.isVisible()
    assert len(host.findChildren(OperationCard)) == 1
    assert window.activity_close.isHidden()


def test_new_chat_clears_operation_cards_and_shows_welcome(
    app: QApplication, window: DesktopWindow
) -> None:
    window.resize(1280, 800)
    _card_events(window, "m1")
    window.chat.append("Siz", "selam")
    assert window._op_widgets and not window.chat.welcome.isVisible()
    window._new_chat()
    _wait(app, lambda: not window._op_widgets)
    assert not window._ops.cards and window._ops.dropped == 0
    assert not window.activity_empty.isHidden() and window.activity.isHidden()
    assert window.chat.welcome.isVisible() and window.chat.toPlainText() == ""


def test_messages_use_bubbles_and_pet_avatar(window: DesktopWindow) -> None:
    window.chat.append("Siz", "kısa")
    window.chat.append("Artemis", "yanıt " * 40)
    bubbles = [(b.objectName(), s) for b, s, _t in window.chat._bubbles]
    assert bubbles == [("userBubble", "Siz"), ("assistantBubble", "Artemis")]
    faces = window.chat.content.findChildren(PetFace)
    assert len(faces) == 1 and faces[0].width() == 28
    assert window.chat.content.maximumWidth() == 680
    assert window.chat._bubbles[-1][0].width() <= 680
