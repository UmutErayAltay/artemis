# This stylesheet styles the desktop UI only. It does not change voice or safety logic.
from pathlib import Path

# Qt draws CSS border triangles as flat bars, so arrows are tiny SVGs in the muted tone.
# Mixed themes share their workspace colors with light/dark, so two tones suffice.
_ASSETS = Path(__file__).resolve().parent

DEFAULT_THEME = "light-mixed"

THEMES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#edf0f4",
        "surface": "#ffffff",
        "soft": "#f4f7fb",
        "text": "#0b1f3f",
        "muted": "#51637c",
        "border": "#d5e0ec",
        "accent": "#365c9e",
        "accent_text": "#ffffff",
        "nav": "#eef5ff",
        "nav_text": "#0b1f3f",
        "nav_muted": "#51637c",
        "nav_selected": "#dbeafe",
        "warning": "#87590d",
        "error": "#b12f36",
        "success": "#1f6d4b",
    },
    "dark": {
        "bg": "#0b0f14",
        "surface": "#171d26",
        "soft": "#1f2936",
        "text": "#e5e8ee",
        "muted": "#9aa4b2",
        "border": "#364159",
        "accent": "#b2caff",
        "accent_text": "#0b1f3f",
        "nav": "#0f1621",
        "nav_text": "#e5e8ee",
        "nav_muted": "#9aa4b2",
        "nav_selected": "#243755",
        "warning": "#ffd285",
        "error": "#ffb0ad",
        "success": "#8fd7ae",
    },
    "light-mixed": {
        "bg": "#edf0f4",
        "surface": "#ffffff",
        "soft": "#f4f7fb",
        "text": "#0b1f3f",
        "muted": "#51637c",
        "border": "#d5e0ec",
        "accent": "#365c9e",
        "accent_text": "#ffffff",
        "nav": "#25344d",
        "nav_text": "#f2f5fc",
        "nav_muted": "#bbc9df",
        "nav_selected": "#405777",
        "warning": "#87590d",
        "error": "#b12f36",
        "success": "#1f6d4b",
    },
    "dark-mixed": {
        "bg": "#0b0f14",
        "surface": "#171d26",
        "soft": "#1f2936",
        "text": "#e5e8ee",
        "muted": "#9aa4b2",
        "border": "#364159",
        "accent": "#b2caff",
        "accent_text": "#0b1f3f",
        "nav": "#e8edf5",
        "nav_text": "#26364d",
        "nav_muted": "#485a76",
        "nav_selected": "#cbd9ee",
        "warning": "#ffd285",
        "error": "#ffb0ad",
        "success": "#8fd7ae",
    },
}

THEME_LABELS: dict[str, str] = {
    "light": "Açık",
    "dark": "Koyu",
    "light-mixed": "Açık ağırlıklı",
    "dark-mixed": "Koyu ağırlıklı",
}


def _tokens(name: str) -> dict[str, str]:
    return THEMES.get(name, THEMES[DEFAULT_THEME])


def stylesheet(name: str) -> str:
    """Semantic Qt style shared by the window and its dialogs.

    Dynamic-property selectors ([selected="true"], [state="current"]) only update
    after the caller runs widget.style().unpolish(widget) and polish(widget).
    Controls: 1px border + 4px 8px padding; focus swaps to 2px border + 3px 7px
    (the only off-scale padding) so the box never shifts.
    """
    t = _tokens(name)
    tone = "dark" if t["bg"] == THEMES["dark"]["bg"] else "light"
    down = (_ASSETS / f"arrow-down-{tone}.svg").as_posix()
    up = (_ASSETS / f"arrow-up-{tone}.svg").as_posix()
    radio = (_ASSETS / f"radio-on-{tone}.svg").as_posix()
    # Plain containers inside a surface would otherwise paint the page bg as a stain.
    # Exact-class selectors (.QWidget) leave inputs, which subclass QWidget, untouched.
    surfaces = ", ".join(
        f"QFrame#{owner} {child}"
        for owner in ("card", "raised", "topbar", "galleryCard", "stepPanel", "sidebar")
        for child in (".QWidget", ".QFrame", ".QStackedWidget", "QCheckBox", "QRadioButton",
                      "QScrollArea > .QWidget > .QWidget")
    )
    ctl = "QPushButton, QLineEdit, QComboBox, QSpinBox"
    foc = ("QPushButton:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus, "
           "QPlainTextEdit:focus, QTextEdit:focus")
    return f"""
QWidget {{ background: {t['bg']}; color: {t['text']};
 font-family: 'Segoe UI'; font-size: 14px; }}
QDialog {{ background: {t['surface']}; }}
QLabel {{ background: transparent; }}
QLabel#status {{ border: none; background: transparent; font-weight: 600; }}
QLabel#title, QLabel#pageTitle {{ font-size: 22px; font-weight: 650; }}
QLabel#muted, QLabel#hint, QLabel#rawToolName, QLabel#eyebrow {{ color: {t['muted']}; }}
QLabel#fieldLabel, QLabel#emptyBody {{ color: {t['muted']}; font-size: 13px; }}
QLabel#meta {{ color: {t['muted']}; font-size: 12px; }}
QLabel#sectionTitle, QLabel#emptyTitle {{ font-size: 16px; font-weight: 600; }}
QLabel#chip {{ background: {t['soft']}; border-radius: 7px; font-size: 12px;
 padding: 2px 8px; }}
QLabel#chipWarning {{ background: transparent; color: {t['warning']};
 border: 1px solid {t['warning']}; border-radius: 7px; font-size: 12px; padding: 2px 8px; }}
QFrame#card, QFrame#raised, QFrame#block, QFrame#topbar {{ background: {t['surface']};
 border: 1px solid {t['border']}; border-radius: 10px; }}
QFrame#galleryCard {{ background: {t['surface']}; border: 1px solid {t['border']};
 border-radius: 12px; }}
QFrame#galleryCard[selected="true"] {{ border: 2px solid {t['accent']}; }}
QFrame#stepPanel {{ background: {t['surface']}; border: 1px solid {t['border']};
 border-radius: 10px; }}
QFrame#emptyState {{ background: transparent; border: none; }}
QFrame#stageBar {{ background: transparent; border: none; }}
QLabel#stage {{ color: {t['muted']}; padding: 4px 8px; border-bottom: 2px solid transparent; }}
QLabel#stage[state="current"] {{ color: {t['accent']}; font-weight: 600;
 border-bottom: 2px solid {t['accent']}; }}
QLabel#stage[state="done"] {{ color: {t['success']}; }}
QLabel#stage[state="todo"] {{ color: {t['muted']}; }}
QLabel#statusPill, QFrame#statusPill, QFrame#topbar QFrame#statusPill {{
 background: {t['soft']}; border: none;
 border-radius: 7px; padding: 4px 12px; font-size: 13px; font-weight: 600; }}
QFrame#messageUser, QFrame#card QFrame#messageUser {{ background: {t['soft']};
 border-radius: 10px; }}
QFrame#messageAssistant, QFrame#stepBlock {{ background: {t['surface']}; }}
QFrame#sidebar {{ background: {t['nav']}; border-radius: 10px; }}
{surfaces} {{ background: transparent; }}
QWidget#flat, QWidget#cardRow {{ background: transparent; }}
QFrame#previewWell {{ background: {t['soft']}; border: none; border-radius: 10px; }}
QPlainTextEdit[readOnly="true"], QTextEdit[readOnly="true"] {{ background: {t['soft']}; }}
QFrame#sidebar QLabel {{ color: {t['nav_text']}; }}
{ctl} {{ background: {t['soft']}; border: 1px solid {t['border']};
 border-radius: 7px; padding: 4px 8px; min-height: 22px; }}
QLineEdit, QComboBox, QSpinBox {{ background: {t['surface']}; color: {t['text']};
 selection-background-color: {t['accent']}; selection-color: {t['accent_text']}; }}
QPlainTextEdit, QTextEdit, QListWidget {{ background: {t['surface']}; color: {t['text']};
 border: 1px solid {t['border']}; border-radius: 7px; padding: 4px 8px;
 selection-background-color: {t['accent']}; selection-color: {t['accent_text']}; }}
QPushButton:hover, QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{
 border-color: {t['accent']}; }}
{foc} {{ border: 2px solid {t['accent']}; padding: 3px 7px; }}
QPushButton:disabled, QComboBox:disabled, QCheckBox:disabled, QLineEdit:disabled {{
 color: {t['muted']}; }}
QPushButton:disabled:hover, QComboBox:disabled:hover {{ border-color: {t['border']}; }}
QPushButton#primary {{ background: {t['accent']}; color: {t['accent_text']};
 border-color: {t['accent']}; font-weight: 650; min-height: 26px; }}
QPushButton#voiceToggle:checked {{ border: 2px solid {t['accent']};
 color: {t['accent']}; padding: 3px 7px; }}
QPushButton#danger {{ color: {t['error']}; border-color: {t['error']}; }}
QPushButton#amber, QLabel#amber {{ color: {t['warning']}; }}
QPushButton#ghost {{ background: transparent; border: none; color: {t['accent']}; }}
QPushButton#ghost:hover {{ background: {t['soft']}; }}
QPushButton#navItem {{ background: {t['nav']}; color: {t['nav_text']}; border: none;
 padding: 0 12px; min-height: 36px; text-align: left; }}
QPushButton#navItem:hover {{ background: {t['nav_selected']}; }}
QPushButton#navItem:checked {{ background: {t['nav_selected']}; font-weight: 600; }}
QFrame#sidebar QPushButton#themeToggle {{ background: {t['nav']}; color: {t['nav_text']};
 border: 1px solid {t['nav_muted']}; }}
QFrame#sidebar QPushButton#themeToggle:hover {{ background: {t['nav_selected']}; }}
QComboBox {{ padding-right: 24px; }}
QComboBox::drop-down {{ border: none; background: transparent; width: 24px;
 subcontrol-origin: padding; subcontrol-position: right center; }}
QComboBox::down-arrow {{ image: url({down}); width: 10px; height: 6px; }}
QComboBox QAbstractItemView {{ background: {t['surface']}; color: {t['text']};
 border: 1px solid {t['border']}; outline: 0;
 selection-background-color: {t['soft']}; selection-color: {t['text']}; }}
QSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button, QSpinBox::down-button {{ background: transparent; border: none;
 width: 24px; subcontrol-origin: border; }}
QSpinBox::up-button {{ subcontrol-position: top right; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; }}
QSpinBox::up-arrow {{ image: url({up}); width: 10px; height: 6px; }}
QSpinBox::down-arrow {{ image: url({down}); width: 10px; height: 6px; }}
QListWidget {{ outline: 0; }}
QListWidget::item {{ padding: 8px 12px; border-left: 2px solid transparent; }}
QListWidget::item:hover {{ background: {t['soft']}; }}
QListWidget::item:selected {{ background: {t['soft']}; color: {t['text']};
 border-left: 2px solid {t['accent']}; }}
QListWidget#sectionList {{ background: transparent; border: none; padding: 0; }}
QListWidget#sectionList::item {{ min-height: 20px; padding: 8px 12px; }}
QCheckBox {{ color: {t['text']}; spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px;
 border: 1px solid {t['border']}; background: {t['surface']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}
QRadioButton {{ color: {t['text']}; spacing: 8px; }}
QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 8px;
 border: 1px solid {t['muted']}; background: {t['surface']}; }}
QRadioButton::indicator:checked {{ image: url({radio}); border: none; }}
QCheckBox:focus, QRadioButton:focus {{ color: {t['accent']}; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: {t['soft']}; padding: 8px 12px; margin-right: 4px;
 border-radius: 7px; }}
QTabBar::tab:selected {{ background: {t['accent']}; color: {t['accent_text']}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['border']}; min-height: 24px; border-radius: 5px; }}
QScrollBar::handle:horizontal {{ background: {t['border']}; min-width: 24px; border-radius: 5px; }}
QScrollBar::handle:hover {{ background: {t['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QProgressBar {{ border: 1px solid {t['border']}; background: {t['soft']};
 border-radius: 5px; text-align: center; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 4px; }}
QToolButton {{ border: 1px solid {t['border']}; padding: 4px 8px; border-radius: 7px; }}
QMenu, QToolTip {{ background: {t['surface']}; color: {t['text']};
 border: 1px solid {t['border']}; padding: 8px; }}
QMenu::item:selected {{ background: {t['soft']}; }}
QSplitter::handle {{ background: {t['bg']}; width: 8px; }}
"""
