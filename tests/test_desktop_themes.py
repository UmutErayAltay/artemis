"""Four native palettes must retain readable text, controls and mixed navigation."""
import re

import pytest

from artemis.channels.desktop.themes import DEFAULT_THEME, THEMES, stylesheet


def contrast(first: str, second: str) -> float:
    def luminance(value: str) -> float:
        rgb = [int(value[i:i+2], 16) / 255 for i in (1, 3, 5)]
        linear = [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4 for x in rgb]
        return sum(x * weight for x, weight in zip(linear, (.2126, .7152, .0722), strict=True))
    a, b = sorted((luminance(first), luminance(second)))
    return (b + .05) / (a + .05)


def test_palettes_are_readable_and_mixed_navigation_is_distinct() -> None:
    for name, palette in THEMES.items():
        for background in ("bg", "surface", "soft"):
            for foreground in ("text", "muted", "warning", "error", "success"):
                assert contrast(palette[foreground], palette[background]) >= 4.5, (
                    name, foreground, background)
            # Metin olmayan öğeler (düğme, odak halkası, seçim çizgisi) için 3:1 yeter.
            assert contrast(palette["accent"], palette[background]) >= 3, (name, background)
        assert contrast(palette["accent_text"], palette["accent"]) >= 4.5, name
        assert contrast(palette["text"], palette["accent_soft"]) >= 4.5, name
        for background in ("nav", "nav_selected"):
            for foreground in ("nav_text", "nav_muted"):
                assert contrast(palette[foreground], palette[background]) >= 4.5, (
                    name, foreground, background)
        # Menü çalışma alanından ayırt edilebilmeli (koyu temada eskiden 1.06:1 idi).
        assert contrast(palette["nav"], palette["bg"]) >= 1.15, name
        # Kart kenarı yüzeyden ayırt edilebilir kalmalı.
        assert contrast(palette["border"], palette["surface"]) >= 1.3, name
    assert THEMES["light-mixed"]["nav"] != THEMES["light"]["nav"]
    assert THEMES["dark-mixed"]["nav"] != THEMES["dark"]["nav"]
    assert stylesheet("invalid") == stylesheet(DEFAULT_THEME)


def test_accent_text_and_control_borders_meet_wcag() -> None:
    """UX-04: accent küçük metin olarak AA (4.5), giriş denetimi kenarı 3:1 geçmeli."""
    for name, palette in THEMES.items():
        for background in ("bg", "surface", "soft", "accent_soft"):
            assert contrast(palette["accent"], palette[background]) >= 4.5, (name, background)
        for background in ("bg", "surface", "soft"):
            assert contrast(palette["control_border"], palette[background]) >= 3, (
                name, background)


def test_themes_share_token_keys_and_warm_type_rules() -> None:
    keys = set(THEMES["light"])
    assert "accent_soft" in keys
    for name, palette in THEMES.items():
        assert set(palette) == keys, name
        css = stylesheet(name)
        assert "Segoe UI Variable Text" in css
        assert "11px" not in css
        for selector in ("QLabel#greeting", "QFrame#userBubble", "QFrame#assistantBubble",
                         "QLabel#hint"):
            assert selector in css, (name, selector)


def test_stylesheet_builds_without_qt_warnings_for_all_themes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qt ayrıştırma hatalarını (Could not parse stylesheet) qWarning olarak yazar."""
    pytest.importorskip("PySide6")  # CI'nin `checks` işi masaüstü ekini kurmaz
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton

    app = QApplication.instance() or QApplication([])
    messages: list[str] = []
    previous = qInstallMessageHandler(lambda kind, ctx, msg: messages.append(msg))
    try:
        for name in THEMES:
            host = QFrame()
            host.setStyleSheet(stylesheet(name))
            for widget in (QLabel("x"), QPushButton("x"), QFrame()):
                widget.setParent(host)
            host.ensurePolished()
            host.show()
            app.processEvents()
            host.close()
    finally:
        qInstallMessageHandler(previous)
    assert not [m for m in messages if "stylesheet" in m.lower()], messages


NEW_NAMES = (
    "QFrame#topbar", "statusPill", "QFrame#stepPanel", "QListWidget#sectionList",
    "QFrame#galleryCard", '#galleryCard[selected="true"]', "QFrame#stageBar",
    "QLabel#stage", '[state="current"]', '[state="done"]', '[state="todo"]',
    "QFrame#emptyState", "QLabel#emptyTitle", "QLabel#emptyBody", "QLabel#fieldLabel",
    "QLabel#sectionTitle", "QLabel#pageTitle", "QLabel#meta", "QLabel#chip",
    "QLabel#chipWarning", "QPushButton#ghost", "QPushButton#themeToggle",
    "QPushButton#navItem", "QToolTip", "QMenu", "QComboBox::drop-down",
    "QComboBox::down-arrow",
)


def test_redesign_selectors_exist_without_gradients_or_solid_list_selection() -> None:
    for name in THEMES:
        css = stylesheet(name)
        assert "gradient" not in css
        for selector in NEW_NAMES:
            assert selector in css, (name, selector)
        rule = css.split("QListWidget::item:selected {")[1].split("}")[0]
        assert "selection-background-color" not in rule
        assert THEMES[name]["accent"] not in rule.split("border-left")[0]


def test_settings_section_list_uses_filled_pill_like_main_nav() -> None:
    for name in THEMES:
        css = stylesheet(name)
        rule = css.split("QListWidget#sectionList::item:selected {")[1].split("}")[0]
        assert THEMES[name]["soft"] in rule and "border-left" not in rule
        base = css.split("QListWidget#sectionList::item {")[-1].split("}")[0]
        assert "border-radius: 18px" in base  # navItem ile aynı hap yarıçapı


def test_arrow_images_exist_for_every_theme() -> None:
    """Qt silently drops a missing url() image; a renamed asset would bring back the bar."""
    import re
    from pathlib import Path

    for name in THEMES:
        for url in re.findall(r'url\("?([^)"]+)"?\)', stylesheet(name)):
            assert Path(url).is_file(), url


def test_containers_inside_surfaces_are_transparent_but_inputs_are_not() -> None:
    sheet = stylesheet(DEFAULT_THEME)
    assert "QFrame#card .QWidget" in sheet
    assert "QFrame#card QLineEdit" not in sheet
    assert "QRadioButton::indicator" in sheet
    assert "QFrame#previewWell" in sheet


def test_buttons_and_fields_share_one_control_height() -> None:
    """Geçmiş düğmeleri ve Ayarlar düğmeleri aynı yükseklik/dolgu ölçüsünü kullanır."""
    for name in THEMES:
        css = stylesheet(name)
        assert "QPushButton {" in css and "min-height: 26px" in css, name
        assert "QPushButton {" in css and "padding: 4px 12px" in css, name
        assert "QSpinBox {" in css and "min-height: 26px" in css, name
        primary = css.split("QPushButton#primary {", 1)[1].split("}", 1)[0]
        assert "min-height" not in primary, name


def test_stylesheet_urls_are_quoted_for_paths_with_spaces() -> None:
    # Boşluklu kurulum yolunda tırnaksız url(...) QSS'i bozar (ok/radyo ikonları gider).
    for name in THEMES:
        urls = re.findall(r"url\(([^)]*)\)", stylesheet(name))
        assert urls and all(u.startswith('"') and u.endswith('"') for u in urls), name
