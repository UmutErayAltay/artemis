"""Ayarlar > Ses > Konuşma: bulut TTS sunucusu, protokol, model/ses seçimi ve gizlilik uyarıları."""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Any

import httpx
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.brain.providers import stored_endpoint_key
from artemis.channels.desktop.form_kit import field, meta_label
from artemis.channels.desktop.thread_shutdown import DETACHED, shutdown_thread
from artemis.config.settings import EndpointConfig, ModelConfig, load_settings

if TYPE_CHECKING:
    from artemis.channels.desktop.settings_panel import SettingsPanel

_SHUTDOWN_WAIT_MS = 6000
# Süresi dolunca ayrılan iş parçacığı bitene dek GC tarafından toplanmasın diye tutulur.
_DETACHED = DETACHED


def _shutdown_thread(thread: QThread | None, *connections: tuple[str, Any]) -> bool:
    # Bekleme süresi modülde kalır (testler kısaltır); gövde ortak modülde.
    return shutdown_thread(thread, *connections, wait_ms=_SHUTDOWN_WAIT_MS)


GEMINI_TTS_VOICES: tuple[str, ...] = (
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
)


def parse_openai_tts_models(payload: Any) -> list[tuple[str, list[str]]]:
    """OpenAI uyumlu `/models?output_modalities=speech` JSON yanıtını çözer; `:free` ilk sırada."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    seen: set[str] = set()
    parsed: list[tuple[str, list[str]]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        raw_voices = item.get("supported_voices")
        voices = (
            [str(v) for v in raw_voices if isinstance(v, str) and v]
            if isinstance(raw_voices, list)
            else []
        )
        parsed.append((model_id, voices))
    parsed.sort(key=lambda entry: (not entry[0].endswith(":free"), entry[0].casefold()))
    return parsed


def parse_gemini_tts_models(payload: Any) -> list[tuple[str, list[str]]]:
    """Gemini `/models?pageSize=1000` yanıtından `tts` modellerini süzer, `models/` önekini atar."""
    if not isinstance(payload, dict):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    seen: set[str] = set()
    parsed: list[tuple[str, list[str]]] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        if not isinstance(raw_name, str) or "tts" not in raw_name.casefold():
            continue
        model_id = raw_name.removeprefix("models/")
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        parsed.append((model_id, list(GEMINI_TTS_VOICES)))
    return parsed


def fetch_tts_models(
    base_url: str, protocol: str, key: str | None
) -> tuple[str, list[tuple[str, list[str]]]]:
    """Bulut konuşma modellerini getirir; hata metnine gövde ve anahtar girmez."""
    headers: dict[str, str] = {}
    if protocol == "gemini":
        url = f"{base_url}/models?pageSize=1000"
        if key:
            headers["x-goog-api-key"] = key
    else:
        url = f"{base_url}/models?output_modalities=speech"
        if key:
            headers["Authorization"] = f"Bearer {key}"
    try:
        response = httpx.get(url, headers=headers, timeout=10.0, follow_redirects=False)
    except httpx.HTTPError:
        return "Sunucuya ulaşılamadı (adres doğru mu, sunucu çalışıyor mu?).", []
    if response.status_code in {401, 403}:
        return f"Sunucu API anahtarını reddetti (HTTP {response.status_code}).", []
    if response.status_code != 200:
        return f"Sunucu beklenmeyen yanıt verdi (HTTP {response.status_code}).", []
    try:
        payload = response.json()
    except ValueError:
        return "Bağlantı var ama model listesi okunamadı; model adını elle yazabilirsin.", []
    models = (
        parse_gemini_tts_models(payload)
        if protocol == "gemini"
        else parse_openai_tts_models(payload)
    )
    return f"Bağlantı tamam, {len(models)} ses modeli bulundu.", models


class TtsModelFetch(QThread):
    done = Signal(str, list)

    def __init__(
        self, base_url: str, protocol: str, key: str | None, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self._base_url = base_url
        self._protocol = protocol
        self._key = key

    def run(self) -> None:
        message, models = fetch_tts_models(self._base_url, self._protocol, self._key)
        self.done.emit(message, models)


class TtsCloudBox(QWidget):
    """Bulut TTS ayar kutusu; panel üzerindeki ilgili alanları kurar ve yönetir."""

    def __init__(self, panel: SettingsPanel) -> None:
        super().__init__(panel)
        # Zayıf referans: panel kutuyu tutuyor; kutu da paneli güçlü tutsaydı döngü kurulur,
        # panel gc olmadan ölmezdi (tests/test_no_signal_cycles.py).
        self._panel_ref = weakref.ref(panel)
        self._fetch: TtsModelFetch | None = None
        self._fetch_for: tuple[str, str] = ("", "")
        self._model_voices: dict[str, list[str]] = {}
        self._speech_models: dict[str, ModelConfig] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        panel.tts_cloud_saved = QComboBox()
        panel.tts_cloud_saved.setAccessibleName("Kayıtlı ses modeli")
        panel.tts_cloud_saved.addItem("Elle ayarla", "")
        field(layout, "Kayıtlı ses modeli", panel.tts_cloud_saved)

        panel.tts_cloud_server = QComboBox()
        panel.tts_cloud_server.setAccessibleName("Bulut konuşma sunucusu")
        field(layout, "Konuşma sunucusu", panel.tts_cloud_server)

        panel.tts_cloud_protocol = QComboBox()
        panel.tts_cloud_protocol.setAccessibleName("Bulut konuşma protokolü")
        panel.tts_cloud_protocol.addItem("OpenAI uyumlu · OpenRouter vb.", "openai")
        panel.tts_cloud_protocol.addItem("Gemini API", "gemini")
        field(layout, "Protokol", panel.tts_cloud_protocol)

        panel.tts_cloud_model = QComboBox()
        panel.tts_cloud_model.setEditable(True)
        panel.tts_cloud_model.setAccessibleName("Bulut konuşma modeli")
        panel.tts_cloud_fetch_button = QPushButton("Ses modellerini getir")
        panel.tts_cloud_fetch_button.setAccessibleName("Ses modellerini getir")
        panel.tts_cloud_fetch_button.clicked.connect(self._fetch_models)
        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        model_row.addWidget(panel.tts_cloud_model, 1)
        model_row.addWidget(panel.tts_cloud_fetch_button)
        field(layout, "Bulut konuşma modeli", model_row)

        panel.tts_cloud_voice = QComboBox()
        panel.tts_cloud_voice.setEditable(True)
        panel.tts_cloud_voice.setAccessibleName("Bulut konuşma sesi")
        field(layout, "Ses", panel.tts_cloud_voice)

        panel.tts_cloud_consent = QCheckBox("Yanıt metni bu bulut sunucusuna gönderilsin")
        panel.tts_cloud_consent.setAccessibleName("Yanıt metni bu bulut sunucusuna gönderilsin")
        layout.addWidget(panel.tts_cloud_consent)

        panel.tts_cloud_paid = QCheckBox("Ücretli modele izin ver")
        panel.tts_cloud_paid.setAccessibleName("Ücretli modele izin ver")
        layout.addWidget(panel.tts_cloud_paid)

        panel.tts_cloud_emotion = QCheckBox("Bu model duygu etiketlerini destekliyor")
        panel.tts_cloud_emotion.setAccessibleName("Bu model duygu etiketlerini destekliyor")
        layout.addWidget(panel.tts_cloud_emotion)

        panel.tts_cloud_emotion_tags = QLineEdit()
        panel.tts_cloud_emotion_tags.setPlaceholderText("[laugh] [sigh] [whisper]")
        panel.tts_cloud_emotion_tags.setAccessibleName("Duygu etiketleri")
        field(layout, "Duygu etiketleri", panel.tts_cloud_emotion_tags)

        panel.tts_cloud_hint = meta_label("")
        panel.tts_cloud_hint.setAccessibleName("Bulut konuşma bilgisi")
        layout.addWidget(panel.tts_cloud_hint)

        panel.tts_combo.currentIndexChanged.connect(self._sync_state)
        panel.tts_cloud_saved.currentIndexChanged.connect(self._saved_picked)
        panel.tts_cloud_server.currentIndexChanged.connect(self._sync_state)
        panel.tts_cloud_protocol.currentIndexChanged.connect(self._sync_state)
        panel.tts_cloud_model.currentTextChanged.connect(self._model_changed)
        panel.tts_cloud_consent.toggled.connect(self._sync_state)
        panel.tts_cloud_paid.toggled.connect(self._sync_state)
        panel.tts_cloud_emotion.toggled.connect(self._sync_state)

        model_edit = panel.tts_cloud_model.lineEdit()
        if model_edit is not None:
            model_edit.textEdited.connect(panel._mark_dirty)
        voice_edit = panel.tts_cloud_voice.lineEdit()
        if voice_edit is not None:
            voice_edit.textEdited.connect(panel._mark_dirty)
        panel.tts_cloud_emotion_tags.textEdited.connect(panel._mark_dirty)

        if not panel._demo:
            self.reload_servers()
        self._sync_state()

    @property
    def _panel(self) -> SettingsPanel:
        panel = self._panel_ref()
        assert panel is not None  # kutu panelin çocuğu: panel ölünce kutu da ölür
        return panel

    def showEvent(self, event: QShowEvent) -> None:
        # Sunucu listesi kutu her göründüğünde yenilenir: providers.changed'e bağlanmak
        # çalışma zamanının yeniden başlatılmasıyla yarışıp kapanışı geciktiriyordu.
        super().showEvent(event)
        self.reload_servers()

    def _endpoint(self) -> tuple[str, EndpointConfig | None]:
        name = str(self._panel.tts_cloud_server.currentData() or "")
        if not name:
            return "", None
        try:
            return name, load_settings(self._panel.config_path).endpoints.get(name)
        except (OSError, ValueError):
            return name, None

    def reload_servers(self) -> None:
        if self._panel._demo:
            return
        try:
            settings = load_settings(self._panel.config_path)
        except (OSError, ValueError):
            return
        endpoints = list(settings.endpoints)
        speech_models = self._speech_models = {
            alias: model
            for alias, model in settings.models.items()
            if model.kind == "speech"
        }

        combo = self._panel.tts_cloud_server
        current = combo.currentData()
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("Seçilmedi", "")
            for name in endpoints:
                combo.addItem(name, name)
            combo.setCurrentIndex(max(0, combo.findData(current)))
        finally:
            combo.blockSignals(blocked)

        saved_combo = self._panel.tts_cloud_saved
        saved_current = saved_combo.currentData()
        saved_blocked = saved_combo.blockSignals(True)
        try:
            saved_combo.clear()
            saved_combo.addItem("Elle ayarla", "")
            for alias in speech_models:
                saved_combo.addItem(alias, alias)
            server = str(self._panel.tts_cloud_server.currentData() or "")
            protocol = str(self._panel.tts_cloud_protocol.currentData() or "openai")
            model_id = self._panel.tts_cloud_model.currentText().strip()
            matched = next(
                (
                    alias
                    for alias, model in speech_models.items()
                    if model.endpoint == server
                    and model.speech_protocol == protocol
                    and model.model == model_id
                ),
                None,
            )
            target = matched if matched is not None else saved_current
            saved_combo.setCurrentIndex(max(0, saved_combo.findData(target)))
        finally:
            saved_combo.blockSignals(saved_blocked)

        self._sync_state()

    def _saved_picked(self, *_args: object) -> None:
        panel = self._panel
        alias = str(panel.tts_cloud_saved.currentData() or "")
        if not alias or panel._demo:
            return
        try:
            model = load_settings(panel.config_path).models.get(alias)
        except (OSError, ValueError):
            return
        if model is None or model.kind != "speech":
            return
        for combo, value in (
            (panel.tts_cloud_server, model.endpoint or ""),
            (panel.tts_cloud_protocol, model.speech_protocol),
        ):
            blocked = combo.blockSignals(True)
            try:
                idx = combo.findData(value)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            finally:
                combo.blockSignals(blocked)
        model_blocked = panel.tts_cloud_model.blockSignals(True)
        try:
            panel.tts_cloud_model.setEditText(model.model)
        finally:
            panel.tts_cloud_model.blockSignals(model_blocked)
        if model.model in self._model_voices:
            self._refill_voices(self._model_voices[model.model])
        panel._mark_dirty()
        self._sync_state()

    def _model_changed(self, *_args: object) -> None:
        model_id = self._panel.tts_cloud_model.currentText().strip()
        if model_id in self._model_voices:
            self._refill_voices(self._model_voices[model_id])
        self._sync_state()

    def _refill_voices(self, voices: list[str]) -> None:
        combo = self._panel.tts_cloud_voice
        current = combo.currentText()
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItems(voices)
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setCurrentIndex(-1)
                combo.setEditText(current)
        finally:
            combo.blockSignals(blocked)

    def _fetch_models(self) -> None:
        if self._fetch is not None and self._fetch.isRunning():
            return
        name, endpoint = self._endpoint()
        if endpoint is None:
            return
        protocol = str(self._panel.tts_cloud_protocol.currentData() or "openai")
        key = stored_endpoint_key(name) if endpoint.api_key else None
        self._panel.tts_cloud_hint.setText("Ses modelleri alınıyor…")
        self._fetch_for = (name, protocol)
        self._fetch = TtsModelFetch(endpoint.base_url, protocol, key, self)
        self._fetch.done.connect(self._fetch_done)
        self._fetch.start()

    def _fetch_done(self, message: str, models: list[tuple[str, list[str]]]) -> None:
        current_for = (
            str(self._panel.tts_cloud_server.currentData() or ""),
            str(self._panel.tts_cloud_protocol.currentData() or "openai"),
        )
        if self._fetch_for != current_for:
            return
        if not models:
            self._sync_state(status_prefix=message)
            return
        self._model_voices = {model_id: list(voices) for model_id, voices in models}
        combo = self._panel.tts_cloud_model
        current = combo.currentText()
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            for model_id, _voices in models:
                combo.addItem(model_id)
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setCurrentIndex(-1)
                combo.setEditText(current)
        finally:
            combo.blockSignals(blocked)
        selected = combo.currentText().strip()
        if selected in self._model_voices:
            self._refill_voices(self._model_voices[selected])
        self._sync_state(status_prefix=message)

    def _sync_state(self, *_args: object, status_prefix: str = "") -> None:
        panel = self._panel
        is_cloud = panel.tts_combo.currentData() == "cloud"
        self.setVisible(is_cloud)
        if not is_cloud:
            panel.tts_cloud_hint.setText("")
            return
        self._drop_diverged_saved()
        name, endpoint = self._endpoint()
        live = not panel._demo
        panel.tts_cloud_fetch_button.setEnabled(live and endpoint is not None)
        panel.tts_cloud_emotion_tags.setEnabled(
            live and panel.tts_cloud_emotion.isChecked()
        )

        parts: list[str] = []
        if status_prefix:
            parts.append(status_prefix)
        parts.append(
            "Yanıt metni konuşmaya çevrilmek üzere seçtiğin bulut sunucusuna gönderilir."
        )
        if endpoint is not None and endpoint.api_key and not stored_endpoint_key(name):
            parts.append(
                "Bu sunucu için anahtar kayıtlı değil; "
                "Ayarlar > Model > Bağlantılar bölümünden kaydet."
            )
        protocol = str(panel.tts_cloud_protocol.currentData() or "openai")
        model_id = panel.tts_cloud_model.currentText().strip()
        paid = protocol == "gemini" or not model_id.endswith(":free")
        if paid and not panel.tts_cloud_paid.isChecked():
            parts.append(
                "Bu model ücretli olabilir; kullanmak için 'Ücretli modele izin ver'i işaretle."
            )
        if not panel.tts_cloud_consent.isChecked():
            parts.append("Metin gönderme izni kapalıyken bulut sesi başlamaz.")
        if panel.tts_cloud_saved.count() <= 1:
            parts.append(
                "Ses modeli eklemek için Ayarlar > Model > Model ekle'de türü Ses seç."
            )
        panel.tts_cloud_hint.setText(" ".join(parts))

    def _drop_diverged_saved(self) -> None:
        # Kayıtlı model seçildikten sonra alanlar elle değişirse seçim "Elle ayarla"ya döner;
        # yoksa liste artık kullanılmayan modeli gösterirdi.
        panel = self._panel
        model = self._speech_models.get(str(panel.tts_cloud_saved.currentData() or ""))
        if model is None:
            return
        same = (
            str(panel.tts_cloud_server.currentData() or "") == (model.endpoint or "")
            and str(panel.tts_cloud_protocol.currentData() or "openai") == model.speech_protocol
            and panel.tts_cloud_model.currentText().strip() == model.model
        )
        if not same:
            blocked = panel.tts_cloud_saved.blockSignals(True)
            try:
                panel.tts_cloud_saved.setCurrentIndex(0)
            finally:
                panel.tts_cloud_saved.blockSignals(blocked)

    def finish(self) -> None:
        if _shutdown_thread(self._fetch, ("done", self._fetch_done)):
            self._fetch = None

    def closeEvent(self, event: QCloseEvent) -> None:
        self.finish()
        super().closeEvent(event)


def build_tts_cloud_box(panel: SettingsPanel, speech_layout: QVBoxLayout) -> TtsCloudBox:
    """Bulut konuşma kutusunu kurar ve Konuşma grubuna ekler."""
    box = TtsCloudBox(panel)
    panel.tts_cloud_box = box
    speech_layout.addWidget(box)
    return box
