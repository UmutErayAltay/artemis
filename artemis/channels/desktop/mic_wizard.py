"""3 adımlı Mikrofon Test Sihirbazı (Ayarlar > Ses > Dinleme).

1. Adım: Seçili mikrofon göstergesi ve canlı RMS giriş düzeyi ölçeri.
2. Adım: Bellekte kısa örnek kaydı (2.5 sn), hoparlörden geri çalma ve gürültü tabanı özeti.
3. Adım: Yalnız yerel FasterWhisperRecognizer ile konuşma tanıma doğrulaması (bulut yok).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.form_kit import divider, meta_label
from artemis.channels.desktop.thread_shutdown import shutdown_thread
from artemis.channels.voice.adapters import (
    FasterWhisperRecognizer,
    SoundDeviceMicrophone,
    SoundDeviceSink,
)
from artemis.channels.voice.audio_routing import frame_input_level
from artemis.channels.voice.contracts import AudioSink, Microphone, PCMChunk, SpeechRecognizer
from artemis.config.settings import VoiceConfig, load_settings
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.settings_panel import SettingsPanel

_SHUTDOWN_WAIT_MS = 3000


@dataclass(frozen=True)
class MicRecordingStats:
    """Bellekte alınan mikrofon test kaydının özet ölçümleri (diske yazılmaz)."""

    pcm: bytes
    sample_rate: int
    rms_avg: int
    rms_peak: int
    noise_floor: int
    frame_count: int
    quality: str


def measure_rms_level(pcm: bytes) -> int:
    """16-bit PCM karesinden 0..100 arası RMS düzeyi döndürür."""
    if not pcm:
        return 0
    return frame_input_level(pcm)


def compute_recording_stats(
    frame_pcms: Sequence[bytes], sample_rate: int = 16000
) -> MicRecordingStats:
    """Kaydedilen karelerden ortalama/tepe RMS ve gürültü tabanını hesaplar."""
    non_empty = [chunk for chunk in frame_pcms if chunk]
    levels = [measure_rms_level(chunk) for chunk in non_empty]
    pcm = b"".join(non_empty)
    rms_avg = round(sum(levels) / len(levels)) if levels else 0
    rms_peak = max(levels, default=0)
    sorted_levels = sorted(levels)
    noise_floor = sorted_levels[len(sorted_levels) // 10] if sorted_levels else 0
    if rms_peak < 5:
        quality = "too_quiet"
    elif noise_floor >= 40:
        quality = "noisy"
    else:
        quality = "ok"
    return MicRecordingStats(
        pcm=pcm,
        sample_rate=sample_rate,
        rms_avg=rms_avg,
        rms_peak=rms_peak,
        noise_floor=noise_floor,
        frame_count=len(non_empty),
        quality=quality,
    )


async def play_pcm_buffer(
    sink: AudioSink,
    pcm: bytes,
    sample_rate: int = 16000,
    *,
    is_interrupted: Callable[[], bool] = lambda: False,
) -> None:
    """Bellekteki PCM tamponunu AudioSink üzerinden tek parça çalar."""
    if not pcm:
        await sink.close()
        return
    cancelled = asyncio.Event()

    async def chunks() -> AsyncIterator[PCMChunk]:
        yield PCMChunk(pcm=pcm, sample_rate=sample_rate, channels=1, sample_width=2)

    play_task = asyncio.create_task(sink.play(chunks(), cancelled, lambda: None))

    async def watch_cancel() -> None:
        while not play_task.done():
            if is_interrupted():
                cancelled.set()
                play_task.cancel()
                return
            await asyncio.sleep(0.025)

    watcher = asyncio.create_task(watch_cancel())
    try:
        try:
            await play_task
        except asyncio.CancelledError:
            if not is_interrupted() and not cancelled.is_set():
                raise
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        if not play_task.done():
            play_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await play_task
        await sink.close()


async def run_step1_meter(
    mic: Microphone,
    on_level: Callable[[int], None],
    *,
    is_interrupted: Callable[[], bool] = lambda: False,
) -> None:
    """1. adım: mikrofon karelerinden canlı RMS düzeyi yayımlar."""
    async def _consume() -> None:
        async for frame in mic.frames():
            if is_interrupted():
                break
            on_level(measure_rms_level(frame.pcm))

    consumer = asyncio.create_task(_consume())

    async def _watch() -> None:
        while not consumer.done():
            if is_interrupted():
                consumer.cancel()
                return
            await asyncio.sleep(0.025)

    watcher = asyncio.create_task(_watch())
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await mic.close()


async def run_step2_record_and_playback(
    mic: Microphone,
    sink: AudioSink,
    *,
    duration_seconds: float = 2.5,
    sample_rate: int = 16000,
    frame_ms: int = 20,
    is_interrupted: Callable[[], bool] = lambda: False,
    on_level: Callable[[int], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    play: bool = True,
) -> MicRecordingStats:
    """2. adım: belleğe örnek ses kaydeder, ölçer ve hoparlörden geri çalar."""
    chunks: list[bytes] = []
    max_frames = max(1, int(round(max(0.1, duration_seconds) * 1000 / max(1, frame_ms))))
    deadline = monotonic() + max(0.1, duration_seconds)
    detected_rate = sample_rate
    sink_closed = False
    if on_phase is not None:
        on_phase("recording")
    try:
        async def _record() -> None:
            nonlocal detected_rate
            async for frame in mic.frames():
                if is_interrupted():
                    break
                detected_rate = frame.sample_rate or detected_rate
                chunks.append(frame.pcm)
                if on_level is not None:
                    on_level(measure_rms_level(frame.pcm))
                if len(chunks) >= max_frames or monotonic() >= deadline:
                    break

        recorder = asyncio.create_task(_record())

        async def _watch() -> None:
            while not recorder.done():
                if is_interrupted():
                    recorder.cancel()
                    return
                await asyncio.sleep(0.025)

        watcher = asyncio.create_task(_watch())
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await recorder
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            await mic.close()

        stats = compute_recording_stats(chunks, sample_rate=detected_rate)
        if play and stats.pcm and not is_interrupted():
            if on_phase is not None:
                on_phase("playback")
            sink_closed = True
            await play_pcm_buffer(
                sink, stats.pcm, stats.sample_rate, is_interrupted=is_interrupted
            )
        return stats
    finally:
        if not sink_closed:
            await sink.close()


async def run_step3_stt_check(pcm: bytes, recognizer: SpeechRecognizer) -> str:
    """3. adım: bellekteki kaydı yalnız yerel tanıyıcıyla yazıya çevirir."""
    if not pcm:
        return ""
    prepare = getattr(recognizer, "prepare", None)
    unload = getattr(recognizer, "unload", None)
    try:
        if callable(prepare):
            await asyncio.to_thread(prepare)
        return (await recognizer.transcribe(pcm)).strip()
    finally:
        if callable(unload):
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                unload()


class MicMeterThread(QThread):
    """1. adım RMS seviye dinleyicisini arka plan iş parçacığında yürütür."""

    level = Signal(int)
    failed = Signal(str)

    def __init__(
        self, mic_factory: Callable[[], Microphone], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._mic_factory = mic_factory

    def run(self) -> None:
        try:
            asyncio.run(
                run_step1_meter(
                    self._mic_factory(),
                    self.level.emit,
                    is_interrupted=self.isInterruptionRequested,
                )
            )
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            self.failed.emit(str(error) or type(error).__name__)


class MicRecordPlayThread(QThread):
    """2. adım kayıt + geri çalmayı arka planda yürütür."""

    level = Signal(int)
    phase = Signal(str)
    finished_stats = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        mic_factory: Callable[[], Microphone],
        sink_factory: Callable[[], AudioSink],
        *,
        duration_seconds: float,
        sample_rate: int,
        frame_ms: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._mic_factory = mic_factory
        self._sink_factory = sink_factory
        self._duration = duration_seconds
        self._sample_rate = sample_rate
        self._frame_ms = frame_ms

    def run(self) -> None:
        try:
            stats = asyncio.run(
                run_step2_record_and_playback(
                    self._mic_factory(),
                    self._sink_factory(),
                    duration_seconds=self._duration,
                    sample_rate=self._sample_rate,
                    frame_ms=self._frame_ms,
                    is_interrupted=self.isInterruptionRequested,
                    on_level=self.level.emit,
                    on_phase=self.phase.emit,
                    play=True,
                )
            )
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            self.failed.emit(str(error) or type(error).__name__)
        else:
            if not self.isInterruptionRequested():
                self.finished_stats.emit(stats)


class MicPlaybackThread(QThread):
    """Kaydedilmiş bellek tamponunu yeniden hoparlörden çalar."""

    done = Signal()
    failed = Signal(str)

    def __init__(
        self,
        sink_factory: Callable[[], AudioSink],
        pcm: bytes,
        sample_rate: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sink_factory = sink_factory
        self._pcm = pcm
        self._sample_rate = sample_rate

    def run(self) -> None:
        try:
            asyncio.run(
                play_pcm_buffer(
                    self._sink_factory(),
                    self._pcm,
                    self._sample_rate,
                    is_interrupted=self.isInterruptionRequested,
                )
            )
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            self.failed.emit(str(error) or type(error).__name__)
        else:
            self.done.emit()


class MicSttTestThread(QThread):
    """3. adım yerel Whisper tanıma testini arka planda yürütür."""

    done = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        pcm: bytes,
        recognizer_factory: Callable[[], SpeechRecognizer],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._pcm = pcm
        self._recognizer_factory = recognizer_factory

    def run(self) -> None:
        try:
            text = asyncio.run(run_step3_stt_check(self._pcm, self._recognizer_factory()))
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            self.failed.emit(str(error) or type(error).__name__)
        else:
            self.done.emit(text)


class MicTestWizard(QDialog):
    """3 adımlı mikrofon doğrulama sihirbazı (seviye, kayıt/çalma, yerel tanıma)."""

    def __init__(
        self,
        voice_config: VoiceConfig,
        *,
        device_label: str = "",
        parent: QWidget | None = None,
        mic_factory: Callable[[], Microphone] | None = None,
        sink_factory: Callable[[], AudioSink] | None = None,
        recognizer_factory: Callable[[], SpeechRecognizer] | None = None,
        duration_seconds: float = 2.5,
        auto_start: bool = True,
    ) -> None:
        super().__init__(parent)
        self._config = voice_config
        self._duration = duration_seconds
        self._auto_start = auto_start
        self._mic_factory: Callable[[], Microphone] = mic_factory or (
            lambda: SoundDeviceMicrophone(voice_config, device=voice_config.input_device)
        )
        self._sink_factory: Callable[[], AudioSink] = sink_factory or SoundDeviceSink
        self._recognizer_factory: Callable[[], SpeechRecognizer] = recognizer_factory or (
            lambda: FasterWhisperRecognizer(voice_config)
        )
        self._recorded_pcm = b""
        self._recorded_rate: int = voice_config.sample_rate
        self._stt_ran_for_pcm = False
        self._meter_thread: MicMeterThread | None = None
        self._record_thread: MicRecordPlayThread | None = None
        self._playback_thread: MicPlaybackThread | None = None
        self._stt_thread: MicSttTestThread | None = None

        self.setWindowTitle(t("Mikrofon Test Sihirbazı"))
        self.setMinimumWidth(520)
        self.setMinimumHeight(360)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        heading = QLabel(t("Mikrofon Test Sihirbazı"))
        heading.setObjectName("title")
        root.addWidget(heading)
        self.step_label = meta_label("")
        root.addWidget(self.step_label)
        root.addWidget(divider())

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_step1(device_label))
        self.stack.addWidget(self._build_step2())
        self.stack.addWidget(self._build_step3())
        root.addWidget(self.stack, 1)

        root.addWidget(divider())
        nav = QHBoxLayout()
        nav.setSpacing(8)
        self.cancel_button = QPushButton(t("Vazgeç"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        nav.addWidget(self.cancel_button)
        nav.addStretch(1)
        self.back_button = QPushButton(t("Geri"))
        self.back_button.setAutoDefault(False)
        self.back_button.clicked.connect(self._prev_step)
        self.next_button = QPushButton(t("İleri"))
        self.next_button.setObjectName("primary")
        self.next_button.setDefault(True)
        self.next_button.clicked.connect(self._next_step)
        nav.addWidget(self.back_button)
        nav.addWidget(self.next_button)
        root.addLayout(nav)

        self.go_to_step(0)

    def _build_step1(self, device_label: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(12)

        device_title = QLabel(t("Seçili mikrofon"))
        device_title.setObjectName("fieldLabel")
        layout.addWidget(device_title)
        self.device_value = QLabel(device_label or t("Sistem varsayılanı"))
        self.device_value.setTextFormat(Qt.TextFormat.PlainText)
        self.device_value.setWordWrap(True)
        layout.addWidget(self.device_value)

        level_title = QLabel(t("Canlı giriş seviyesi"))
        level_title.setObjectName("fieldLabel")
        layout.addWidget(level_title)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.meter_bar = QProgressBar()
        self.meter_bar.setObjectName("levelMeter")
        self.meter_bar.setRange(0, 100)
        self.meter_bar.setValue(0)
        self.meter_bar.setTextVisible(False)
        self.meter_bar.setFixedHeight(8)
        self.meter_value = QLabel(t("%{value}").format(value=0))
        self.meter_value.setObjectName("meta")
        row.addWidget(self.meter_bar, 1)
        row.addWidget(self.meter_value)
        layout.addLayout(row)

        self.meter_hint = meta_label(
            t(
                "Mikrofonun yanında normal sesle konuş; "
                "çubuk hareket etmiyorsa doğru giriş aygıtını seç."
            )
        )
        layout.addWidget(self.meter_hint)
        self.meter_status = meta_label("")
        layout.addWidget(self.meter_status)

        btn_row = QHBoxLayout()
        self.meter_retry_button = QPushButton(t("Ölçümü yenile"))
        self.meter_retry_button.setAutoDefault(False)
        self.meter_retry_button.clicked.connect(self.start_meter)
        btn_row.addWidget(self.meter_retry_button)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)
        layout.addStretch(1)
        return page

    def _build_step2(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(12)

        intro = meta_label(
            t(
                "Kısa bir cümle kaydet ve hoparlörden dinle; "
                "kayıt yalnız bellekte tutulur, diske yazılmaz."
            )
        )
        layout.addWidget(intro)

        self.record_bar = QProgressBar()
        self.record_bar.setObjectName("levelMeter")
        self.record_bar.setRange(0, 100)
        self.record_bar.setValue(0)
        self.record_bar.setTextVisible(False)
        self.record_bar.setFixedHeight(8)
        layout.addWidget(self.record_bar)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.record_button = QPushButton(t("2.5 sn kaydet ve çal"))
        self.record_button.setAutoDefault(False)
        self.record_button.clicked.connect(self.start_record)
        self.replay_button = QPushButton(t("Tekrar çal"))
        self.replay_button.setAutoDefault(False)
        self.replay_button.setEnabled(False)
        self.replay_button.clicked.connect(self.start_replay)
        actions.addWidget(self.record_button)
        actions.addWidget(self.replay_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.record_summary = meta_label(t("Henüz kayıt alınmadı."))
        layout.addWidget(self.record_summary)
        self.record_hint = meta_label("")
        layout.addWidget(self.record_hint)
        layout.addStretch(1)
        return page

    def _build_step3(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(12)

        intro = meta_label(
            t("Kaydedilen ses yalnız yerel Whisper modeliyle yazıya çevrilir; bulut kullanılmaz.")
        )
        layout.addWidget(intro)

        actions = QHBoxLayout()
        self.stt_run_button = QPushButton(t("Tanımayı çalıştır"))
        self.stt_run_button.setAutoDefault(False)
        self.stt_run_button.clicked.connect(self.start_stt_test)
        actions.addWidget(self.stt_run_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.stt_status = meta_label(t("Tanıma testi bekleniyor."))
        layout.addWidget(self.stt_status)

        result_box = QFrame()
        result_box.setObjectName("card")
        box_layout = QVBoxLayout(result_box)
        box_layout.setContentsMargins(12, 12, 12, 12)
        self.stt_result = QLabel("")
        self.stt_result.setTextFormat(Qt.TextFormat.PlainText)
        self.stt_result.setWordWrap(True)
        box_layout.addWidget(self.stt_result)
        layout.addWidget(result_box)
        layout.addStretch(1)
        return page

    def go_to_step(self, index: int) -> None:
        clamped = max(0, min(2, index))
        if clamped != 0:
            self._stop_meter()
        self.stack.setCurrentIndex(clamped)
        step_names = (
            t("Seviye ölçümü"),
            t("Kayıt ve geri çalma"),
            t("Yerel konuşma tanıma"),
        )
        self.step_label.setText(
            t("Adım {step}/3 · {name}").format(step=clamped + 1, name=step_names[clamped])
        )
        self.back_button.setEnabled(clamped > 0)
        self.next_button.setText(t("Bitir") if clamped == 2 else t("İleri"))
        if clamped == 0 and self._auto_start:
            self.start_meter()
        elif clamped == 2:
            stt_running = self._stt_thread is not None and self._stt_thread.isRunning()
            self.stt_run_button.setEnabled(bool(self._recorded_pcm) and not stt_running)
            if self._auto_start and self._recorded_pcm and not self._stt_ran_for_pcm:
                self.start_stt_test()

    def _prev_step(self) -> None:
        self.go_to_step(self.stack.currentIndex() - 1)

    def _next_step(self) -> None:
        current = self.stack.currentIndex()
        if current >= 2:
            self.accept()
            return
        self.go_to_step(current + 1)

    def start_meter(self) -> None:
        self._stop_meter()
        thread = MicMeterThread(self._mic_factory, self)
        thread.level.connect(self._on_meter_level)
        thread.failed.connect(self._on_meter_failed)
        thread.finished.connect(self._on_meter_finished)
        thread.finished.connect(thread.deleteLater)
        self._meter_thread = thread
        self.meter_status.setText(t("Mikrofon dinleniyor…"))
        thread.start()

    def _stop_meter(self) -> None:
        if shutdown_thread(
            self._meter_thread,
            ("level", self._on_meter_level),
            ("failed", self._on_meter_failed),
            ("finished", self._on_meter_finished),
            wait_ms=_SHUTDOWN_WAIT_MS,
        ) or (self._meter_thread is not None and not self._meter_thread.isRunning()):
            self._meter_thread = None

    def _on_meter_level(self, value: int) -> None:
        self.meter_bar.setValue(value)
        self.meter_value.setText(t("%{value}").format(value=value))

    def _on_meter_failed(self, reason: str) -> None:
        self.meter_status.setText(t("Mikrofon okunamadı: {reason}").format(reason=reason))

    def _on_meter_finished(self) -> None:
        self._meter_thread = None

    def start_record(self) -> None:
        if self._record_thread is not None and self._record_thread.isRunning():
            return
        self._stop_meter()
        self.record_button.setEnabled(False)
        self.replay_button.setEnabled(False)
        self.record_hint.setText("")
        self.record_summary.setText(t("Kaydediliyor…"))
        thread = MicRecordPlayThread(
            self._mic_factory,
            self._sink_factory,
            duration_seconds=self._duration,
            sample_rate=self._config.sample_rate,
            frame_ms=self._config.frame_ms,
            parent=self,
        )
        thread.level.connect(self.record_bar.setValue)
        thread.phase.connect(self._on_record_phase)
        thread.finished_stats.connect(self._on_record_done)
        thread.failed.connect(self._on_record_failed)
        thread.finished.connect(self._on_record_finished)
        thread.finished.connect(thread.deleteLater)
        self._record_thread = thread
        thread.start()

    def _on_record_phase(self, phase: str) -> None:
        if phase == "recording":
            self.record_summary.setText(t("Kaydediliyor…"))
        elif phase == "playback":
            self.record_summary.setText(t("Kayıt çalınıyor…"))

    def _on_record_done(self, stats: MicRecordingStats) -> None:
        self._recorded_pcm = stats.pcm
        self._recorded_rate = stats.sample_rate
        self._stt_ran_for_pcm = False
        self.replay_button.setEnabled(bool(stats.pcm))
        stt_running = self._stt_thread is not None and self._stt_thread.isRunning()
        self.stt_run_button.setEnabled(bool(stats.pcm) and not stt_running)
        self.record_summary.setText(
            t("Ortalama: %{avg} · Tepe: %{peak} · Gürültü tabanı: %{floor}").format(
                avg=stats.rms_avg,
                peak=stats.rms_peak,
                floor=stats.noise_floor,
            )
        )
        if not stats.pcm:
            self.record_hint.setText(t("Mikrofondan ses verisi alınamadı."))
        elif stats.quality == "too_quiet":
            self.record_hint.setText(
                t("Ses çok düşük görünüyor; mikrofona yaklaş veya kazancı artır.")
            )
        elif stats.quality == "noisy":
            self.record_hint.setText(
                t("Arka plan gürültüsü yüksek; Gürültü engellemeyi açmayı dene.")
            )
        else:
            self.record_hint.setText(t("Kayıt ve oynatma net görünüyor."))

    def _on_record_failed(self, reason: str) -> None:
        self.record_summary.setText(t("Kayıt/çalma başarısız: {reason}").format(reason=reason))

    def _on_record_finished(self) -> None:
        self._record_thread = None
        self.record_button.setEnabled(True)
        self.replay_button.setEnabled(bool(self._recorded_pcm))
        stt_running = self._stt_thread is not None and self._stt_thread.isRunning()
        self.stt_run_button.setEnabled(bool(self._recorded_pcm) and not stt_running)

    def start_replay(self) -> None:
        if not self._recorded_pcm or (
            self._playback_thread is not None and self._playback_thread.isRunning()
        ):
            return
        self.replay_button.setEnabled(False)
        thread = MicPlaybackThread(
            self._sink_factory, self._recorded_pcm, self._recorded_rate, self
        )
        thread.done.connect(self._on_replay_done)
        thread.failed.connect(self._on_replay_failed)
        thread.finished.connect(self._on_replay_finished)
        thread.finished.connect(thread.deleteLater)
        self._playback_thread = thread
        thread.start()

    def _on_replay_done(self) -> None:
        self.record_hint.setText(t("Kayıt yeniden çalındı."))

    def _on_replay_failed(self, reason: str) -> None:
        self.record_hint.setText(t("Kayıt çalınamadı: {reason}").format(reason=reason))

    def _on_replay_finished(self) -> None:
        self._playback_thread = None
        self.replay_button.setEnabled(bool(self._recorded_pcm))

    def start_stt_test(self) -> None:
        if not self._recorded_pcm:
            self.stt_status.setText(t("Önce 2. adımda kısa bir ses kaydı al."))
            return
        if self._stt_thread is not None and self._stt_thread.isRunning():
            return
        self.stt_run_button.setEnabled(False)
        self.stt_status.setText(t("Yerel model ile konuşma tanınıyor…"))
        self.stt_result.setText("")
        thread = MicSttTestThread(self._recorded_pcm, self._recognizer_factory, self)
        thread.done.connect(self._on_stt_done)
        thread.failed.connect(self._on_stt_failed)
        thread.finished.connect(self._on_stt_finished)
        thread.finished.connect(thread.deleteLater)
        self._stt_thread = thread
        thread.start()

    def _on_stt_done(self, text: str) -> None:
        self._stt_ran_for_pcm = True
        if text:
            self.stt_status.setText(t("Yerel tanıma başarılı."))
            self.stt_result.setText(text)
        else:
            self.stt_status.setText(
                t("Konuşma tanınamadı; daha net bir cümleyle 2. adımda tekrar kaydet.")
            )
            self.stt_result.setText("")

    def _on_stt_failed(self, reason: str) -> None:
        self.stt_status.setText(t("Yerel tanıma başarısız: {reason}").format(reason=reason))

    def _on_stt_finished(self) -> None:
        self._stt_thread = None
        self.stt_run_button.setEnabled(bool(self._recorded_pcm))

    def finish_threads(self) -> None:
        """Açık tüm arka plan test iş parçacıklarını güvenle durdurur."""
        self._stop_meter()
        if shutdown_thread(
            self._record_thread,
            ("level", self.record_bar.setValue),
            ("phase", self._on_record_phase),
            ("finished_stats", self._on_record_done),
            ("failed", self._on_record_failed),
            ("finished", self._on_record_finished),
            wait_ms=_SHUTDOWN_WAIT_MS,
        ) or (self._record_thread is not None and not self._record_thread.isRunning()):
            self._record_thread = None
        if shutdown_thread(
            self._playback_thread,
            ("done", self._on_replay_done),
            ("failed", self._on_replay_failed),
            ("finished", self._on_replay_finished),
            wait_ms=_SHUTDOWN_WAIT_MS,
        ) or (self._playback_thread is not None and not self._playback_thread.isRunning()):
            self._playback_thread = None
        if shutdown_thread(
            self._stt_thread,
            ("done", self._on_stt_done),
            ("failed", self._on_stt_failed),
            ("finished", self._on_stt_finished),
            wait_ms=_SHUTDOWN_WAIT_MS,
        ) or (self._stt_thread is not None and not self._stt_thread.isRunning()):
            self._stt_thread = None

    def accept(self) -> None:
        self.finish_threads()
        super().accept()

    def reject(self) -> None:
        self.finish_threads()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.finish_threads()
        super().closeEvent(event)


def open_mic_wizard(panel: SettingsPanel) -> int:
    """Ayarlar > Ses > Dinleme'den 3 adımlı Mikrofon Test Sihirbazı'nı açar."""
    try:
        voice_cfg = load_settings(panel.config_path).voice
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        voice_cfg = VoiceConfig()
    device = panel.device_combo.currentData()
    if device is not None or panel.device_combo.currentIndex() == 0:
        voice_cfg = voice_cfg.model_copy(update={"input_device": device})
    device_label = panel.device_combo.currentText().strip()
    wizard = MicTestWizard(voice_cfg, device_label=device_label, parent=panel)
    return wizard.exec()
