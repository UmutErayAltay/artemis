"""Canlı dil geçişinde widget ağacının yerinde çevrilmesi testleri."""

from __future__ import annotations

from collections.abc import Iterator

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


def test_retranslate_widgets_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    _ensure_app(monkeypatch)
    from PySide6.QtWidgets import (
        QComboBox,
        QLabel,
        QPushButton,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )

    from artemis.channels.desktop.retranslate import retranslate_widgets
    from artemis.i18n import set_language, t

    set_language("tr")
    root = QWidget()
    root.setWindowTitle(t("Ayarlar"))
    root.setToolTip(t("Arayüz dili"))
    layout = QVBoxLayout(root)

    label = QLabel(t("Dil"), root)
    label.setToolTip(t("Arayüz dili"))
    layout.addWidget(label)

    button = QPushButton(t("Kaydet"), root)
    button.setToolTip(t("Tekrar dene"))
    layout.addWidget(button)

    combo = QComboBox(root)
    combo.addItem(t("Türkçe"), "tr")
    combo.addItem("English", "en")
    layout.addWidget(combo)

    tabs = QTabWidget(root)
    tab_page = QWidget()
    tabs.addTab(tab_page, t("Dil"))
    tabs.setTabToolTip(0, t("Arayüz dili"))
    layout.addWidget(tabs)

    set_language("en")
    retranslate_widgets([root], "en")

    assert root.windowTitle() == "Settings"
    assert root.toolTip() == "Interface language"
    assert label.text() == "Language"
    assert label.toolTip() == "Interface language"
    assert button.text() == "Save"
    assert button.toolTip() == "Retry"
    assert combo.itemText(0) == "Turkish"
    assert combo.itemData(0) == "tr"
    assert combo.itemText(1) == "English"
    assert combo.itemData(1) == "en"
    assert tabs.tabText(0) == "Language"
    assert tabs.tabToolTip(0) == "Interface language"

    set_language("tr")
    retranslate_widgets([root], "tr")

    assert root.windowTitle() == "Ayarlar"
    assert label.text() == "Dil"
    assert button.text() == "Kaydet"
    assert combo.itemText(0) == "Türkçe"
    assert combo.itemData(0) == "tr"
    assert tabs.tabText(0) == "Dil"
    assert tabs.tabToolTip(0) == "Arayüz dili"
