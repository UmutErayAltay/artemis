"""Kaydırma aralığı yalnız görünen sayfaya uyar (Ayarlar bölümleri, Petler atölyesi)."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QScrollArea

from artemis.channels.desktop.app import DesktopWindow, build_window
from artemis.channels.desktop.settings_panel import _BAR_HEIGHT, SettingsPanel
from artemis.config.settings import ModelConfig
from tests.test_settings_panel import config


@pytest.fixture
def app() -> QApplication:
    current = QApplication.instance()
    return current if isinstance(current, QApplication) else QApplication([])


def _settle(app: QApplication) -> None:
    for _ in range(3):  # yerleşim istekleri zincirleme gelir
        app.processEvents()


def _check_sections(panel: SettingsPanel, app: QApplication) -> None:
    scroll = panel.sections.scroll_area
    bar = scroll.verticalScrollBar()
    ranges = []
    for index in range(len(panel.section_names())):
        panel.show_section(index)
        _settle(app)
        page = panel.stack.currentWidget()
        # Aralık sayfanın kendi içeriğinden gelir (sayfa alt dolgusu zaten içinde), en uzun
        # sayfadan değil.
        assert bar.maximum() <= max(0, page.sizeHint().height() - scroll.viewport().height()) + 8
        ranges.append(bar.maximum())
    assert max(ranges) > 0 and min(ranges) < max(ranges)  # kısa sayfa uzun kadar kaymaz
    # Alt dolgu yapışkan çubuk kadar: son satır erişilebilir kalır.
    assert panel.stack.widget(0).layout().contentsMargins().bottom() == _BAR_HEIGHT
    panel.show_section(0)
    _settle(app)
    bar.setValue(bar.maximum())
    assert bar.value() > 0
    panel.show_section(1)
    _settle(app)
    assert bar.value() == 0
    bar.setValue(bar.maximum())
    panel.sections.setCurrentRow(2)  # kullanıcı yolu da sıfırlar
    _settle(app)
    assert bar.value() == 0


def test_settings_panel_scroll_range_follows_current_page(
    tmp_path: Path, app: QApplication
) -> None:
    panel = SettingsPanel(config(tmp_path))
    panel.resize(1056, 700)
    panel.show()
    _settle(app)
    _check_sections(panel, app)
    panel.deleteLater()


def test_real_window_settings_and_pet_workshop(tmp_path: Path, app: QApplication) -> None:
    window: DesktopWindow = build_window(tmp_path / "ignored.yaml", demo=True, app=app)
    try:
        window.pet.hide()
        window.resize(1280, 850)
        window.show()
        _settle(app)
        window.tabs.setCurrentWidget(window.settings_scroll)
        _settle(app)
        _check_sections(window.settings_panel, app)

        pets = window.settings_panel.pet
        creator = pets.creator
        assert creator is not None
        creator._demo = False
        creator._models = {"y": ModelConfig(provider="ollama", model="m",
                                            input_per_million=Decimal(0),
                                            output_per_million=Decimal(0))}
        creator._fill_models()
        creator._update_buttons()
        window.tabs.setCurrentWidget(window.pet_page)
        for size in ((1280, 850), (900, 700)):
            window.resize(*size)
            pets.show_gallery()
            _settle(app)
            gallery_max = window.pet_scroll.verticalScrollBar().maximum()
            # Galeri kendi içeriğinden uzun kaydırılmaz (atölyenin yüksekliği karışmaz).
            assert gallery_max <= max(
                0, pets.sizeHint().height() - window.pet_scroll.viewport().height()
            )
            pets.show_workshop()
            _settle(app)
            # İç içe kaydırıcı yok: atölyede görünür tek dikey kaydırma bölgesi pet_scroll.
            visible = [s for s in window.pet_page.findChildren(QScrollArea) if s.isVisible()]
            assert visible == [window.pet_scroll]
            # Atölye sayfanın yüksekliğini kullanır: küçük kutu yok (önceden ~%33 idi).
            assert creator.height() >= 0.6 * window.pet_scroll.viewport().height()
            pets.show_gallery()
            _settle(app)
            assert window.pet_scroll.verticalScrollBar().maximum() == gallery_max
    finally:
        window._request_exit()
        end = 0
        while not window.bridge.stopped and end < 400:
            app.processEvents()
            end += 1
