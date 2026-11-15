"""UX denetimi düzeltmeleri: ortak sayfa kabuğu, dar gezinme, diyalog kabuğu, sade durumlar."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QRect
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QPushButton,
    QScrollArea,
    QWidget,
)

from artemis.channels.desktop.app import DesktopWindow, build_window
from artemis.channels.desktop.automation_panel import AutomationPanel
from artemis.channels.desktop.code_agent_config import AgentDialog
from artemis.channels.desktop.data_panel import DataPanel
from artemis.channels.desktop.pet_section import _PetCard
from artemis.channels.desktop.projects_panel import PAGE_MAX_WIDTH, ProjectsPanel
from artemis.channels.desktop.themes import THEMES, stylesheet
from artemis.channels.desktop.trigger_dialog import TriggerDialog
from artemis.channels.desktop.triggers_section import TriggersSection
from tests.test_trigger_form import make_config


@pytest.fixture
def app() -> QApplication:
    current = QApplication.instance()
    return current if isinstance(current, QApplication) else QApplication([])


def _settle(app: QApplication) -> None:
    for _ in range(4):
        app.processEvents()


@pytest.fixture
def window(tmp_path: Path, app: QApplication) -> Iterator[DesktopWindow]:
    win = build_window(tmp_path / "ignored.yaml", demo=True, app=app)
    win.pet.hide()
    win.show()
    _settle(app)
    yield win
    win._request_exit()
    end = 0
    while not win.bridge.stopped and end < 400:
        app.processEvents()
        end += 1


def _title_x(window: DesktopWindow, page: QWidget) -> int:
    labels = [
        label for label in page.findChildren(QLabel)
        if label.objectName() == "pageTitle" and label.isVisible()
    ]
    assert labels, page
    return int(labels[0].mapTo(window, labels[0].rect().topLeft()).x())


def test_nav_is_icon_only_when_narrow(window: DesktopWindow, app: QApplication) -> None:
    window.resize(1100, 800)
    _settle(app)
    assert window.nav_buttons[window.pet_page].text() == "Petler"
    window.resize(800, 700)
    _settle(app)
    for button in window.nav_buttons.values():
        assert button.text() == ""  # etiket yalnız ipucu ve erişilebilir adda
        assert button.toolTip() == button.accessibleName() != ""
    assert window.sidebar.width() < 100
    window.resize(1100, 800)
    _settle(app)
    assert window.nav_buttons[window.pet_page].text() == "Petler"
    assert window.theme_toggle.text() != ""


def test_settings_section_combo_replaces_list_when_narrow(
    window: DesktopWindow, app: QApplication
) -> None:
    window.tabs.setCurrentWidget(window.settings_scroll)
    panel = window.settings_panel
    window.resize(1500, 900)
    _settle(app)
    assert panel.nav.isVisible() and not panel.section_combo.isVisible()
    window.resize(780, 900)
    _settle(app)
    assert not panel.nav.isVisible() and panel.section_combo.isVisible()
    names = panel.section_names()
    combo = panel.section_combo
    pages = [combo.itemText(i) for i in range(combo.count()) if combo.itemData(i) is not None]
    assert pages == names  # aynı etiketler, aynı sıra
    headers = [combo.itemText(i) for i in range(combo.count()) if combo.itemData(i) is None]
    assert headers == list(panel.section_groups())  # grup yapısı korunur
    target = names.index("Telegram")
    combo.setCurrentIndex(combo.findData(target))
    combo.activated.emit(combo.currentIndex())
    assert panel.stack.currentIndex() == target
    panel.show_section(names.index("Model"))  # kodla gezinme seçiciyi de günceller
    assert combo.currentText() == "Model"


def test_main_pages_share_one_shell(window: DesktopWindow, app: QApplication) -> None:
    window.resize(1920, 1080)
    _settle(app)
    xs = {}
    for name, page in (
        ("Projeler", window.projects_page), ("Otomasyon", window.automation_page),
        ("Geçmiş", window.data_page), ("Petler", window.pet_page),
        ("Ayarlar", window.settings_scroll),
    ):
        window.tabs.setCurrentWidget(page)
        _settle(app)
        xs[name] = _title_x(window, page)
    # Yedek yazı tipinde (QT_QPA_FONTDIR yok) Projeler'in iki sütunu 1240px'e sığmaz; sayfa kendi
    # asgarisine genişler ve kasıtlı olarak sınırın dışında kalır.
    body = window.projects_panel.findChild(QScrollArea).widget()  # type: ignore[union-attr]
    if body.width() > PAGE_MAX_WIDTH:
        xs.pop("Projeler")
    # Başlık her sekmede aynı x'te; dikey çubuğu olan sayfa yarım çubuk (≤10px) kayar.
    assert max(xs.values()) - min(xs.values()) <= 10, xs
    # Başlık bloğun sol kenarıdır; blok sekme alanında ortalı ve en çok 1240px.
    left = xs["Ayarlar"] - window.tabs.mapTo(window, window.tabs.rect().topLeft()).x()
    assert window.tabs.width() - 2 * left <= PAGE_MAX_WIDTH + 2


def test_history_title_matches_nav_and_mentions_reminders(window: DesktopWindow) -> None:
    assert window.nav_buttons[window.data_page].accessibleName() == "Geçmiş"
    titles = [label.text() for label in window.data_page.findChildren(QLabel)]
    assert "Geçmiş" in titles
    assert any("hatırlatma" in text for text in titles)


def test_automation_links_open_settings_sections(app: QApplication) -> None:
    panel = AutomationPanel(demo=True)
    seen: list[str] = []
    panel.settings_requested.connect(seen.append)
    assert list(panel.settings_links) == ["Rutinler", "Tetikleyiciler", "Gün özeti", "Araştırma"]
    for link in panel.settings_links.values():
        link.click()
    assert seen == list(panel.settings_links)


def test_pet_chip_does_not_cover_icon(app: QApplication) -> None:
    pixmap = QPixmap(64, 64)
    pixmap.fill()
    card = _PetCard("Küre", "Artemis", pixmap)
    card.resize(140, 120)
    card.set_state(selected=True, highlighted=False)
    card.show()
    _settle(app)
    assert card.chip.isVisible()
    assert not card.chip.geometry().intersects(card.image.geometry())


def _trigger_dialog() -> QDialog:
    return TriggerDialog(None, [], ["r"], "")


def _agent_dialog() -> QDialog:
    return AgentDialog(("claude",))


@pytest.mark.parametrize("factory", [_trigger_dialog, _agent_dialog])
def test_dialogs_have_primary_action_and_fit_small_screens(
    app: QApplication, factory: Callable[[], QDialog]
) -> None:
    dialog = factory()
    ok: QPushButton = dialog.ok_button  # type: ignore[attr-defined]
    cancel: QPushButton = dialog.cancel_button  # type: ignore[attr-defined]
    assert ok.objectName() == "primary" and ok.text() in {"Ekle", "Kaydet"}
    dialog.show()
    for available in (QRect(0, 0, 1366, 728), QRect(0, 0, 1366, 480)):
        dialog.fit_to_available(available)  # type: ignore[attr-defined]
        _settle(app)
        assert dialog.height() <= available.height()
        for button in (ok, cancel):
            assert dialog.rect().contains(button.geometry()), (available, button.text())
        assert cancel.x() < ok.x()  # ApprovalDialog gibi: ikincil solda, birincil sağda
    dialog.close()


def test_demo_note_shown_once_on_projects(app: QApplication) -> None:
    panel = ProjectsPanel(demo=True)
    panel.show()
    _settle(app)
    assert panel.demo_note.isVisible()
    assert panel.start_reason.text() == "" and panel.prepare_reason.text() == ""
    assert panel.notice.text() == ""
    live = ProjectsPanel(demo=False)
    assert not live.demo_note.isVisible()
    live.deleteLater()
    panel.deleteLater()


def test_retention_spin_matches_apply_button(app: QApplication) -> None:
    panel = DataPanel(demo=True)
    panel.setStyleSheet(stylesheet("light-mixed"))  # ölçüler tema kurallarından gelir
    panel.resize(1000, 800)
    panel.show()
    _settle(app)
    spin_edit = panel.days.lineEdit()
    assert spin_edit is not None and spin_edit.textMargins().left() == 8
    assert panel.days.height() == panel.retention.height()
    assert panel.days.mapTo(panel, panel.days.rect().bottomLeft()).y() == panel.retention.mapTo(
        panel, panel.retention.rect().bottomLeft()
    ).y()
    panel.deleteLater()


def test_disabled_primary_keeps_primary_look() -> None:
    for name, palette in THEMES.items():
        rule = stylesheet(name).split("QPushButton#primary:disabled {")[1].split("}")[0]
        assert palette["accent"] in rule and "font-weight: 650" in rule, name


def test_triggers_empty_state_has_single_add_action(tmp_path: Path, app: QApplication) -> None:
    path = make_config(tmp_path, triggers={"enabled": True, "items": []})
    section = TriggersSection(path)
    section.show()
    _settle(app)
    buttons = section.findChildren(QPushButton)
    adds = [b for b in buttons if b.text() == "Tetikleyici ekle" and b.isVisible()]
    assert len(adds) == 1 and adds[0].objectName() == "primary"
    assert not [b for b in buttons if "dosya" in b.text().lower()]
    section.deleteLater()
