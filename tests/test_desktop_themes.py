"""Four native palettes must retain readable text, controls and mixed navigation."""
from artemis.channels.desktop.themes import DEFAULT_THEME, THEMES, stylesheet


def contrast(first: str, second: str) -> float:
    def luminance(value: str) -> float:
        rgb = [int(value[i:i+2], 16) / 255 for i in (1, 3, 5)]
        linear = [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4 for x in rgb]
        return sum(x * weight for x, weight in zip(linear, (.2126, .7152, .0722), strict=True))
    a, b = sorted((luminance(first), luminance(second)))
    return (b + .05) / (a + .05)


def test_palettes_are_readable_and_mixed_navigation_is_distinct() -> None:
    for palette in THEMES.values():
        for background in ("bg", "surface", "soft"):
            for foreground in ("text", "muted", "warning", "error", "success"):
                assert contrast(palette[foreground], palette[background]) >= 4.5
        assert contrast(palette["accent"], palette["accent_text"]) >= 4.5
        for background in ("nav", "nav_selected"):
            assert contrast(palette["nav_text"], palette[background]) >= 4.5
    assert THEMES["light-mixed"]["nav"] != THEMES["light"]["nav"]
    assert THEMES["dark-mixed"]["nav"] != THEMES["dark"]["nav"]
    assert stylesheet("invalid") == stylesheet(DEFAULT_THEME)


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


def test_arrow_images_exist_for_every_theme() -> None:
    """Qt silently drops a missing url() image; a renamed asset would bring back the bar."""
    import re
    from pathlib import Path

    for name in THEMES:
        for url in re.findall(r"url\(([^)]+)\)", stylesheet(name)):
            assert Path(url).is_file(), url


def test_containers_inside_surfaces_are_transparent_but_inputs_are_not() -> None:
    sheet = stylesheet(DEFAULT_THEME)
    assert "QFrame#card .QWidget" in sheet
    assert "QFrame#card QLineEdit" not in sheet
    assert "QRadioButton::indicator" in sheet
    assert "QFrame#previewWell" in sheet
