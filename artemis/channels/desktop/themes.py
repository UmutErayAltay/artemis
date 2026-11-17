# This stylesheet styles the desktop UI only. It does not change voice or safety logic.
from pathlib import Path

# Qt draws CSS border triangles as flat bars, so arrows are tiny SVGs in the muted tone.
# Mixed themes share their workspace colors with light/dark, so two tones suffice.
_ASSETS = Path(__file__).resolve().parent

DEFAULT_THEME = "dark"

# Sıcak yoldaş paleti (DESIGN.md "Warm companion direction"): krem/kemik ve sıcak mürekkep;
# kurumsal slate-mavi bırakıldı. Düz tonlar, gradyan/parıltı yok.
# accent #0f7f7a -> #0b7570: bağlantı/ghost düğme metni küçük yazıda accent_soft üstünde 4.04:1 idi
# (AA için 4.5); artık bg, surface, soft ve accent_soft üstünde >= 4.5:1 (testte kilitli).
# nav_muted #6b5f55 -> #5f544a: krem menüde nav_selected üstünde 4.23:1 idi (AA için 4.5 gerekir).
# Menü/çalışma alanı ayrımı: nav önce bg ile ~1.07:1 (açık), ~1.06:1 (koyu) idi, görünmüyordu;
# artık her temada >= 1.15:1 (testte kilitli). Koyu menü artık bg'den biraz AÇIK sıcak ton.
_LIGHT = {
    "bg": "#f6f1e9",
    "surface": "#fffdf9",
    "soft": "#efe8dc",
    "text": "#2a2420",
    "muted": "#6b5f55",
    "border": "#e2d8c8",
    # Giriş denetimi kenarı: kart/ayraç kenarı sade kalır, alan sınırı >= 3:1 olmalı (WCAG 1.4.11).
    "control_border": "#8a7b69",
    "accent": "#0b7570",
    "accent_text": "#ffffff",
    "accent_soft": "#d9efec",
    "nav": "#e8dfcf",
    "nav_text": "#2a2420",
    "nav_muted": "#5f544a",
    "nav_selected": "#dacdb7",
    "warning": "#8a5a0b",
    "error": "#b3392f",
    "success": "#1f6d4b",
}
_DARK = {
    "bg": "#16120f",
    "surface": "#201b17",
    "soft": "#2a2420",
    "text": "#f0e9df",
    "muted": "#b3a698",
    "border": "#3b332c",
    "control_border": "#7d7064",
    "accent": "#5fd0c5",
    "accent_text": "#0d2926",
    "accent_soft": "#1f3a37",
    "nav": "#292119",
    "nav_text": "#f0e9df",
    "nav_muted": "#b3a698",
    "nav_selected": "#3d332a",
    "warning": "#f2c46d",
    "error": "#ff9d8f",
    "success": "#8fd7ae",
}
_WARM_NAV = {"nav": "#2b231d", "nav_text": "#f4ece1", "nav_muted": "#cdbfae",
             "nav_selected": "#43382f"}
_CREAM_NAV = {k: _LIGHT[k] for k in ("nav", "nav_text", "nav_muted", "nav_selected")}

THEMES: dict[str, dict[str, str]] = {
    "light": _LIGHT,
    "dark": _DARK,
    "light-mixed": {**_LIGHT, **_WARM_NAV},
    "dark-mixed": {**_DARK, **_CREAM_NAV},
}

THEME_LABELS: dict[str, str] = {
    "light": "Açık",
    "dark": "Koyu",
    "light-mixed": "Açık ağırlıklı",
    "dark-mixed": "Koyu ağırlıklı",
}


def _tokens(name: str) -> dict[str, str]:
    return THEMES.get(name, THEMES[DEFAULT_THEME])


def _qss_controls(t: dict[str, str], up: str, down: str) -> str:
    # Plain containers inside a surface would otherwise paint the page bg as a stain.
    # Exact-class selectors (.QWidget) leave inputs, which subclass QWidget, untouched.
    surfaces = ", ".join(
        f"QFrame#{owner} {child}"
        for owner in ("card", "raised", "topbar", "galleryCard", "stepPanel", "sidebar",
                      "userBubble", "assistantBubble")
        for child in (".QWidget", ".QFrame", ".QStackedWidget", "QCheckBox", "QRadioButton",
                      "QScrollArea > .QWidget > .QWidget")
    )
    ctl = "QPushButton, QLineEdit, QComboBox, QSpinBox"
    foc = ("QPushButton:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus, "
           "QPlainTextEdit:focus, QTextEdit:focus")
    return f"""
QWidget {{ background: {t['bg']}; color: {t['text']};
 font-family: 'Segoe UI Variable Text', 'Segoe UI'; font-size: 14px; }}
QDialog {{ background: {t['surface']}; }}
QDialog QRadioButton, QDialog QCheckBox {{ background: transparent; }}
QLabel {{ background: transparent; }}
QLabel#status {{ border: none; background: transparent; font-weight: 600; }}
QLabel#greeting {{ font-size: 28px; font-weight: 650; }}
QLabel#title, QLabel#pageTitle {{ font-size: 22px; font-weight: 650; }}
QLabel#muted, QLabel#rawToolName, QLabel#eyebrow {{ color: {t['muted']}; }}
QLabel#hint {{ color: {t['muted']}; font-size: 12px; }}
QLabel#fieldLabel, QLabel#emptyBody {{ color: {t['muted']}; font-size: 13px; }}
QLabel#meta {{ color: {t['muted']}; font-size: 12px; }}
QLabel#sectionTitle, QLabel#emptyTitle {{ font-size: 16px; font-weight: 600; }}
QLabel#chip {{ background: {t['soft']}; border-radius: 8px; font-size: 13px;
 padding: 2px 8px; }}
QLabel#chipWarning {{ background: {t['soft']}; color: {t['warning']}; border-radius: 8px;
 font-size: 13px; font-weight: 600; padding: 2px 8px; }}
QFrame#card, QFrame#raised, QFrame#block, QFrame#topbar {{ background: {t['surface']};
 border: 1px solid {t['border']}; border-radius: 12px; }}
QFrame#galleryCard {{ background: {t['surface']}; border: 1px solid {t['border']};
 border-radius: 14px; }}
QFrame#galleryCard[selected="true"] {{ border: 2px solid {t['accent']}; }}
QFrame#stepPanel {{ background: {t['surface']}; border: 1px solid {t['border']};
 border-radius: 12px; }}
QFrame#opCard {{ border: 1px solid {t['border']}; border-radius: 12px; }}
QFrame#emptyState {{ background: transparent; border: none; }}
QFrame#stageBar {{ background: transparent; border: none; }}
QLabel#stage {{ color: {t['muted']}; padding: 4px 8px; border-bottom: 2px solid transparent; }}
QLabel#stage[state="current"] {{ color: {t['accent']}; font-weight: 600;
 border-bottom: 2px solid {t['accent']}; }}
QLabel#stage[state="done"] {{ color: {t['success']}; }}
QLabel#stage[state="todo"] {{ color: {t['muted']}; }}
QLabel#statusPill, QFrame#statusPill, QFrame#topbar QFrame#statusPill {{
 background: {t['soft']}; border: none;
 border-radius: 8px; padding: 4px 12px; font-size: 13px; font-weight: 600; }}
QFrame#messageUser, QFrame#card QFrame#messageUser {{ background: {t['soft']};
 border-radius: 12px; }}
QFrame#userBubble, QFrame#card QFrame#userBubble {{ background: {t['accent_soft']};
 color: {t['text']}; border: none; border-radius: 14px; }}
QFrame#userBubble QLabel {{ color: {t['text']}; font-size: 14px; }}
QFrame#messageAssistant, QFrame#stepBlock {{ background: {t['surface']}; }}
QFrame#assistantBubble {{ background: transparent; border: none; }}
QFrame#sidebar {{ background: {t['nav']}; border-radius: 12px; }}
{surfaces} {{ background: transparent; }}
QWidget#flat, QWidget#cardRow {{ background: transparent; }}
QFrame#previewWell {{ background: {t['soft']}; border: none; border-radius: 12px; }}
QPlainTextEdit[readOnly="true"], QTextEdit[readOnly="true"] {{ background: {t['soft']}; }}
QFrame#sidebar QLabel {{ color: {t['nav_text']}; }}
{ctl} {{ background: {t['soft']}; border: 1px solid {t['border']};
 border-radius: 8px; padding: 4px 8px; min-height: 22px; }}
/* Tek kontrol yüksekliği: min-height içerik kutusudur, Qt üstüne 4px dolgu ve 1px çerçeve ekler.
   26px + 2*4 + 2*1 = 36px toplam; düğme, metin alanı, kutu ve sayaç aynı hizada kalır. */
QPushButton {{ padding: 4px 12px; min-height: 26px; }}
QLineEdit, QComboBox, QSpinBox {{ min-height: 26px; background: {t['surface']};
 color: {t['text']}; selection-background-color: {t['accent']};
 selection-color: {t['accent_text']}; }}
QPlainTextEdit, QTextEdit, QListWidget {{ background: {t['surface']}; color: {t['text']};
 border: 1px solid {t['border']}; border-radius: 8px; padding: 4px 8px;
 selection-background-color: {t['accent']}; selection-color: {t['accent_text']}; }}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
 border: 1px solid {t['control_border']}; }}
QPushButton:hover, QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{
 border-color: {t['accent']}; }}
{foc} {{ border: 2px solid {t['accent']}; padding: 3px 7px; }}
QPushButton:disabled, QComboBox:disabled, QCheckBox:disabled, QLineEdit:disabled {{
 color: {t['muted']}; }}
QPushButton:disabled:hover, QComboBox:disabled:hover {{ border-color: {t['border']}; }}
QPushButton#primary {{ background: {t['accent']}; color: {t['accent_text']};
 border-color: {t['accent']}; font-weight: 650; }}
/* Metin bağlantısı, etiket ve kenar çubuğu düğmeleri kendi küçük ölçüsünü korur (22px içerik). */
QPushButton#ghost, QPushButton#disclosure, QPushButton#chip, QPushButton#themeToggle {{
 min-height: 22px; min-width: 0; padding: 4px 8px; }}
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
"""


def _qss_components(t: dict[str, str], down: str, radio: str) -> str:
    return f"""QListWidget {{ outline: 0; }}
QListWidget::item {{ padding: 8px 12px; border-left: 2px solid transparent; }}
QListWidget::item:hover {{ background: {t['soft']}; }}
QListWidget::item:selected {{ background: {t['soft']}; color: {t['text']};
 border-left: 2px solid {t['accent']}; }}
QListWidget#sectionList {{ background: transparent; border: none; padding: 0; }}
QListWidget#sectionList::item {{ min-height: 20px; padding: 8px 12px; }}
QCheckBox {{ color: {t['text']}; spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px;
 border: 1px solid {t['control_border']}; background: {t['surface']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}
QRadioButton {{ color: {t['text']}; spacing: 8px; }}
QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 8px;
 border: 1px solid {t['control_border']}; background: {t['surface']}; }}
QRadioButton::indicator:checked {{ image: url({radio}); border: none; }}
QCheckBox:focus, QRadioButton:focus {{ color: {t['accent']}; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: {t['soft']}; padding: 8px 12px; margin-right: 4px;
 border-radius: 8px; }}
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
QToolButton {{ border: 1px solid {t['border']}; padding: 4px 8px; border-radius: 8px; }}
QMenu, QToolTip {{ background: {t['surface']}; color: {t['text']};
 border: 1px solid {t['border']}; padding: 8px; }}
QToolButton#helpTip {{ border: 1px solid {t['border']}; border-radius: 9px; padding: 0;
 background: {t['soft']}; color: {t['muted']}; font-size: 12px; font-weight: 600; }}
QFrame#helpPopup {{ background: {t['surface']}; border: 1px solid {t['control_border']};
 border-radius: 8px; }}
QFrame#helpPopup QLabel {{ color: {t['text']}; background: transparent; }}
QToolButton#helpTip:hover, QToolButton#helpTip:focus, QToolButton#helpTip:checked {{
 color: {t['accent']};
 border-color: {t['accent']}; }}
QMenu::item:selected {{ background: {t['soft']}; }}
QSplitter::handle {{ background: {t['bg']}; width: 8px; }}
/* wave2-shell */
QPushButton#iconToggle {{ background: {t['soft']}; border: 1px solid {t['border']};
 border-radius: 8px; padding: 0; min-width: 30px; max-width: 30px; min-height: 30px;
 max-height: 30px; }}
QPushButton#iconToggle:checked {{ background: {t['accent_soft']};
 border: 1px solid {t['accent']}; }}
QPushButton#iconToggle:disabled {{ background: transparent; }}
QPushButton#chip {{ background: {t['soft']}; border: 1px solid {t['border']}; border-radius: 14px;
 font-size: 13px; padding: 4px 12px; }}
QPushButton#chip:hover {{ border-color: {t['accent']}; background: {t['accent_soft']}; }}
QPushButton#navItem {{ border-radius: 18px; }}
QFrame#opCard, QFrame#stepBlock {{ background: transparent; }}
QFrame#composer {{ background: {t['surface']}; border: 1px solid {t['border']};
 border-radius: 14px; }}
QFrame#composer QPlainTextEdit {{ background: transparent; border: none; padding: 4px 8px; }}
QLabel#approvalNotice {{ background: transparent; color: {t['warning']};
 border: 1px solid {t['warning']}; border-radius: 8px; font-size: 13px; padding: 8px 12px; }}
/* wave2-settings */
QFrame#divider {{ background: {t['border']}; border: none; }}
QWidget#group {{ background: transparent; }}
QProgressBar#levelMeter {{ background: {t['soft']}; border: none; border-radius: 3px;
 min-height: 6px; max-height: 6px; }}
QProgressBar#levelMeter::chunk {{ background: {t['accent']}; border-radius: 3px; }}
QComboBox:disabled, QLineEdit:disabled {{ background: {t['soft']}; color: {t['muted']}; }}
/* Ayarlar iç listesi ana gezinmeyle (navItem) aynı dili konuşur: dolu hap, sol çizgi yok. */
QListWidget#sectionList::item {{ min-height: 20px; padding: 8px 12px; border: none;
 border-radius: 18px; }}
QListWidget#sectionList::item:selected {{ background: {t['soft']}; color: {t['text']};
 border: none; font-weight: 600; }}
QFrame#themeCard {{ background: {t['surface']}; border: 1px solid {t['border']};
 border-radius: 12px; padding: 1px; }}
QFrame#themeCard[selected="true"] {{ border: 2px solid {t['accent']}; padding: 0; }}
QFrame#themeCard QLabel, QFrame#themeCard QRadioButton {{ background: transparent; }}
QFrame#themeCard QRadioButton {{ spacing: 0; font-weight: 600; }}
QFrame#themeCard QRadioButton::indicator {{ width: 0; height: 0; border: none; }}
QWidget#settingsPanel QPushButton#primary:disabled {{ background: {t['soft']};
 color: {t['muted']}; border-color: {t['accent']}; font-weight: 650; }}
/* wave2-pets */
QFrame#petHero {{ background: {t['accent_soft']}; border: none; border-radius: 14px; }}
QLabel#petStage {{ background: {t['surface']}; border-radius: 14px; }}
QLabel#petName {{ font-size: 13px; font-weight: 600; }}
QLabel#selectedChip {{ background: {t['accent']}; color: {t['accent_text']};
 border-radius: 8px; font-size: 12px; font-weight: 600; padding: 0 8px; }}
QListWidget#galleryList {{ background: transparent; border: none; padding: 0; }}
QListWidget#galleryList::item, QListWidget#galleryList::item:selected,
QListWidget#galleryList::item:hover {{ background: transparent; border: none; padding: 0; }}
QFrame#galleryCard {{ padding: 1px; }}
QFrame#galleryCard[selected="true"] {{ padding: 0; }}
QFrame#galleryCard[highlighted="true"] {{ background: {t['soft']};
 border: 1px solid {t['accent']}; }}
QFrame#galleryCard[highlighted="true"][selected="true"] {{ border: 2px solid {t['accent']}; }}
/* Klavye odağı: 2px accent çerçeve + accent_soft zemin; padding 0 ile kutu kaymaz.
   "Seçili" (surface zemin + chip) ile karışmasın diye zemin ayrışır. */
QFrame#galleryCard[focused="true"] {{ background: {t['accent_soft']};
 border: 2px solid {t['accent']}; padding: 0; }}
QPushButton#importButton {{ padding-right: 28px; }}
QPushButton#importButton::menu-indicator {{ image: url({down}); width: 10px; height: 6px;
 subcontrol-origin: padding; subcontrol-position: right center; right: 10px; }}
QFrame#petWell {{ background: {t['soft']}; border: 2px dashed {t['border']};
 border-radius: 12px; }}
QFrame#petWell QLabel {{ background: transparent; }}
QFrame#petSilhouette {{ background: transparent; color: {t['muted']};
 selection-background-color: {t['soft']}; border: none; }}
/* wave2-lists */
QPushButton#disclosure {{ background: transparent; border: none; color: {t['text']};
 font-weight: 600; padding: 4px 8px; text-align: left; }}
QPushButton#disclosure:hover, QPushButton#disclosure:checked {{ color: {t['accent']}; }}
QLabel#statusChip {{ background: {t['soft']}; color: {t['text']}; border-radius: 8px;
 font-size: 13px; font-weight: 600; padding: 2px 8px; }}
QLabel#statusChip[tone="running"] {{ background: {t['accent_soft']}; color: {t['text']}; }}
QLabel#statusChip[tone="waiting"] {{ background: transparent; color: {t['warning']};
 border: 1px solid {t['warning']}; }}
QLabel#statusChip[tone="done"] {{ background: transparent; color: {t['success']};
 border: 1px solid {t['success']}; }}
QLabel#statusChip[tone="error"] {{ background: transparent; color: {t['error']};
 border: 1px solid {t['error']}; }}
QLabel#warnNote {{ color: {t['warning']}; font-size: 12px; }}
QFrame#dangerZone {{ background: transparent; border: 1px solid {t['error']};
 border-radius: 12px; }}
QFrame#dangerZone QLabel#sectionTitle {{ color: {t['error']}; }}
/* Devre dışı birincil düğme birincil kalır: accent çerçeve + kalın yazı, ama dolgu/metin sönük. */
QPushButton#primary:disabled {{ background: {t['soft']}; color: {t['muted']};
 border-color: {t['accent']}; font-weight: 650; }}
QSpinBox:disabled {{ background: {t['soft']}; color: {t['muted']}; }}
/* Bağlama sihirbazı: seçilebilir kartlar ve sayfa yüzeyi. */
QPushButton#choiceCard {{ background: {t['surface']}; border: 1px solid {t['border']};
 border-radius: 12px; padding: 0; text-align: left; min-height: 80px; }}
QPushButton#choiceCard:hover {{ border-color: {t['accent']}; }}
QPushButton#choiceCard:checked {{ background: {t['accent_soft']};
 border: 2px solid {t['accent']}; }}
QLabel#choiceTitle {{ font-size: 15px; font-weight: 650; background: transparent; }}
QLabel#choiceDetail {{ color: {t['muted']}; font-size: 12px; background: transparent; }}
QLabel#choiceBadge {{ background: {t['accent']}; color: {t['accent_text']}; border-radius: 8px;
 padding: 1px 8px; font-size: 12px; font-weight: 650; }}
QFrame#wizardPage {{ background: {t['surface']}; border: 1px solid {t['border']};
 border-radius: 12px; }}
QFrame#localFound {{ background: {t['accent_soft']}; border-radius: 10px; }}
/* /wave2-lists */
"""


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
    return _qss_controls(t, up, down) + _qss_components(t, down, radio)
