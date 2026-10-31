"""Ayarlar > Bağlantılar ve Modeller (ADR 0034): uç, anahtar ve model tanımı.

Modeller tek yerden yönetilir: eklenen modeller tek listede durur, beyin modeli o listeden
seçilir. Anahtar YAML'a, loga ve mesajlara girmez; yalnız Kimlik Bilgisi Yöneticisi'ne yazılır.
"""

from __future__ import annotations

import asyncio
import html
import re
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
import yaml
from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from artemis.brain.errors import ProviderError
from artemis.brain.models import Message
from artemis.brain.providers import (
    ClaudeCliProvider,
    endpoint_key_variable,
    provider_problem,
    stored_endpoint_key,
)
from artemis.channels.desktop import themes
from artemis.config.settings import ENDPOINT_ID_RE, EndpointConfig, ModelConfig, load_settings
from artemis.tools.credentials import delete_secret, write_secret

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
    ("Mistral", "mistral", "https://api.mistral.ai/v1", True),
    ("LM Studio", "lmstudio", "http://127.0.0.1:1234/v1", False),
    ("Ollama", "ollama", "http://127.0.0.1:11434/v1", False),
)
CLI_MODELS = ("haiku", "sonnet", "opus")
CLI_KEY = ""  # bağlantı seçicide Claude aboneliğinin verisi (uç kimlikleri boş olamaz)
CLI_NAME = "Claude aboneliği"
_BAD_URL = "Adres geçersiz: https:// ile başlamalı (http yalnız yerel sunucu için)."

# Satır içi renkler (HTML etiketleri) temadan gelir; sabit onaltılık renk yok, tema değişince
# set_theme yeniden çizer.
_COLOR_KEYS = ("muted", "accent", "warning", "error")


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFixedHeight(1)
    return line


def group(
    title: str, hint: str | None = None, *, divided: bool = True
) -> tuple[QWidget, QVBoxLayout]:
    """Düz ayar grubu: ince çizgi + başlık + kısa ipucu. Kart içinde kart olmaması için çerçeve yok;
    çizgi grubun içinde olduğundan grup gizlenince çizgi de gider."""
    box = QWidget()
    box.setObjectName("group")
    outer = QVBoxLayout(box)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(12)
    if divided:
        outer.addWidget(divider())
        outer.addSpacing(12)  # çizginin altı da üstü gibi 24px (sayfa aralığı 12 + bu 12)
    heading = QVBoxLayout()
    heading.setSpacing(4)
    head = QLabel(title)
    head.setObjectName("sectionTitle")
    heading.addWidget(head)
    if hint:
        heading.addWidget(meta_label(hint))
    outer.addLayout(heading)
    return box, outer


def meta_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("meta")
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def field(
    parent: QVBoxLayout, label: str, control: QWidget | QLayout, hint: str | None = None
) -> None:
    """Etiket alanın üstünde (13px, 4px boşluk); ipucu altta 12px soluk."""
    group = QVBoxLayout()
    group.setSpacing(4)
    caption = QLabel(label)
    caption.setObjectName("fieldLabel")
    group.addWidget(caption)
    if isinstance(control, QWidget):
        caption.setBuddy(control)
        group.addWidget(control)
    else:
        group.addLayout(control)
    if hint:
        group.addWidget(meta_label(hint))
    parent.addLayout(group)


def button_row(*buttons: QPushButton) -> QHBoxLayout:
    """Sağa yaslı düğme satırı; birincil düğme en sonda verilir, içeriğe göre genişler."""
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addStretch(1)
    for button in buttons:
        button.setMinimumWidth(88)
        row.addWidget(button)
    return row


def probe_models(base_url: str, key: str | None) -> tuple[str, list[str]]:
    """GET {base_url}/models; yanıt gövdesi, başlık ve anahtar mesaja asla girmez."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        response = httpx.get(
            f"{base_url}/models", headers=headers, timeout=5, follow_redirects=False
        )
    except httpx.HTTPError:
        return "Sunucuya ulaşılamadı (adres doğru mu, sunucu çalışıyor mu?).", []
    if response.status_code in {401, 403}:
        return "Sunucu API anahtarını reddetti; anahtarı kontrol edin.", []
    if response.status_code == 404:
        return "/models desteklenmiyor; model adını elle yazabilirsiniz.", []
    if response.status_code != 200:
        return f"Sunucu beklenmeyen yanıt verdi (HTTP {response.status_code}).", []
    try:
        data = response.json()["data"]
        models = sorted(
            {
                item["id"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        )
    except (ValueError, KeyError, TypeError):
        return "Bağlantı var ama model listesi okunamadı; model adını elle yazabilirsiniz.", []
    return f"Bağlantı tamam, {len(models)} model bulundu.", models


class ConnectionTest(QThread):
    done = Signal(str, list)

    def __init__(self, base_url: str, key: str | None, parent: QWidget) -> None:
        super().__init__(parent)
        self._base_url = base_url
        self._key = key

    def run(self) -> None:
        message, models = probe_models(self._base_url, self._key)
        self.done.emit(message, models)


class CliTest(QThread):
    """Tek küçük gerçek istek atar (abonelik kotasından az miktar harcar)."""

    done = Signal(str)

    def __init__(self, config_path: Path, model: str, parent: QWidget) -> None:
        super().__init__(parent)
        self._config_path = config_path
        self._model = model

    def run(self) -> None:
        try:
            settings = load_settings(self._config_path)
            model = ModelConfig(
                provider="claude_cli",
                model=self._model,
                input_per_million=Decimal(0),
                output_per_million=Decimal(0),
            )
            prompt = [Message(role="user", content="Tek kelimeyle yanıt ver: merhaba")]
            provider = ClaudeCliProvider(settings, model)
            completion = asyncio.run(provider.complete(prompt, [], timeout=60))
        except ProviderError as error:
            self.done.emit(str(error))
            return
        except (OSError, ValueError):
            self.done.emit("Test çalıştırılamadı.")
            return
        self.done.emit(
            "Claude Code aboneliği çalışıyor."
            if completion.content.strip()
            else "Claude Code boş yanıt verdi."
        )


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", text).strip("-")[:40]


class ProviderSection(QWidget):
    changed = Signal()  # YAML değişti; pencere çalışma zamanını yeniden başlatır

    def __init__(self, config_path: Path, *, demo: bool = False) -> None:
        super().__init__()
        self.config_path = config_path
        self._test: ConnectionTest | None = None
        self._fetch: ConnectionTest | None = None
        self._cli_test: CliTest | None = None
        self._auto_alias = ""
        self._problem: str | None = None
        self._colors = {key: themes.THEMES[themes.DEFAULT_THEME][key] for key in _COLOR_KEYS}
        self._model_data: list[tuple[str, bool, bool, str, str]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        # A: modeller önce (tek liste; beyin modeli yalnız buradan seçilir)
        model_box, models = group(
            "Modeller", "Eklediğin modeller. Beyin olan model sohbetlere cevap verir.",
            divided=False,
        )
        layout.addWidget(model_box)
        self.banner = QLabel()  # "Beyin: ..." satırı veya sağlayıcı sorunu
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)  # _render_banner metin varsa gösterir
        models.addWidget(self.banner)
        self.model_list = QListWidget()
        self.model_list.setAccessibleName("Eklenen modeller")
        models.addWidget(self.model_list)
        self.model_list.setVisible(False)  # reload() dolu olunca gösterir
        self.model_empty = meta_label("Henüz model eklenmedi. Aşağıdan bir model ekle.")
        models.addWidget(self.model_empty)
        self.primary_button = QPushButton("Beyin yap")
        self.primary_button.setObjectName("primary")
        self.primary_button.clicked.connect(self._make_primary)
        self.remove_button = QPushButton("Kaldır")
        self.remove_button.setObjectName("danger")
        self.remove_button.clicked.connect(self._remove_model)
        self.remove_button.setVisible(False)  # reload() model varsa gösterir
        self.primary_button.setVisible(False)
        models.addLayout(button_row(self.remove_button, self.primary_button))
        # B: model ekle
        add_box, add = group("Model ekle")
        layout.addWidget(add_box)
        self.add_connection = QComboBox()
        self.add_connection.setAccessibleName("Modelin bağlantısı")
        self.add_connection.currentIndexChanged.connect(self._connection_changed)
        self.model_name = QComboBox()
        self.model_name.setEditable(True)
        self.model_name.setAccessibleName("Model")
        self.model_hint = self.model_name.lineEdit()
        self.model_name.editTextChanged.connect(self._model_text_changed)
        self.fetch_button = QPushButton("Listeyi getir")
        self.fetch_button.clicked.connect(self._fetch_models)
        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        model_row.addWidget(self.model_name, 1)
        model_row.addWidget(self.fetch_button)
        self.alias = QLineEdit()
        self.alias.setAccessibleName("Model takma adı")
        self.paid = QCheckBox("Ücretli model")
        self.input_price = QLineEdit("0")
        self.input_price.setAccessibleName("Girdi USD/milyon")
        self.output_price = QLineEdit("0")
        self.output_price.setAccessibleName("Çıktı USD/milyon")
        self.price_box = QWidget()
        self.price_box.setObjectName("cardRow")
        prices = QVBoxLayout(self.price_box)
        prices.setContentsMargins(0, 0, 0, 0)
        prices.setSpacing(12)
        field(prices, "Girdi fiyatı (USD / milyon jeton)", self.input_price)
        field(prices, "Çıktı fiyatı (USD / milyon jeton)", self.output_price)
        self.price_box.setVisible(False)
        self.paid.toggled.connect(self.price_box.setVisible)
        self.make_primary = QCheckBox("Beyin modeli yap")
        field(add, "Bağlantı", self.add_connection)
        field(add, "Model", model_row)
        field(add, "Takma ad", self.alias)
        add.addWidget(self.paid)
        add.addWidget(self.price_box)
        add.addWidget(self.make_primary)
        self.add_button = QPushButton("Ekle")
        self.add_button.setObjectName("primary")
        self.add_button.clicked.connect(self._add_model)
        self.cli_test_button = QPushButton("Aboneliği test et")
        self.cli_test_button.setToolTip("Küçük bir gerçek istek atar; kotadan az harcar.")
        self.cli_test_button.clicked.connect(self._test_cli)
        add.addLayout(button_row(self.cli_test_button, self.add_button))
        # C: bağlantı ekle/düzenle; modeller önde kalsın diye varsayılan kapalı
        self.connection_toggle = QPushButton("Bağlantı ekle veya düzenle")
        self.connection_toggle.setObjectName("ghost")
        self.connection_toggle.setCheckable(True)
        layout.addWidget(divider())
        layout.addWidget(self.connection_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.connection_box, conn = group(
            "Bağlantılar",
            "Modellerin çalıştığı sunucular. Listeden seçip düzenle ya da yenisini yaz.",
            divided=False,
        )
        self.connection_box.setVisible(False)
        self.connection_toggle.toggled.connect(self.connection_box.setVisible)
        layout.addWidget(self.connection_box)
        self.endpoint_list = QListWidget()
        self.endpoint_list.setMaximumHeight(112)
        self.endpoint_list.setAccessibleName("Tanımlı bağlantılar")
        self.endpoint_list.currentItemChanged.connect(self._select)
        conn.addWidget(self.endpoint_list)
        self.preset = QComboBox()
        self.preset.setAccessibleName("Bağlantı şablonu")
        for index, (label, *_rest) in enumerate(PRESETS):
            self.preset.addItem(label, index)
        self.preset.activated.connect(self._apply_preset)
        self.endpoint_id = QLineEdit()
        self.endpoint_id.setAccessibleName("Bağlantı kimliği")
        self.endpoint_id.setMaxLength(40)
        self.base_url = QLineEdit()
        self.base_url.setAccessibleName("Bağlantı adresi")
        self.needs_key = QCheckBox("API anahtarı gerekli")
        self.needs_key.setChecked(True)
        self.key = QLineEdit()
        self.key.setAccessibleName("API anahtarı")
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        field(conn, "Şablon", self.preset)
        field(conn, "Kimlik (kısa ad)", self.endpoint_id)
        field(conn, "Adres", self.base_url)
        conn.addWidget(self.needs_key)
        field(conn, "API anahtarı", self.key)
        self.test_button = QPushButton("Bağlantıyı test et")
        self.test_button.clicked.connect(self._test_connection)
        self.save_button = QPushButton("Kaydet")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._save)
        self.delete_button = QPushButton("Sil")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete)
        conn.addLayout(button_row(self.delete_button, self.test_button, self.save_button))
        self.status = QLabel()
        self.status.setWordWrap(True)
        # Uzun yol/adres tek "sözcük" sayılıp sütunu genişletmesin (yatay kaydırma çıkmasın).
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.status)
        self._sync_cli_button()
        self._sync_model_field()
        if demo:
            # Demo'da reload çalışmaz; boş açılır liste yerine ne yapılacağını söyleyen soluk satır.
            self.add_connection.addItem("Bağlantı yok — aşağıdan ekle", None)
            self._sync_model_field()
            self.setEnabled(False)
            self.status.setText("Demo modunda bağlantılar ve modeller değiştirilmez.")
        else:
            self.reload()

    def show_problem(self, reason: str | None) -> None:
        self._problem = reason
        self._render_banner()

    @staticmethod
    def _connection_label(model: ModelConfig) -> str:
        if model.provider == "openai_compatible":
            return str(model.endpoint)
        return CLI_NAME if model.provider == "claude_cli" else model.provider

    def set_theme(self, palette: dict[str, str]) -> None:
        """Satır içi renkleri yeniden çizer; bağlantı taslağına (reload) dokunmaz."""
        self._colors = {key: palette[key] for key in _COLOR_KEYS}
        self._render_banner()
        self._render_models()

    def _render_banner(self) -> None:
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            return
        model = settings.models.get(settings.primary_model)
        if self._problem:
            text, color = self._problem, self._colors["error"]
        elif model is not None:
            text = f"Beyin: {settings.primary_model} ({self._connection_label(model)})"
            color = self._colors["accent"]
        else:
            text, color = "Henüz beyin modeli seçilmedi.", self._colors["error"]
        self.banner.setText(text)
        self.banner.setVisible(bool(text))
        self.banner.setStyleSheet(f"color: {color}; font-weight: 600;")

    def reload(self) -> None:
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        self._problem = provider_problem(settings)
        self._render_banner()
        self.make_primary.setChecked(self._problem is not None)
        if self._problem and not settings.endpoints:
            self.connection_toggle.setChecked(True)  # ilk kurulumda form gizli kalmasın
        self._model_data = [
            (
                alias,
                alias == settings.primary_model,
                # Eski openrouter/anthropic/ollama modelleri (CPA yolu) taşınmaz; kullanıcı siler.
                model.provider not in {"openai_compatible", "claude_cli"},
                self._connection_label(model),
                model.model,
            )
            for alias, model in settings.models.items()
        ]
        self._render_models()
        # Liste kendi içinde kaydırılmasın: en çok 6 satır kadar uzar.
        self.model_list.setFixedHeight(min(max(self.model_list.count(), 1), 6) * 70 + 12)
        self.model_list.setVisible(self.model_list.count() > 0)
        self.model_empty.setVisible(self.model_list.count() == 0)
        # Boş listede "Kaldır"/"Beyin yap" işlenecek bir şey bulamaz; gizlenir.
        self.remove_button.setVisible(self.model_list.count() > 0)
        self.primary_button.setVisible(self.model_list.count() > 0)
        self.endpoint_list.clear()
        for name, endpoint in settings.endpoints.items():
            if not endpoint.api_key:
                state = "anahtarsız"
            else:
                state = "anahtar kayıtlı" if stored_endpoint_key(name) else "anahtar yok"
            self.endpoint_list.addItem(f"{name} — {endpoint.base_url} ({state})")
            self.endpoint_list.item(self.endpoint_list.count() - 1).setData(
                Qt.ItemDataRole.UserRole, name
            )
        chosen = self.add_connection.currentData()
        self.add_connection.blockSignals(True)
        self.add_connection.clear()
        for name in settings.endpoints:
            self.add_connection.addItem(name, name)
        self.add_connection.addItem(CLI_NAME, CLI_KEY)
        self.add_connection.setCurrentIndex(max(self.add_connection.findData(chosen), 0))
        self.add_connection.blockSignals(False)
        if self.add_connection.currentData() != chosen:
            self._connection_changed()
        self._sync_cli_button()

    def _render_models(self) -> None:
        selected = self.model_list.currentItem()
        chosen = selected.data(Qt.ItemDataRole.UserRole) if selected is not None else None
        self.model_list.clear()
        for alias, is_brain, legacy, connection, model_id in self._model_data:
            brain = (
                f"&nbsp;&nbsp;<span style='color:{self._colors['accent']}'>&#9679; Beyin</span>"
                if is_brain
                else ""
            )
            tag = (
                f"&nbsp;&nbsp;<span style='color:{self._colors['warning']}'>eski ayar</span>"
                if legacy else ""
            )
            label = QLabel(
                f"<b>{html.escape(alias)}</b>{brain}{tag}<br>"
                f"<span style='color:{self._colors['muted']}'>"
                f"{html.escape(connection)} · {html.escape(model_id)}</span>"
            )
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            label.setStyleSheet("background: transparent; padding: 2px 0;")
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, alias)
            # +24: öğe dolgusu (8px) etiketi kırpmasın
            item.setSizeHint(label.sizeHint() + QSize(0, 24))
            self.model_list.addItem(item)
            self.model_list.setItemWidget(item, label)
            if alias == chosen:
                self.model_list.setCurrentItem(item)

    def model_rows(self) -> list[tuple[str, str]]:
        """(takma ad, düz metin satırı); testler ve erişilebilirlik için."""
        rows = []
        for index in range(self.model_list.count()):
            item = self.model_list.item(index)
            label = self.model_list.itemWidget(item)
            text = html.unescape(re.sub(r"<[^>]+>", " ", label.text())) if label else ""  # type: ignore[attr-defined]
            rows.append((item.data(Qt.ItemDataRole.UserRole), " ".join(text.split())))
        return rows

    def select_model(self, alias: str) -> None:
        for index in range(self.model_list.count()):
            if self.model_list.item(index).data(Qt.ItemDataRole.UserRole) == alias:
                self.model_list.setCurrentRow(index)

    def _select(self, item: object) -> None:
        name = item.data(Qt.ItemDataRole.UserRole) if item is not None else None  # type: ignore[attr-defined]
        if name is None:
            return
        try:
            endpoint = load_settings(self.config_path).endpoints[name]
        except (OSError, ValueError, KeyError):
            return
        self.endpoint_id.setText(name)
        self.base_url.setText(endpoint.base_url)
        self.needs_key.setChecked(endpoint.api_key)
        self.key.clear()
        self.key.setPlaceholderText("kayıtlı" if stored_endpoint_key(name) else "")

    def _apply_preset(self) -> None:
        _label, slug, url, needs_key = PRESETS[self.preset.currentData()]
        if not self.endpoint_id.text() or self.endpoint_id.text() in {p[1] for p in PRESETS}:
            self.endpoint_id.setText(slug)
        self.base_url.setText(url)
        self.needs_key.setChecked(needs_key)

    def _valid_form(self) -> tuple[str, str] | None:
        name, url = self.endpoint_id.text().strip(), self.base_url.text().strip()
        if not ENDPOINT_ID_RE.fullmatch(name):
            self.status.setText("Kimlik 1-40 karakter olmalı: harf, rakam, _ ve -.")
            return None
        try:
            EndpointConfig(base_url=url)
        except ValueError:  # pydantic mesajı girdiyi içerebilir; sabit metin kullanılır
            self.status.setText(_BAD_URL)
            return None
        return name, url.rstrip("/")

    def _test_connection(self) -> None:
        form = self._valid_form()
        if form is None or (self._test is not None and self._test.isRunning()):
            return
        name, url = form
        key = None
        if self.needs_key.isChecked():
            key = self.key.text().strip() or stored_endpoint_key(name)
        self.status.setText("Bağlantı deneniyor…")
        self._test = ConnectionTest(url, key, self)
        self._test.done.connect(lambda message, _models: self.status.setText(message))
        self._test.start()

    def _fetch_models(self) -> None:
        """Seçili bağlantının model listesini getirir; yalnız kayıtlı uç ve kayıtlı anahtar."""
        name = self.add_connection.currentData()
        if name == CLI_KEY:
            self._fill_models(list(CLI_MODELS))
            return
        if name is None or (self._fetch is not None and self._fetch.isRunning()):
            self.status.setText("Önce bir bağlantı ekleyin.")
            return
        try:
            endpoint = load_settings(self.config_path).endpoints[name]
        except (OSError, ValueError, KeyError):
            self.status.setText("Ayarlar okunamadı.")
            return
        key = stored_endpoint_key(name) if endpoint.api_key else None
        self.status.setText("Model listesi alınıyor…")
        self._fetch = ConnectionTest(endpoint.base_url, key, self)
        self._fetch.done.connect(self._fetch_done)
        self._fetch.start()

    def _fetch_done(self, message: str, models: list[str]) -> None:
        self.status.setText(message)
        if models:
            self._fill_models(models)

    def _fill_models(self, models: list[str]) -> None:
        self.model_name.clear()
        self.model_name.addItems(models)

    def _sync_model_field(self) -> None:
        """Bağlantı yokken model alanı soluk ve kapalı; neden boş olduğunu placeholder söyler."""
        ready = self.add_connection.currentData() is not None
        self.model_name.setEnabled(ready)
        self.fetch_button.setEnabled(ready)
        if self.model_hint is not None:
            self.model_hint.setPlaceholderText(
                "Model adı yaz veya listeyi getir" if ready else "Önce bağlantı seç"
            )

    def _connection_changed(self) -> None:
        self._sync_cli_button()
        self._sync_model_field()
        self._fill_models(list(CLI_MODELS) if self.add_connection.currentData() == CLI_KEY else [])
        if self.add_connection.currentData() == CLI_KEY:
            found = shutil.which("claude")
            self.status.setText(
                f"Claude Code CLI bulundu: {found}"
                if found
                else "Claude Code CLI bulunamadı. Kurun ve `claude` ile giriş yapın."
            )

    def _sync_cli_button(self) -> None:
        self.cli_test_button.setVisible(self.add_connection.currentData() == CLI_KEY)

    def _default_alias(self, model: str) -> str:
        slug = _slug(model)
        if self.add_connection.currentData() == CLI_KEY and slug:
            return f"claude-cli-{slug}"[:40]
        return slug

    def _model_text_changed(self, text: str) -> None:
        # Takma ad model adından türer; kullanıcı elle değiştirdiyse ezilmez.
        if self.alias.text() in {"", self._auto_alias}:
            self._auto_alias = self._default_alias(text.strip())
            self.alias.setText(self._auto_alias)

    def _raw(self) -> dict[str, object]:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}

    def _write(self, raw: dict[str, object]) -> bool:
        # Döngüsel içe aktarmayı önlemek için geç alınır (settings_panel bu bölümü içerir).
        from artemis.channels.desktop.settings_panel import _write_validated

        try:
            _write_validated(self.config_path, raw)
        except (OSError, ValueError, yaml.YAMLError):
            self.status.setText("Kaydedilemedi; mevcut ayarlar korunuyor.")
            return False
        return True

    def _save(self) -> None:
        form = self._valid_form()
        if form is None:
            return
        name, url = form
        raw = self._raw()
        endpoints: dict[str, object] = raw.setdefault("endpoints", {})  # type: ignore[assignment]
        endpoints[name] = {"base_url": url, "api_key": self.needs_key.isChecked()}
        if not self._write(raw):
            return
        message = "Bağlantı kaydedildi."
        typed = self.key.text().strip()
        if typed and self.needs_key.isChecked():
            try:
                write_secret(typed, f"Artemis/{name}")
            except OSError:
                message = (
                    "Bağlantı kaydedildi ama anahtar saklanamadı; "
                    f"ortamda {endpoint_key_variable(name)} tanımlayın."
                )
        self.key.clear()
        self.reload()
        # Yeni model eklerken kaydedilen bağlantı hazır seçili gelir.
        self.add_connection.setCurrentIndex(self.add_connection.findData(name))
        self.status.setText(message)
        self.changed.emit()

    def _delete(self) -> None:
        name = self.endpoint_id.text().strip()
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        if name not in settings.endpoints:
            self.status.setText("Silinecek kayıtlı bir bağlantı seçin.")
            return
        if any(model.endpoint == name for model in settings.models.values()):
            self.status.setText(
                "Bu bağlantıyı kullanan modeller var; önce Modeller listesinden 'Kaldır' ile "
                "o modelleri kaldırın."
            )
            return
        raw = self._raw()
        raw["endpoints"].pop(name, None)  # type: ignore[attr-defined]
        if not self._write(raw):
            return
        delete_secret(f"Artemis/{name}")
        self.endpoint_id.clear()
        self.base_url.clear()
        self.key.clear()
        self.reload()
        self.status.setText("Bağlantı silindi.")
        self.changed.emit()

    def _test_cli(self) -> None:
        if self._cli_test is not None and self._cli_test.isRunning():
            return
        self.status.setText("Claude Code deneniyor…")
        model = self.model_name.currentText().strip() or "haiku"
        self._cli_test = CliTest(self.config_path, model, self)
        self._cli_test.done.connect(self.status.setText)
        self._cli_test.start()

    def _add_model(self) -> None:
        connection = self.add_connection.currentData()
        model = self.model_name.currentText().strip()
        alias = self.alias.text().strip() or self._default_alias(model)
        if connection is None:
            self.status.setText("Önce bir bağlantı ekleyin.")
            return
        if not model or not alias:
            self.status.setText("Model adı yazın veya listeden seçin.")
            return
        try:
            prices = (
                [Decimal(self.input_price.text() or "0"), Decimal(self.output_price.text() or "0")]
                if self.paid.isChecked() and connection != CLI_KEY
                else [Decimal(0), Decimal(0)]
            )
            settings = load_settings(self.config_path)
        except InvalidOperation:
            self.status.setText("Fiyat sayı olmalı (ücretsiz için 0).")
            return
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        if connection != CLI_KEY and connection not in settings.endpoints:
            self.status.setText("Önce bağlantıyı kaydedin.")
            return
        if alias in settings.models:
            self.status.setText("Bu takma ad kullanımda; başka bir ad yazın.")
            return
        raw = self._raw()
        models: dict[str, object] = raw.setdefault("models", {})  # type: ignore[assignment]
        entry: dict[str, str] = {
            "provider": "claude_cli" if connection == CLI_KEY else "openai_compatible",
            "model": model,
            "input_per_million": str(prices[0]),
            "output_per_million": str(prices[1]),
        }
        if connection != CLI_KEY:
            entry["endpoint"] = connection
        models[alias] = entry
        primary = self.make_primary.isChecked()
        if primary:
            raw["primary_model"] = alias
        if not self._write(raw):
            return
        self.reload()
        paid = any(price > 0 for price in prices) and not settings.allow_paid
        self.status.setText(
            "Model eklendi."
            + (
                " Ücretli model bütçe korumasınca engellenir; izin için allow_paid gerekir."
                if paid
                else ""
            )
            + (" Beyin modeli yapıldı." if primary else "")
        )
        self.changed.emit()

    def _selected_alias(self) -> str | None:
        item = self.model_list.currentItem()
        if item is None:
            self.status.setText("Önce listeden bir model seçin.")
            return None
        return str(item.data(Qt.ItemDataRole.UserRole))

    def _make_primary(self) -> None:
        alias = self._selected_alias()
        if alias is None:
            return
        try:
            raw = self._raw()
        except (OSError, yaml.YAMLError):
            self.status.setText("Ayarlar okunamadı.")
            return
        # Takma ada göre yazılır: aynı model kimliğini paylaşan modeller karışmaz.
        raw["primary_model"] = alias
        if not self._write(raw):
            return
        self.reload()
        self.status.setText(f"Beyin modeli: {alias}.")
        self.changed.emit()

    def _remove_model(self) -> None:
        alias = self._selected_alias()
        if alias is None:
            return
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        if alias in {settings.primary_model, settings.fallback_model}:
            self.status.setText(
                "Beyin veya yedek model kaldırılamaz; önce başka bir modeli beyin yapın."
            )
            return
        raw = self._raw()
        raw["models"].pop(alias, None)  # type: ignore[attr-defined]
        if not self._write(raw):
            return
        self.reload()
        self.status.setText("Model kaldırıldı.")
        self.changed.emit()
