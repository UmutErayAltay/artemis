"""Ayarlar > Ses > Yanıt (Çıkış): Edge/Piper ses seçicileri ve ses önizlemesi."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.form_kit import _HeightForWidthBox, field, meta_label
from artemis.channels.desktop.thread_shutdown import DETACHED, shutdown_thread
from artemis.channels.voice.adapters import (
    EdgeTTSynthesizer,
    EmaSynthesizer,
    PiperSynthesizer,
    SoundDeviceSink,
)
from artemis.channels.voice.cloud_tts import CloudSpeechSynthesizer
from artemis.config.settings import (
    EndpointConfig,
    Settings,
    VoiceConfig,
    cloud_tts_model,
    load_settings,
)
from artemis.i18n import current_language, t
from artemis.safety.audit import AuditLog

if TYPE_CHECKING:
    from artemis.channels.desktop.settings_panel import SettingsPanel

CURATED_EDGE_VOICES: tuple[str, ...] = (
    "tr-TR-EmelNeural",
    "tr-TR-AhmetNeural",
    "en-US-JennyNeural",
    "en-US-AriaNeural",
    "en-GB-SoniaNeural",
    "en-US-GuyNeural",
)
CURATED_PIPER_VOICES: tuple[str, ...] = (
    "tr_TR-dfki-medium",
    "en_US-lessac-medium",
    "en_GB-alba-medium",
)

PREVIEW_SAMPLE_TR = "Merhaba, ben Artemis. Ses önizlemesi hazır."
PREVIEW_SAMPLE_EN = "Hello, I am Artemis. Voice preview is ready."

_SHUTDOWN_WAIT_MS = 6000
_DETACHED = DETACHED


def _shutdown_thread(thread: QThread | None, *connections: tuple[str, Any]) -> bool:
    return shutdown_thread(thread, *connections, wait_ms=_SHUTDOWN_WAIT_MS)


def discover_piper_voices(config_path: Path, configured_model: Path | None = None) -> list[str]:
    """Yerel .onnx ses dosyalarını ve küratörlü Piper ses kimliklerini birleştirir."""
    found: list[str] = []
    seen: set[str] = set()
    search_dirs: list[Path] = []
    if configured_model is not None:
        search_dirs.append(configured_model.parent)
    search_dirs.extend([config_path.parent / "models", config_path.parent.parent / "models"])
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for onnx in sorted(directory.glob("*.onnx")):
            stem = onnx.stem
            if stem and stem not in seen:
                seen.add(stem)
                found.append(stem)
    for preset in CURATED_PIPER_VOICES:
        if preset not in seen:
            seen.add(preset)
            found.append(preset)
    return found


def resolve_piper_paths(
    config_path: Path,
    piper_voice: str,
    configured_model: Path | None,
    configured_config: Path | None,
) -> tuple[Path | None, Path | None]:
    """Seçilen `piper_voice` kimliğini veya kayıtlı `piper_model` yolunu diskte çözer."""
    voice_id = piper_voice.strip()
    if not voice_id:
        if configured_model is not None and configured_model.is_file():
            return configured_model, configured_config
        return None, None
    search_dirs: list[Path] = []
    if configured_model is not None:
        search_dirs.append(configured_model.parent)
    search_dirs.extend([config_path.parent / "models", config_path.parent.parent / "models"])
    for directory in search_dirs:
        candidate = directory / f"{voice_id}.onnx"
        if candidate.is_file():
            sibling = Path(f"{candidate}.json")
            if sibling.is_file():
                return candidate, sibling
            cfg = configured_config if configured_model == candidate else None
            return candidate, cfg
    if (
        configured_model is not None
        and configured_model.is_file()
        and configured_model.stem == voice_id
    ):
        return configured_model, configured_config
    return None, None


@dataclass(frozen=True)
class VoicePreviewRequest:
    """Tek cümlelik ses önizlemesi için dondurulmuş UI ayar anlık görüntüsü."""

    backend: str
    edge_voice: str
    piper_voice: str
    piper_model: Path | None
    piper_config: Path | None
    tts_cloud_endpoint: str
    tts_cloud_protocol: str
    tts_cloud_model: str
    tts_cloud_voice: str
    tts_cloud_sample_rate: int
    tts_cloud_allow_paid: bool
    allow_cloud_tts_text: bool
    ffmpeg: str
    sample_text: str
    paid_override: bool | None = None


SynthesizerFactory = Callable[[VoiceConfig, EndpointConfig | None, AuditLog], Any]
SinkFactory = Callable[[], Any]


def _default_synthesizer(
    config: VoiceConfig,
    endpoint: EndpointConfig | None,
    audit: AuditLog,
    *,
    paid: bool | None = None,
) -> Any:
    if config.tts_backend == "piper":
        return PiperSynthesizer(config)
    if config.tts_backend == "ema":
        return EmaSynthesizer(config)
    if config.tts_backend == "edge":
        return EdgeTTSynthesizer(config, audit)
    if config.tts_backend == "cloud":
        if endpoint is None:
            raise ValueError("Bulut konuşma sesi için sunucu seçilmeli.")
        return CloudSpeechSynthesizer(
            config, config.tts_cloud_endpoint, endpoint, audit, paid=paid
        )
    raise ValueError(f"Desteklenmeyen konuşma motoru: {config.tts_backend}")


async def run_voice_preview(
    request: VoicePreviewRequest,
    endpoint: EndpointConfig | None,
    audit_path: Path,
    *,
    is_interrupted: Callable[[], bool] = lambda: False,
    synthesizer_factory: SynthesizerFactory | None = None,
    sink_factory: SinkFactory | None = None,
) -> None:
    """Seçilen TTS motoruyla tek cümlelik ses üretir ve çalar."""
    if request.backend in ("edge", "cloud") and not request.allow_cloud_tts_text:
        raise ValueError(t("Metin gönderme izni kapalı; bulut veya Edge sesi önizlenemedi."))

    voice_cfg = VoiceConfig(
        tts_backend=request.backend,  # type: ignore[arg-type]
        tts_local_fallback=False,
        edge_voice=request.edge_voice,
        piper_voice=request.piper_voice,
        piper_model=request.piper_model,
        piper_config=request.piper_config,
        tts_cloud_endpoint=request.tts_cloud_endpoint,
        tts_cloud_protocol=request.tts_cloud_protocol,  # type: ignore[arg-type]
        tts_cloud_model=request.tts_cloud_model,
        tts_cloud_voice=request.tts_cloud_voice,
        tts_cloud_sample_rate=request.tts_cloud_sample_rate,
        tts_cloud_allow_paid=request.tts_cloud_allow_paid,
        allow_cloud_tts_text=request.allow_cloud_tts_text,
        ffmpeg=request.ffmpeg,
    )
    audit = AuditLog(audit_path)
    synthesizer = (
        synthesizer_factory(voice_cfg, endpoint, audit)
        if synthesizer_factory is not None
        else _default_synthesizer(voice_cfg, endpoint, audit, paid=request.paid_override)
    )
    sink = (sink_factory or SoundDeviceSink)()
    cancelled = asyncio.Event()

    async def _watch_cancel() -> None:
        while not cancelled.is_set():
            if is_interrupted():
                cancelled.set()
                return
            await asyncio.sleep(0.05)

    watcher = asyncio.create_task(_watch_cancel())
    try:
        await sink.play(
            synthesizer.stream(request.sample_text, cancelled),
            cancelled,
            lambda: None,
        )
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await sink.close()
        await synthesizer.close()


class VoicePreviewThread(QThread):
    """GUI'yi dondurmadan arka planda ses önizlemesi çalıştırır."""

    done = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        request: VoicePreviewRequest,
        endpoint: EndpointConfig | None,
        audit_path: Path,
        parent: QWidget | None = None,
        *,
        synthesizer_factory: SynthesizerFactory | None = None,
        sink_factory: SinkFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self.request = request
        self.endpoint = endpoint
        self.audit_path = audit_path
        self._synthesizer_factory = synthesizer_factory
        self._sink_factory = sink_factory

    def run(self) -> None:
        try:
            asyncio.run(
                run_voice_preview(
                    self.request,
                    self.endpoint,
                    self.audit_path,
                    is_interrupted=self.isInterruptionRequested,
                    synthesizer_factory=self._synthesizer_factory,
                    sink_factory=self._sink_factory,
                )
            )
        except (ValueError, OSError, RuntimeError, ImportError) as error:
            self.failed.emit(str(error) or type(error).__name__)
        else:
            if self.isInterruptionRequested():
                self.done.emit(t("Önizleme durduruldu."))
            else:
                self.done.emit(t("Önizleme tamamlandı."))


def _resolve_cloud_paid_override(
    loaded: Settings,
    *,
    endpoint_name: str,
    protocol: str,
    cloud_model: str,
    saved_alias: str,
) -> bool | None:
    """Seçili bulut konuşma modelinin kayıtlı ücret durumunu güncel UI değerleriyle çözer."""
    target_model = cloud_model.strip().removeprefix("models/")
    saved = loaded.models.get(saved_alias) if saved_alias else None
    if saved is not None:
        matches = (
            saved.kind == "speech"
            and (saved.endpoint or "") == endpoint_name
            and saved.speech_protocol == protocol
            and saved.model.removeprefix("models/") == target_model
        )
        if not matches:
            saved = None
    if saved is None and protocol in ("openai", "gemini"):
        effective_voice = loaded.voice.model_copy(
            update={
                "tts_cloud_endpoint": endpoint_name,
                "tts_cloud_protocol": protocol,
                "tts_cloud_model": cloud_model,
            }
        )
        saved = cloud_tts_model(loaded.model_copy(update={"voice": effective_voice}))
    if saved is None:
        return None
    return saved.input_per_million > 0 or saved.output_per_million > 0


def validate_preview_request(
    panel: SettingsPanel,
) -> tuple[VoicePreviewRequest | None, EndpointConfig | None, Path | None, str | None]:
    """Paneldeki güncel widget değerlerinden güvenli bir önizleme isteği kurar."""
    try:
        loaded = load_settings(panel.config_path)
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        return None, None, None, t("Ayar dosyası okunamadı.")

    backend = str(panel.tts_combo.currentData() or "piper")
    edge_voice = panel.edge_voice_combo.currentText().strip()
    piper_voice = panel.piper_voice_combo.currentText().strip()
    consent = panel.tts_cloud_consent.isChecked()
    endpoint_name = str(panel.tts_cloud_server.currentData() or "")
    protocol = str(panel.tts_cloud_protocol.currentData() or "openai")
    cloud_model = panel.tts_cloud_model.currentText().strip()
    cloud_voice = panel.tts_cloud_voice.currentText().strip()
    allow_paid = panel.tts_cloud_paid.isChecked()
    endpoint = loaded.endpoints.get(endpoint_name) if endpoint_name else None

    if backend in ("edge", "cloud") and not consent:
        return (
            None,
            None,
            None,
            t("Metin gönderme izni kapalı; bulut veya Edge sesi önizlenemedi."),
        )
    if backend == "edge" and not edge_voice:
        return None, None, None, t("Önce bir Edge ses kimliği seç veya yaz.")

    piper_model: Path | None = None
    piper_config: Path | None = None
    if backend == "piper":
        piper_model, piper_config = resolve_piper_paths(
            panel.config_path, piper_voice, loaded.voice.piper_model, loaded.voice.piper_config
        )
        if piper_model is None or not piper_model.is_file():
            return None, None, None, t("Piper ses dosyası (.onnx) bulunamadı.")

    paid_override: bool | None = None
    if backend == "cloud":
        if not endpoint_name or endpoint is None:
            return None, None, None, t("Önce bir konuşma sunucusu seç.")
        if not cloud_model:
            return None, None, None, t("Önce bir bulut konuşma modeli seç veya yaz.")
        saved_alias = str(panel.tts_cloud_saved.currentData() or "")
        paid_override = _resolve_cloud_paid_override(
            loaded,
            endpoint_name=endpoint_name,
            protocol=protocol,
            cloud_model=cloud_model,
            saved_alias=saved_alias,
        )
        is_paid = (
            paid_override
            if paid_override is not None
            else bool(
                endpoint.api_key
                and (protocol == "gemini" or not cloud_model.endswith(":free"))
            )
        )
        if is_paid and not allow_paid:
            return (
                None,
                None,
                None,
                t("Ücretli model için 'Ücretli modele izin ver'i işaretle."),
            )

    sample = PREVIEW_SAMPLE_EN if current_language() == "en" else PREVIEW_SAMPLE_TR
    req = VoicePreviewRequest(
        backend=backend,
        edge_voice=edge_voice,
        piper_voice=piper_voice,
        piper_model=piper_model,
        piper_config=piper_config,
        tts_cloud_endpoint=endpoint_name,
        tts_cloud_protocol=protocol,
        tts_cloud_model=cloud_model,
        tts_cloud_voice=cloud_voice,
        tts_cloud_sample_rate=loaded.voice.tts_cloud_sample_rate,
        tts_cloud_allow_paid=allow_paid,
        allow_cloud_tts_text=consent,
        ffmpeg=loaded.voice.ffmpeg,
        sample_text=sample,
        paid_override=paid_override,
    )
    return req, endpoint, loaded.audit_path, None


def build_voice_output_controls(panel: SettingsPanel, speech: QVBoxLayout) -> None:
    """Edge/Piper ses seçicileri, ortak bulut metin izni ve önizleme düğmesini kurar."""
    panel.tts_edge_box = _HeightForWidthBox()
    edge_layout = QVBoxLayout(panel.tts_edge_box)
    edge_layout.setContentsMargins(0, 0, 0, 0)
    panel.edge_voice_combo = QComboBox()
    panel.edge_voice_combo.setEditable(True)
    panel.edge_voice_combo.setAccessibleName(t("Edge konuşma sesi"))
    panel.edge_voice_combo.addItems(CURATED_EDGE_VOICES)
    field(
        edge_layout,
        t("Edge sesi"),
        panel.edge_voice_combo,
        t("Varsayılan Edge TTS ses kimliği (ör. tr-TR-EmelNeural)."),
    )
    speech.addWidget(panel.tts_edge_box)

    panel.tts_piper_box = _HeightForWidthBox()
    piper_layout = QVBoxLayout(panel.tts_piper_box)
    piper_layout.setContentsMargins(0, 0, 0, 0)
    panel.piper_voice_combo = QComboBox()
    panel.piper_voice_combo.setEditable(True)
    panel.piper_voice_combo.setAccessibleName(t("Piper konuşma sesi"))
    panel.piper_voice_combo.addItems(discover_piper_voices(panel.config_path))
    field(
        piper_layout,
        t("Piper sesi"),
        panel.piper_voice_combo,
        t("Yerel Piper ses kimliği (ör. tr_TR-dfki-medium)."),
    )
    speech.addWidget(panel.tts_piper_box)

    panel.tts_cloud_consent = QCheckBox(t("Yanıt metni bu bulut sunucusuna gönderilsin"))
    panel.tts_cloud_consent.setAccessibleName(t("Yanıt metni bu bulut sunucusuna gönderilsin"))
    speech.addWidget(panel.tts_cloud_consent)

    preview_row = QHBoxLayout()
    preview_row.setSpacing(8)
    panel.tts_preview_button = QPushButton(t("Sesi Test Et / Önizle"))
    panel.tts_preview_button.setAccessibleName(t("Sesi Test Et / Önizle"))
    panel.tts_preview_status = meta_label("")
    panel.tts_preview_status.setAccessibleName(t("Ses önizleme durumu"))
    preview_row.addWidget(panel.tts_preview_button)
    preview_row.addWidget(panel.tts_preview_status, 1, Qt.AlignmentFlag.AlignVCenter)
    speech.addLayout(preview_row)

    panel.tts_preview_thread = None
    panel.tts_preview_button.clicked.connect(panel._toggle_voice_preview)
    panel.tts_combo.currentIndexChanged.connect(panel._sync_voice_output_controls)
    panel.tts_cloud_consent.toggled.connect(panel._sync_voice_output_controls)


def sync_voice_output_controls(panel: SettingsPanel) -> None:
    """Seçili konuşma motoruna göre Edge/Piper seçicilerini ve ortak metin iznini gösterir."""
    backend = str(panel.tts_combo.currentData() or "")
    panel.tts_edge_box.setVisible(backend == "edge")
    panel.tts_piper_box.setVisible(backend == "piper")
    panel.tts_cloud_consent.setVisible(backend in ("edge", "cloud"))
    running = panel.tts_preview_thread is not None and panel.tts_preview_thread.isRunning()
    panel.tts_preview_button.setEnabled(not panel._demo)
    label = t("Önizlemeyi durdur") if running else t("Sesi Test Et / Önizle")
    panel.tts_preview_button.setText(label)


def toggle_voice_preview(
    panel: SettingsPanel,
    *,
    synthesizer_factory: SynthesizerFactory | None = None,
    sink_factory: SinkFactory | None = None,
) -> None:
    """Ses önizlemesini başlatır veya çalışıyorsa durdurur."""
    if panel._demo:
        return
    running = panel.tts_preview_thread
    if running is not None and running.isRunning():
        running.requestInterruption()
        panel.tts_preview_status.setText(t("Önizleme durduruluyor…"))
        return
    req, endpoint, audit_path, error = validate_preview_request(panel)
    if error is not None or req is None or audit_path is None:
        panel.tts_preview_status.setText(error or t("Ses önizlenemedi."))
        return
    thread = VoicePreviewThread(
        req,
        endpoint,
        audit_path,
        panel,
        synthesizer_factory=synthesizer_factory,
        sink_factory=sink_factory,
    )
    thread.done.connect(panel._voice_preview_done)
    thread.failed.connect(panel._voice_preview_failed)
    thread.finished.connect(panel._voice_preview_finished)
    thread.finished.connect(thread.deleteLater)
    panel.tts_preview_thread = thread
    panel.tts_preview_status.setText(t("Ses önizleniyor…"))
    sync_voice_output_controls(panel)
    thread.start()
