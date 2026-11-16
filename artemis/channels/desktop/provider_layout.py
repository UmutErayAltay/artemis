"""Ayarlar > Bağlantılar ve Modeller sayfasının yerleşimi (ProviderSection'dan ayrıldı).

Üç kutu: modeller, model ekle, bağlantılar. Davranış ProviderSection'dadır; burası yalnız
widget'ları kurar ve `section` üzerindeki alanlara bağlar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.form_kit import button_row, divider, field, group, meta_label
from artemis.channels.desktop.widgets import with_help

if TYPE_CHECKING:
    from artemis.channels.desktop.provider_section import ProviderSection

# (görünen ad, önerilen kimlik, adres, anahtar gerekli mi)
PRESETS: tuple[tuple[str, str, str, bool], ...] = (
    ("Özel uç / yerel proxy", "", "", True),
    ("OpenRouter", "openrouter", "https://openrouter.ai/api/v1", True),
    ("OpenAI", "openai", "https://api.openai.com/v1", True),
    ("Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta/openai", True),
    ("Anthropic (Claude)", "claude", "https://api.anthropic.com/v1", True),
    ("DeepSeek", "deepseek", "https://api.deepseek.com/v1", True),
    ("Z.ai GLM", "zai", "https://api.z.ai/api/paas/v4", True),
    ("Groq", "groq", "https://api.groq.com/openai/v1", True),
    ("xAI (Grok)", "xai", "https://api.x.ai/v1", True),
    ("Mistral", "mistral", "https://api.mistral.ai/v1", True),
    ("LM Studio", "lmstudio", "http://127.0.0.1:1234/v1", False),
    ("Ollama", "ollama", "http://127.0.0.1:11434/v1", False),
)

FORM_MAX_WIDTH = 640  # Model ekle / Bağlantılar formu
PARALLEL_TEST = 4

_SORTS = (
    ("Eklenme sırası", "added"),
    ("Başarı", "score"),
    ("Bağlantı", "connection"),
    ("Rol", "role"),
    ("Ad", "name"),
)


# Rol anahtarı -> (ad, kapatınca, açınca) durum metni.
ROLE_TEXTS = {
    "hard_model": (
        "Zor işler", "Zor işler modeli kapatıldı; her tur beyin modeliyle çalışır.",
        "Zor işler modeli: {alias}. Çok adımlı ve planlama isteyen turlar bunu kullanır.",
    ),
    "fallback_model": (
        "Arıza yedeği", "Arıza yedeği kapatıldı; beyin modeli yanıt vermezse tur hata ile biter.",
        "Arıza yedeği: {alias}. Beyin modeli kotaya takılır ya da yanıt vermezse Artemis bununla "
        "sürer.",
    ),
    "computer_use": (
        "Bilgisayar kullanımı", "Bilgisayar kullanımı modeli kapatıldı; beyin modeli kullanılır.",
        "Bilgisayar kullanımı modeli: {alias}.",
    ),
}


def build_models_box(section: ProviderSection, layout: QVBoxLayout) -> None:
    model_box, models = group(
        "Modeller", "Eklediğin modeller. Beyin olan model sohbetlere cevap verir.",
        divided=False,
    )
    layout.addWidget(model_box)
    section.banner = QLabel()  # "Beyin: ..." satırı veya sağlayıcı sorunu
    section.banner.setWordWrap(True)
    section.banner.setVisible(False)  # _render_banner metin varsa gösterir
    section._test_settings = QSettings(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, "Artemis", "model_test"
    )
    # Sıralama yalnız görünüm: config'teki model sırası değişmez.
    section.model_sort = QComboBox()
    section.model_sort.setAccessibleName("Modelleri sırala")
    for label, key in _SORTS:
        section.model_sort.addItem(label, key)
    saved = section.model_sort.findData(section._test_settings.value("sort", "added"))
    section.model_sort.setCurrentIndex(max(saved, 0))
    section.model_sort.currentIndexChanged.connect(section._sort_changed)
    section.sort_label = meta_label("Sırala")
    models.addWidget(section.banner)
    section.model_list = QListWidget()
    section.model_list.setAccessibleName("Eklenen modeller")
    section.model_list.setVisible(False)  # reload() dolu olunca gösterir
    section.model_empty = meta_label("Henüz model eklenmedi. Aşağıdan bir model ekle.")
    # Roller: seçili modelin rolleri; işlemler mevcut _make_primary/_toggle_role yollarıdır.
    section.primary_button = QPushButton("Beyin")
    section.primary_button.clicked.connect(section._make_primary)
    # Model kademesi: seçili model zor işler (çok adımlı, planlama) turlarını üstlenir.
    section.hard_button = QPushButton("Zor işler")
    section.hard_button.clicked.connect(section._toggle_hard_role)
    # Arıza yedeği: beyin modeli kota/bağlantı/sunucu hatası verince tur bununla sürer.
    section.fallback_button = QPushButton("Yedek")
    section.fallback_button.clicked.connect(section._toggle_fallback_role)
    section.computer_button = QPushButton("Bilgisayar")
    section.computer_button.clicked.connect(section._toggle_computer_role)
    section.role_buttons = (
        section.primary_button, section.hard_button, section.fallback_button,
        section.computer_button,
    )
    for button in section.role_buttons:
        button.setCheckable(True)
        # Eylem reddedilirse (yazılamadı, resim modeli) işaret gerçek duruma döner.
        button.clicked.connect(section._sync_detail)
    section.primary_button.setToolTip("Sohbetlere bu model cevap verir.")
    section.primary_button.setAccessibleName("Beyin modeli yap")
    section.hard_button.setAccessibleName("Zor işler modeli yap")
    section.fallback_button.setAccessibleName("Arıza yedeği yap")
    section.computer_button.setAccessibleName("Bilgisayar kullanımı modeli yap")
    section._style_roles()
    section.remove_button = QPushButton("Modeli kaldır")
    section.remove_button.setObjectName("danger")
    section.remove_button.setStyleSheet("border: none; background: transparent;")
    section.remove_button.clicked.connect(section._remove_model)
    # Test: tek düğme; çalışırken metni ve eylemi "Durdur"a döner.
    section.model_test_button = QPushButton("Test et")
    section.model_test_button.clicked.connect(section._test_or_stop)
    section.parallel_test = QCheckBox("Paralel çalıştır")
    section.parallel_test.setChecked(section._saved_parallel())
    section.parallel_test.toggled.connect(section._remember_parallel)
    section._parallel_tip()
    section.retry_button = QPushButton("Kalanları yeniden dene")
    section.retry_button.setToolTip("Kalan senaryoları aynı modelle bir kez daha çalıştırır.")
    section.retry_button.clicked.connect(section._retry_failed)
    section.retry_button.setVisible(False)
    section.test_list = QListWidget()
    section.test_list.setAccessibleName("Test senaryoları")
    section.test_list.setMaximumHeight(160)
    section.test_list.setVisible(False)
    section.test_result = meta_label("")
    # Seçili model ayrıntısı: Rol, Test, kaldırma.
    section.detail = QFrame()
    section.detail.setObjectName("card")
    detail = QVBoxLayout(section.detail)
    detail.setContentsMargins(16, 16, 16, 16)
    detail.setSpacing(12)
    section.detail_title = QLabel()
    section.detail_title.setObjectName("sectionTitle")
    detail.addWidget(section.detail_title)
    role_row = QHBoxLayout()
    role_row.setSpacing(8)
    for button in section.role_buttons:
        role_row.addWidget(button)
    role_row.addStretch(1)
    field(detail, "Rol", role_row, help=(
        "Beyin: sohbetlere bu model cevap verir. Zor işler: çok adımlı ve planlama isteyen "
        "turları bu model alır. Yedek: beyin kota, bağlantı ya da sunucu hatası verince tur "
        "bununla sürer. Bilgisayar: ekranı görüp tıklama bu modelle çalışır."
    ))
    test_row = QHBoxLayout()
    test_row.setSpacing(12)
    test_row.addWidget(section.model_test_button)
    test_row.addLayout(with_help(section.parallel_test, (
        f"Senaryolardan {PARALLEL_TEST}'ü aynı anda çalışır; API modellerinde testi kısaltır. "
        "Yerel sunucu aynı anda tek istek kaldırıyorsa yavaşlatabilir."
    )))
    test_row.addStretch(1)
    test_box = QVBoxLayout()
    test_box.setSpacing(8)
    test_box.addLayout(test_row)
    test_box.addWidget(section.test_list)
    test_box.addWidget(section.test_result)
    retry_row = QHBoxLayout()
    retry_row.addWidget(section.retry_button)
    retry_row.addStretch(1)
    test_box.addLayout(retry_row)
    field(detail, "Test", test_box, help=(
        "Seçili modeli senaryolarla dener. Ücretli modelde önce tahmini tutarı sorar; test "
        "bütçesi bu tahminin iki katıyla sınırlanır; yeniden denemede yalnız kalanların payı "
        "sorulur. Resim modelleri denenmez."
    ))
    detail.addLayout(button_row(section.remove_button))
    section.detail.setVisible(False)  # reload() model varsa gösterir
    section.model_list.currentItemChanged.connect(section._selection_changed)
    # Test edilmiş modellerden çıkan önerilen düzen (ana + zor işler) ve tek tık uygulama.
    section.layout_label = meta_label("")
    section.layout_label.setVisible(False)
    section.apply_layout_button = QPushButton("Bu düzeni uygula")
    section.apply_layout_button.clicked.connect(section._apply_layout)
    section.apply_layout_button.setVisible(False)
    # Geniş alanda liste solda, ayrıntı sağda; dar alanda ayrıntı listenin altına iner.
    section.model_split = QBoxLayout(QBoxLayout.Direction.TopToBottom)
    section.model_split.setSpacing(16)
    left = QVBoxLayout()
    left.setSpacing(12)
    # "Sırala" liste sütununun başlığında, listenin sağ kenarıyla hizalı: eskiden bütün sayfa
    # genişliğine yaslanıp kaydırma çubuğuna biniyordu (UX-03).
    sort_row = QHBoxLayout()
    sort_row.setContentsMargins(0, 0, 0, 0)
    sort_row.setSpacing(8)
    sort_row.addStretch(1)
    sort_row.addWidget(section.sort_label, 0, Qt.AlignmentFlag.AlignVCenter)
    sort_row.addWidget(section.model_sort, 0, Qt.AlignmentFlag.AlignVCenter)
    left.addLayout(sort_row)
    # Gizli başlar: reload() iki ve daha çok modelde açar; demo/boş listede sıralanacak şey yok.
    section.model_sort.setVisible(False)
    section.sort_label.setVisible(False)
    left.addWidget(section.model_list)
    left.addWidget(section.model_empty)
    left.addWidget(section.layout_label)
    section.apply_layout_button.setMinimumWidth(88)
    left.addLayout(section._tip_after(section.apply_layout_button, (
        "Test sonuçlarına göre önerilen Beyin, Zor işler ve bilgisayar kullanımı "
        "modellerini tek tıkla ayara yazar. Ayarlardan çıkınca geçerli olur."
    )))
    left.addStretch(1)
    section.model_split.addLayout(left, 1)
    section.model_split.addWidget(section.detail, 1, Qt.AlignmentFlag.AlignTop)
    models.addLayout(section.model_split)


def build_add_box(section: ProviderSection, layout: QVBoxLayout) -> None:
    add_box, add = group("Model ekle")
    # Form tam genişliğe yayılmasın: okunur bir sütun genişliğinde, solda.
    add_box.setMaximumWidth(FORM_MAX_WIDTH)
    layout.addWidget(add_box)
    section.add_connection = QComboBox()
    section.add_connection.setAccessibleName("Modelin bağlantısı")
    section.add_connection.currentIndexChanged.connect(section._connection_changed)
    section.model_name = QComboBox()
    section.model_name.setEditable(True)
    section.model_name.setAccessibleName("Model")
    section.model_hint = section.model_name.lineEdit()
    section.model_name.editTextChanged.connect(section._model_text_changed)
    section.fetch_button = QPushButton("Listeyi getir")
    section.fetch_button.clicked.connect(section._fetch_models)
    model_row = QHBoxLayout()
    model_row.setSpacing(8)
    model_row.addWidget(section.model_name, 1)
    model_row.addWidget(section.fetch_button)
    section.alias = QLineEdit()
    section.alias.setAccessibleName("Model takma adı")
    section.paid = QCheckBox("Ücretli model")
    section.input_price = QLineEdit("0")
    section.input_price.setAccessibleName("Girdi USD/milyon")
    section.output_price = QLineEdit("0")
    section.output_price.setAccessibleName("Çıktı USD/milyon")
    section.price_box = QWidget()
    section.price_box.setObjectName("cardRow")
    prices = QVBoxLayout(section.price_box)
    prices.setContentsMargins(0, 0, 0, 0)
    prices.setSpacing(12)
    field(prices, "Girdi fiyatı (USD / milyon jeton)", section.input_price)
    field(prices, "Çıktı fiyatı (USD / milyon jeton)", section.output_price)
    section.price_box.setVisible(False)
    section.kind = QComboBox()
    section.kind.setAccessibleName("Model türü")
    section.kind.addItem("Sohbet", "chat")
    section.kind.addItem("Resim", "image")
    section.image_price = QLineEdit("0")
    section.image_price.setAccessibleName("Resim başına USD")
    section.image_box = QWidget()
    section.image_box.setObjectName("cardRow")
    image_layout = QVBoxLayout(section.image_box)
    image_layout.setContentsMargins(0, 0, 0, 0)
    field(
        image_layout, "Resim başına fiyat (USD)", section.image_price,
        "Her çağrıda bütçeden bu tutar ayrılır (rezervasyon).",
    )
    section.image_box.setVisible(False)
    section.make_primary = QCheckBox("Beyin modeli yap")
    section.kind.currentIndexChanged.connect(section._sync_kind)
    section.paid.toggled.connect(section._sync_kind)
    field(add, "Bağlantı", section.add_connection)
    field(add, "Tür", section.kind)
    field(add, "Model", model_row)
    field(add, "Takma ad", section.alias)
    add.addLayout(with_help(section.paid, (
        "Modelin kullanım başına ücret aldığını işaretler ve fiyat alanlarını açar. "
        "Ücretli model bütçe korumasına takılır; ücretli kullanım izni ayar dosyasından "
        "verilir."
    )))
    add.addWidget(section.price_box)
    add.addWidget(section.image_box)
    add.addWidget(section.make_primary)
    section.add_button = QPushButton("Ekle")
    section.add_button.setObjectName("primary")
    section.add_button.clicked.connect(section._add_model)
    section.cli_test_button = QPushButton("Aboneliği test et")
    section.cli_test_button.setToolTip("Küçük bir gerçek istek atar; kotadan az harcar.")
    section.cli_test_button.clicked.connect(section._test_cli)
    add.addLayout(button_row(section.cli_test_button, section.add_button))


def build_connections_box(section: ProviderSection, layout: QVBoxLayout) -> None:
    section.connection_toggle = QPushButton("Bağlantı ekle veya düzenle")
    section.connection_toggle.setObjectName("ghost")
    section.connection_toggle.setCheckable(True)
    layout.addWidget(divider())
    layout.addWidget(section.connection_toggle, 0, Qt.AlignmentFlag.AlignLeft)
    section.connection_box, conn = group(
        "Bağlantılar",
        "Modellerin çalıştığı sunucular. Listeden seçip düzenle ya da yenisini yaz.",
        divided=False,
    )
    section.connection_box.setVisible(False)
    section.connection_box.setMaximumWidth(FORM_MAX_WIDTH)
    section.connection_toggle.toggled.connect(section.connection_box.setVisible)
    layout.addWidget(section.connection_box)
    section.endpoint_list = QListWidget()
    section.endpoint_list.setAccessibleName("Tanımlı bağlantılar")
    section.endpoint_list.currentItemChanged.connect(section._select)
    conn.addWidget(section.endpoint_list)
    section.preset = QComboBox()
    section.preset.setAccessibleName("Bağlantı şablonu")
    for index, (label, *_rest) in enumerate(PRESETS):
        section.preset.addItem(label, index)
    section.preset.activated.connect(section._apply_preset)
    section.endpoint_id = QLineEdit()
    section.endpoint_id.setAccessibleName("Bağlantı kimliği")
    section.endpoint_id.setMaxLength(40)
    section.base_url = QLineEdit()
    section.base_url.setAccessibleName("Bağlantı adresi")
    section.needs_key = QCheckBox("API anahtarı gerekli")
    section.needs_key.setChecked(True)
    section.key = QLineEdit()
    section.key.setAccessibleName("API anahtarı")
    section.key.setEchoMode(QLineEdit.EchoMode.Password)
    field(conn, "Şablon", section.preset)
    field(conn, "Kimlik (kısa ad)", section.endpoint_id)
    field(conn, "Adres", section.base_url)
    conn.addWidget(section.needs_key)
    field(conn, "API anahtarı", section.key, help=(
        "Anahtar Windows kimlik kasasına yazılır, ayar dosyasına (YAML) asla girmez. "
        "Kaydedince alan temizlenir; boş bırakırsan kayıtlı anahtar korunur."
    ))
    section.test_button = QPushButton("Bağlantıyı test et")
    section.test_button.clicked.connect(section._test_connection)
    section.save_button = QPushButton("Kaydet")
    section.save_button.setObjectName("primary")
    section.save_button.clicked.connect(section._save)
    section.delete_button = QPushButton("Sil")
    section.delete_button.setObjectName("danger")
    section.delete_button.clicked.connect(section._delete)
    conn.addLayout(button_row(
        section.delete_button, section.test_button, section.save_button,
    ))
    section.status = QLabel()
    section.status.setWordWrap(True)
    # Uzun yol/adres tek "sözcük" sayılıp sütunu genişletmesin (yatay kaydırma çıkmasın).
    section.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    layout.addWidget(section.status)
