"""Ana pencerenin yerleşimi (DesktopWindow'dan ayrıldı): üst çubuk, sohbet sayfası, kenar çubuğu.

Davranış DesktopWindow'dadır; burası yalnız widget'ları kurar ve `window` üzerindeki alanlara
bağlar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop import shell_assets, themes
from artemis.channels.desktop.chat_widgets import (
    _CHAT_MAX_WIDTH,
    _VOICE_LABELS,
    ChatTranscript,
    PromptEdit,
    _ChatPage,
    _Drawer,
)
from artemis.channels.desktop.page_kit import make_page_header
from artemis.channels.desktop.profile_bar import build_profile_button
from artemis.channels.desktop.widgets import HelpTip, PetFace
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow

_NAV_WIDTH = 176
_NAV_COMPACT_WIDTH = 72

_HELP_AUTO = (
    "Onaylı: bilgisayar kullanımı, pencere kontrolü ve ekran okuma her seferinde sorar. Otomatik: "
    "bu üçü onay sormadan çalışır (dış içerik okunmuş turda da); Esc durdurur. Diğer araçlar "
    "etkilenmez. Anında geçerli."
)

_HELP_NEW_CHAT = (
    "Modelin bağlamını sıfırlar; sonraki mesaj boş geçmişle başlar. Kalıcı konuşma arşivini "
    "silmez. Çalışan tur varken sıfırlanmaz."
)

_HELP_QUICK = (
    "Küçük, hep üstte duran hızlı komut penceresini açar; yazdığın komut aynı sohbete gider."
)


def icon_toggle(accessible_name: str) -> QPushButton:
    """32px simge anahtarı; işaretli durumu `iconToggle:checked` kuralı belirgin çizer."""
    button = QPushButton()
    button.setObjectName("iconToggle")
    button.setCheckable(True)
    button.setFixedSize(32, 32)
    button.setIconSize(QSize(18, 18))
    button.setAccessibleName(accessible_name)
    return button


def build_topbar(window: DesktopWindow) -> QFrame:
    """56px üst çubuk: pet + marka (+Demo), model çipi, ses/oto onay simgeleri, durum."""
    header = QFrame()
    header.setObjectName("topbar")
    header.setFixedHeight(56)
    window._topbar = header
    row = QHBoxLayout(header)
    row.setContentsMargins(12, 8, 12, 8)
    row.setSpacing(8)
    window.brand_pet = PetFace(32, themes.THEMES[window._theme])
    row.addWidget(window.brand_pet)
    title = QLabel("Artemis")
    title.setObjectName("sectionTitle")
    title.setToolTip(t("Güvenli masaüstü asistanı"))
    row.addWidget(title)
    window.demo_chip = None
    if window._demo:
        window.demo_chip = QLabel(t("Demo"))
        window.demo_chip.setObjectName("chipWarning")
        window.demo_chip.setToolTip(t("Demo modu; gerçek işlem yapılmaz"))
        window.demo_chip.setAccessibleName(t("Demo modu; gerçek işlem yapılmaz"))
        row.addWidget(window.demo_chip, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addStretch(1)

    # Model: ortada tek çip; dar pencerede kısalır (bkz. _fit_shell).
    # Çip düğmedir: Ayarlar > Model'i açar, klavyeyle de erişilir.
    window.model_caption = QLabel(t("Model"))
    window.model_caption.setObjectName("meta")
    window.model_value = QPushButton(t("Yükleniyor…"))
    window.model_value.setObjectName("chip")
    window.model_value.setAccessibleName(t("Kullanılan beyin modeli"))
    window.model_value.setToolTip(t("Kullanılan beyin modeli"))
    window.model_value.setCursor(Qt.CursorShape.PointingHandCursor)
    window.model_value.clicked.connect(window._show_model_settings)
    value_font = window.model_value.font()
    value_font.setPixelSize(13)
    window.model_value.setFont(value_font)
    window.model_box = window.model_value
    row.addWidget(window.model_caption, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(window.model_value, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addStretch(1)

    # voice_dot/voice_state köprü ve testler için canlı kalan, görünmeyen aynalardır;
    # görünür tek ses denetimi simge anahtarıdır, durum metni ipucunda okunur.
    window.voice_dot = QLabel("●", window)
    window.voice_dot.setObjectName("voiceDot")
    window.voice_dot.hide()
    window.voice_state = QLabel(t(_VOICE_LABELS["off"]), window)
    window.voice_state.setAccessibleName(t("Ses durumu"))
    window.voice_state.hide()
    window.budget_chip = QLabel("$0.00")
    window.budget_chip.setObjectName("chip")
    window.budget_chip.setAccessibleName(t("Günlük bütçe ve harcama"))
    row.addWidget(window.budget_chip, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(build_profile_button(window), 0, Qt.AlignmentFlag.AlignVCenter)
    window.voice_button = icon_toggle(t("Sesi aç veya kapat"))
    window.voice_button.clicked.connect(window._toggle_voice)
    row.addWidget(window.voice_button)
    window.auto_button = icon_toggle(
        t("Bilgisayar kullanımında otomatik onayı aç veya kapat")
    )
    window.auto_button.toggled.connect(window._toggle_auto_mode)
    row.addWidget(window.auto_button)
    row.addWidget(HelpTip(t(_HELP_AUTO)), 0, Qt.AlignmentFlag.AlignVCenter)
    status_pill = QFrame()
    window.status_pill = status_pill
    status_pill.setObjectName("statusPill")
    # Düz durum çipi tıklanmaz (simge anahtarları tıklanır); ikisi de ipucu taşır.
    status_pill.setToolTip(t("Çalışma durumu"))
    status_pill.setAccessibleName(t("Çalışma durumu"))
    # Çip içeriğine sığar: genişlemez, metin değişince yeniden ölçülür.
    status_pill.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    status_row = QHBoxLayout(status_pill)
    status_row.setContentsMargins(0, 0, 0, 0)
    status_row.setSpacing(6)
    window.status_dot = QLabel("●")
    window.status_dot.setObjectName("statusDot")
    window.status_dot.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    window.status_label = QLabel("")
    window.status_label.setObjectName("status")
    window.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    window.status_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    window._set_status(t("Başlatılıyor…"), "running")
    status_row.addWidget(window.status_dot)
    status_row.addWidget(window.status_label)
    row.addWidget(status_pill, 0, Qt.AlignmentFlag.AlignVCenter)
    return header


def _topbar_item_width(widget: QWidget) -> int:
    if widget.minimumWidth() == widget.maximumWidth() and widget.minimumWidth() > 0:
        return widget.minimumWidth()
    return max(widget.minimumWidth(), widget.sizeHint().width(), 0)


def fit_topbar(window: DesktopWindow) -> None:
    """Üst çubukta durum hapının kırpılmaması için ikincil etiketleri sığdırır."""
    header = getattr(window, "_topbar", None)
    if header is None:
        pill = getattr(window, "status_pill", None)
        header = pill.parentWidget() if pill is not None else None
    layout = header.layout() if header is not None else None
    if header is None or layout is None:
        return
    width = window.width()
    if window.demo_chip is not None:
        window.demo_chip.setVisible(True)
    window.model_box.setVisible(width >= 800)
    window.model_caption.setVisible(width >= 800)
    window.budget_chip.setVisible(width >= 1000)
    profile_button = getattr(window, "profile_button", None)
    if profile_button is not None:
        profile_button.setVisible(True)

    central = window.centralWidget()
    outer_layout = central.layout() if central is not None else None
    outer = outer_layout.contentsMargins() if outer_layout is not None else None
    outer_x = (outer.left() + outer.right()) if outer is not None else 32
    margins = layout.contentsMargins()
    available = max(
        0,
        (width - outer_x) - margins.left() - margins.right() - 2 * header.frameWidth(),
    )
    spacing = max(0, layout.spacing())
    widgets = [
        w
        for i in range(layout.count())
        if (item := layout.itemAt(i)) is not None
        and (w := item.widget()) is not None
        and not w.isHidden()
    ]
    if not widgets:
        return
    needed = sum(_topbar_item_width(w) for w in widgets) + spacing * (len(widgets) - 1)
    optional_items: list[QWidget] = [window.model_caption, window.budget_chip]
    if profile_button is not None:
        optional_items.append(profile_button)
    optional_items.append(window.model_box)
    if window.demo_chip is not None:
        optional_items.append(window.demo_chip)
    for optional in optional_items:
        if needed <= available:
            break
        if not optional.isHidden():
            needed -= _topbar_item_width(optional) + spacing
            optional.setVisible(False)


def build_chat_page(window: DesktopWindow) -> QWidget:
    page = _ChatPage()
    page_row = QHBoxLayout(page)
    page_row.setContentsMargins(0, 0, 0, 0)
    page_row.setSpacing(0)
    main_column = QWidget()
    main_column.setObjectName("flat")
    page_row.addWidget(main_column, 1)

    column = QVBoxLayout(main_column)
    column.setContentsMargins(24, 0, 24, 24)
    column.setSpacing(16)
    window.quick_action = QPushButton(t("Hızlı komut"))
    window.quick_action.clicked.connect(window._show_overlay)
    window.new_chat_button = QPushButton(t("Yeni sohbet"))
    window.new_chat_button.setToolTip(t("Yeni bağlam açar; kalıcı konuşma arşivini silmez"))
    window.new_chat_button.clicked.connect(window._new_chat)
    # Dar pencerede son işlem kartları bu düğmeyle yandan açılan sayfada görünür.
    window.operations_button = QPushButton(t("İşlemler"))
    window.operations_button.setToolTip(t("Mesaj başına işlem kartlarını açar"))
    window.operations_button.setAccessibleName(t("Mesaj işlemlerini aç"))
    window.operations_button.clicked.connect(window._open_operations)
    column.addWidget(
        make_page_header(
            # Marka üst çubukta, sayfa adı kenar çubuğunda zaten var; üçüncü kez yazılmaz.
            "",
            actions=(
                window.operations_button,
                (window.new_chat_button, t(_HELP_NEW_CHAT)),
                (window.quick_action, t(_HELP_QUICK)),
            ),
        )
    )
    # Başlıktan sonra iki sütun: sohbet+yazma alanı ve (geniş pencerede) son işlem kartı.
    window._chat_row = QHBoxLayout()
    window._chat_row.setSpacing(16)
    column.addLayout(window._chat_row, 1)
    left = QVBoxLayout()
    left.setSpacing(16)
    window._chat_row.addLayout(left, 1)

    # Konuşma düz zemindedir (kart yok); okunur sütun ortalanır.
    window.chat = ChatTranscript(
        window._demo, window._on_welcome_chip, themes.THEMES[window._theme],
        pet_source=window._pet_pixmap,
    )
    window.chat.setObjectName("chatTranscript")
    window.chat.setAccessibleName(t("Seçilebilir sohbet geçmişi"))
    left.addWidget(window.chat, 1)

    # Zamanı gelen rutin için soru satırı (ADR 0058): Çalıştır tıklaması normal, onaylı tur.
    window._routine_name = ""
    window._routine_suggest = ""
    window._summary_day = ""
    window.routine_bar = QFrame()
    window.routine_bar.setObjectName("composer")
    window.routine_bar.setMaximumWidth(_CHAT_MAX_WIDTH)
    routine_row = QHBoxLayout(window.routine_bar)
    routine_row.setContentsMargins(12, 8, 8, 8)
    routine_row.setSpacing(8)
    window.routine_label = QLabel("")
    window.routine_label.setWordWrap(True)
    window.routine_label.setTextFormat(Qt.TextFormat.PlainText)
    window.routine_run_button = QPushButton(t("Çalıştır"))
    window.routine_run_button.setObjectName("primary")
    window.routine_dismiss_button = QPushButton(t("Kapat"))
    # Yalnız gün özeti bildiriminde (ADR 0066) ve vault açıkken görünür.
    window.summary_vault_button = QPushButton(t("Vault'a ekle"))
    window.summary_vault_button.hide()
    window.summary_vault_button.clicked.connect(window._append_summary_to_vault)
    window.routine_run_button.clicked.connect(window._run_routine_notice)
    window.routine_dismiss_button.clicked.connect(window.routine_bar.hide)
    routine_row.addWidget(window.routine_label, 1)
    routine_row.addWidget(window.routine_dismiss_button)
    routine_row.addWidget(window.summary_vault_button)
    routine_row.addWidget(window.routine_run_button)
    window.routine_bar.hide()
    # Yazma alanıyla aynı ortalanmış sütun: genişlik ve sol kenar her pencerede aynı kalır.
    notice_wrap = QHBoxLayout()
    notice_wrap.addStretch(1)
    notice_wrap.addWidget(window.routine_bar, 1000)
    notice_wrap.addStretch(1)
    left.addLayout(notice_wrap)

    composer = QFrame()
    composer.setObjectName("composer")
    composer.setMaximumWidth(_CHAT_MAX_WIDTH)
    composer_row = QHBoxLayout(composer)
    composer_row.setContentsMargins(12, 8, 8, 8)
    composer_row.setSpacing(8)
    window.prompt = PromptEdit(window._submit_main)
    window.prompt.setObjectName("mainPrompt")
    window.prompt.setPlaceholderText(t("Artemis'e yaz…"))
    hint = t("Enter gönderir, Shift+Enter yeni satır")
    window.prompt.setToolTip(hint)
    window.prompt.setAccessibleDescription(hint)
    composer_row.addWidget(window.prompt, 1)
    # Üst çubuktaki ses anahtarıyla aynı yol; yalnız tıklama ona iletilir.
    window.composer_voice = icon_toggle(t("Sesi aç veya kapat (yazma alanı)"))
    window.composer_voice.clicked.connect(window._composer_voice_clicked)
    window.cancel_button = QPushButton(t("İptal"))
    window.cancel_button.setMinimumWidth(72)
    window.cancel_button.setFixedHeight(36)
    window.cancel_button.hide()  # yalnız iş sürerken görünür (bkz. _set_controls)
    window.send_button = QPushButton(t("Gönder"))
    window.send_button.setObjectName("primary")
    window.send_button.setMinimumWidth(88)
    window.send_button.setFixedHeight(36)
    window.cancel_button.clicked.connect(window._cancel_active)
    window.send_button.clicked.connect(window._submit_main)
    bottom = Qt.AlignmentFlag.AlignBottom
    composer_row.addWidget(window.composer_voice, 0, bottom)
    composer_row.addWidget(window.cancel_button, 0, bottom)
    composer_row.addWidget(window.send_button, 0, bottom)
    # Sütun ortalanır: kenar boşlukları esner, yazma alanı 680px'e kadar genişler.
    composer_wrap = QHBoxLayout()
    composer_wrap.addStretch(1)
    composer_wrap.addWidget(composer, 1000)
    composer_wrap.addStretch(1)
    left.addLayout(composer_wrap)

    # Son işlem paneli: ≥1100px'te sağ sütun, daha darda yandan açılan sayfa.
    window.activity_panel = QFrame()
    window.activity_panel.setObjectName("stepPanel")
    window.activity_panel.setFixedWidth(280)
    panel_layout = QVBoxLayout(window.activity_panel)
    panel_layout.setContentsMargins(16, 16, 16, 16)
    panel_layout.setSpacing(12)
    activity_title = QLabel(t("Mesaj işlemleri"))
    activity_title.setObjectName("sectionTitle")
    window.activity_close = QPushButton(t("Kapat"))
    window.activity_close.setObjectName("ghost")
    window.activity_close.setAccessibleName(t("Mesaj işlemleri sayfasını kapat"))
    window.activity_close.clicked.connect(window._close_operations)
    window.activity_close.hide()
    title_row = QHBoxLayout()
    title_row.setSpacing(8)
    title_row.addWidget(activity_title)
    title_row.addStretch(1)
    title_row.addWidget(window.activity_close)
    # Her mesaj bir kart; en yeni üstte. Kartlar kaydırılır, yatay kaydırma yoktur.
    window.activity = QScrollArea()
    window.activity.setAccessibleName(t("Mesaj başına işlemler"))
    window.activity.setWidgetResizable(True)
    window.activity.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    window.activity.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    window.activity.setFrameShape(QFrame.Shape.NoFrame)
    window.activity.setStyleSheet(
        "QScrollArea { border: none; background: transparent; }"
        "QScrollArea > QWidget > QWidget { background: transparent; }"
    )
    cards_host = QWidget()
    cards_host.setObjectName("flat")
    window.activity_cards = QVBoxLayout(cards_host)
    window.activity_cards.setContentsMargins(0, 0, 8, 0)
    window.activity_cards.setSpacing(8)
    window.activity_cards.addStretch(1)
    window.activity.setWidget(cards_host)
    window.activity.hide()
    window.activity_notice = QLabel("")
    window.activity_notice.setObjectName("meta")
    window.activity_notice.setWordWrap(True)
    window.activity_notice.hide()
    window.activity_empty = QLabel(t("Henüz bir işlem yok"))
    window.activity_empty.setObjectName("meta")
    panel_layout.addLayout(title_row)
    # Boş durumda başlığın hemen altında kalır; satırlar üstten dizilir.
    panel_layout.addWidget(window.activity_notice)
    panel_layout.addWidget(window.activity_empty, 1, Qt.AlignmentFlag.AlignTop)
    panel_layout.addWidget(window.activity, 1)
    window._chat_row.addWidget(window.activity_panel)
    window._panel_in_drawer = False
    window.drawer = _Drawer(page, window._close_operations)
    page.drawer = window.drawer
    return page


def build_sidebar(window: DesktopWindow, pages: list[tuple[str, QWidget]]) -> QFrame:
    sidebar = QFrame()
    sidebar.setObjectName("sidebar")
    sidebar.setFixedWidth(_NAV_WIDTH)
    window.sidebar = sidebar
    navigation = QVBoxLayout(sidebar)
    navigation.setContentsMargins(12, 12, 12, 12)
    navigation.setSpacing(4)
    window.nav_buttons = {}
    window._nav_icon_names = {}
    for label, page in pages:
        if label == "Ayarlar":
            navigation.addStretch(1)
        shown = t(label)
        button = QPushButton(shown)
        button.setObjectName("navItem")
        button.setCheckable(True)
        button.setAutoExclusive(True)
        button.setAccessibleName(shown)
        button.setToolTip(shown)  # dar pencerede yalnız ikon kalır; ad ipucunda
        button.setIconSize(QSize(20, 20))
        button.clicked.connect(
            lambda _checked=False, target=page: window.tabs.setCurrentWidget(target)
        )
        window.nav_buttons[page] = button
        window._nav_icon_names[button] = shell_assets.NAV_ICONS[label]
        navigation.addWidget(button)
    window.theme_toggle = QPushButton()
    window.theme_toggle.setObjectName("themeToggle")
    window.theme_toggle.setFixedHeight(32)
    window.theme_toggle.setIconSize(QSize(16, 16))
    window.theme_toggle.clicked.connect(window._toggle_theme)
    navigation.addWidget(window.theme_toggle)
    return sidebar


def page_frame(widget: QWidget) -> QWidget:
    """Kendi 24px iç boşluğu olmayan sayfa 24px kenar payıyla barındırılır.

    Ayarlar ve sohbet bu payı kendisi taşır; diğerleri aynı hizada başlasın diye
    burada eklenir. Sayfaların kendi kaydırıcısı korunur.
    """
    host = QWidget()
    host_layout = QVBoxLayout(host)
    host_layout.setContentsMargins(24, 0, 24, 24)
    host_layout.addWidget(widget)
    return host
