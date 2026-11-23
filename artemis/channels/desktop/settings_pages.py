"""Ayarlar paneli sayfaları (SettingsPanel'den ayrıldı): kabuk, çerçeve ve 1. grup sayfaları.

Davranış SettingsPanel'dedir; 2. ve 3. grup sayfaları `settings_pages_extra.py` içindedir.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.form_kit import (
    _HeightForWidthBox,
    _SpinBox,
    button_row,
    divider,
    field,
    group,
    meta_label,
)
from artemis.channels.desktop.page_kit import CurrentPageStack, SettingsTabWidget
from artemis.channels.desktop.pet_section import PetSection
from artemis.channels.desktop.pet_sprite import pets_dir_for
from artemis.channels.desktop.provider_section import ProviderSection
from artemis.channels.desktop.retranslate import set_i18n_context
from artemis.channels.desktop.settings_pages_extra import (
    default_repos_dir as default_repos_dir,
)
from artemis.channels.desktop.settings_widgets import (
    _GROUP_ROLE,
    _HEADER_HEIGHT,
    _ITEM_HEIGHT,
    _Notice,
    _SavedNote,
    _SearchBox,
    _SectionDelegate,
    _SectionList,
    _ThemeCard,
)
from artemis.channels.desktop.themes import DEFAULT_THEME, THEME_LABELS
from artemis.channels.desktop.tts_cloud_box import build_tts_cloud_box
from artemis.channels.desktop.voice_model_manager import VoiceModelManagerWidget
from artemis.channels.desktop.voice_preview import build_voice_output_controls
from artemis.channels.desktop.voice_telemetry_card import VoiceTelemetryCard
from artemis.channels.desktop.widgets import with_help
from artemis.i18n import current_language, resolve_source, t

if TYPE_CHECKING:
    from artemis.channels.desktop.settings_panel import SettingsPanel

_HINT_MAX_WIDTH = 640
_REVERT_TOOLTIP = (
    "Ayarlar açıldığındaki kayıtlı hâline döner. "
    "API anahtarları ve kasadaki parolalar geri alınmaz."
)


def _check_hint(text: str) -> QLabel:
    label = meta_label(text)
    label.setContentsMargins(24, 0, 0, 0)
    label.setMaximumWidth(_HINT_MAX_WIDTH)
    return label


def build_shell(panel: SettingsPanel) -> None:
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(24, 0, 24, 24)
    layout.setSpacing(12)
    title = QLabel(t("Ayarlar"))
    title.setObjectName("pageTitle")
    title.setFixedHeight(40)
    panel.notice = _Notice()
    panel.retry_button = QPushButton(t("Tekrar dene"))
    panel.retry_button.setToolTip(t("Kaydedilemeyen değişiklikleri yeniden yazmayı dener."))
    panel.retry_button.setVisible(False)
    panel.retry_button.clicked.connect(panel.flush)
    panel.revert_button = QPushButton(t("Değişiklikleri geri al"))
    panel.revert_button.setToolTip(t(_REVERT_TOOLTIP))
    panel.revert_button.setVisible(False)
    panel.revert_button.clicked.connect(panel._revert)
    title_row = QHBoxLayout()
    title_row.setSpacing(12)
    title_row.addWidget(title)
    title_row.addWidget(panel.notice, 1)
    title_row.addWidget(panel.retry_button)
    title_row.addWidget(panel.revert_button)
    panel.save_button = QPushButton(t("Kaydet"))
    panel.save_button.setObjectName("primary")
    panel.save_button.setToolTip(t("Değişiklikleri hemen kaydeder ve uygular."))
    panel.save_button.setVisible(False)
    panel.save_button.clicked.connect(panel._save_now)
    title_row.addWidget(panel.save_button)
    panel.title_row = title_row
    layout.addLayout(title_row)
    panel.sections = _SectionList()
    panel.sections.setObjectName("sectionList")
    panel.sections.setFixedWidth(200)
    panel.sections.setAccessibleName(t("Ayar bölümleri"))
    panel.sections.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    panel._section_delegate = _SectionDelegate(panel.sections)
    panel.sections.setItemDelegate(panel._section_delegate)
    panel.stack = CurrentPageStack()
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(panel.stack)
    panel.sections.scroll_area = scroll
    panel.section_combo = QComboBox()
    panel.section_combo.setAccessibleName(t("Bölüm"))
    panel.section_combo.setVisible(False)
    panel._compact = False
    body = QWidget()
    body_row = QHBoxLayout(body)
    body_row.setContentsMargins(0, 0, 0, 0)
    body_row.setSpacing(16)
    nav = QWidget()
    panel.nav = nav
    nav.setFixedWidth(200)
    nav_col = QVBoxLayout(nav)
    nav_col.setContentsMargins(0, 0, 0, 12)
    nav_col.setSpacing(8)
    panel.search = _SearchBox()
    panel.search.setPlaceholderText(t("Ayarlarda ara"))
    panel.search.setAccessibleName(t("Ayarlarda ara"))
    panel.search.setFixedHeight(36)
    panel.search.setClearButtonEnabled(True)
    panel.no_results = QLabel(t("Sonuç yok"))
    panel.no_results.setObjectName("meta")
    panel.no_results.setVisible(False)
    nav_col.addWidget(panel.search)
    nav_col.addWidget(panel.sections, 1)
    nav_col.addWidget(panel.no_results)
    body_row.addWidget(nav)
    body_row.addWidget(scroll, 1)
    layout.addWidget(panel.section_combo)
    layout.addWidget(body, 1)


def add_page(
    panel: SettingsPanel, name: str, description: str, group_title: str = "", *,
    wide: bool = False,
) -> QVBoxLayout:
    """Bölüm sayfası: 880px'i aşmayan sütun, solda; tek satır açıklama."""
    outer = _HeightForWidthBox()
    row = QVBoxLayout(outer)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    column = _HeightForWidthBox()
    column.setMaximumWidth(16777215 if wide else 880)
    row.addWidget(column)
    content = QVBoxLayout(column)
    content.setContentsMargins(0, 0, 0, 0)
    content.setSpacing(24)
    saved = _SavedNote()
    panel.saved_notes[name] = saved
    canonical = resolve_source(name) or name
    panel._canonical_saved_notes[canonical] = saved
    heading = QHBoxLayout()
    heading.addWidget(meta_label(description), 1)
    heading.addWidget(saved, 0, Qt.AlignmentFlag.AlignVCenter)
    content.addLayout(heading)
    panel.sections.addItem(name)
    item = panel.sections.item(panel.sections.count() - 1)
    if group_title:
        panel._current_group = group_title
    panel._row_groups.append((panel._current_group, bool(group_title)))
    if group_title:
        item.setData(_GROUP_ROLE, group_title)
    item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{name}, {panel._current_group}")
    item.setSizeHint(QSize(0, _ITEM_HEIGHT + (_HEADER_HEIGHT if group_title else 0)))
    panel.stack.addWidget(outer)
    return content


def build_voice_page(panel: SettingsPanel) -> None:
    """1. sayfa: Ses (Dinleme (Giriş) | Yanıt (Çıkış) | Ses modelleri)."""
    voice = add_page(panel, t("Ses"), t("Uyandırma, tanıma, konuşma sesi ve ölçüm."), t("Temel"))
    tabs = SettingsTabWidget()
    panel.voice_tabs = tabs
    panel._page_tabs["Ses"] = tabs
    voice.addWidget(tabs)

    listen_page = _HeightForWidthBox()
    listen_layout = QVBoxLayout(listen_page)
    listen_layout.setContentsMargins(0, 16, 0, 0)
    listen_layout.setSpacing(24)
    _wake_group(panel, listen_layout)
    _stt_group(panel, listen_layout)
    _cleanup_group(panel, listen_layout)
    panel.voice_telemetry_card = VoiceTelemetryCard(panel.config_path)
    listen_layout.addWidget(panel.voice_telemetry_card)
    listen_layout.addStretch(1)
    tabs.addTab(listen_page, t("Dinleme (Giriş)"))

    reply_page = _HeightForWidthBox()
    reply_layout = QVBoxLayout(reply_page)
    reply_layout.setContentsMargins(0, 16, 0, 0)
    reply_layout.setSpacing(24)
    _speech_group(panel, reply_layout)
    reply_layout.addStretch(1)
    tabs.addTab(reply_page, t("Yanıt (Çıkış)"))

    models_page = _HeightForWidthBox()
    models_layout = QVBoxLayout(models_page)
    models_layout.setContentsMargins(0, 16, 0, 0)
    models_layout.setSpacing(24)
    panel.voice_model_manager = VoiceModelManagerWidget(
        panel.config_path, parent=panel, demo=panel._demo
    )
    models_layout.addWidget(panel.voice_model_manager)
    models_layout.addStretch(1)
    tabs.addTab(models_page, t("Ses modelleri"))
    voice.addStretch(1)


def _wake_group(panel: SettingsPanel, voice: QVBoxLayout) -> None:
    wake_box, wake = group(t("Uyandırma ve mikrofon"), divided=False)
    voice.addWidget(wake_box)
    panel.wake_name = QLineEdit()
    panel.wake_name.setText("Artemis")
    panel.wake_name.setMaxLength(64)
    panel.wake_name.setAccessibleName(t("Uyandırma adı"))
    panel.wake_name.setPlaceholderText(t("Örneğin: Artemis, Ali veya Veli"))
    panel.wake_name.setToolTip(t(
        "Sesle çağırmak için kullanacağın ad. Uygulamanın adı Artemis olarak kalır. "
        "Ayarlar'dan çıkınca ses yeniden başlatılır ve ad geçerli olur."
    ))
    field(
        wake,
        t("Uyandırma adı"),
        panel.wake_name,
        t("Sesle çağırma adı; uygulamanın adı Artemis kalır."),
    )
    panel.device_combo = QComboBox()
    panel.device_combo.setAccessibleName(t("Mikrofon girişi"))
    panel._no_device()
    panel.mic_test_button = QPushButton(t("Mikrofonu test et"))
    panel.mic_test_button.setAccessibleName(t("Mikrofonu test et"))
    panel.mic_test_button.clicked.connect(panel._open_mic_wizard)
    device_row = QHBoxLayout()
    device_row.setSpacing(8)
    device_row.addWidget(panel.device_combo, 1)
    device_row.addWidget(panel.mic_test_button)
    field(wake, t("Mikrofon girişi"), device_row)
    panel.current_device = QLabel(t("Seçilmedi"))
    panel.current_device.setWordWrap(True)
    field(wake, t("Kullanılan mikrofon"), panel.current_device)
    panel.input_level = QProgressBar()
    panel.input_level.setObjectName("levelMeter")
    panel.input_level.setRange(0, 100)
    panel.input_level.setValue(0)
    panel.input_level.setTextVisible(False)
    panel.input_level.setFixedHeight(6)
    panel.input_level.setAccessibleName(t("Ses girişi seviyesi"))
    panel.level_value = QLabel(t("%{value}").format(value=0))
    panel.level_value.setObjectName("meta")
    max_pct = t("%{value}").format(value=100)
    panel.level_value.setFixedWidth(panel.level_value.fontMetrics().horizontalAdvance(max_pct) + 8)
    panel.level_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    panel.input_level.valueChanged.connect(
        lambda value: panel.level_value.setText(t("%{value}").format(value=value))
    )
    level_row = QHBoxLayout()
    level_row.setSpacing(12)
    level_row.addWidget(panel.input_level, 1)
    level_row.addWidget(panel.level_value)
    field(wake, t("Ses girişi"), level_row, t("Konuşunca çubuk dolar."))
    panel.last_heard = QLabel(t("Henüz konuşma algılanmadı."))
    panel.last_heard.setTextFormat(Qt.TextFormat.PlainText)
    panel.last_heard.setWordWrap(True)
    field(wake, t("Son duyulan"), panel.last_heard)
    panel.continuous_mode = QCheckBox(t("Kesintisiz sohbet"))
    wake.addLayout(with_help(panel.continuous_mode, t(
        "Uyandırma adı söylemeden sürekli dinler. Karma tanıma açıksa kesintisiz kipte her söz "
        "bulut sunucusuna gider."
    )))
    wake.addWidget(_check_hint(t("Uyandırma adı söylemeden sürekli dinler.")))
    panel.ptt_enabled = QCheckBox(t("Bas-konuş (Push-to-Talk)"))
    wake.addWidget(panel.ptt_enabled)
    wake.addWidget(_check_hint(
        t("Belirlenen kısayolu basılı tutarken dinler, bırakınca komutu işler.")
    ))
    panel.ptt_hotkey = QLineEdit()
    panel.ptt_hotkey.setMaxLength(40)
    panel.ptt_hotkey.setAccessibleName(t("Bas-konuş kısayolu"))
    panel.ptt_hotkey.setPlaceholderText("ctrl+alt+v")
    field(
        wake,
        t("Bas-konuş kısayolu"),
        panel.ptt_hotkey,
        t("Bas-konuş için genel kısayol (ör. ctrl+alt+v; boş bırakılırsa kapalı)."),
    )


def _stt_group(panel: SettingsPanel, voice: QVBoxLayout) -> None:
    stt_card, stt_layout = group(t("Tanıma"))
    voice.addWidget(stt_card)
    stt_controls = QHBoxLayout()
    stt_controls.setSpacing(8)
    panel.stt_combo = QComboBox()
    panel.stt_combo.setAccessibleName(t("Tanıma modeli"))
    panel.stt_download = QPushButton(t("İndir"))
    panel.stt_download.setMinimumWidth(88)
    stt_controls.addWidget(panel.stt_combo, 1)
    stt_controls.addWidget(panel.stt_download)
    field(stt_layout, t("Tanıma modeli"), stt_controls, help=t(
        "Büyük model (large) daha doğru tanır ama daha çok bellek/VRAM ve disk kullanır, ilk "
        "yükleme uzar. Küçük model hızlıdır, Türkçede hata payı artar. Ses yeniden "
        "başlayınca uygulanır."
    ))
    panel.stt_status = meta_label("")
    stt_layout.addWidget(panel.stt_status)
    panel.stt_language_combo = QComboBox()
    panel.stt_language_combo.setAccessibleName(t("Tanıma dili"))
    panel.stt_language_combo.addItem(t("Türkçe"), "tr")
    panel.stt_language_combo.addItem(t("Türkçe + İngilizce terimler"), "tr_en")
    panel.stt_language_combo.addItem("English", "en")
    panel.stt_language_combo.addItem(t("Otomatik algıla"), "auto")
    field(
        stt_layout,
        t("Tanıma dili"),
        panel.stt_language_combo,
        t("Konuştuğun dil. İngilizce arayüzde İngilizce konuşacaksan English seç."),
    )
    panel.precision_combo = QComboBox()
    panel.precision_combo.setAccessibleName(t("Tanıma hassasiyeti"))
    panel.precision_combo.setToolTip(t(
        "Dengeli ölçümde En yüksek hassasiyetle aynı doğrulukta, yaklaşık 1,8 GB daha az "
        "ekran kartı belleği kullanır. İşlemcide çalışırken seçici kapalıdır. Değişiklik ses "
        "yeniden başlayınca uygulanır."
    ))
    panel.precision_combo.addItem(t("Dengeli (az bellek)"), "int8_float16")
    panel.precision_combo.addItem(t("En yüksek hassasiyet"), "float16")
    field(stt_layout, t("Tanıma hassasiyeti"), panel.precision_combo, help=t(
        "En yüksek hassasiyet (float16) daha çok ekran kartı belleği kullanır; Dengeli ölçümde "
        "aynı doğrulukta yaklaşık 1,8 GB daha az bellek ister. İşlemcide seçici kapalıdır."
    ))
    panel.hybrid_stt = QCheckBox(t("Karma tanıma (komutları bulutta çöz)"))
    panel.hybrid_stt.setToolTip(t(
        "Uyandırma bu bilgisayarda kalır; komut olan sözün sesi seçtiğin bulut sunucusuna "
        "gider. Değişiklik ses yeniden başlayınca uygulanır."
    ))
    stt_layout.addLayout(with_help(panel.hybrid_stt, t(
        "Uyandırma bu bilgisayarda kalır; komut olan sözün sesi seçtiğin bulut sunucusuna "
        "gider (gizlilik bedeli). Ses yeniden başlayınca uygulanır."
    )))
    panel.direct_audio_input = QCheckBox(t("Doğrudan sesli komut (sesi beyin modeline gönder)"))
    panel.direct_audio_input.setToolTip(t(
        "Uyandırma yerelde kalır; komut sesi ayrıca metne çevrilmeden doğrudan seçili "
        "beyin modeline gönderilir. Desteklemeyen modelde yerel metne düşer. Değişiklik "
        "ses yeniden başlayınca uygulanır."
    ))
    stt_layout.addLayout(with_help(panel.direct_audio_input, t(
        "Uyandırma yerelde kalır; komut sesi doğrudan seçili beyin modeline (ör. Gemini) "
        "gönderilir (gizlilik bedeli). Desteklemeyen modelde yerel metne düşer. Ses "
        "yeniden başlayınca uygulanır."
    )))
    panel.multimodal_screen_context = QCheckBox(
        t("Sesli komutta etkin ekranı paylaş (çok kipli)")
    )
    panel.multimodal_screen_context.setToolTip(
        t(
            "Yalnız Doğrudan sesli komut ve bulut ekran izni birlikte açıkken, "
            "komut anındaki ekran görüntüsü bellekte modele eklenir; diske yazılmaz. "
            "Gizli profilde kapalıdır."
        )
    )
    stt_layout.addWidget(panel.multimodal_screen_context)
    panel.multimodal_screen_hint = _check_hint(
        t(
            "Etkin ekranı bellekte gönderir; doğrudan sesli komut ve bulut ekran "
            "izni gerektirir, diske yazılmaz."
        )
    )
    stt_layout.addWidget(panel.multimodal_screen_hint)
    panel.cloud_box = QWidget()
    cloud_layout = QVBoxLayout(panel.cloud_box)
    cloud_layout.setContentsMargins(0, 0, 0, 0)
    panel.cloud_server = QComboBox()
    panel.cloud_server.setAccessibleName(t("Bulut tanıma sunucusu"))
    field(cloud_layout, t("Tanıma sunucusu"), panel.cloud_server)
    panel.cloud_protocol = QComboBox()
    panel.cloud_protocol.setAccessibleName(t("Bulut tanıma protokolü"))
    panel.cloud_protocol.addItem(t("OpenAI uyumlu (Whisper)"), "openai")
    panel.cloud_protocol.addItem("xAI Grok", "xai")
    field(cloud_layout, t("Protokol"), panel.cloud_protocol)
    panel.cloud_key = QLineEdit()
    panel.cloud_key.setAccessibleName(t("Bulut tanıma API anahtarı"))
    panel.cloud_key.setEchoMode(QLineEdit.EchoMode.Password)
    panel.cloud_key_button = QPushButton(t("Anahtarı kaydet"))
    panel.cloud_key_button.clicked.connect(panel._save_cloud_key)
    key_row = QHBoxLayout()
    key_row.setSpacing(8)
    key_row.addWidget(panel.cloud_key, 1)
    key_row.addWidget(panel.cloud_key_button)
    field(cloud_layout, t("API anahtarı"), key_row)
    panel.cloud_model = QComboBox()
    panel.cloud_model.setEditable(True)
    panel.cloud_model.setAccessibleName(t("Bulut tanıma modeli"))
    panel.cloud_fetch_button = QPushButton(t("Modelleri getir"))
    panel.cloud_fetch_button.clicked.connect(panel._fetch_cloud_models)
    model_row = QHBoxLayout()
    model_row.setSpacing(8)
    model_row.addWidget(panel.cloud_model, 1)
    model_row.addWidget(panel.cloud_fetch_button)
    field(cloud_layout, t("Bulut tanıma modeli"), model_row)
    panel._cloud_fetch = None
    panel._cloud_fetch_for = ""
    stt_layout.addWidget(panel.cloud_box)
    panel.cloud_stt_hint = meta_label("")
    stt_layout.addWidget(panel.cloud_stt_hint)
    panel.hybrid_stt.toggled.connect(panel._update_cloud_hint)
    panel.cloud_protocol.currentIndexChanged.connect(panel._update_cloud_hint)
    panel.cloud_server.activated.connect(panel._cloud_server_picked)
    panel.cloud_server.currentIndexChanged.connect(panel._update_cloud_hint)
    panel._update_cloud_hint()


def _speech_group(panel: SettingsPanel, voice: QVBoxLayout) -> None:
    speech_card, speech = group(t("Konuşma"), divided=False)
    voice.addWidget(speech_card)
    panel.tts_combo = QComboBox()
    panel.tts_combo.setAccessibleName(t("Konuşma sesi"))
    panel.tts_combo.setToolTip(t("Değişiklik ses yeniden başlayınca uygulanır."))
    panel.tts_combo.addItem(t("Edge · Emel (bulut, internet gerekir)"), "edge")
    panel.tts_combo.addItem(t("Piper (yerel)"), "piper")
    ema_missing = importlib.util.find_spec("ema_lightning") is None
    ema_text = (
        t("EMA (yerel, Türkçe) · kurulu değil") if ema_missing else t("EMA (yerel, Türkçe)")
    )
    panel.tts_combo.addItem(ema_text, "ema")
    if ema_missing:
        ema_index = panel.tts_combo.count() - 1
        panel.tts_combo.model().item(ema_index).setEnabled(False)  # type: ignore[attr-defined]
    panel.tts_combo.addItem(t("Bulut modeli (OpenRouter, Gemini…)"), "cloud")
    field(speech, t("Konuşma sesi"), panel.tts_combo, help=t(
        "Edge (bulut): yanıt metni konuşmaya çevrilmek üzere internete, bir bulut hizmetine "
        "gider. Piper ve EMA bu bilgisayarda çalışır, metin dışarı çıkmaz. Ses yeniden "
        "başlayınca uygulanır."
    ))
    panel.tts_piper_hint = meta_label("")
    speech.addWidget(panel.tts_piper_hint)

    def _update_piper_hint(*_args: object) -> None:
        panel.tts_piper_hint.setText(
            t("Piper sesi Türkçe; İngilizce yanıtlar için Edge ya da bulut ses seç.")
        )
        panel.tts_piper_hint.setVisible(
            current_language() == "en" and panel.tts_combo.currentData() == "piper"
        )

    _update_piper_hint()
    panel.tts_combo.currentIndexChanged.connect(_update_piper_hint)
    build_voice_output_controls(panel, speech)
    build_tts_cloud_box(panel, speech)
    panel.barge_in_box = QCheckBox(t("Konuşurken sesimle sustur"))
    speech.addWidget(panel.barge_in_box)
    panel.followup_box = QCheckBox(t("Yanıttan sonra adsız dinle"))
    speech.addWidget(panel.followup_box)
    speech.addWidget(_check_hint(t(
        "Cevap bitince ya da sesinle susturunca, bir süre uyandırma adı söylemeden dinler."
    )))
    panel.echo_cancellation = QCheckBox(t("Yankı engelleme"))
    panel.echo_cancellation.setToolTip(t(
        "WebRTC yankı giderici; Artemis'in hoparlörden çıkan kendi sesini mikrofondan "
        "siler, böylece konuşurken araya girmek ve kendi sesini komut sanmamak güvenilir "
        "olur. Yerelde çalışır. Kulaklıkla gerekmez. Değişiklik ses yeniden başlayınca "
        "uygulanır."
    ))
    speech.addWidget(panel.echo_cancellation)
    speech.addWidget(_check_hint(t("Hoparlörle kullanırken aç; kulaklıkta gerekmez.")))


def _cleanup_group(panel: SettingsPanel, voice: QVBoxLayout) -> None:
    clean_card, clean = group(t("Canlı yazı ve ses temizleme"))
    voice.addWidget(clean_card)
    panel.live_transcript = QCheckBox(t("Canlı yazı (konuşurken)"))
    panel.live_transcript.setToolTip(t(
        "Konuşurken metin pet balonunda akar; yalnız gösterimdir, komutu etkilemez. "
        "Ek bir bileşen kurulumu ister. Değişiklik ses yeniden başlayınca uygulanır."
    ))
    clean.addWidget(panel.live_transcript)
    clean.addWidget(
        meta_label(t("Yerel, işlemcide çalışır. Powered by mihu · mihu Community License"))
    )
    panel.noise_suppression = QCheckBox(t("Gürültü engelleme"))
    panel.noise_suppression.setToolTip(t(
        "DeepFilterNet3; işlemcide çalışır, ekran kartı gerekmez. Yankı gidermez. "
        "Model pakete dahildir; indirme gerekmez. "
        "Değişiklik ses yeniden başlayınca uygulanır."
    ))
    clean.addWidget(panel.noise_suppression)
    panel.game_mode = QCheckBox(t("Oyun modu"))
    panel.game_mode.setToolTip(t(
        "Tam ekran oyun öndeyken büyük tanıma modeli ekran kartı belleğinden bırakılır, "
        "hafif yerel model (Small) devralır; bulut hedefinde Small CPU'da yalnız uyandırmayı "
        "dinler, komut buluta gider (Small gerekir). Oyundan çıkınca "
        "büyük model geri yüklenir. Değişiklik ses yeniden başlayınca uygulanır."
    ))
    clean.addWidget(panel.game_mode)
    panel.game_mode_target = QComboBox()
    panel.game_mode_target.addItem(t("Hafif yerel model (Small)"), "local_light")
    panel.game_mode_target.addItem(t("Bulut komut + yerel uyandırma (VRAM boşalır)"), "cloud")
    field(
        clean,
        t("Oyun modu hedefi"),
        panel.game_mode_target,
        t(
            "Bulut seçilirse tam ekran oyunda büyük model bırakılır; "
            "Ses tanıma bölümündeki bulut sunucusu ayarları kullanılır."
        ),
    )
    panel.game_mode.toggled.connect(panel._update_cloud_hint)
    panel.game_mode_target.currentIndexChanged.connect(panel._update_cloud_hint)
    panel._update_cloud_hint()
    panel.advanced_toggle = QToolButton()
    panel.advanced_toggle.setText(t("Gelişmiş ses ayarları"))
    panel.advanced_toggle.setCheckable(True)
    panel.advanced_toggle.setChecked(False)
    panel.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    panel.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
    voice.addWidget(divider())
    voice.addWidget(panel.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)
    panel.advanced_box, advanced_layout = group(t("Gelişmiş ses ayarları"), divided=False)
    panel.noise_level = QComboBox()
    for label, key in ((t("Hafif"), "hafif"), (t("Orta"), "orta"), (t("Güçlü"), "guclu")):
        panel.noise_level.addItem(label, key)
    panel.noise_level.setAccessibleName(t("Gürültü engelleme düzeyi"))
    panel.noise_level.setToolTip(t(
        "Düzey bastırma sınırıdır; algılamayı etkiler. Tanıma varsayılan olarak ham sesi alır."
    ))
    panel.noise_level.setEnabled(False)
    panel.noise_suppression.toggled.connect(panel.noise_level.setEnabled)
    field(advanced_layout, t("Gürültü engelleme düzeyi"), panel.noise_level)
    panel.noise_stt = QCheckBox(t("Tanımaya da temizlenmiş ses ver"))
    panel.noise_stt.setToolTip(t(
        "Kapalıyken (önerilen) temizlenmiş ses yalnız konuşma algılamaya, cümle bitişine ve "
        "söz kesmeye gider; konuşma tanıma aynı aralığın ham sesini alır. Ölçümlerde Whisper "
        "temizlenmiş sesle çoğunlukla daha kötü sonuç verdi."
    ))
    panel.noise_stt.setEnabled(False)
    panel.noise_suppression.toggled.connect(panel.noise_stt.setEnabled)
    advanced_layout.addWidget(panel.noise_stt)
    panel.advanced_devices = QCheckBox(t("Sanal ve diğer ses girişlerini göster"))
    panel.advanced_devices.toggled.connect(panel._toggle_device_detail)
    advanced_layout.addWidget(panel.advanced_devices)
    panel.refresh_button = QPushButton(t("Mikrofon listesini yenile"))
    panel.refresh_button.clicked.connect(panel.refresh)
    advanced_layout.addLayout(button_row(panel.refresh_button))
    voice.addWidget(panel.advanced_box)
    panel.advanced_box.setVisible(False)
    panel.stt_combo.currentIndexChanged.connect(panel._stt_selection_changed)
    panel.stt_download.clicked.connect(panel._download_stt)
    panel._populate_stt_choices(None)
    panel.advanced_toggle.toggled.connect(panel._toggle_advanced)


def build_shortcuts_page(panel: SettingsPanel) -> None:
    """2. sayfa: Dikte ve Kısayollar."""
    page = add_page(
        panel,
        t("Dikte ve Kısayollar"),
        t("Genel ses kısayolu, dikte ve seçili metin eylemleri."),
    )
    voice_box, voice_hk = group(t("Genel ses kısayolu"), divided=False)
    page.addWidget(voice_box)
    panel.listening_hotkey = QLineEdit()
    panel.listening_hotkey.setMaxLength(40)
    panel.listening_hotkey.setAccessibleName(t("Ses kısayolu"))
    panel.listening_hotkey.setPlaceholderText("ctrl+space")
    field(
        voice_hk,
        t("Ses kısayolu"),
        panel.listening_hotkey,
        t(
            "Mikrofonu açıp kapatır (boş bırakılırsa kapalı). Dikte ve seçili metin "
            "kısayolundan farklı olmalı."
        ),
    )

    dict_box, dict_layout = group(t("Dikte"))
    page.addWidget(dict_box)
    panel.dictation_enabled = QCheckBox(t("Dikteyi kullan"))
    dict_layout.addWidget(panel.dictation_enabled)
    dict_layout.addWidget(_check_hint(t(
        "Kısayola basıp konuşun, tekrar basın: metin odaktaki uygulamaya yazılır. "
        "Komut değildir, Artemis'e gitmez. Ses açıkken çalışmaz. Esc yazımı durdurur."
    )))
    panel.dictation_hotkey = QLineEdit()
    panel.dictation_hotkey.setMaxLength(40)
    panel.dictation_hotkey.setAccessibleName(t("Dikte kısayolu"))
    panel.dictation_hotkey.setPlaceholderText(t("Örneğin: ctrl+alt+d"))
    field(dict_layout, t("Dikte tuşu"), panel.dictation_hotkey)
    panel.dictation_max_seconds = _SpinBox()
    panel.dictation_max_seconds.setRange(1, 600)
    panel.dictation_max_seconds.setValue(120)
    panel.dictation_max_seconds.setAccessibleName(t("Dikte üst süresi"))
    panel.dictation_max_seconds.setToolTip(t(
        "İkinci kez kısayola basmadan kaydın kendiliğinden duracağı üst süre (1–600 sn)."
    ))
    field(dict_layout, t("Dikte üst süresi (sn)"), panel.dictation_max_seconds)
    panel.dictation_polish = QCheckBox(t("Diktede noktalama ve büyük harfi düzelt"))
    dict_layout.addLayout(
        with_help(
            panel.dictation_polish,
            t(
                "Açıkken tanınan metin yazılmadan önce tek adımlı, araçsız model çağrısıyla "
                "noktalama ve büyük harf için düzeltilir; konuşma geçmişine girmez. Gizli "
                "çalışma profilinde yalnız yerel model kullanılır."
            ),
        )
    )
    dict_layout.addWidget(_check_hint(t(
        "İsteğe bağlıdır (varsayılan kapalı). Hata veya gizlilik kısıtında ham dikte "
        "metni korunur."
    )))

    sel_box, sel_layout = group(t("Seçili metin eylemleri"))
    page.addWidget(sel_box)
    panel.selection_enabled = QCheckBox(t("Seçili metin eylemlerini kullan"))
    sel_layout.addWidget(panel.selection_enabled)
    sel_layout.addWidget(_check_hint(t(
        "Bir uygulamada metin seçip kısayola basın: Çevir, Düzelt, Özetle ya da Açıkla. "
        "Seçimi almak için pano geçici kullanılır ve eski metin geri konur; metin dışı pano "
        "içeriği (resim, dosya) geri konamaz. Metin modele araçsız gider, geçmişe girmez. "
        "Pano için bulut izni yoksa yalnız yerel model kullanılır."
    )))
    panel.selection_hotkey = QLineEdit()
    panel.selection_hotkey.setMaxLength(40)
    panel.selection_hotkey.setAccessibleName(t("Seçili metin kısayolu"))
    panel.selection_hotkey.setPlaceholderText(t("Örneğin: ctrl+alt+s"))
    field(sel_layout, t("Seçili metin tuşu"), panel.selection_hotkey)
    panel.selection_translate_to = QLineEdit()
    panel.selection_translate_to.setMaxLength(40)
    panel.selection_translate_to.setAccessibleName(t("Çeviri dili"))
    panel.selection_translate_to.setPlaceholderText(t("Örneğin: en"))
    field(sel_layout, t("Çeviri dili"), panel.selection_translate_to)
    page.addStretch(1)


def build_model_page(
    panel: SettingsPanel, pets_dir: Path | None, codex_pets_dir: Path | None
) -> None:
    """3. sayfa: Model ve Bağlantılar (wide=True; PetSection ayrı sekmede)."""
    config_path = panel.config_path
    demo = panel._demo
    model_page = add_page(
        panel, t("Model ve Bağlantılar"), t("Beyin modelini seç ve bağlantıları yönet."), wide=True
    )
    panel.providers = ProviderSection(config_path, demo=demo)
    panel.providers.changed.connect(panel._load_cloud_servers)
    panel.providers.changed.connect(panel.saved_notes[t("Model ve Bağlantılar")].flash)
    panel.providers.staged.connect(panel._mark_dirty)
    panel._page_tabs["Model ve Bağlantılar"] = panel.providers.tabs
    model_page.addWidget(panel.providers)
    model_page.addStretch(1)

    panel.pet = PetSection(
        pets_dir if pets_dir is not None else pets_dir_for(config_path),
        codex_pets_dir,
        demo=demo,
        config_path=config_path,
    )


def build_look_page(panel: SettingsPanel) -> None:
    """4. sayfa: Görünüm."""
    look = add_page(panel, t("Görünüm"), t("Arayüz renkleri."))
    theme_box, theme = group(t("Tema"), divided=False)
    look.addWidget(theme_box)
    panel.theme_combo = QComboBox(panel)
    panel.theme_combo.setAccessibleName(t("Arayüz teması"))
    panel.theme_combo.setVisible(False)
    for key, label in THEME_LABELS.items():
        panel.theme_combo.addItem(t(label, context="theme"), key)
    set_i18n_context(panel.theme_combo, "theme")
    panel.theme_combo.setCurrentIndex(panel.theme_combo.findData(DEFAULT_THEME))
    panel.theme_cards: dict[str, _ThemeCard] = {}
    theme_group = QButtonGroup(panel)
    panel._theme_grid = QGridLayout()
    panel._theme_grid.setSpacing(12)
    for key, label in THEME_LABELS.items():
        theme_card = _ThemeCard(key, t(label, context="theme"))
        theme_card.radio.setProperty("theme", key)
        theme_card.radio.toggled.connect(panel._theme_picked)
        theme_group.addButton(theme_card.radio)
        panel.theme_cards[key] = theme_card
    theme.addLayout(panel._theme_grid)
    panel._theme_columns = 0
    panel._layout_theme_cards(2)
    panel.theme_combo.currentIndexChanged.connect(panel._emit_theme)
    panel.theme_combo.currentIndexChanged.connect(panel._sync_theme_cards)
    panel.theme_combo.currentIndexChanged.connect(panel.saved_notes[t("Görünüm")].flash)
    panel._sync_theme_cards()
    lang_box, lang = group(t("Dil"))
    panel.language_combo = QComboBox()
    panel.language_combo.setAccessibleName(t("Arayüz dili"))
    panel.language_combo.addItem(t("Türkçe"), "tr")
    panel.language_combo.addItem("English", "en")
    field(
        lang,
        t("Arayüz dili"),
        panel.language_combo,
        hint=t("Arayüz hemen değişir; bazı durum metinleri bir sonraki güncellemede değişir."),
    )
    look.addWidget(lang_box)
    look.addStretch(1)
