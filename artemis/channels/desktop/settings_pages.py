"""Ayarlar paneli sayfaları (SettingsPanel'den ayrıldı): kabuk, çerçeve ve sayfa kurulumları.

Davranış SettingsPanel'dedir; burası yalnız widget'ları kurar ve `panel` üzerindeki alanlara
bağlar.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    QListWidget,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.calendar_mail_section import CalendarMailSection
from artemis.channels.desktop.file_search_section import FileSearchSection
from artemis.channels.desktop.form_kit import button_row, divider, field, group, meta_label
from artemis.channels.desktop.page_kit import CurrentPageStack
from artemis.channels.desktop.pet_section import PetSection
from artemis.channels.desktop.pet_sprite import pets_dir_for
from artemis.channels.desktop.provider_section import ProviderSection
from artemis.channels.desktop.routines_section import RoutinesSection
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
from artemis.channels.desktop.skills_section import SkillsSection
from artemis.channels.desktop.telegram_section import TelegramSection
from artemis.channels.desktop.themes import DEFAULT_THEME, THEME_LABELS
from artemis.channels.desktop.triggers_section import TriggersSection
from artemis.channels.desktop.tts_cloud_box import build_tts_cloud_box
from artemis.channels.desktop.update_section import UpdateSection
from artemis.channels.desktop.widgets import HelpTip, with_help
from artemis.config.settings import BUILTIN_AGENT
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.settings_panel import SettingsPanel

_HINT_MAX_WIDTH = 640  # onay kutusu açıklaması bundan geniş satıra uzamaz
_REVERT_TOOLTIP = (
    "Ayarlar açıldığındaki kayıtlı hâline döner. "
    "API anahtarları ve kasadaki parolalar geri alınmaz."
)


def default_repos_dir() -> str:
    return str(Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop" / "Projeler")


def _indent(box: QCheckBox, help: str = "") -> QHBoxLayout:  # noqa: A002
    """Bağlı seçenek: ana kutunun altında, gösterge genişliği kadar girintili; `help` kutunun
    hemen sağına "?" koyar."""
    row = QHBoxLayout()
    row.setContentsMargins(24, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(box)
    if help:
        row.addWidget(HelpTip(help), 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
    return row


def _check_hint(text: str) -> QLabel:
    """Onay kutusunun altındaki 12px soluk açıklama; kutu metni uzayıp satırı taşırmasın diye
    ayrıntı buraya (sarılabilir) konur. Girinti = gösterge 16px + boşluk 8px."""
    label = meta_label(text)
    label.setContentsMargins(24, 0, 0, 0)
    label.setMaximumWidth(_HINT_MAX_WIDTH)  # tek satırda 850px uzamasın; okunur genişlikte sarılır
    return label


def build_shell(panel: SettingsPanel) -> None:
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(24, 0, 24, 24)
    layout.setSpacing(12)
    title = QLabel(t("Ayarlar"))
    title.setObjectName("pageTitle")
    title.setFixedHeight(40)
    # Durum yazısı ve geri alma düğmesi başlıkla aynı satırda, sağda: alt çubuk yok.
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
    # Bölüm listesi kaydırma alanının dışında: uzun sayfayı kaydırınca liste yerinde kalır.
    # Yalnız sayfa kaydırılır; liste kendi kaydırıcısını yalnız pencere kısaysa gösterir.
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
    # Dar panelde bölüm seçici listenin yerini alır ve sayfalar 720px pencerede sığar
    # (tests/test_ux_narrow.py); yatay çubuk yalnız kırpılan içeriği saklardı, o yüzden kapalı.
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(panel.stack)
    panel.sections.scroll_area = scroll
    # Dar panelde listenin yerini alan seçici; yapı (grup başlıkları) listeyle aynıdır.
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
    """Bölüm sayfası: 880px'i aşmayan sütun, solda; tek satır açıklama. Sayfa adı
    bölüm listesinde zaten seçili görünür, sayfada tekrarlanmaz. `group_title`
    grubun ilk sayfasında grup başlığını açar."""
    outer = QWidget()
    row = QHBoxLayout(outer)
    row.setContentsMargins(0, 0, 0, 0)
    column = QWidget()
    # Model sayfası liste + ayrıntıyı yan yana koyar; bloğun bütün sayfa alanını kullanır.
    column.setMaximumWidth(16777215 if wide else 880)
    content = QVBoxLayout(column)
    content.setContentsMargins(0, 0, 0, 0)
    content.setSpacing(24)
    row.addWidget(column, 1)
    row.addStretch(0)
    # Anında kaydedilen sayfalar açıklamanın sağında kısa "Kaydedildi" gösterir.
    saved = _SavedNote()
    panel.saved_notes[name] = saved
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
    voice = add_page(panel, t("Ses"), t("Uyandırma, tanıma ve konuşma."), t("Temel"))
    _wake_group(panel, voice)
    _stt_group(panel, voice)
    _speech_group(panel, voice)
    _cleanup_group(panel, voice)


def _wake_group(panel: SettingsPanel, voice: QVBoxLayout) -> None:
    # Uyandırma ve mikrofon
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
    panel._no_device()  # demo'da ve yenilemeden önce boş açılır liste görünmesin
    field(wake, t("Mikrofon girişi"), panel.device_combo)
    panel.current_device = QLabel(t("Seçilmedi"))
    panel.current_device.setWordWrap(True)
    field(wake, t("Kullanılan mikrofon"), panel.current_device)
    # Etiketli seviye ölçeri: düğmeye benzeyen ortalı yazı yerine ince çubuk + değer.
    panel.input_level = QProgressBar()
    panel.input_level.setObjectName("levelMeter")
    panel.input_level.setRange(0, 100)
    panel.input_level.setValue(0)
    panel.input_level.setTextVisible(False)
    panel.input_level.setFixedHeight(6)
    panel.input_level.setAccessibleName(t("Ses girişi seviyesi"))
    panel.level_value = QLabel(t("%{value}").format(value=0))
    panel.level_value.setObjectName("meta")
    # "%100" sığmalı: sabit genişlik satırın içinde kalır, dar pencerede kırpılmaz.
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


def _stt_group(panel: SettingsPanel, voice: QVBoxLayout) -> None:
    # Tanıma
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
    # Konuşma
    speech_card, speech = group(t("Konuşma"))
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
    build_tts_cloud_box(panel, speech)
    panel.continuous_mode = QCheckBox(t("Kesintisiz sohbet"))
    speech.addLayout(with_help(panel.continuous_mode, t(
        "Uyandırma adı söylemeden sürekli dinler. Karma tanıma açıksa kesintisiz kipte her söz "
        "bulut sunucusuna gider."
    )))
    speech.addWidget(_check_hint(t("Uyandırma adı söylemeden sürekli dinler.")))
    panel.dictation_enabled = QCheckBox(t("Dikteyi kullan"))
    speech.addWidget(panel.dictation_enabled)
    speech.addWidget(_check_hint(t(
        "Kısayola basıp konuşun, tekrar basın: metin odaktaki uygulamaya yazılır. "
        "Komut değildir, Artemis'e gitmez. Ses açıkken çalışmaz. Esc yazımı durdurur."
    )))
    panel.dictation_hotkey = QLineEdit()
    panel.dictation_hotkey.setMaxLength(40)
    panel.dictation_hotkey.setAccessibleName(t("Dikte kısayolu"))
    panel.dictation_hotkey.setPlaceholderText(t("Örneğin: ctrl+alt+d"))
    field(speech, t("Dikte tuşu"), panel.dictation_hotkey)
    panel.selection_enabled = QCheckBox(t("Seçili metin eylemlerini kullan"))
    speech.addWidget(panel.selection_enabled)
    speech.addWidget(_check_hint(t(
        "Bir uygulamada metin seçip kısayola basın: Çevir, Düzelt, Özetle ya da Açıkla. "
        "Seçimi almak için pano geçici kullanılır ve eski metin geri konur; metin dışı pano "
        "içeriği (resim, dosya) geri konamaz. Metin modele araçsız gider, geçmişe girmez. "
        "Pano için bulut izni yoksa yalnız yerel model kullanılır."
    )))
    panel.selection_hotkey = QLineEdit()
    panel.selection_hotkey.setMaxLength(40)
    panel.selection_hotkey.setAccessibleName(t("Seçili metin kısayolu"))
    panel.selection_hotkey.setPlaceholderText(t("Örneğin: ctrl+alt+s"))
    field(speech, t("Seçili metin tuşu"), panel.selection_hotkey)
    panel.selection_translate_to = QLineEdit()
    panel.selection_translate_to.setMaxLength(40)
    panel.selection_translate_to.setAccessibleName(t("Çeviri dili"))
    panel.selection_translate_to.setPlaceholderText(t("Örneğin: en"))
    field(speech, t("Çeviri dili"), panel.selection_translate_to)
    panel.barge_in_box = QCheckBox(t("Konuşurken sesimle sustur"))
    speech.addWidget(panel.barge_in_box)
    panel.followup_box = QCheckBox(t("Yanıttan sonra adsız dinle"))
    speech.addWidget(panel.followup_box)
    speech.addWidget(_check_hint(t(
        "Cevap bitince ya da sesinle susturunca, bir süre uyandırma adı söylemeden dinler."
    )))


def _cleanup_group(panel: SettingsPanel, voice: QVBoxLayout) -> None:
    # Ses temizleme ve canlı yazı
    clean_card, clean = group(t("Canlı yazı ve ses temizleme"))
    voice.addWidget(clean_card)
    panel.live_transcript = QCheckBox(t("Canlı yazı (konuşurken)"))
    panel.live_transcript.setToolTip(t(
        "Konuşurken metin pet balonunda akar; yalnız gösterimdir, komutu etkilemez. "
        "Ek bir bileşen kurulumu ister. Değişiklik ses yeniden başlayınca uygulanır."
    ))
    clean.addWidget(panel.live_transcript)
    # Lisans atfı (mihu Community License): "Powered by mihu" arayüzde görünür kalmalı.
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
    panel.echo_cancellation = QCheckBox(t("Yankı engelleme"))
    panel.echo_cancellation.setToolTip(t(
        "WebRTC yankı giderici; Artemis'in hoparlörden çıkan kendi sesini mikrofondan "
        "siler, böylece konuşurken araya girmek ve kendi sesini komut sanmamak güvenilir "
        "olur. Yerelde çalışır. Kulaklıkla gerekmez. Değişiklik ses yeniden başlayınca "
        "uygulanır."
    ))
    clean.addWidget(panel.echo_cancellation)
    clean.addWidget(_check_hint(t("Hoparlörle kullanırken aç; kulaklıkta gerekmez.")))
    panel.advanced_toggle = QToolButton()
    panel.advanced_toggle.setText(t("Gelişmiş ses ayarları"))
    panel.advanced_toggle.setCheckable(True)
    panel.advanced_toggle.setChecked(False)
    panel.advanced_toggle.setToolButtonStyle(
        Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    )
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
    voice.addStretch(1)
    panel.stt_combo.currentIndexChanged.connect(panel._stt_selection_changed)
    panel.stt_download.clicked.connect(panel._download_stt)
    panel._populate_stt_choices(None)
    panel.advanced_toggle.toggled.connect(panel._toggle_advanced)


def build_model_page(
    panel: SettingsPanel, pets_dir: Path | None, codex_pets_dir: Path | None
) -> None:
    config_path = panel.config_path
    demo = panel._demo
    # Model: bağlantılar ve modeller ProviderSection'dadır.
    model_page = add_page(
        panel, t("Model"), t("Beyin modelini seç ve bağlantıları yönet."), wide=True
    )
    panel.providers = ProviderSection(config_path, demo=demo)
    panel.providers.changed.connect(panel._load_cloud_servers)
    panel.providers.changed.connect(panel.saved_notes[t("Model")].flash)
    model_page.addWidget(panel.providers)
    model_page.addStretch(1)

    # Pet bölümü ayarlarda gösterilmez; app.py kendi sayfasında barındırır.
    panel.pet = PetSection(
        pets_dir if pets_dir is not None else pets_dir_for(config_path),
        codex_pets_dir,
        demo=demo,
        config_path=config_path,
    )


def build_look_page(panel: SettingsPanel) -> None:
    look = add_page(panel, t("Görünüm"), t("Arayüz renkleri."))
    theme_box, theme = group(t("Tema"), divided=False)
    look.addWidget(theme_box)
    # app.py ve testler bu seçiciyi okur/yazar; kartlar ona bağlı, kendisi görünmez.
    panel.theme_combo = QComboBox(panel)
    panel.theme_combo.setAccessibleName(t("Arayüz teması"))
    panel.theme_combo.setVisible(False)
    for key, label in THEME_LABELS.items():
        panel.theme_combo.addItem(t(label), key)
    panel.theme_combo.setCurrentIndex(panel.theme_combo.findData(DEFAULT_THEME))
    panel.theme_cards: dict[str, _ThemeCard] = {}
    theme_group = QButtonGroup(panel)
    panel._theme_grid = QGridLayout()
    panel._theme_grid.setSpacing(12)
    for key, label in THEME_LABELS.items():
        theme_card = _ThemeCard(key, t(label))
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
        hint=t("Değişiklik Artemis yeniden başlatılınca geçerli olur."),
    )
    look.addWidget(lang_box)
    look.addStretch(1)


def build_routines_page(panel: SettingsPanel) -> None:
    config_path = panel.config_path
    demo = panel._demo
    routines_page = add_page(
        panel,
        t("Rutinler"),
        t("Adlandırılmış, sabit adımlı işler (örn. “işe başla”)."),
        t("Otomasyon"),
    )
    panel.proactive_enabled = QCheckBox(t("Zamanlı işler"))
    routines_page.addWidget(panel.proactive_enabled)
    routines_page.addWidget(_check_hint(t(
        "Hatırlatmalar, rutin saatleri, tetikleyiciler ve gün özeti bunun açık olmasını ister."
    )))
    panel.routines_enabled = QCheckBox(t("Rutinleri kullan"))
    routines_page.addWidget(panel.routines_enabled)
    routines_page.addWidget(_check_hint(t(
        "Sesle, yazıyla veya Otomasyon sayfasından çalıştırılır. Her adım kendi onay "
        "kurallarıyla yürür; onaylanmayan ya da başarısız ilk adımda durur."
    )))
    panel.routines = RoutinesSection(config_path, demo=demo)
    routines_page.addWidget(panel.routines)
    routines_page.addStretch(1)


def build_triggers_page(panel: SettingsPanel) -> None:
    config_path = panel.config_path
    demo = panel._demo
    triggers_page = add_page(
        panel,
        t("Tetikleyiciler"),
        t("Bir klasöre yeni dosya gelince Artemis haber verir."),
    )
    panel.triggers_enabled = QCheckBox(t("Tetikleyicileri kullan"))
    triggers_page.addWidget(panel.triggers_enabled)
    triggers_page.addWidget(_check_hint(t(
        "Klasör yoklanır; ilk bakışta var olan dosyalar bildirilmez. Dosya yazılması bitince "
        "bir kez haber verilir. Dosya adları Telegram'a gönderilmez. Zamanlı işler açık olmalı."
    )))
    panel.triggers = TriggersSection(config_path, demo=demo)
    triggers_page.addWidget(panel.triggers)
    triggers_page.addStretch(1)
    panel.triggers.saved.connect(panel._triggers_saved)
    panel.triggers.proactive_requested.connect(panel._tick_proactive)


def build_summary_page(panel: SettingsPanel) -> None:
    summary_page = add_page(
        panel,
        t("Gün özeti"),
        t("Belirlenen saatte günün kısa bir kaydını yazar."),
    )
    panel.daily_summary_enabled = QCheckBox(t("Gün özetini kullan"))
    summary_page.addLayout(with_help(panel.daily_summary_enabled, t(
        "Belirlenen saatte yalnız sayılardan (araç, dosya, iş, hatırlatma) kısa bir özet "
        "yazar; model çağrılmaz, konuşma okunmaz, Telegram'a gitmez. Vault'a yalnız "
        "bildirimdeki düğmeyle, onayla eklenir."
    )))
    summary_page.addWidget(_check_hint(t(
        "Konuşma metni okunmaz; yalnız bugünün araç, dosya, iş ve hatırlatma sayıları "
        "Artemis'in veri klasöründeki günlük klasörüne yazılır. Telegram'a gitmez. Vault'a "
        "ekleme yalnız bildirimdeki düğmeyle ve onayla olur. Zamanlı işler açık olmalı."
    )))
    panel.daily_summary_at = QLineEdit()
    panel.daily_summary_at.setMaxLength(5)
    panel.daily_summary_at.setAccessibleName(t("Özet saati"))
    panel.daily_summary_at.setPlaceholderText("22:30")
    field(summary_page, t("Özet saati (SS:DD)"), panel.daily_summary_at)
    summary_page.addStretch(1)


def build_research_page(panel: SettingsPanel) -> None:
    research_page = add_page(
        panel,
        t("Araştırma"),
        t("Bir konuyu arka planda web aramasıyla araştırıp kaynaklı rapor yazdırır."),
    )
    panel.research_enabled = QCheckBox(t("Araştırma işlerini kullan"))
    research_page.addLayout(with_help(panel.research_enabled, t(
        "Konuyu yalnız web aramasıyla araştırıp kaynaklı rapor yazar. Her araştırma başlamadan "
        "onay sorulur (varsayılan en çok 12 arama). İş sohbet geçmişini, hafızayı ve notlarını "
        "görmez."
    )))
    research_page.addWidget(_check_hint(t(
        "Her araştırma başlamadan önce onay sorulur; onay en fazla 12 web araması içindir. "
        "İş sohbet geçmişini, hafızayı ve notlarını görmez. Rapor Artemis'in veri "
        "klasöründeki araştırma klasörüne yazılır, kasaya kendiliğinden eklenmez."
    )))
    research_page.addStretch(1)


def build_telegram_page(panel: SettingsPanel) -> None:
    config_path = panel.config_path
    demo = panel._demo
    telegram_page = add_page(
        panel,
        "Telegram",
        t("Bot token'ı ve sohbet eşleştirme."),
        t("Bağlantılar"),
    )
    panel.telegram = TelegramSection(config_path, demo=demo)
    panel.telegram.changed.connect(panel.saved_notes["Telegram"].flash)
    panel.telegram.channel_changed.connect(panel.saved_notes["Telegram"].flash)
    telegram_page.addWidget(panel.telegram)
    panel.telegram_voice = QCheckBox(t("Ses mesajlarını kabul et"))
    panel.telegram_photo = QCheckBox(t("Fotoğrafları kabul et"))
    panel.telegram_files = QCheckBox(t("Dosyaları kabul et"))
    telegram_page.addLayout(with_help(panel.telegram_voice, t(
        "Ses, fotoğraf ve dosya kabulü varsayılan kapalıdır; açınca bu içerik bu bilgisayarda "
        "işlenir (ses yazıya, fotoğraf yerel OCR ile okunur, dosya çalışma alanına kaydedilir, "
        "açılmaz). Ses onay yerine geçmez; onay yalnız düğmeyle verilir."
    )))
    telegram_page.addWidget(panel.telegram_photo)
    telegram_page.addWidget(panel.telegram_files)
    telegram_page.addWidget(_check_hint(t(
        "Ses yazıya çevrilir ve komut olarak çalışır "
        "(en çok 1 MB, 60 sn); fotoğraftaki metin yerel OCR ile okunur ve gizli sayılır; "
        "dosyalar çalışma alanındaki Telegram klasörüne kaydedilir, açılmaz. İletilmiş "
        "mesajlar yine çalışmaz."
    )))
    telegram_page.addStretch(1)


def build_calendar_mail_page(panel: SettingsPanel) -> None:
    config_path = panel.config_path
    demo = panel._demo
    cal_page = add_page(
        panel,
        t("Takvim ve posta"),
        t("Takvimi ve okunmamış postayı salt okunur oku (sorular ve sabah brifingi için)."),
    )
    takvim_box, takvim = group(t("Takvim"), divided=False)
    cal_page.addWidget(takvim_box)
    panel.calendar_enabled = QCheckBox(t("Takvimi kullan"))
    takvim.addWidget(panel.calendar_enabled)
    panel.calendar_cloud = QCheckBox(t("Takvim içeriği bulut modele ve Telegram'a gidebilsin"))
    takvim.addLayout(_indent(panel.calendar_cloud, t(
        "Kapalıyken takvim içeriği yalnız yerel modele gider. Açıkken bulut modele ve "
        "Telegram'a da gidebilir; dışarı çıkan veri artar."
    )))
    panel.calendar_enabled.toggled.connect(panel.calendar_cloud.setEnabled)
    panel.calendar_cloud.setEnabled(panel.calendar_enabled.isChecked())
    posta_box, posta = group(t("Posta"))
    cal_page.addWidget(posta_box)
    panel.mail_enabled = QCheckBox(t("Postayı kullan"))
    posta.addWidget(panel.mail_enabled)
    panel.mail_cloud = QCheckBox(t("Posta içeriği bulut modele ve Telegram'a gidebilsin"))
    posta.addLayout(_indent(panel.mail_cloud, t(
        "Kapalıyken posta içeriği yalnız yerel modele gider. Açıkken bulut modele ve "
        "Telegram'a da gidebilir; dışarı çıkan veri artar."
    )))
    panel.mail_enabled.toggled.connect(panel.mail_cloud.setEnabled)
    panel.mail_cloud.setEnabled(panel.mail_enabled.isChecked())
    posta.addWidget(_check_hint(t(
        "Kapalıyken içerik yalnız yerel modele gider. Posta okundu işaretlenmez, hiçbir "
        "ileti silinmez ya da taşınmaz. Eki indirilmez."
    )))
    panel.mail_host = QLineEdit()
    panel.mail_host.setAccessibleName(t("IMAP sunucusu"))
    panel.mail_host.setPlaceholderText("imap.gmail.com")
    field(posta, t("IMAP sunucusu"), panel.mail_host)
    panel.mail_port = QLineEdit()
    panel.mail_port.setAccessibleName(t("IMAP portu"))
    field(posta, t("Port"), panel.mail_port)
    panel.mail_username = QLineEdit()
    panel.mail_username.setAccessibleName(t("Posta kullanıcı adı"))
    field(posta, t("Kullanıcı adı"), panel.mail_username)
    panel.mail_folder = QLineEdit()
    panel.mail_folder.setAccessibleName(t("Posta klasörü"))
    field(posta, t("Klasör"), panel.mail_folder)
    # Takvim adresi ve posta parolası kendi kutularına (yukarıdaki iki grup) eklenir.
    panel.calendar_mail = CalendarMailSection(
        config_path, demo=demo, calendar=takvim, mail=posta
    )
    cal_page.addWidget(panel.calendar_mail)
    cal_page.addStretch(1)


def build_github_page(panel: SettingsPanel) -> None:
    github = add_page(
        panel,
        "GitHub",
        t("Kendi repolarını kod ajanının erişimine bağla ve hangi ajanın çalışacağını seç."),
    )
    account_box, account = group(t("Hesap"), divided=False)
    github.addWidget(account_box)
    panel.github_owner = QLineEdit()
    panel.github_owner.setAccessibleName(t("GitHub kullanıcı adı"))
    panel.github_owner.setPlaceholderText(t("kullanıcı-adı"))
    panel.repos_dir = QLineEdit()
    panel.repos_dir.setAccessibleName(t("Repo klasörü"))
    panel.repos_dir.setPlaceholderText(default_repos_dir())
    panel.repos_browse = QPushButton(t("Klasör seç"))
    panel.repos_browse.setMinimumWidth(88)
    panel.repos_browse.clicked.connect(panel._browse_repos_dir)
    folder_row = QHBoxLayout()
    folder_row.setSpacing(8)
    folder_row.addWidget(panel.repos_dir, 1)
    folder_row.addWidget(panel.repos_browse)
    field(account, t("Kullanıcı adı"), panel.github_owner)
    field(account, t("Repo klasörü"), folder_row)
    panel.github_auto = QCheckBox(t("Otomatik eşitle"))
    panel.github_auto.setChecked(True)
    account.addWidget(panel.github_auto)
    panel.github_button = QPushButton(t("Bağla ve repoları çek"))
    panel.github_button.clicked.connect(panel._bind_github)
    account.addLayout(button_row(panel.github_button))
    result_box, result = group(t("Eşitleme"))
    github.addWidget(result_box)
    panel.github_status = meta_label(t(
        "Kendi repoların (fork ve arşivlenmişler hariç) kod ajanının erişimine girer. "
        "Giriş GitHub CLI (gh) ile yapılır; Artemis anahtarını görmez."
    ))
    result.addWidget(panel.github_status)
    panel.github_list = QListWidget()
    panel.github_list.setMaximumHeight(144)
    panel.github_list.setAccessibleName(t("GitHub eşitleme sonuçları"))
    result.addWidget(panel.github_list)
    # Kod ajanları GitHub sayfasında: aynı `code_agent` config bölümü ve aynı "kod ajanı"
    # bağlamı; Uygulamalar sayfası açılacak masaüstü uygulamalarını yönetir. Düz grup,
    # Uygulamalar listesiyle aynı kirli-durum/Uygula akışı (ikinci kaydetme yolu yok).
    agents_box, agents = group(
        t("Kod ajanları"),
        t("Arka plan proje işinde çalışacak ajanlar."),
    )
    github.addWidget(agents_box)
    panel.agent_list = QListWidget()
    panel.agent_list.setMaximumHeight(144)
    panel.agent_list.setAccessibleName(t("Kod ajanları"))
    agents.addWidget(panel.agent_list)
    panel.default_agent_combo = QComboBox()
    panel.default_agent_combo.setAccessibleName(t("Varsayılan kod ajanı"))
    field(
        agents,
        t("Varsayılan ajan"),
        panel.default_agent_combo,
        t("Projede ayrıca seçilmediyse bu ajan çalışır."),
    )
    panel.add_custom_agent_button = QPushButton(t("Özel ajan ekle…"))
    panel.add_custom_agent_button.clicked.connect(panel._add_custom_agent)
    agents.addLayout(button_row(panel.add_custom_agent_button))
    panel.agent_hint = meta_label("")
    agents.addWidget(panel.agent_hint)
    panel.code_agents: dict[str, dict[str, Any]] = {}
    panel.default_agent = BUILTIN_AGENT
    panel._builtin_command = "claude"
    panel._refresh_code_agents()
    panel.default_agent_combo.activated.connect(panel._default_agent_picked)
    github.addStretch(1)


def build_skills_page(panel: SettingsPanel) -> None:
    config_path = panel.config_path
    demo = panel._demo
    skills_page = add_page(
        panel,
        t("Beceriler"),
        t("Elle yazdığın .md dosyalarıyla Artemis'e yeni çalışma yöntemleri öğret."),
        t("Bilgi"),
    )
    panel.skills_enabled = QCheckBox(t("Becerileri kullan"))
    skills_page.addLayout(with_help(panel.skills_enabled, t(
        "Elle yazdığın .md becerileri Artemis'e çalışma yöntemi olarak öğretir. Artemis kendi "
        "başına beceri yazamaz; beceri izin vermez. Beceri metni bulut modele gidebilir, "
        "kişisel veri yazma."
    )))
    skills_page.addWidget(_check_hint(t(
        "Dosyalar varsayılan olarak Artemis'in kullanıcı verisi klasöründeki beceriler "
        "klasöründe durur; Klasör seç ile başka bir klasör seçebilirsin. Açılışta ve "
        "Yenile'de okunur. Beceriler Artemis içinde eklenir, düzenlenir ve silinir; "
        "dosyayı başka bir düzenleyicide açmana gerek yok. Artemis kendi başına beceri "
        "yazamaz; beceri izin de vermez, her adım normal onaydan geçer. Beceri metni bulut "
        "modele gidebilir, kişisel veri yazma. Mesajı /beceri-adı ile başlatarak da "
        "çağırabilirsin."
    )))
    panel.skills = SkillsSection(config_path, demo=demo)
    skills_page.addWidget(panel.skills)
    skills_page.addStretch(1)


def build_files_page(panel: SettingsPanel) -> None:
    config_path = panel.config_path
    demo = panel._demo
    files_page = add_page(
        panel,
        t("Dosya araması"),
        t("Seçtiğin klasörlerde dosya adı ve içeriğiyle arama."),
    )
    panel.file_search_enabled = QCheckBox(t("Dosya aramasını kullan"))
    files_page.addLayout(with_help(panel.file_search_enabled, t(
        "Seçtiğin klasörlerde dosya adı ve içeriğiyle arama yapar. Gizli görünen dosyalar "
        "(.env, anahtar, parola adları) ve bağlantılar asla dizinlenmez. Varsayılan kapalı; "
        "sonuçta yol ve kısa alıntı gelir."
    )))
    files_page.addWidget(_check_hint(t(
        "Metin, kod ve docx dosyalarının içeriği; PDF'lerin yalnız adı aranır. Sonuçta yol "
        "ve kısa alıntı gelir, dosyanın tamamı gelmez. Bulut izni verilmediği sürece dosya "
        "içeriği yalnız yerel modele gider; bu izin henüz panelde yok, ayar dosyasından "
        "verilir."
    )))
    panel.file_search = FileSearchSection(config_path, demo=demo)
    panel.file_search_roots = panel.file_search.roots_list
    files_page.addWidget(panel.file_search)
    files_page.addStretch(1)


def build_apps_page(panel: SettingsPanel) -> None:
    apps = add_page(
        panel,
        t("Uygulamalar"),
        t("Artemis'in açabileceği uygulamalar ve bilgisayar kullanımı."),
    )
    access_box, access = group(t("Erişim"), divided=False)
    apps.addWidget(access_box)
    panel.all_applications = QCheckBox(t("Bütün kurulu uygulamalara erişim"))
    panel.all_applications.toggled.connect(panel._sync_application_access)
    access.addWidget(panel.all_applications)
    panel.computer_auto = QCheckBox(t("Otomatik bilgisayar kullanımı"))
    panel.computer_auto.setToolTip(t(
        "Hassas adımlar ve temiz turda başlangıç sorulmaz; Esc her an durdurur. Terminal, "
        "Artemis ve parola yöneticisi engeli ile dış içerikten sonraki onay yine geçerlidir."
    ))
    access.addLayout(with_help(panel.computer_auto, t(
        "Adımları sormadan yapar; Esc her an durdurur. Terminal, Artemis ve parola yöneticisi "
        "engeli ile dış içerikten sonraki onay yine geçerlidir. Üst çubuktaki düğmeyle aynı "
        "ayar."
    )))
    access.addWidget(_check_hint(t(
        "Adımları sormadan yapar; Esc her an durdurur. Üst çubuktaki otomatik onay "
        "düğmesiyle aynı ayardır."
    )))
    panel.computer_observation = QComboBox()
    panel.computer_observation.setAccessibleName(t("Bilgisayar kullanımı gözlemi"))
    for label, key in (
        (t("Ekran öğeleri (UIA) · hızlı, metin modeli de çalışır"), "uia"),
        (t("Karma (UIA, takılınca görüntü) · hızlı ve esnek"), "hybrid"),
        (t("Ekran görüntüsü · görsel model gerekir"), "screen"),
        (t("İkisi birden · en sağlam, en yavaş"), "both"),
    ):
        panel.computer_observation.addItem(label, key)
    field(access, t("Gözlem"), panel.computer_observation, help=t(
        "UIA pencerenin düğme ve alanlarını adlarıyla okur; Karma normalde UIA kullanır, "
        "liste boşsa veya takılırsa tek adımlık ekran görüntüsü alır."
    ))
    list_box, listing = group(t("İzinli uygulamalar"), help=t(
        "Artemis yalnız bu listedeki uygulamaları açabilir. Uygulama ekle bir .exe seçtirir, "
        "Listeden çıkar seçili kaydı siler. Onaysız kapatma izni panelde yok, ayar "
        "dosyasından verilir."
    ))
    apps.addWidget(list_box)
    panel.application_list = QListWidget()
    panel.application_list.setMaximumHeight(144)
    panel.application_list.setAccessibleName(t("İzinli uygulamalar"))
    listing.addWidget(panel.application_list)
    panel.add_application_button = QPushButton(t("Uygulama ekle"))
    panel.add_application_button.setObjectName("primary")
    panel.add_application_button.clicked.connect(panel._add_application)
    panel.remove_application_button = QPushButton(t("Listeden çıkar"))
    panel.remove_application_button.clicked.connect(panel._remove_application)
    listing.addLayout(button_row(panel.remove_application_button, panel.add_application_button))
    panel.application_hint = meta_label(t("Artemis listedeki uygulamaları açabilir."))
    listing.addWidget(panel.application_hint)
    apps.addStretch(1)


def build_about_page(panel: SettingsPanel) -> None:
    about = add_page(panel, t("Hakkında"), t("Sürüm ve güncelleme denetimi."))
    box, body = group(t("Güncelleme"), divided=False)
    panel.update_section = UpdateSection(panel)
    body.addWidget(panel.update_section)
    about.addWidget(box)
