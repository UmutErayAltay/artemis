"""Ayarlar > Model sağlayıcıları: uç, anahtar ve model tanımı (ADR 0034).

Anahtar YAML'a, loga ve mesajlara girmez; yalnız Kimlik Bilgisi Yöneticisi'ne yazılır.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
import yaml
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.brain.errors import ProviderError
from jarvis.brain.models import Message
from jarvis.brain.providers import ClaudeCliProvider, endpoint_key_variable, stored_endpoint_key
from jarvis.config.settings import ENDPOINT_ID_RE, EndpointConfig, ModelConfig, load_settings
from jarvis.tools.credentials import delete_secret, write_secret

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
CLI_LABEL = "Abonelik: Claude (Claude Code CLI)"
CLI_MODELS = ("haiku", "sonnet", "opus")
_BAD_URL = "Adres geçersiz: https:// ile başlamalı (http yalnız yerel sunucu için)."


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


class ProviderSection(QWidget):
    changed = Signal()  # YAML değişti; pencere çalışma zamanını yeniden başlatır

    def __init__(self, config_path: Path, *, demo: bool = False) -> None:
        super().__init__()
        self.config_path = config_path
        self._test: ConnectionTest | None = None
        self._cli_test: CliTest | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Model sağlayıcıları")
        title.setObjectName("section")
        layout.addWidget(title)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.hide()
        layout.addWidget(self.banner)
        self.endpoint_list = QListWidget()
        self.endpoint_list.setMaximumHeight(110)
        self.endpoint_list.setAccessibleName("Tanımlı sağlayıcılar")
        self.endpoint_list.currentItemChanged.connect(self._select)
        layout.addWidget(self.endpoint_list)
        form = QFormLayout()
        self.preset = QComboBox()
        self.preset.setAccessibleName("Sağlayıcı şablonu")
        for index, (label, *_rest) in enumerate(PRESETS):
            self.preset.addItem(label, index)
        self.preset.addItem(CLI_LABEL, -1)
        self.preset.activated.connect(self._apply_preset)
        self.endpoint_id = QLineEdit()
        self.endpoint_id.setAccessibleName("Sağlayıcı kimliği")
        self.endpoint_id.setMaxLength(40)
        self.base_url = QLineEdit()
        self.base_url.setAccessibleName("Sağlayıcı adresi")
        self.needs_key = QCheckBox("API anahtarı gerekli")
        self.needs_key.setChecked(True)
        self.key = QLineEdit()
        self.key.setAccessibleName("API anahtarı")
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self._form = form
        form.addRow("Şablon", self.preset)
        form.addRow("Kimlik", self.endpoint_id)
        form.addRow("Adres", self.base_url)
        form.addRow("", self.needs_key)
        form.addRow("API anahtarı", self.key)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.test_button = QPushButton("Bağlantıyı test et")
        self.test_button.clicked.connect(self._test_connection)
        self.save_button = QPushButton("Kaydet")
        self.save_button.clicked.connect(self._save)
        self.delete_button = QPushButton("Sil")
        self.delete_button.clicked.connect(self._delete)
        for button in (self.test_button, self.save_button, self.delete_button):
            buttons.addWidget(button)
        self.cli_test_button = QPushButton("Test et (abonelik kotasından az harcar)")
        self.cli_test_button.clicked.connect(self._test_cli)
        buttons.addWidget(self.cli_test_button)
        layout.addLayout(buttons)
        self._endpoint_buttons = (self.test_button, self.save_button, self.delete_button)
        model_form = QFormLayout()
        self.model_name = QComboBox()
        self.model_name.setEditable(True)
        self.model_name.setAccessibleName("Sağlayıcı modeli")
        self.alias = QLineEdit()
        self.alias.setAccessibleName("Model takma adı")
        self.input_price = QLineEdit("0")
        self.output_price = QLineEdit("0")
        self._model_form = model_form
        model_form.addRow("Model", self.model_name)
        model_form.addRow("Takma ad", self.alias)
        model_form.addRow("Girdi USD/milyon", self.input_price)
        model_form.addRow("Çıktı USD/milyon", self.output_price)
        layout.addLayout(model_form)
        model_buttons = QHBoxLayout()
        self.add_model_button = QPushButton("Modeli ekle")
        self.add_model_button.clicked.connect(lambda: self._add_model(primary=False))
        self.primary_button = QPushButton("Birincil model yap")
        self.primary_button.clicked.connect(lambda: self._add_model(primary=True))
        model_buttons.addWidget(self.add_model_button)
        model_buttons.addWidget(self.primary_button)
        layout.addLayout(model_buttons)
        self.registered = QComboBox()
        self.registered.setAccessibleName("Kayıtlı sağlayıcı modelleri")
        self.remove_model_button = QPushButton("Modeli kaldır")
        self.remove_model_button.clicked.connect(self._remove_model)
        remove_row = QHBoxLayout()
        remove_row.addWidget(self.registered, 1)
        remove_row.addWidget(self.remove_model_button)
        layout.addLayout(remove_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._cli_mode(False)
        if demo:
            self.setEnabled(False)
            self.status.setText("Demo modunda sağlayıcılar değiştirilmez.")
        else:
            self.reload()

    def show_problem(self, reason: str | None) -> None:
        self.banner.setText(reason or "")
        self.banner.setVisible(bool(reason))

    def reload(self) -> None:
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        self.registered.clear()
        for alias, model in settings.models.items():
            if model.provider == "openai_compatible":
                self.registered.addItem(f"{alias} ({model.endpoint})", alias)
            elif model.provider == "claude_cli":
                self.registered.addItem(f"{alias} (Claude Code)", alias)
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

    def _cli_mode(self, on: bool) -> None:
        self.cli_active = on
        for row in (1, 2, 3, 4):  # kimlik, adres, anahtar gerekli, anahtar
            self._form.setRowVisible(row, not on)
        for row in (2, 3):  # girdi/çıktı fiyatı
            self._model_form.setRowVisible(row, not on)
        for button in self._endpoint_buttons:
            button.setVisible(not on)
        self.cli_test_button.setVisible(on)

    def _apply_preset(self) -> None:
        on = self.preset.currentData() == -1
        self._cli_mode(on)
        if on:
            found = shutil.which("claude")
            self.status.setText(
                f"Claude Code CLI bulundu: {found}"
                if found
                else "Claude Code CLI bulunamadı. Kurun ve `claude` ile giriş yapın."
            )
            self.model_name.clear()
            self.model_name.addItems(CLI_MODELS)
            return
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
        self._test.done.connect(self._test_done)
        self._test.start()

    def _test_done(self, message: str, models: list[str]) -> None:
        self.status.setText(message)
        if models:
            self.model_name.clear()
            self.model_name.addItems(models)

    def _raw(self) -> dict[str, object]:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}

    def _write(self, raw: dict[str, object]) -> bool:
        # Döngüsel içe aktarmayı önlemek için geç alınır (settings_panel bu bölümü içerir).
        from jarvis.channels.desktop.settings_panel import _write_validated

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
        message = "Sağlayıcı kaydedildi."
        typed = self.key.text().strip()
        if typed and self.needs_key.isChecked():
            try:
                write_secret(typed, f"Jarvis/{name}")
            except OSError:
                message = (
                    "Sağlayıcı kaydedildi ama anahtar saklanamadı; "
                    f"ortamda {endpoint_key_variable(name)} tanımlayın."
                )
        self.key.clear()
        self.reload()
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
            self.status.setText("Silinecek kayıtlı bir sağlayıcı seçin.")
            return
        if any(model.endpoint == name for model in settings.models.values()):
            self.status.setText(
                "Bu sağlayıcıyı kullanan modeller var; önce 'Modeli kaldır' ile modelleri kaldırın."
            )
            return
        raw = self._raw()
        raw["endpoints"].pop(name, None)  # type: ignore[attr-defined]
        if not self._write(raw):
            return
        delete_secret(f"Jarvis/{name}")
        self.endpoint_id.clear()
        self.base_url.clear()
        self.key.clear()
        self.reload()
        self.status.setText("Sağlayıcı silindi.")
        self.changed.emit()

    def _test_cli(self) -> None:
        if self._cli_test is not None and self._cli_test.isRunning():
            return
        self.status.setText("Claude Code deneniyor…")
        model = self.model_name.currentText().strip() or "haiku"
        self._cli_test = CliTest(self.config_path, model, self)
        self._cli_test.done.connect(self.status.setText)
        self._cli_test.start()

    def _add_model(self, *, primary: bool) -> None:
        if self.cli_active:
            return self._add_cli_model(primary=primary)
        name = self.endpoint_id.text().strip()
        model = self.model_name.currentText().strip()
        alias = self.alias.text().strip() or re.sub(r"[^A-Za-z0-9_-]", "-", model).strip("-")[:40]
        try:
            prices = [
                Decimal(self.input_price.text() or "0"),
                Decimal(self.output_price.text() or "0"),
            ]
            settings = load_settings(self.config_path)
        except InvalidOperation:
            self.status.setText("Fiyat sayı olmalı (ücretsiz için 0).")
            return
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        if name not in settings.endpoints:
            self.status.setText("Önce sağlayıcıyı kaydedin.")
            return
        if not model or not alias:
            self.status.setText("Model adı yazın veya listeden seçin.")
            return
        raw = self._raw()
        models: dict[str, object] = raw.setdefault("models", {})  # type: ignore[assignment]
        models[alias] = {
            "provider": "openai_compatible",
            "endpoint": name,
            "model": model,
            "input_per_million": str(prices[0]),
            "output_per_million": str(prices[1]),
        }
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
            + (" Birincil model yapıldı." if primary else "")
        )
        self.changed.emit()

    def _add_cli_model(self, *, primary: bool) -> None:
        model = self.model_name.currentText().strip()
        slug = re.sub(r"[^A-Za-z0-9_-]", "-", model)
        alias = self.alias.text().strip() or f"claude-cli-{slug}"[:40]
        if not model:
            self.status.setText("Model adı yazın veya listeden seçin.")
            return
        try:
            raw = self._raw()
        except (OSError, yaml.YAMLError):
            self.status.setText("Ayarlar okunamadı.")
            return
        models: dict[str, object] = raw.setdefault("models", {})  # type: ignore[assignment]
        models[alias] = {
            "provider": "claude_cli",
            "model": model,
            "input_per_million": "0",
            "output_per_million": "0",
        }
        if primary:
            raw["primary_model"] = alias
        if not self._write(raw):
            return
        self.reload()
        self.status.setText("Model eklendi." + (" Birincil model yapıldı." if primary else ""))
        self.changed.emit()

    def _remove_model(self) -> None:
        alias = self.registered.currentData()
        if alias is None:
            self.status.setText("Kaldırılacak kayıtlı bir model seçin.")
            return
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText("Ayarlar okunamadı.")
            return
        if alias in {settings.primary_model, settings.fallback_model}:
            self.status.setText("Birincil veya yedek model kaldırılamaz; önce başka model seçin.")
            return
        raw = self._raw()
        raw["models"].pop(alias, None)  # type: ignore[attr-defined]
        if not self._write(raw):
            return
        self.reload()
        self.status.setText("Model kaldırıldı.")
        self.changed.emit()
