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


def test_apply_is_disabled_until_dirty_and_survives_window_enable(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path)
    assert not panel.apply_button.isEnabled() and panel.dirty_label.isHidden()
    panel.apply_button.setEnabled(True)  # pencere (_set_controls) hazır diyor; değişiklik yok
    assert not panel.apply_button.isEnabled()
    panel.barge_in_box.setChecked(not panel.barge_in_box.isChecked())
    assert panel.apply_button.isEnabled()
    assert panel.dirty_label.text() == "Kaydedilmemiş değişiklik"
    panel.apply_button.setEnabled(False)  # işlem sürerken pencere kapatır
    assert not panel.apply_button.isEnabled()
    panel.apply_button.setEnabled(True)
    assert panel.apply_button.isEnabled()
    panel.refresh()
    assert not panel.apply_button.isEnabled()
    panel.deleteLater()


def test_theme_change_does_not_dirty_the_form(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path)
    panel.theme_cards["dark"].radio.click()
    panel.set_theme("light")
    assert panel.dirty_label.isHidden() and not panel.apply_button.isEnabled()
    panel.show_section(panel.section_names().index("Görünüm"))
    texts = [label.text() for label in panel.findChildren(type(panel.dirty_label))]
    assert any("Hemen uygulanır" in text for text in texts)
    panel.deleteLater()


def test_device_toggle_rebuild_is_not_a_user_change(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path)
    panel.advanced_devices.setChecked(True)
    assert panel.dirty_label.isHidden()
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


def test_last_row_is_reachable_above_the_bar(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path)
    bar = panel.apply_button.parentWidget()
    assert bar is not None
    for index in range(panel.stack.count()):
        layout = panel.stack.widget(index).layout()
        assert layout is not None
        assert layout.contentsMargins().bottom() >= bar.height()
    panel.deleteLater()


def test_section_rows_are_36px(tmp_path: Path, qt_app) -> None:
    panel = _panel(tmp_path)
    for row in range(panel.sections.count()):
        assert panel.sections.item(row).sizeHint().height() == 36
    panel.deleteLater()
