"""Ayarlar > Bağlantılar ve Modeller (ADR 0034): uç, anahtar ve model tanımı.

Modeller tek yerden yönetilir: eklenen modeller tek listede durur, beyin modeli o listeden
seçilir. Anahtar YAML'a, loga ve mesajlara girmez; yalnız Kimlik Bilgisi Yöneticisi'ne yazılır.
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from decimal import ROUND_UP, Decimal, InvalidOperation
from pathlib import Path

import yaml
from PySide6.QtCore import (
    QProcess,
    QProcessEnvironment,
    QSettings,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCloseEvent, QColor, QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.brain.model_report import merge_retry, report_summary, suggest_layout
from artemis.brain.providers import (
    credential_prefix,
    endpoint_key_variable,
    provider_problem,
    stored_endpoint_key,
)
from artemis.channels.desktop import themes
from artemis.channels.desktop.provider_helpers import (
    CLI_NAME,
    ModelRow,
    _is_local_model,
    _scenario_count,
    _scenario_ids,
    _scenario_label,
    _slug,
    _write_json,
    build_model_rows,
    connection_label,
    filter_model_rows,
    layout_suggestion_text,
    load_reports,
    model_row_html,
    sort_model_rows,
    speech_cost_map,
    speech_cost_text_from_fields,
)
from artemis.channels.desktop.provider_helpers import probe_models as probe_models
from artemis.channels.desktop.provider_helpers import store_endpoint_key as store_endpoint_key
from artemis.channels.desktop.provider_layout import (
    PARALLEL_CHOICES,
    PARALLEL_TEST,
    PRESETS,
    ROLE_TEXTS,
    build_add_box,
    build_connections_box,
    build_models_box,
)
from artemis.channels.desktop.provider_workers import CliTest, ConnectionTest
from artemis.channels.desktop.settings_io import _write_validated
from artemis.channels.desktop.thread_shutdown import shutdown_thread
from artemis.channels.desktop.widgets import with_help
from artemis.config.settings import ENDPOINT_ID_RE, EndpointConfig, load_settings
from artemis.i18n import t
from artemis.tools.credentials import delete_secret
from artemis.tools.credentials import write_secret as write_secret
from artemis.tools.platform import child_environment

# Model testi bir koşuda yaklaşık bu kadar token harcar (2026-11-07 ölçümü: ~340 bin giriş);
# ücretli modelde tahmini maliyet ve bütçe tavanı bundan hesaplanır.
TEST_INPUT_TOKENS = 520_000  # + bilgisayar senaryoları (ekran görüntüleri)
TEST_OUTPUT_TOKENS = 20_000

CLI_MODELS = ("haiku", "sonnet", "opus")
CLI_KEY = ""  # bağlantı seçicide Claude aboneliğinin verisi (uç kimlikleri boş olamaz)

_BAD_URL = "Adres geçersiz: https:// ile başlamalı (http yalnız yerel sunucu için)."

# Satır içi renkler (HTML etiketleri) temadan gelir; sabit onaltılık renk yok, tema değişince
# set_theme yeniden çizer.
_COLOR_KEYS = ("muted", "accent", "warning", "error", "bg")
WIDE_AT = 900  # bu genişlikten itibaren model ayrıntısı listenin yanında durur
_Row = ModelRow
_STATE_TEXT = {"bekliyor": "○", "geçti": "✓", "kaldı": "✗", "tehlikeli": "⚠"}


class ProviderSection(QWidget):
    changed = Signal()  # YAML değişti; pencere çalışma zamanını yeniden başlatır
    # Alanları provider_layout kurar; burada yalnız tipleri bildirilir.
    banner: QLabel
    _test_settings: QSettings
    sort_bar: QWidget
    model_filter: QComboBox
    filter_label: QLabel
    model_sort: QComboBox
    sort_label: QLabel
    model_list: QListWidget
    model_empty: QLabel
    primary_button: QPushButton
    hard_button: QPushButton
    fallback_button: QPushButton
    computer_button: QPushButton
    role_buttons: tuple[QPushButton, ...]
    remove_button: QPushButton
    model_test_button: QPushButton
    parallel_test: QCheckBox
    parallel_count: QComboBox
    retry_button: QPushButton
    test_list: QListWidget
    test_result: QLabel
    detail: QFrame
    detail_title: QLabel
    layout_label: QLabel
    apply_layout_button: QPushButton
    model_split: QBoxLayout
    add_connection: QComboBox
    model_name: QComboBox
    model_hint: QLineEdit | None
    fetch_button: QPushButton
    alias: QLineEdit
    paid: QCheckBox
    input_price: QLineEdit
    output_price: QLineEdit
    price_box: QWidget
    kind: QComboBox
    image_price: QLineEdit
    image_box: QWidget
    speech_protocol: QComboBox
    speech_cost: QLabel
    speech_box: QWidget
    chat_box: QWidget
    kind_info: QLabel
    make_primary: QCheckBox
    add_button: QPushButton
    cli_test_button: QPushButton
    connection_toggle: QPushButton
    connection_box: QWidget
    endpoint_list: QListWidget
    preset: QComboBox
    endpoint_id: QLineEdit
    base_url: QLineEdit
    needs_key: QCheckBox
    key: QLineEdit
    test_button: QPushButton
    save_button: QPushButton
    delete_button: QPushButton
    status: QLabel

    def __init__(self, config_path: Path, *, demo: bool = False) -> None:
        super().__init__()
        self.config_path = config_path
        self._button_tips: dict[QPushButton, QWidget] = {}
        self._test: ConnectionTest | None = None
        self._fetch: ConnectionTest | None = None
        self._cli_test: CliTest | None = None
        self._auto_alias = ""
        self._problem: str | None = None
        self._colors = {key: themes.THEMES[themes.DEFAULT_THEME][key] for key in _COLOR_KEYS}
        # (takma ad, beyin mi, eski ayar mı, bağlantı, model kimliği, tür, zor işler mi,
        # son test özeti)
        self._model_data: list[ModelRow] = []
        self._speech_cost: dict[str, str] = {}
        self._reports_seen: dict[str, dict[str, object]] = {}  # takma ad -> son rapor
        self._model_test: QProcess | None = None
        self._testing = ""
        self._fallback = ""
        self._computer = ""
        self._test_report = Path()
        self._test_rows: dict[str, str] = {}  # senaryo kimliği -> bekliyor/geçti/kaldı
        self._second: set[str] = set()  # 2. denemede geçenler
        self._test_buffer = ""
        self._stopping = False
        self._retrying = False
        self._last: tuple[Path, dict[str, object]] | None = None  # (rapor yolu, gösterilen rapor)
        self._suggested: tuple[str | None, str | None, str | None] = (None, None, None)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        build_models_box(self, layout)
        build_add_box(self, layout)
        build_connections_box(self, layout)
        self._sync_cli_button()
        self._sync_model_field()
        if demo:
            # Demo'da reload çalışmaz; boş açılır liste yerine ne yapılacağını söyleyen soluk satır.
            self.add_connection.addItem(t("Bağlantı yok — aşağıdan ekle"), None)
            self._sync_model_field()
            self.setEnabled(False)
            self.status.setText(t("Demo modunda bağlantılar ve modeller değiştirilmez."))
        else:
            self.reload()

    def _tip_after(self, button: QPushButton, text: str) -> QHBoxLayout:
        """Düğme + "?" satırı; düğme gizlenince baloncuk da gizlenir (`_show_button`)."""
        row = with_help(button, text)
        item = row.itemAt(1)
        tip = item.widget() if item is not None else None
        assert tip is not None
        tip.setVisible(not button.isHidden())
        self._button_tips[button] = tip
        return row

    def _show_button(self, button: QPushButton, visible: bool) -> None:
        button.setVisible(visible)
        tip = self._button_tips.get(button)
        if tip is not None:
            tip.setVisible(visible)

    def _style_roles(self) -> None:
        # İşaretli rol vurgu çerçevesi alır; "✓" metni durumu renksiz de belli eder.
        sheet = f"QPushButton:checked {{ border: 1px solid {self._colors['accent']}; }}"
        for button in self.role_buttons:
            button.setStyleSheet(sheet)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        wide = event.size().width() >= WIDE_AT
        self.model_split.setDirection(
            QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        )

    def show_problem(self, reason: str | None) -> None:
        self._problem = reason
        self._render_banner()

    def set_theme(self, palette: dict[str, str]) -> None:
        """Satır içi renkleri yeniden çizer; bağlantı taslağına (reload) dokunmaz."""
        self._colors = {key: palette[key] for key in _COLOR_KEYS}
        self._render_banner()
        self._style_roles()
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
            text = t("Beyin: {model} ({conn})").format(
                model=settings.primary_model, conn=connection_label(model)
            )
            if settings.hard_model:
                text += " · " + t("Zor işler: {model}").format(model=settings.hard_model)
            if settings.fallback_model and settings.fallback_model != settings.primary_model:
                text += " · " + t("Yedek: {model}").format(model=settings.fallback_model)
            color = self._colors["accent"]
        else:
            text, color = t("Henüz beyin modeli seçilmedi."), self._colors["error"]
        self.banner.setText(text)
        self.banner.setVisible(bool(text))
        self.banner.setStyleSheet(f"color: {color}; font-weight: 600;")

    def reload(self) -> None:
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText(t("Ayarlar okunamadı."))
            return
        self._problem = provider_problem(settings)
        self._render_banner()
        self.make_primary.setChecked(self._problem is not None)
        if self._problem and not settings.endpoints:
            self.connection_toggle.setChecked(True)  # ilk kurulumda form gizli kalmasın
        reports = self._reports_seen = load_reports(settings)
        self._fallback = settings.fallback_model or ""
        self._computer = settings.computer_use.model or ""
        self._model_data = build_model_rows(settings, reports)
        self._speech_cost = speech_cost_map(settings)
        self._render_models()
        self._suggested = suggest_layout(list(reports.values()), settings.primary_model)
        ana, zor, pc = self._suggested
        current = (
            settings.primary_model, settings.hard_model or None, settings.computer_use.model or None
        )
        self.layout_label.setText(layout_suggestion_text(self._suggested, current))
        self.layout_label.setVisible(bool(ana))
        self._show_button(self.apply_layout_button, bool(ana) and (ana, zor, pc) != current)
        many = len(self._model_data) > 1
        self.sort_bar.setVisible(many)
        self.model_filter.setVisible(many)
        self.filter_label.setVisible(many)
        self.model_sort.setVisible(many)
        self.sort_label.setVisible(many)
        self._sync_test_buttons()
        self.endpoint_list.clear()
        for name, endpoint in settings.endpoints.items():
            if not endpoint.api_key:
                state = t("anahtarsız")
            else:
                state = t("anahtar kayıtlı") if stored_endpoint_key(name) else t("anahtar yok")
            self.endpoint_list.addItem(f"{name} — {endpoint.base_url} ({state})")
            self.endpoint_list.item(self.endpoint_list.count() - 1).setData(
                Qt.ItemDataRole.UserRole, name
            )
        # 4 satır görünür, fazlası liste içinde kayar.
        row = self.endpoint_list.sizeHintForRow(0) if self.endpoint_list.count() else -1
        if row <= 0:
            row = self.endpoint_list.fontMetrics().height() + 16
        self.endpoint_list.setFixedHeight(4 * row + 2 * self.endpoint_list.frameWidth())
        chosen = self.add_connection.currentData()
        self.add_connection.blockSignals(True)
        self.add_connection.clear()
        for name in settings.endpoints:
            self.add_connection.addItem(name, name)
        self.add_connection.addItem(t(CLI_NAME), CLI_KEY)
        self.add_connection.setCurrentIndex(max(self.add_connection.findData(chosen), 0))
        self.add_connection.blockSignals(False)
        if self.add_connection.currentData() != chosen:
            self._connection_changed()
        self._sync_cli_button()

    def _render_models(self) -> None:
        selected = self.model_list.currentItem()
        chosen = selected.data(Qt.ItemDataRole.UserRole) if selected is not None else None
        self.model_list.clear()
        for row in self._sorted():
            alias = row[0]
            label = QLabel(
                model_row_html(
                    row, self._colors, self._fallback, self._testing, self._reports_seen
                )
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
        has_items = self.model_list.count() > 0
        if self.model_list.currentItem() is None and has_items:
            # Ayrıntı paneli her zaman bir modeli gösterir: önce beyin modeli.
            brain_aliases = {row[0] for row in self._model_data if row[1]}
            brain = next(
                (
                    i for i in range(self.model_list.count())
                    if self.model_list.item(i).data(Qt.ItemDataRole.UserRole) in brain_aliases
                ),
                0,
            )
            self.model_list.setCurrentRow(brain)
        # Liste kendi içinde kaydırılmasın: en çok 6 satır kadar uzar.
        heights = [
            self.model_list.item(index).sizeHint().height()
            for index in range(min(self.model_list.count(), 6))
        ]
        self.model_list.setFixedHeight((sum(heights) or 70) + 12)
        self.model_list.setVisible(has_items)
        self.model_empty.setText(
            t("Bu türde model yok.")
            if self._model_data and not has_items
            else t("Henüz model eklenmedi. Aşağıdan bir model ekle.")
        )
        self.model_empty.setVisible(not has_items)
        self.detail.setVisible(has_items)
        self._sync_detail()

    def _sorted(self) -> list[_Row]:
        filtered = filter_model_rows(self._model_data, self.model_filter.currentData())
        return sort_model_rows(
            filtered,
            self.model_sort.currentData(),
            self._reports_seen,
            self._fallback,
        )

    def _filter_changed(self, *_args: object) -> None:
        self._test_settings.setValue("filter", self.model_filter.currentData())
        self._render_models()

    def _sort_changed(self, *_args: object) -> None:
        self._test_settings.setValue("sort", self.model_sort.currentData())
        self._render_models()

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
        self.key.setPlaceholderText(t("kayıtlı") if stored_endpoint_key(name) else "")

    def _apply_preset(self) -> None:
        _label, slug, url, needs_key = PRESETS[self.preset.currentData()]
        if not self.endpoint_id.text() or self.endpoint_id.text() in {p[1] for p in PRESETS}:
            self.endpoint_id.setText(slug)
        self.base_url.setText(url)
        self.needs_key.setChecked(needs_key)

    def _valid_form(self) -> tuple[str, str] | None:
        name, url = self.endpoint_id.text().strip(), self.base_url.text().strip()
        if not ENDPOINT_ID_RE.fullmatch(name):
            self.status.setText(t("Kimlik 1-40 karakter olmalı: harf, rakam, _ ve -."))
            return None
        try:
            EndpointConfig(base_url=url)
        except ValueError:  # pydantic mesajı girdiyi içerebilir; sabit metin kullanılır
            self.status.setText(t(_BAD_URL))
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
        self.status.setText(t("Bağlantı deneniyor…"))
        self._test = ConnectionTest(url, key, self, probe_models)
        self._test.done.connect(self._connection_test_done)
        self._test.start()

    def _fetch_models(self) -> None:
        """Seçili bağlantının model listesini getirir; yalnız kayıtlı uç ve kayıtlı anahtar."""
        name = self.add_connection.currentData()
        if name == CLI_KEY:
            self._fill_models(list(CLI_MODELS))
            return
        if name is None or (self._fetch is not None and self._fetch.isRunning()):
            self.status.setText(t("Önce bir bağlantı ekleyin."))
            return
        try:
            endpoint = load_settings(self.config_path).endpoints[name]
        except (OSError, ValueError, KeyError):
            self.status.setText(t("Ayarlar okunamadı."))
            return
        key = stored_endpoint_key(name) if endpoint.api_key else None
        self.status.setText(t("Model listesi alınıyor…"))
        self._fetch = ConnectionTest(endpoint.base_url, key, self, probe_models)
        self._fetch.done.connect(self._fetch_done)
        self._fetch.start()

    def _connection_test_done(self, message: str, _models: list[str]) -> None:
        self.status.setText(message)

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
                t("Model adı yaz veya listeyi getir") if ready else t("Önce bağlantı seç")
            )

    def _connection_changed(self) -> None:
        self._sync_cli_button()
        self._sync_model_field()
        self._fill_models(list(CLI_MODELS) if self.add_connection.currentData() == CLI_KEY else [])
        if self.add_connection.currentData() == CLI_KEY:
            found = shutil.which("claude")
            self.status.setText(
                t("Claude Code CLI bulundu: {found}").format(found=found)
                if found
                else t("Claude Code CLI bulunamadı. Kur ve Claude Code ile giriş yap.")
            )

    def _sync_cli_button(self) -> None:
        cli = self.add_connection.currentData() == CLI_KEY
        self.cli_test_button.setVisible(cli)
        # Resim ve ses modelleri openai_compatible uç ister; Claude aboneliğinde seçilemez.
        tip = t("Resim ve ses modelleri Claude aboneliğiyle kullanılamaz.") if cli else ""
        for index in (1, 2):
            item = self.kind.model().item(index)  # type: ignore[attr-defined]
            item.setEnabled(not cli)
            item.setToolTip(tip)
        if cli:
            self.kind.setCurrentIndex(0)
        self.kind.setToolTip(tip)

    def _sync_kind(self, *_args: object) -> None:
        kind = self.kind.currentData()
        image = kind == "image"
        speech = kind == "speech"
        paid = self.paid.isChecked()
        self.price_box.setVisible(paid and kind in ("chat", "speech"))
        self.image_box.setVisible(paid and image)
        self.speech_box.setVisible(speech)
        self.speech_cost.setText(
            speech_cost_text_from_fields(self.input_price.text(), self.output_price.text())
            if speech and paid else ""
        )
        # Resim ve ses modelleri beyin olamaz (settings doğrular); seçenek kapalı ve işaretsiz.
        self.make_primary.setEnabled(kind == "chat")
        if kind != "chat":
            self.make_primary.setChecked(False)

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

    def _cli_test_done(self, message: str) -> None:
        self.status.setText(message)

    def _raw(self) -> dict[str, object]:
        loaded = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        raw: dict[str, object] = loaded if isinstance(loaded, dict) else {}
        # `endpoints:`/`models:` null ise setdefault None dönerdi; boş sözlük yapılır.
        for key in ("endpoints", "models"):
            if not isinstance(raw.get(key), dict):
                raw[key] = {}
        if "computer_use" in raw and not isinstance(raw["computer_use"], dict):
            raw["computer_use"] = {}
        return raw

    def _write(self, raw: dict[str, object]) -> bool:
        try:
            _write_validated(self.config_path, raw)
        except (OSError, ValueError, yaml.YAMLError):
            self.status.setText(t("Kaydedilemedi; mevcut ayarlar korunuyor."))
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
        message = t("Bağlantı kaydedildi.")
        typed = self.key.text().strip()
        if typed and self.needs_key.isChecked():
            failure = store_endpoint_key(name, typed, write_secret)
            if failure:
                message = t("Bağlantı kaydedildi ama anahtar saklanamadı; {failure}").format(
                    failure=failure
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
            self.status.setText(t("Ayarlar okunamadı."))
            return
        if name not in settings.endpoints:
            self.status.setText(t("Silinecek kayıtlı bir bağlantı seçin."))
            return
        if any(model.endpoint == name for model in settings.models.values()):
            self.status.setText(
                t(
                    "Bu bağlantıyı kullanan modeller var; önce Modeller listesinden 'Kaldır' ile "
                    "o modelleri kaldırın."
                )
            )
            return
        raw = self._raw()
        raw["endpoints"].pop(name, None)  # type: ignore[attr-defined]
        if not self._write(raw):
            return
        delete_secret(f"{credential_prefix()}/{name}")
        self.endpoint_id.clear()
        self.base_url.clear()
        self.key.clear()
        self.reload()
        self.status.setText(t("Bağlantı silindi."))
        self.changed.emit()

    def _test_cli(self) -> None:
        if self._cli_test is not None and self._cli_test.isRunning():
            return
        self.status.setText(t("Claude Code deneniyor…"))
        model = self.model_name.currentText().strip() or "haiku"
        self._cli_test = CliTest(self.config_path, model, self)
        self._cli_test.done.connect(self._cli_test_done)
        self._cli_test.start()

    def _add_model(self) -> None:
        connection = self.add_connection.currentData()
        model = self.model_name.currentText().strip()
        alias = self.alias.text().strip() or self._default_alias(model)
        if connection is None:
            self.status.setText(t("Önce bir bağlantı ekleyin."))
            return
        if not model or not alias:
            self.status.setText(t("Model adı yazın veya listeden seçin."))
            return
        kind = str(self.kind.currentData() or "chat")
        image = kind == "image"
        speech = kind == "speech"
        if image and connection == CLI_KEY:
            self.status.setText(t("Resim modeli Claude aboneliğiyle kullanılamaz; bir uç seçin."))
            return
        if speech and connection == CLI_KEY:
            self.status.setText(t("Ses modeli Claude aboneliğiyle kullanılamaz; bir uç seçin."))
            return
        try:
            prices = (
                [Decimal(self.input_price.text() or "0"), Decimal(self.output_price.text() or "0")]
                if self.paid.isChecked() and connection != CLI_KEY and kind in ("chat", "speech")
                else [Decimal(0), Decimal(0)]
            )
            image_cost = (
                Decimal(self.image_price.text() or "0")
                if image and self.paid.isChecked()
                else Decimal(0)
            )
            if any(not value.is_finite() or value < 0 for value in (*prices, image_cost)):
                raise InvalidOperation
            settings = load_settings(self.config_path)
        except InvalidOperation:
            self.status.setText(t("Fiyat sayı olmalı (ücretsiz için 0)."))
            return
        except (OSError, ValueError):
            self.status.setText(t("Ayarlar okunamadı."))
            return
        if connection != CLI_KEY and connection not in settings.endpoints:
            self.status.setText(t("Önce bağlantıyı kaydedin."))
            return
        if alias in settings.models:
            self.status.setText(t("Bu takma ad kullanımda; başka bir ad yazın."))
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
        if image:
            entry["kind"] = "image"
            entry["image_cost_usd"] = str(image_cost)
        elif speech:
            entry["kind"] = "speech"
            entry["speech_protocol"] = str(self.speech_protocol.currentData() or "openai")
        models[alias] = entry
        primary = self.make_primary.isChecked() and kind == "chat"
        if primary:
            raw["primary_model"] = alias
        if not self._write(raw):
            return
        self.reload()
        paid_speech = speech and any(price > 0 for price in prices)
        paid = not speech and not settings.allow_paid and (
            any(price > 0 for price in prices) or image_cost > 0
        )
        self.status.setText(
            t("Model eklendi.")
            + (
                " " + t(
                    "Ücretli model bütçe korumasınca engellenir; ücretli kullanım izni ayar "
                    "dosyasından verilir."
                )
                if paid else ""
            )
            + (
                " " + t("Ses panelinde 'Ücretli modele izin ver' işaretlenmeden kullanılmaz.")
                if paid_speech else ""
            )
            + (
                " " + t("Resim başına fiyat her çağrıda bütçeden ayrılan bir rezervasyondur.")
                if image_cost > 0 else ""
            )
            + (" " + t("Beyin modeli yapıldı.") if primary else "")
        )
        self.changed.emit()

    def _selected_alias(self) -> str | None:
        item = self.model_list.currentItem()
        if item is None:
            self.status.setText(t("Önce listeden bir model seçin."))
            return None
        return str(item.data(Qt.ItemDataRole.UserRole))

    def _make_primary(self) -> None:
        alias = self._selected_alias()
        if alias is None:
            return
        try:
            raw = self._raw()
        except (OSError, yaml.YAMLError):
            self.status.setText(t("Ayarlar okunamadı."))
            return
        if any(row[0] == alias and row[5] != "chat" for row in self._model_data):
            msg = t("Resim ve ses modelleri beyin modeli olamaz; sohbet modeli seçin.")
            self.status.setText(msg)
            return
        # Takma ada göre yazılır: aynı model kimliğini paylaşan modeller karışmaz.
        raw["primary_model"] = alias
        if raw.get("fallback_model") == alias:
            raw.pop("fallback_model")  # beyin olan model kendi arıza yedeği olamaz
        if not self._write(raw):
            return
        self.reload()
        self.status.setText(t("Beyin modeli: {alias}.").format(alias=alias))
        self.changed.emit()

    def _remove_model(self) -> None:
        alias = self._selected_alias()
        if alias is None:
            return
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText(t("Ayarlar okunamadı."))
            return
        if alias == settings.primary_model:
            self.status.setText(t("Beyin modeli kaldırılamaz; önce başka bir modeli beyin yapın."))
            return
        for holder, role, button in (
            (settings.fallback_model, t("Arıza yedeği"), t("Yedek'i")),
            (settings.hard_model, t("Zor işler modeli"), t("Zor işler'i")),
            (settings.computer_use.model, t("Bilgisayar kullanımı modeli"), t("Bilgisayar'ı")),
        ):
            if alias == holder:
                self.status.setText(
                    t("{role} kaldırılamaz; önce Rol'den {button} kapat.").format(
                        role=role, button=button
                    )
                )
                return
        raw = self._raw()
        raw["models"].pop(alias, None)  # type: ignore[attr-defined]
        if not self._write(raw):
            return
        self.reload()
        self.status.setText(t("Model kaldırıldı."))
        self.changed.emit()

    # --- Model kademesi ve model testi ---------------------------------------------------

    # Bağlı yöntemler: self'i yakalayan lambda Qt bağlantısında referans döngüsü kurar ve gc
    # paneli yanlış iş parçacığında toplayınca erişim ihlali çıkar (2026-10-31 bulgusu).
    def _toggle_hard_role(self) -> None:
        self._toggle_role("hard_model")

    def _toggle_fallback_role(self) -> None:
        self._toggle_role("fallback_model")

    def _toggle_computer_role(self) -> None:
        self._toggle_role("computer_use")

    def _selection_changed(self, *_args: object) -> None:
        self._sync_detail()  # paralel kutusunu da günceller

    def _saved_parallel(self) -> bool:
        return bool(self._test_settings.value("parallel", False, type=bool))

    def _saved_parallel_count(self) -> int:
        val = str(self._test_settings.value("parallel_count", PARALLEL_TEST))
        return int(val) if val.isdigit() and int(val) in PARALLEL_CHOICES else PARALLEL_TEST

    def _remember_parallel(self, on: bool) -> None:
        self._test_settings.setValue("parallel", on)
        self.parallel_count.setEnabled(on)

    def _remember_parallel_count(self, *_args: object) -> None:
        count = int(self.parallel_count.currentData() or PARALLEL_TEST)
        self._test_settings.setValue("parallel_count", count)
        self._parallel_tip()

    def _parallel_tip(self) -> None:
        """Paralel her modelde seçilebilir; yerel sunucuda yavaşlayabileceği ipucunda yazar."""
        alias = self._current_alias()
        count = int(self.parallel_count.currentData() or PARALLEL_TEST)
        try:
            settings = load_settings(self.config_path)
            local = alias in settings.models and _is_local_model(
                settings.models[alias], settings.endpoints
            )
        except (OSError, ValueError):
            local = False
        tip = (
            t(
                "Aynı anda {count} senaryo. Bu bağlantı bu bilgisayarda çalışıyor: "
                "sunucu tek istek kaldırıyorsa sırayla çalıştırmak daha hızlı olabilir."
            ).format(count=count)
            if local
            else t("Aynı anda {count} senaryo; API modellerinde testi kısaltır.").format(
                count=count
            )
        )
        self.parallel_test.setToolTip(tip)
        self.parallel_count.setToolTip(tip)

    def _abort_model_test(self, message: str) -> None:
        """Test süreci bitmeden ya da hatayla düştüğünde arayüzü kurtarır (eski rapor okunmaz)."""
        process, self._model_test, self._testing = self._model_test, None, ""
        self._retrying = False
        if process is not None:
            process.deleteLater()
        self.reload()
        self._render_test_list()
        self.status.setText(message)

    def _model_test_error(self, error: QProcess.ProcessError) -> None:
        # FailedToStart'ta `finished` gelmez; arayüz "test sürüyor"da kalmasın.
        if not self._stopping and self._model_test is not None:
            self._abort_model_test(
                t("{model} testi başlatılamadı veya kesildi.").format(
                    model=self._testing or t("Model")
                )
            )

    def _model_test_finished(
        self,
        exit_code: int = 0,
        exit_status: QProcess.ExitStatus = QProcess.ExitStatus.NormalExit,
    ) -> None:
        if self._stopping:
            self._stopping = self._retrying = False
            process, self._model_test, self._testing = self._model_test, None, ""
            if process is not None:
                process.deleteLater()
            self.reload()
            self.status.setText(t("Test durduruldu"))
            return
        if self._model_test is None and not self._testing:
            return
        if exit_code != 0 or exit_status != QProcess.ExitStatus.NormalExit:
            self._abort_model_test(
                t("{model} testi hata ile sonlandı (kod {code}).").format(
                    model=self._testing, code=exit_code
                )
            )
            return
        self._model_test_done(self._testing, self._test_report)

    def shutdown(self) -> None:
        """Panel kapanırken test iş parçacıklarını ve alt süreci sınırlı sürede durdurur."""
        threads: tuple[tuple[str, Callable[..., None]], ...] = (
            ("_test", self._connection_test_done),
            ("_fetch", self._fetch_done),
            ("_cli_test", self._cli_test_done),
        )
        for attr, slot in threads:
            thread = getattr(self, attr, None)
            setattr(self, attr, None)
            shutdown_thread(thread, ("done", slot), wait_ms=500)
        process, self._model_test, self._testing = self._model_test, None, ""
        self._retrying = False
        if process is None:
            return
        self._stopping = True
        for signal, slot in (
            (process.finished, self._model_test_finished),
            (process.errorOccurred, self._model_test_error),
            (process.readyReadStandardOutput, self._model_test_output),
        ):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect(slot)
        if process.state() != QProcess.ProcessState.NotRunning:
            process.terminate()
            if not process.waitForFinished(500):
                process.kill()
                process.waitForFinished(500)
        process.deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)

    def _current_alias(self) -> str | None:
        item = self.model_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def _sync_detail(self, *_args: object) -> None:
        """Ayrıntı paneli seçili modelin durumunu gösterir."""
        selected = self._current_alias()
        row = next((r for r in self._model_data if r[0] == selected), None)
        kind = row[5] if row else "chat"
        self.chat_box.setVisible(kind == "chat")
        self.kind_info.setVisible(kind != "chat")
        if row and kind == "image":
            self.kind_info.setText(
                t(
                    "Görsel modeli · Bağlantı: {conn} · Model: {model}\n"
                    "Resim üretme isteklerinde kullanılır. Beyin, zor işler, yedek ve "
                    "bilgisayar rolleri ile sohbet testi yalnız yazı modelleri içindir."
                ).format(conn=row[3], model=row[4])
            )
        elif row and kind == "speech":
            note = self._speech_cost.get(row[0], "")
            self.kind_info.setText(
                t(
                    "Ses modeli · Bağlantı: {conn} · Model: {model}\n"
                    "Kullanmak için Ayarlar > Ses > Konuşma sesi'nde Bulut'u seçip bu "
                    "modeli 'Bulut konuşma modeli' listesinden seç. Roller ve sohbet testi "
                    "yalnız yazı modelleri içindir."
                ).format(conn=row[3], model=row[4])
                + (f"\n{note}" if note else "")
            )
        else:
            self.kind_info.setText("")
        brain, non_chat, hard = (
            (row[1], kind != "chat", row[6]) if row else (False, False, False)
        )
        fallback = bool(selected) and selected == self._fallback
        computer = bool(selected) and selected == self._computer
        self.detail_title.setText(selected or "")
        reason = t("Resim ve ses modelleri bu rolü üstlenemez.") if non_chat else (
            t("Bu model zaten beyin; bu rol için başka bir model seç.") if brain else ""
        )
        hard_tip = t("Çok adımlı ve planlama isteyen turlar bu modelle çalışır.")
        fallback_tip = t(
            "Beyin modeli kotaya takılır ya da yanıt vermezse Artemis bu modelle devam eder."
        )
        computer_tip = t("Ekranı görüp tıklama bu modelle çalışır; seçilmezse beyin kullanılır.")
        for button, name, on, tip, free in (
            (self.primary_button, t("Beyin"), brain, None, not non_chat),
            (self.hard_button, t("Zor işler"), hard, hard_tip, not non_chat),
            (self.fallback_button, t("Yedek"), fallback, fallback_tip, not non_chat and not brain),
            (self.computer_button, t("Bilgisayar"), computer, computer_tip, not non_chat),
        ):
            button.setChecked(on)
            button.setText(("✓ " if on else "") + name)
            button.setEnabled(free)
            if tip:
                button.setToolTip(tip if free else reason)
        summary = (
            report_summary(self._reports_seen[selected]) if selected in self._reports_seen else ""
        )
        self.test_result.setText(
            summary.replace(t("Test:"), t("Son test:"), 1)
            if summary else t("Bu model henüz test edilmedi.")
        )
        self._sync_test_buttons()
        self._parallel_tip()

    def _toggle_role(self, key: str) -> None:
        """Seçili modele zor işler/yedek/bilgisayar (computer_use.model) rolü; 2. tık kapatır."""
        alias = self._selected_alias()
        if alias is None:
            return
        name, off, on = ROLE_TEXTS[key]
        try:
            settings = load_settings(self.config_path)
            raw = self._raw()
        except (OSError, ValueError, yaml.YAMLError):
            self.status.setText(t("Ayarlar okunamadı."))
            return
        model = settings.models.get(alias)
        if model is None or model.kind != "chat":
            self.status.setText(
                t("Resim ve ses modelleri {role} modeli olamaz; sohbet modeli seçin.").format(
                    role=t(name).lower()
                )
            )
            return
        if key == "fallback_model" and alias == settings.primary_model:
            self.status.setText(
                t("Bu model zaten beyin; {role} için başka bir model seçin.").format(
                    role=t(name).lower()
                )
            )
            return
        # computer_use.model iç içe alandır; diğer roller kök anahtardır.
        section = raw.setdefault("computer_use", {}) if key == "computer_use" else raw
        field = "model" if key == "computer_use" else key
        if not isinstance(section, dict):
            self.status.setText(t("Ayarlar okunamadı."))
            return
        removing = section.get(field) == alias
        if removing:
            section.pop(field, None)
        else:
            section[field] = alias
        if not self._write(raw):
            return
        self.reload()
        self.status.setText(t(off) if removing else t(on).format(alias=alias))
        self.changed.emit()

    def _confirm_paid(self, alias: str, cost: Decimal) -> bool:
        """Ücretli modelde testten önce bir kez sorulur (varsayılan Hayır)."""
        answer = QMessageBox.question(
            self,
            t("Ücretli model testi"),
            t("{alias} ücretli. Test yaklaşık {cost} USD harcayabilir. Başlatılsın mı?").format(
                alias=alias, cost=cost
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _test_model(self) -> None:
        self._start_test(self._selected_alias(), None)

    def _retry_failed(self) -> None:
        failed = [key for key, state in self._test_rows.items() if state in ("kaldı", "tehlikeli")]
        if self._last is None or not failed:
            return
        alias = str(self._last[1].get("model", ""))
        if alias in [row[0] for row in self._model_data]:
            self._start_test(alias, failed)

    def _start_test(self, alias: str | None, only: list[str] | None) -> None:
        if alias is None:
            return
        if self._model_test is not None:
            self.status.setText(
                t("{model} testi sürüyor; bitmesini bekleyin.").format(model=self._testing)
            )
            return
        try:
            settings = load_settings(self.config_path)
        except (OSError, ValueError):
            self.status.setText(t("Ayarlar okunamadı."))
            return
        model = settings.models[alias]
        if model.kind == "image":
            self.status.setText(t("Resim modeli sohbet senaryolarıyla test edilmez."))
            return
        if model.kind == "speech":
            self.status.setText(t("Ses modeli sohbet senaryolarıyla test edilmez."))
            return
        arguments = ["-m", "bench.model_testi", "--config", str(self.config_path), "--model", alias]
        if model.input_per_million or model.output_per_million:
            cost = (
                Decimal(TEST_INPUT_TOKENS) * model.input_per_million
                + Decimal(TEST_OUTPUT_TOKENS) * model.output_per_million
            ) / Decimal(1_000_000)
            if only:  # yeniden deneme yalnız kalan senaryoların payını harcar
                cost = cost * len(only) / _scenario_count()
            cost = cost.quantize(Decimal("0.01"), rounding=ROUND_UP)
            if not self._confirm_paid(alias, cost):
                self.status.setText(t("Test başlatılmadı."))
                return
            # Bütçe tavanı tahminin iki katı: test bunu aşamaz (rezervasyon korur).
            arguments += ["--ucretli-onay", "--butce", str(cost * 2)]
        if self.parallel_test.isChecked():
            count = int(self.parallel_count.currentData() or PARALLEL_TEST)
            arguments += ["--paralel", str(count)]
        for scenario_id in only or []:
            arguments += ["--senaryo", scenario_id]
        # Yeniden deneme ayrı dosyaya yazar: tam rapor kısmi raporla ezilmez, sonra birleştirilir.
        folder = settings.state_dir / "model_testleri"
        slug = _slug(alias) or "model"
        report = folder / f"{slug}{'.yeniden' if only else ''}.json"
        # Eski rapor kalırsa çöken koşu önceki sonucu gösterirdi; başlamadan silinir.
        report.unlink(missing_ok=True)
        if not only:
            (folder / f"{slug}.yeniden.json").unlink(missing_ok=True)
        arguments += ["--rapor", str(report)]
        process = QProcess(self)
        environment = QProcessEnvironment()
        # Alt sürece ortamın tamamı verilmez (CLAUDE.md): OS değişkenleri + bu ucun anahtarı.
        variables = dict(child_environment(), PYTHONIOENCODING="utf-8")
        if model.endpoint:
            name = endpoint_key_variable(model.endpoint)
            if name in os.environ:
                variables[name] = os.environ[name]
        for name, value in variables.items():
            environment.insert(name, value)
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._model_test_output)
        self._test_report = report
        process.finished.connect(self._model_test_finished)
        process.errorOccurred.connect(self._model_test_error)
        self._model_test, self._testing = process, alias
        self._retrying, self._test_buffer = bool(only), ""
        if only:
            self._test_rows.update(dict.fromkeys(only, "bekliyor"))
        else:
            self._test_rows = dict.fromkeys(_scenario_ids(), "bekliyor")
            self._second.clear()
            self._last = None
        self._render_test_list()
        self._render_models()
        count = len(only) if only else _scenario_count()
        self.status.setText(
            t("{alias} test ediliyor ({count} senaryo, birkaç dakika sürer)…").format(
                alias=alias, count=count
            )
        )
        process.start(sys.executable, arguments)

    def _stop_test(self) -> None:
        process = self._model_test
        if process is None or self._stopping:
            return
        self._stopping = True
        self.status.setText(t("Test durduruluyor…"))
        process.terminate()
        QTimer.singleShot(3000, self._kill_test)

    def _kill_test(self) -> None:
        process = self._model_test
        if (
            process is not None and self._stopping
            and process.state() != QProcess.ProcessState.NotRunning
        ):
            process.kill()

    def _render_test_list(self) -> None:
        self.test_list.clear()
        for key, state in self._test_rows.items():
            note = " " + t("(2. deneme)") if state == "geçti" and key in self._second else ""
            item = QListWidgetItem(f"{_STATE_TEXT[state]} {_scenario_label(key)}: {t(state)}{note}")
            # Kalan adım kırmızı tonla parlar; tehlikeli adım dolu kırmızı ve kalın: ayırt edilir.
            if state in ("kaldı", "tehlikeli"):
                red = QColor(self._colors["error"])
                fill = QColor(red)
                if state == "kaldı":
                    fill.setAlpha(45)
                item.setBackground(fill)
                item.setForeground(QColor(self._colors["bg"]) if state == "tehlikeli" else red)
                font = item.font()
                font.setBold(state == "tehlikeli")
                item.setFont(font)
            self.test_list.addItem(item)
        self._sync_test_buttons()

    def _sync_test_buttons(self) -> None:
        running = self._model_test is not None
        selected = self._current_alias()
        mine = running and self._testing == selected
        failed = any(state in ("kaldı", "tehlikeli") for state in self._test_rows.values())
        # Satırlar son/çalışan testin modeline aittir; başka model seçiliyken gösterilmez.
        shown = self._testing or (str(self._last[1].get("model", "")) if self._last else "")
        here = shown == selected
        self.test_list.setVisible(bool(self._test_rows) and here)
        # Tek düğme: test sürerken aynı yerde "Durdur" olur; başka modelin testi sürüyorsa kapalı.
        self.model_test_button.setText(t("Durdur") if mine else t("Test et"))
        self.model_test_button.setEnabled(not running or mine)
        self.model_test_button.setToolTip(
            t("Çalışan testi durdurur; rapor kaydedilmez.") if mine
            else t("{model} testi sürüyor; bitmesini bekle.").format(model=self._testing) if running
            else t(
                "{count} senaryoluk testi arka planda çalıştırır; bilgisayarına dokunmaz."
            ).format(count=_scenario_count())
        )
        # Kademeli testin ("ana + zor") adı tek model değil: yeniden deneme yalnız tek modelde.
        retryable = self._last is not None and str(self._last[1].get("model", "")) in [
            row[0] for row in self._model_data
        ]
        self._show_button(self.retry_button, not running and failed and retryable and here)

    def _test_or_stop(self) -> None:
        if self._model_test is not None and self._testing == self._current_alias():
            self._stop_test()
        else:
            self._test_model()

    def _model_test_output(self) -> None:
        if self._model_test is None:
            return
        text = bytes(self._model_test.readAllStandardOutput().data()).decode("utf-8", "replace")
        self._test_buffer += text
        *lines, self._test_buffer = self._test_buffer.split("\n")
        for line in lines:
            parts = line.split()
            if len(parts) == 4 and parts[0] == "ilerleme":
                self.status.setText(
                    t("{model} test ediliyor: {done} senaryo bitti…").format(
                        model=self._testing, done=parts[1]
                    )
                )
                if parts[2] in self._test_rows and parts[3] in _STATE_TEXT:
                    self._test_rows[parts[2]] = parts[3]
                    if parts[3] == "geçti" and self._retrying:
                        self._second.add(parts[2])
                    self._render_test_list()

    def _model_test_done(self, alias: str, report_path: Path) -> None:
        process, self._model_test, self._testing = self._model_test, None, ""
        retrying, self._retrying = self._retrying, False
        if process is not None:
            process.deleteLater()
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if retrying and self._last is not None:
                base_path, base = self._last
                report_path.unlink(missing_ok=True)
                report_path = base_path
                report = merge_retry(base, report)
                _write_json(report_path, report)
        except (OSError, ValueError, KeyError, TypeError):
            self.reload()
            self._render_test_list()
            self.status.setText(
                t("{alias} testi tamamlanamadı (bağlantı veya ayar sorunu).").format(alias=alias)
            )
            return
        self._last = (report_path, report)
        done = report.get("scenarios", [])
        self._second = {item["id"] for item in done if item.get("attempt") == 2}
        self._test_rows.update(
            {item["id"]: "tehlikeli" if item.get("dangerous") else "geçti" if item["success"]
             else "kaldı" for item in done}
        )
        self._render_test_list()
        self.reload()
        self.status.setText(f"{alias}: {report_summary(report)}. {report.get('summary', '')}")

    def _apply_layout(self) -> None:
        ana, zor, pc = self._suggested
        if not ana:
            return
        try:
            raw = self._raw()
        except (OSError, yaml.YAMLError):
            self.status.setText(t("Ayarlar okunamadı."))
            return
        raw["primary_model"] = ana
        if zor:
            raw["hard_model"] = zor
        else:
            raw.pop("hard_model", None)
        # Boş computer_use.model ana modeli kullanır; öneri ana modelse boş bırakılır.
        computer = raw.setdefault("computer_use", {})
        if isinstance(computer, dict):
            computer["model"] = pc or ""
        if not self._write(raw):
            return
        self.reload()
        self.status.setText(
            t("Düzen uygulandı. Beyin: {ana}").format(ana=ana)
            + (t(", zor işler: {zor}").format(zor=zor) if zor else "")
            + (t(", bilgisayar kullanımı: {pc}").format(pc=pc) if pc else "") + "."
        )
        self.changed.emit()
