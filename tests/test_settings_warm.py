"""Ayarlar paneli (sıcak yeniden tasarım): kirli durum, placeholder, tema renkleri, alt dolgu."""

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from artemis.channels.desktop import provider_section, themes
from artemis.channels.desktop.settings_panel import SettingsPanel
from tests.test_settings_panel import config


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _panel(tmp_path: Path, *, demo: bool = False) -> SettingsPanel:
    return SettingsPanel(config(tmp_path), demo=demo)


def test_demo_never_autosaves_and_has_no_revert(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path, demo=True)
    panel.barge_in_box.setChecked(not panel.barge_in_box.isChecked())
    assert not panel._dirty and not panel._save_timer.isActive()
    assert panel.revert_button.isHidden()
    panel.deleteLater()


def test_theme_change_does_not_dirty_the_form(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path)
    panel.theme_cards["dark"].radio.click()
    panel.set_theme("light")
    assert not panel._dirty
    # anında kaydedilen sayfa açıklamayla değil kısa "Kaydedildi" ile bildirir
    assert panel.saved_notes["Görünüm"].text() == "Kaydedildi"
    assert not panel.saved_notes["Görünüm"].isHidden()
    panel.deleteLater()


def test_device_toggle_rebuild_is_not_a_user_change(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path)
    panel.advanced_devices.setChecked(True)
    assert not panel._dirty
    panel.deleteLater()


def test_empty_combos_show_muted_placeholders(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path, demo=True)
    assert panel.device_combo.currentText() == "Mikrofon bulunamadı"
    assert not panel.device_combo.isEnabled()
    providers = panel.providers
    assert providers.add_connection.currentText() == "Bağlantı yok — aşağıdan ekle"
    assert providers.model_hint is not None
    assert providers.model_hint.placeholderText() == "Önce bağlantı seç"
    assert not providers.model_name.isEnabled()
    panel.deleteLater()


def test_current_microphone_is_never_empty(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path, demo=True)
    assert panel.current_device.text() == "Seçilmedi"
    panel._set_current_device("  ")
    assert panel.current_device.text() == "Seçilmedi"
    panel._set_current_device("Dahili mikrofon")
    assert panel.current_device.text() == "Dahili mikrofon"
    panel.deleteLater()


def test_level_meter_is_a_labelled_slim_bar(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path)
    assert panel.level_value.text() == "%0"
    panel.input_level.setValue(40)  # app.py aynı çağrıyı yapar
    assert panel.level_value.text() == "%40"
    assert not panel.input_level.isTextVisible() and panel.input_level.height() <= 6
    panel.deleteLater()


def test_provider_section_has_no_hardcoded_colors() -> None:
    source = Path(provider_section.__file__).read_text(encoding="utf-8")
    assert not re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", source)


def test_theme_change_restyles_provider_section(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path)  # config'teki eski sağlayıcılı model "eski ayar" etiketi taşır
    section = panel.providers
    assert section.model_list.count() == 1
    section.model_list.setCurrentRow(0)
    light, dark = themes.THEMES["light"], themes.THEMES["dark"]

    def row_html() -> str:
        return section.model_list.itemWidget(section.model_list.item(0)).text()  # type: ignore[attr-defined]

    section.set_theme(light)
    assert light["warning"] in row_html()
    section.set_theme(dark)
    text = row_html()
    assert dark["warning"] in text and light["warning"] not in text
    assert section.model_list.currentRow() == 0  # seçim korunur
    banner_style = section.banner.styleSheet()
    assert dark["accent"] in banner_style or dark["error"] in banner_style
    panel.deleteLater()


def test_no_sticky_bar_and_notice_lives_in_title_row(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path)
    assert not hasattr(panel, "apply_button") and not hasattr(panel, "dirty_label")
    assert panel.title_row.indexOf(panel.notice) >= 0
    assert panel.title_row.indexOf(panel.revert_button) >= 0
    for index in range(panel.stack.count()):
        layout = panel.stack.widget(index).layout()
        assert layout is not None and layout.contentsMargins().bottom() == 0
    panel.deleteLater()


def test_section_rows_are_tight_and_group_headers_add_height(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path)
    firsts = {names[0] for names in panel.section_groups().values()}
    for row in range(panel.sections.count()):
        item = panel.sections.item(row)
        assert item.sizeHint().height() == (52 if item.text() in firsts else 30)
    panel.deleteLater()
