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


def test_ambiguous_strings_round_trip_across_all_string_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aynı İngilizce karşılığı paylaşan anahtarlar tüm widget türlerinde tam geri dönmeli."""
    _ensure_app(monkeypatch)
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QGroupBox,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMenu,
        QPlainTextEdit,
        QPushButton,
        QRadioButton,
        QTabBar,
        QTabWidget,
        QTextEdit,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    from artemis.channels.desktop.retranslate import retranslate_widgets
    from artemis.i18n import set_language, t

    set_language("tr")
    root = QWidget()
    root.setWindowTitle(t("Test et"))
    root.setToolTip(t("Vazgeç"))
    root.setAccessibleName(t("Tekrar dene"))
    root.setAccessibleDescription(t("Yeniden dene"))
    root.setStatusTip(t("Test et"))
    layout = QVBoxLayout(root)

    label_test_et = QLabel(t("Test et"), root)
    label_test = QLabel(t("Test"), root)
    label_tpl = QLabel(t("{count} not var.").format(count=3), root)
    button = QPushButton(t("Test et"), root)
    checkbox = QCheckBox(t("Vazgeç"), root)
    radio = QRadioButton(t("İptal"), root)
    line_edit = QLineEdit(root)
    line_edit.setPlaceholderText(t("Test et"))
    text_edit = QTextEdit(root)
    text_edit.setPlaceholderText(t("Vazgeç"))
    plain_edit = QPlainTextEdit(root)
    plain_edit.setPlaceholderText(t("Tekrar dene"))

    group_box = QGroupBox(t("Test et"), root)

    menu = QMenu(t("Test et"), root)
    action_implicit = menu.addAction(t("Test et"))
    action_explicit = menu.addAction(t("Vazgeç"))
    action_explicit.setToolTip(t("Yeniden dene"))
    action_explicit.setStatusTip(t("Test et"))
    submenu = menu.addMenu(t("Tekrar dene"))
    sub_action = submenu.addAction(t("İptal"))
    detached_action = QAction(t("Test et"))
    root.addAction(detached_action)

    combo = QComboBox(root)
    combo.addItem(t("Test et"), "a")
    combo.addItem(t("Test"), "b")
    combo.addItem(t("Vazgeç"), "c")
    combo.addItem(t("İptal"), "d")

    tabs = QTabWidget(root)
    tabs.addTab(QWidget(), t("Test et"))
    tabs.setTabToolTip(0, t("Vazgeç"))
    tabs.addTab(QWidget(), t("Test"))
    tabs.setTabToolTip(1, t("İptal"))

    standalone_bar = QTabBar(root)
    standalone_bar.addTab(t("Test et"))
    standalone_bar.setTabToolTip(0, t("Yeniden dene"))

    listing = QListWidget(root)
    item0 = QListWidgetItem(t("Test et"))
    item0.setToolTip(t("Vazgeç"))
    item0.setData(Qt.ItemDataRole.UserRole, "payload-0")
    item1 = QListWidgetItem(t("Test"))
    item1.setToolTip(t("İptal"))
    listing.addItem(item0)
    listing.addItem(item1)

    tree = QTreeWidget(root)
    tree.setColumnCount(2)
    tree.setHeaderLabels([t("Test et"), t("Vazgeç")])
    header = tree.headerItem()
    assert header is not None
    header.setToolTip(0, t("Tekrar dene"))
    top_item = QTreeWidgetItem([t("Test et"), t("İptal")])
    top_item.setToolTip(0, t("Yeniden dene"))
    child_item = QTreeWidgetItem([t("Vazgeç"), t("Test")])
    top_item.addChild(child_item)
    tree.addTopLevelItem(top_item)

    for w in (
        label_test_et,
        label_test,
        label_tpl,
        button,
        checkbox,
        radio,
        line_edit,
        text_edit,
        plain_edit,
        group_box,
        combo,
        tabs,
        standalone_bar,
        listing,
        tree,
    ):
        layout.addWidget(w)

    for _ in range(2):
        set_language("en")
        retranslate_widgets([root, menu], "en")

        assert root.windowTitle() == "Test"
        assert root.toolTip() == "Cancel"
        assert root.accessibleName() == "Retry"
        assert root.accessibleDescription() == "Retry"
        assert root.statusTip() == "Test"
        assert label_test_et.text() == "Test"
        assert label_test.text() == "Test"
        assert label_tpl.text() == "3 notes found."
        assert button.text() == "Test"
        assert checkbox.text() == "Cancel"
        assert radio.text() == "Cancel"
        assert line_edit.placeholderText() == "Test"
        assert text_edit.placeholderText() == "Cancel"
        assert plain_edit.placeholderText() == "Retry"
        assert group_box.title() == "Test"
        assert menu.title() == "Test"
        assert action_implicit.text() == "Test"
        assert action_implicit.toolTip() == "Test"
        assert action_explicit.text() == "Cancel"
        assert action_explicit.toolTip() == "Retry"
        assert action_explicit.statusTip() == "Test"
        assert submenu.title() == "Retry"
        assert sub_action.text() == "Cancel"
        assert detached_action.text() == "Test"
        assert [combo.itemText(i) for i in range(4)] == ["Test", "Test", "Cancel", "Cancel"]
        assert [combo.itemData(i) for i in range(4)] == ["a", "b", "c", "d"]
        assert tabs.tabText(0) == "Test"
        assert tabs.tabToolTip(0) == "Cancel"
        assert tabs.tabText(1) == "Test"
        assert tabs.tabToolTip(1) == "Cancel"
        assert standalone_bar.tabText(0) == "Test"
        assert standalone_bar.tabToolTip(0) == "Retry"
        assert item0.text() == "Test"
        assert item0.toolTip() == "Cancel"
        assert item0.data(Qt.ItemDataRole.UserRole) == "payload-0"
        assert item1.text() == "Test"
        assert item1.toolTip() == "Cancel"
        assert header.text(0) == "Test"
        assert header.text(1) == "Cancel"
        assert header.toolTip(0) == "Retry"
        assert top_item.text(0) == "Test"
        assert top_item.text(1) == "Cancel"
        assert top_item.toolTip(0) == "Retry"
        assert child_item.text(0) == "Cancel"
        assert child_item.text(1) == "Test"

        set_language("tr")
        retranslate_widgets([root, menu], "tr")

        assert root.windowTitle() == "Test et"
        assert root.toolTip() == "Vazgeç"
        assert root.accessibleName() == "Tekrar dene"
        assert root.accessibleDescription() == "Yeniden dene"
        assert root.statusTip() == "Test et"
        assert label_test_et.text() == "Test et"
        assert label_test.text() == "Test"
        assert label_tpl.text() == "3 not var."
        assert button.text() == "Test et"
        assert checkbox.text() == "Vazgeç"
        assert radio.text() == "İptal"
        assert line_edit.placeholderText() == "Test et"
        assert text_edit.placeholderText() == "Vazgeç"
        assert plain_edit.placeholderText() == "Tekrar dene"
        assert group_box.title() == "Test et"
        assert menu.title() == "Test et"
        assert action_implicit.text() == "Test et"
        assert action_implicit.toolTip() == "Test et"
        assert action_explicit.text() == "Vazgeç"
        assert action_explicit.toolTip() == "Yeniden dene"
        assert action_explicit.statusTip() == "Test et"
        assert submenu.title() == "Tekrar dene"
        assert sub_action.text() == "İptal"
        assert detached_action.text() == "Test et"
        assert [combo.itemText(i) for i in range(4)] == ["Test et", "Test", "Vazgeç", "İptal"]
        assert [combo.itemData(i) for i in range(4)] == ["a", "b", "c", "d"]
        assert tabs.tabText(0) == "Test et"
        assert tabs.tabToolTip(0) == "Vazgeç"
        assert tabs.tabText(1) == "Test"
        assert tabs.tabToolTip(1) == "İptal"
        assert standalone_bar.tabText(0) == "Test et"
        assert standalone_bar.tabToolTip(0) == "Yeniden dene"
        assert item0.text() == "Test et"
        assert item0.toolTip() == "Vazgeç"
        assert item0.data(Qt.ItemDataRole.UserRole) == "payload-0"
        assert item1.text() == "Test"
        assert item1.toolTip() == "İptal"
        assert header.text(0) == "Test et"
        assert header.text(1) == "Vazgeç"
        assert header.toolTip(0) == "Tekrar dene"
        assert top_item.text(0) == "Test et"
        assert top_item.text(1) == "İptal"
        assert top_item.toolTip(0) == "Yeniden dene"
        assert child_item.text(0) == "Vazgeç"
        assert child_item.text(1) == "Test"


def test_runtime_mutation_after_switch_is_picked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Son geçişten sonra uygulama kodu metni değiştirirse yeni metin izlenip çevrilmeli."""
    _ensure_app(monkeypatch)
    from PySide6.QtWidgets import (
        QComboBox,
        QLabel,
        QListWidget,
        QPushButton,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    from artemis.channels.desktop.retranslate import retranslate_widgets
    from artemis.i18n import set_language, t

    set_language("tr")
    root = QWidget()
    layout = QVBoxLayout(root)
    button = QPushButton(t("Test et"), root)
    label = QLabel(t("Vazgeç"), root)
    combo = QComboBox(root)
    combo.addItem(t("Test et"), "k")
    listing = QListWidget(root)
    listing.addItem(t("Test et"))
    tree = QTreeWidget(root)
    tree.setColumnCount(1)
    row = QTreeWidgetItem([t("Test et")])
    tree.addTopLevelItem(row)
    for w in (button, label, combo, listing, tree):
        layout.addWidget(w)

    set_language("en")
    retranslate_widgets([root], "en")
    assert button.text() == "Test"
    assert label.text() == "Cancel"
    assert combo.itemText(0) == "Test"

    # İngilizce etkinken uygulama kodu yeni metin yazar.
    button.setText(t("Kaydet"))
    label.setText("Ayşe Yılmaz")
    combo.setItemText(0, t("Kaydet"))
    list_item = listing.item(0)
    assert list_item is not None
    list_item.setText(t("Kaydet"))
    row.setText(0, t("Kaydet"))

    set_language("tr")
    retranslate_widgets([root], "tr")
    assert button.text() == "Kaydet"
    assert label.text() == "Ayşe Yılmaz"
    assert combo.itemText(0) == "Kaydet"
    assert list_item.text() == "Kaydet"
    assert row.text(0) == "Kaydet"

    set_language("en")
    retranslate_widgets([root], "en")
    assert button.text() == "Save"
    assert label.text() == "Ayşe Yılmaz"
    assert combo.itemText(0) == "Save"
    assert list_item.text() == "Save"
    assert row.text(0) == "Save"

    # Metin boşaltılıp sonradan yeniden yazılırsa eski kaynak hortlamamalı.
    button.setText("")
    retranslate_widgets([root], "tr")
    button.setText("Test")
    retranslate_widgets([root], "tr")
    assert button.text() == "Test"


def test_non_catalog_text_stays_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _ensure_app(monkeypatch)
    from PySide6.QtWidgets import (
        QComboBox,
        QLabel,
        QLineEdit,
        QListWidget,
        QPushButton,
        QTabWidget,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    from artemis.channels.desktop.retranslate import retranslate_widgets
    from artemis.i18n import set_language

    set_language("tr")
    root = QWidget()
    root.setWindowTitle("Umut-PC")
    layout = QVBoxLayout(root)
    label = QLabel("Ayşe Yılmaz", root)
    label.setToolTip("C:/Users/Ayse/notes.md")
    button = QPushButton("proje-42", root)
    line = QLineEdit(root)
    line.setPlaceholderText("ornek@alan.com")
    combo = QComboBox(root)
    combo.addItem("English", "en")
    combo.addItem("gpt-5-custom", "custom")
    tabs = QTabWidget(root)
    tabs.addTab(QWidget(), "Workspace-A")
    listing = QListWidget(root)
    listing.addItem("kisisel-not.md")
    tree = QTreeWidget(root)
    tree.setColumnCount(1)
    tree.setHeaderLabels(["Proje-X"])
    tree.addTopLevelItem(QTreeWidgetItem(["dosya-1.txt"]))
    for w in (label, button, line, combo, tabs, listing, tree):
        layout.addWidget(w)

    for lang in ("en", "tr", "en"):
        set_language(lang)
        retranslate_widgets([root], lang)
        assert root.windowTitle() == "Umut-PC"
        assert label.text() == "Ayşe Yılmaz"
        assert label.toolTip() == "C:/Users/Ayse/notes.md"
        assert button.text() == "proje-42"
        assert line.placeholderText() == "ornek@alan.com"
        assert combo.itemText(0) == "English"
        assert combo.itemText(1) == "gpt-5-custom"
        assert tabs.tabText(0) == "Workspace-A"
        first_item = listing.item(0)
        assert first_item is not None and first_item.text() == "kisisel-not.md"
        header = tree.headerItem()
        assert header is not None and header.text(0) == "Proje-X"
        top = tree.topLevelItem(0)
        assert top is not None and top.text(0) == "dosya-1.txt"


def test_context_round_trip_theme_selector_and_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """Tema seçici ("theme") ve araç argümanı ("field") bağlamları canlı geçişte korunmalı."""
    from pathlib import Path

    _ensure_app(monkeypatch)
    from PySide6.QtWidgets import QLabel

    from artemis.channels.desktop.retranslate import retranslate_widgets, set_i18n_context
    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.channels.desktop.tool_titles import arg_label
    from artemis.i18n import set_language

    config_path = Path(str(tmp_path)) / "config.yaml"

    # 1. Türkçede kurulan Ayarlar paneli: tema "Açık" -> "Light" -> "Açık"
    set_language("tr")
    panel = SettingsPanel(config_path, demo=True)
    field_label = QLabel(arg_label("enabled"))
    set_i18n_context(field_label, "field")
    try:
        light_idx = panel.theme_combo.findData("light")
        assert light_idx >= 0
        assert panel.theme_combo.itemText(light_idx) == "Açık"
        assert panel.theme_cards["light"].radio.text() == "Açık"
        assert field_label.text() == "Açık"

        set_language("en")
        retranslate_widgets([panel, field_label], "en")
        assert panel.theme_combo.itemText(light_idx) == "Light"
        assert panel.theme_cards["light"].radio.text() == "Light"
        assert panel.theme_cards["dark"].radio.text() == "Dark"
        assert field_label.text() == "Enabled"
        speech_filter_idx = panel.providers.model_filter.findData("speech")
        assert panel.providers.model_filter.itemText(speech_filter_idx) == "Speech"
        speech_kind_idx = panel.providers.kind.findData("speech")
        assert panel.providers.kind.itemText(speech_kind_idx) == "Speech"

        set_language("tr")
        retranslate_widgets([panel, field_label], "tr")
        assert panel.theme_combo.itemText(light_idx) == "Açık"
        assert panel.theme_cards["light"].radio.text() == "Açık"
        assert panel.theme_cards["dark"].radio.text() == "Koyu"
        assert field_label.text() == "Açık"
        assert panel.providers.model_filter.itemText(speech_filter_idx) == "Ses"
        assert panel.providers.kind.itemText(speech_kind_idx) == "Ses"

        set_language("en")
        retranslate_widgets([panel, field_label], "en")
        assert panel.theme_combo.itemText(light_idx) == "Light"
        assert panel.theme_cards["light"].radio.text() == "Light"
        assert field_label.text() == "Enabled"
    finally:
        panel.close()

    # 2. İngilizcede kurulan Ayarlar paneli: "Light" -> "Açık" -> "Light"
    set_language("en")
    en_panel = SettingsPanel(config_path, demo=True)
    try:
        light_idx = en_panel.theme_combo.findData("light")
        assert en_panel.theme_combo.itemText(light_idx) == "Light"
        assert en_panel.theme_cards["light"].radio.text() == "Light"

        set_language("tr")
        retranslate_widgets([en_panel], "tr")
        assert en_panel.theme_combo.itemText(light_idx) == "Açık"
        assert en_panel.theme_cards["light"].radio.text() == "Açık"

        set_language("en")
        retranslate_widgets([en_panel], "en")
        assert en_panel.theme_combo.itemText(light_idx) == "Light"
        assert en_panel.theme_cards["light"].radio.text() == "Light"
    finally:
        en_panel.close()
