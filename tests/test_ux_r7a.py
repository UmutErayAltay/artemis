"""UX denetimi düzeltmeleri: ortak sayfa kabuğu, dar gezinme, diyalog kabuğu, sade durumlar."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QRect, QSize, Qt
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
from artemis.channels.desktop.form_kit import button_row
from artemis.channels.desktop.pet_section import _PetCard
from artemis.channels.desktop.projects_panel import PAGE_MAX_WIDTH, ProjectsPanel
from artemis.channels.desktop.skills_section import SkillsSection
from artemis.channels.desktop.themes import THEMES, stylesheet
from artemis.channels.desktop.trigger_dialog import TriggerDialog
from artemis.channels.desktop.triggers_section import TriggersSection
from artemis.channels.desktop.update_section import UpdateSection
from artemis.i18n import set_language, t
from artemis.skills import ensure_example
from artemis.update_check import UpdateResult
from tests.test_skills_section import make_config as make_skills_config
from tests.test_trigger_form import make_config


@pytest.fixture(autouse=True)
def _restore_language() -> Iterator[None]:
    """Her testten sonra arayüz dilini varsayılan Türkçeye döndürür."""
    set_language("tr")
    try:
        yield
    finally:
        set_language("tr")


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
    target = names.index("Bağlantılar")
    combo.setCurrentIndex(combo.findData(target))
    combo.activated.emit(combo.currentIndex())
    assert panel.stack.currentIndex() == target
    panel.show_section(names.index("Model ve Bağlantılar"))  # kodla gezinme seçiciyi de günceller
    assert combo.currentText() == "Model ve Bağlantılar"
    # Grup başlığı ölü değil: tıklanınca grubun ilk sayfasına gider, kutuda sayfa adı görünür.
    header = next(
        i for i in range(combo.count()) if combo.itemText(i) == "Dış Bağlantılar ve Sistem"
    )
    combo.setCurrentIndex(header)
    combo.activated.emit(header)
    assert panel.stack.currentIndex() == names.index("Bağlantılar")
    assert combo.currentText() == "Bağlantılar"


def test_settings_list_shows_at_default_window_size(
    window: DesktopWindow, app: QApplication
) -> None:
    """Varsayılan 1180px pencerede bölüm listesi yanda durur, açılır seçiciye düşmez."""
    window.tabs.setCurrentWidget(window.settings_scroll)
    window.resize(1180, 780)
    _settle(app)
    assert window.settings_panel.nav.isVisible()
    assert not window.settings_panel.section_combo.isVisible()


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


def test_flow_row_ignores_hidden_widgets(app: QApplication) -> None:
    """`_FlowRow` `.isHidden()` olan çocukları gösterim öncesinde ve sonrasında yok sayar."""
    host = QWidget()
    visible = QPushButton("Aç", host)
    hidden = QPushButton("Çok daha uzun gizli düğme metni", host)
    hidden.setMinimumWidth(260)
    hidden.hide()
    row = button_row(visible, hidden)
    host.setLayout(row)

    # Üst widget henüz show() edilmeden önce bile yalnızca görünür düğme ölçülür.
    expected = row.itemAt(0).sizeHint()
    assert row.sizeHint() == expected
    assert row.minimumSize().width() == row.itemAt(0).minimumSize().width()
    assert row.heightForWidth(expected.width()) == expected.height()

    host.resize(320, 80)
    host.show()
    _settle(app)
    assert visible.geometry().right() == host.rect().right()
    hidden.show()
    _settle(app)
    assert row.sizeHint().width() > expected.width()
    host.deleteLater()


def test_flow_row_hidden_middle_wraps_after_show(app: QApplication) -> None:
    """İki görünür düğme arasındaki gizli öğe açılınca satır üst üste binmeden sarılır."""
    host = QWidget()
    first = QPushButton("Bir", host)
    middle = QPushButton("Orta", host)
    last = QPushButton("Son", host)
    first.setMinimumWidth(100)
    middle.setMinimumWidth(140)
    last.setMinimumWidth(100)
    middle.hide()

    row = button_row(first, middle, last)
    host.setLayout(row)
    host.resize(260, 120)
    host.show()
    _settle(app)

    assert first.y() == last.y()
    assert first.geometry().right() < last.geometry().left()

    middle.show()
    _settle(app)

    shown = (first, middle, last)
    for button in shown:
        assert button.isVisible()
        assert host.rect().contains(button.geometry()), button.geometry()
    for i, left in enumerate(shown):
        for right in shown[i + 1 :]:
            assert not left.geometry().intersects(right.geometry())
    assert first.y() == middle.y() < last.y()
    assert row.heightForWidth(260) >= last.geometry().bottom() + 1
    host.deleteLater()


def test_flow_row_all_hidden_has_zero_size_hint(app: QApplication) -> None:
    """Tüm çocukları gizli `_FlowRow` kenar boşluğu olsa bile sıfır boyut ipucu döner."""
    host = QWidget()
    first = QPushButton("Bir", host)
    second = QPushButton("İki", host)
    first.hide()
    second.hide()

    row = button_row(first, second)
    row.setContentsMargins(10, 6, 14, 8)
    host.setLayout(row)

    assert row.sizeHint() == QSize(0, 0)
    assert row.minimumSize() == QSize(0, 0)
    assert row.heightForWidth(240) == 0
    host.deleteLater()


def test_flow_row_contents_margins_and_no_vertical_expand(app: QApplication) -> None:
    """`_FlowRow` contentsMargins değerlerini hesaba katar ve dikey büyümez."""
    host = QWidget()
    first = QPushButton("Tamam", host)
    second = QPushButton("İptal", host)
    row = button_row(first, second)

    base_hint = row.sizeHint()
    base_min = row.minimumSize()
    base_hfw = row.heightForWidth(base_hint.width())

    row.setContentsMargins(10, 6, 14, 8)
    assert row.expandingDirections() == Qt.Orientation(0)
    assert row.sizeHint() == QSize(base_hint.width() + 24, base_hint.height() + 14)
    assert row.minimumSize() == QSize(base_min.width() + 24, base_min.height() + 14)
    assert row.heightForWidth(base_hint.width() + 24) == base_hfw + 14
    assert row.heightForWidth(base_hint.width() + 23) > base_hfw + 14

    host.setLayout(row)
    host.resize(base_hint.width() + 24, base_hfw + 14)
    host.show()
    _settle(app)
    assert second.geometry().right() == host.rect().right() - 14
    assert first.geometry().top() >= 6
    assert first.geometry().left() >= 10
    host.deleteLater()


@pytest.mark.parametrize("lang", ["tr", "en"])
def test_update_section_fits_narrow_viewport(lang: str, app: QApplication) -> None:
    """Güncelleme bölümü dar görünümde (<= 420px) iki dilde de taşmadan sarılır."""
    set_language(lang)
    section = UpdateSection(frozen=True)
    section.resize(400, 300)
    section.show()
    section._on_done(
        UpdateResult(
            status="newer",
            latest="v9.9.9",
            url="https://github.com/UmutErayAltay/artemis/releases/tag/v9.9.9",
            message=t("Yeni sürüm var: {latest}.").format(latest="v9.9.9"),
            installer_url="https://github.com/UmutErayAltay/artemis/releases/download/v9.9.9/s.exe",
            installer_name="Artemis-Setup-9.9.9.exe",
            installer_size=1_000_000,
            installer_sha256="a" * 64,
        )
    )
    section.cancel_button.setVisible(True)
    _settle(app)

    assert section.minimumSizeHint().width() <= 420
    visible_buttons = [
        b for b in (
            section.check_button,
            section.open_button,
            section.install_button,
            section.cancel_button,
        )
        if b.isVisible()
    ]
    assert len(visible_buttons) == 4
    for button in visible_buttons:
        assert button.width() >= button.sizeHint().width()
        assert section.rect().contains(button.geometry()), (lang, button.text(), button.geometry())

    section.finish()
    section.deleteLater()


def test_skills_empty_state_has_single_primary_cta(tmp_path: Path, app: QApplication) -> None:
    """Beceriler boş-etkin durumda tek birincil CTA gösterir; dolunca üst düğme geri gelir."""
    vault = tmp_path / "vault"
    vault.mkdir()
    section = SkillsSection(make_skills_config(tmp_path, vault))
    section.show()
    _settle(app)

    empty_cta = section.valid_area.empty.action_button
    assert empty_cta is not None
    assert empty_cta.objectName() == "primary"
    visible_new = [
        b for b in section.findChildren(QPushButton)
        if b.text() == "Yeni beceri…" and b.isVisible()
    ]
    assert visible_new == [empty_cta]
    assert not section.new_button.isVisible()

    assert section._settings is not None
    ensure_example(section._settings)
    section.refresh()
    _settle(app)

    assert section.valid_list.count() >= 1
    assert section.new_button.isVisible()
    visible_after = [
        b for b in section.findChildren(QPushButton)
        if b.text() == "Yeni beceri…" and b.isVisible()
    ]
    assert visible_after == [section.new_button]
    section.deleteLater()


def test_open_settings_section_resolves_turkish_keys_in_english(
    tmp_path: Path, app: QApplication
) -> None:
    """İngilizce arayüzde Otomasyon bağlantılarının Türkçe bölüm anahtarları eşleşir."""
    set_language("en")
    win = build_window(tmp_path / "ignored.yaml", demo=True, app=app)
    try:
        win.pet.hide()
        win.show()
        _settle(app)
        names = win.settings_panel.section_names()
        expected_routes = {
            "Rutinler": ("Otomasyon Kuralları", 0),
            "Tetikleyiciler": ("Otomasyon Kuralları", 1),
            "Gün özeti": ("Otomasyon Kuralları", 2),
            "Araştırma": ("Otomasyon Kuralları", 3),
            "Model": ("Model ve Bağlantılar", None),
        }
        for key, (page_tr, subtab) in expected_routes.items():
            win.automation_panel.settings_requested.emit(key)
            _settle(app)
            assert win.tabs.currentWidget() is win.settings_scroll
            assert names[win.settings_panel.sections.currentRow()] == t(page_tr)
            if subtab is not None:
                assert win.settings_panel.automation_tabs.currentIndex() == subtab
    finally:
        win._request_exit()
        end = 0
        while not win.bridge.stopped and end < 400:
            app.processEvents()
            end += 1


@pytest.mark.parametrize(
    ("lang", "expected"),
    [("tr", "Demo modunda kapalı."), ("en", "Disabled in demo mode.")],
)
def test_projects_demo_buttons_have_disabled_tooltip(
    lang: str, expected: str, app: QApplication
) -> None:
    """Demo modunda Projeler birincil düğmeleri devre dışı nedenini araç ipucuyla açıklar."""
    set_language(lang)
    panel = ProjectsPanel(demo=True)
    assert not panel.prepare_button.isEnabled()
    assert not panel.start_button.isEnabled()
    assert panel.prepare_button.toolTip() == expected
    assert panel.start_button.toolTip() == expected
    assert panel.prepare_reason.text() == ""
    assert panel.start_reason.text() == ""
    panel.deleteLater()
