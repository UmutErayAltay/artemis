"""Mikrofon, uygulama ve GitHub seçimleri; modeller ProviderSection'dadır."""

from __future__ import annotations

import importlib
import importlib.util
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.channels.desktop.provider_section import ProviderSection
from jarvis.channels.voice import stt_catalog
from jarvis.config.settings import GITHUB_OWNER_RE, Settings, load_settings
from jarvis.tools.github_repos import GitHubError, Runner, SyncReport, run_command, sync_repos


def save_choices(
    path: Path, device: int | str | None,
    *, applications: dict[str, str] | None = None,
    allow_all_applications: bool | None = None,
    code_agent: dict[str, Any] | None = None,
    continuous_mode: bool | None = None,
    noise_suppression: bool | None = None,
    noise_suppression_level: str | None = None,
    noise_suppression_stt: bool | None = None,
    echo_cancellation: bool | None = None,
    stt_model: Path | None = None,
    tts_backend: str | None = None,
    computer_use_mode: str | None = None,
) -> None:
    """Diğer alanları (model seçimi dahil) korur; doğrulayıp aynı dizinde atomik değiştirir."""
    load_settings(path)  # bozuk config'i kaydetmeden önce yakalar
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("voice", {})["input_device"] = device
    if continuous_mode is not None:
        raw["voice"]["continuous_mode"] = continuous_mode
    if noise_suppression is not None:
        raw["voice"]["noise_suppression"] = noise_suppression
    if noise_suppression_level is not None:
        raw["voice"]["noise_suppression_level"] = noise_suppression_level
    if noise_suppression_stt is not None:
        raw["voice"]["noise_suppression_stt"] = noise_suppression_stt
    if echo_cancellation is not None:
        raw["voice"]["echo_cancellation"] = echo_cancellation
    if stt_model is not None:
        raw["voice"]["stt_model"] = str(stt_model)
    if tts_backend is not None:
        raw["voice"]["tts_backend"] = tts_backend
    if applications is not None:
        raw["applications"] = applications
        raw["close_without_confirmation"] = [
            name for name in raw.get("close_without_confirmation", []) if name in applications
        ]
    if allow_all_applications is not None:
        raw["allow_all_applications"] = allow_all_applications
    if code_agent is not None:
        _merge_code_agent(raw, code_agent)
    if computer_use_mode is not None:
        raw.setdefault("computer_use", {})["mode"] = computer_use_mode
    _write_validated(path, raw)


def save_computer_mode(path: Path, mode: str) -> None:
    """Yalnız bilgisayar kullanımı kipini yazar; üst çubuktaki düğme Uygula beklemez."""
    if mode not in {"accept", "auto"}:
        raise ValueError(f"Geçersiz bilgisayar kullanımı kipi: {mode!r}")
    load_settings(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("computer_use", {})["mode"] = mode
    _write_validated(path, raw)


def save_code_agent(path: Path, code_agent: dict[str, Any]) -> None:
    """Yalnız code_agent alanları; model/cihaz seçimine bağlı değil (GitHub bağı hemen kalıcı)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    _merge_code_agent(raw, code_agent)
    _write_validated(path, raw)


def _merge_code_agent(raw: dict[str, Any], code_agent: dict[str, Any]) -> None:
    # None değer alanı siler (örn. GitHub bağını kaldırmak); diğer code_agent alanları korunur.
    section = raw.get("code_agent") or {}
    for key, value in code_agent.items():
        if value is None:
            section.pop(key, None)
        else:
            section[key] = value
    raw["code_agent"] = section


def _write_validated(path: Path, raw: dict[str, Any]) -> None:
    Settings.model_validate(raw)
    descriptor, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yaml.safe_dump(raw, stream, allow_unicode=True, sort_keys=False)
        load_settings(Path(temporary))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def microphone_choices(
    devices: list[Any], hosts: list[Any], active: int | str,
    *, advanced: bool = False,
) -> list[tuple[str, int, list[int]]]:
    """Aynı fiziksel mikrofonun sürücü kopyalarını tek seçenekte birleştirir."""
    inputs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    wasapi = [(i, d) for i, d in inputs if hosts[d["hostapi"]]["name"] == "Windows WASAPI"]
    canonical = wasapi or sorted(inputs, key=lambda pair: -len(pair[1]["name"]))
    result: list[tuple[str, int, list[int]]] = []
    grouped: set[int] = set()
    for _index, device in canonical:
        name = device["name"].strip()
        if any(word in name.casefold() for word in (
            "ses eşleştiricisi", "birincil ses", "sound mapper"
        )):
            continue
        if _index in grouped:
            continue
        matches = [
            (i, d) for i, d in inputs
            if name.casefold().startswith(d["name"].strip().casefold())
        ]
        indices = [i for i, _ in matches]
        grouped.update(indices)
        # MME burada 16 kHz mono akışıyla uyumlu mevcut sürücü yolunu korur.
        selected = min(matches, key=lambda pair: hosts[pair[1]["hostapi"]]["name"] != "MME")[0]
        if isinstance(active, int) and active in indices:
            selected = active
        if name.startswith("Microphone Array ("):
            name = "Dahili mikrofon — " + name[len("Microphone Array ("):].rstrip(")")
        elif name.startswith("Mikrofon ("):
            name = "Mikrofon — " + name[len("Mikrofon ("):].rstrip(")")
        result.append((name, selected, indices))
    if advanced and wasapi:
        seen = {name.casefold() for name, _, _ in result}
        for index, device in inputs:
            name = device["name"].strip()
            if index in grouped or name.casefold() in seen:
                continue
            if any(word in name.casefold() for word in (
                "ses eşleştiricisi", "birincil ses", "sound mapper"
            )):
                continue
            seen.add(name.casefold())
            result.append((name, index, [index]))
    return result


GitHubSyncFunction = Callable[[str, Path, Runner, Callable[[], bool]], SyncReport]


class GitHubSync(QThread):
    """gh/git alt süreçlerini GUI dışında koşar."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(
        self, owner: str, repos_dir: Path, sync: GitHubSyncFunction, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.owner = owner
        self.repos_dir = repos_dir
        self._sync = sync

    def run(self) -> None:
        try:
            self.done.emit(
                self._sync(self.owner, self.repos_dir, run_command, self.isInterruptionRequested)
            )
        except GitHubError as error:
            self.failed.emit(str(error))


class SttDownload(QThread):
    """Whisper modelini GUI olay döngüsünü kilitlemeden indirir."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name

    def run(self) -> None:
        try:
            path = stt_catalog.download(self.name)
        except Exception as error:
            # Hata iletisi yol veya belirteç taşıyabilir; yalnız sınıf adı gösterilir.
            self.failed.emit(type(error).__name__)
        else:
            self.done.emit(path)


def default_repos_dir() -> str:
    return str(Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop" / "Projeler")


class SettingsPanel(QWidget):
    apply_requested = Signal(object)  # mikrofon aygıtı; model seçimi ProviderSection'da
    # (SyncReport, otomatik_mi): otomatik eşitleme sonucu pencere tarafından baloncukla duyurulur.
    github_synced = Signal(object, bool)

    def __init__(
        self,
        config_path: Path,
        *,
        demo: bool = False,
        github_sync: GitHubSyncFunction = sync_repos,
    ) -> None:
        super().__init__()
        self.config_path = config_path
        self._demo = demo
        self._gpu_mb = stt_catalog.gpu_memory_mb()
        self._recommended_stt = stt_catalog.recommended(self._gpu_mb)
        self.github: GitHubSync | None = None
        self.stt_download_thread: SttDownload | None = None
        self._github_sync = github_sync
        self._github_auto = False
        self.applications: dict[str, str] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)
        title = QLabel("Ayarlar")
        title.setObjectName("title")
        layout.addWidget(title)
        self.sections = QTabWidget()
        layout.addWidget(self.sections, 1)

        def tab_page() -> tuple[QScrollArea, QVBoxLayout]:
            """Her bölümü kendi kaydırılabilir sayfasına yerleştirir."""
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            content = QWidget()
            page_layout = QVBoxLayout(content)
            page_layout.setContentsMargins(0, 12, 0, 0)
            page_layout.setSpacing(10)
            scroll.setWidget(content)
            return scroll, page_layout

        voice_scroll, voice_layout = tab_page()
        self.sections.addTab(voice_scroll, "Ses")
        model_scroll, model_layout = tab_page()
        self.sections.addTab(model_scroll, "Model")
        self.providers = ProviderSection(config_path, demo=demo)
        model_layout.addWidget(self.providers)
        model_layout.addStretch(1)
        form = QFormLayout()
        self.device_combo = QComboBox()
        self.device_combo.setAccessibleName("Mikrofon girişi")
        self.current_device = QLabel()
        self.current_device.setWordWrap(True)
        form.addRow("Mikrofon girişi", self.device_combo)
        form.addRow("Kullanılan mikrofon", self.current_device)
        self.input_level = QProgressBar()
        self.input_level.setRange(0, 100)
        self.input_level.setValue(0)
        self.input_level.setFormat("Mikrofon seviyesi: %p%")
        form.addRow("Ses girişi", self.input_level)
        self.last_heard = QLabel("Henüz konuşma algılanmadı.")
        self.last_heard.setTextFormat(Qt.TextFormat.PlainText)
        self.last_heard.setWordWrap(True)
        form.addRow("Son duyulan", self.last_heard)
        stt_box = QWidget()
        stt_layout = QVBoxLayout(stt_box)
        stt_layout.setContentsMargins(0, 0, 0, 0)
        stt_controls = QHBoxLayout()
        stt_controls.setContentsMargins(0, 0, 0, 0)
        self.stt_combo = QComboBox()
        self.stt_combo.setAccessibleName("Tanıma modeli")
        self.stt_download = QPushButton("İndir")
        stt_controls.addWidget(self.stt_combo, 1)
        stt_controls.addWidget(self.stt_download)
        stt_layout.addLayout(stt_controls)
        self.stt_status = QLabel()
        self.stt_status.setWordWrap(True)
        self.stt_status.setTextFormat(Qt.TextFormat.PlainText)
        stt_layout.addWidget(self.stt_status)
        form.addRow("Tanıma modeli", stt_box)
        self.tts_combo = QComboBox()
        self.tts_combo.setAccessibleName("Konuşma sesi")
        self.tts_combo.setToolTip("Değişiklik ses yeniden başlayınca uygulanır.")
        self.tts_combo.addItem("Edge · Emel (bulut, internet gerekir)", "edge")
        self.tts_combo.addItem("Piper (yerel)", "piper")
        ema_missing = importlib.util.find_spec("ema_lightning") is None
        ema_text = "EMA (yerel, Türkçe)"
        if ema_missing:
            ema_text += " · kurulu değil"
        self.tts_combo.addItem(ema_text, "ema")
        if ema_missing:
            ema_index = self.tts_combo.count() - 1
            self.tts_combo.model().item(ema_index).setEnabled(False)  # type: ignore[attr-defined]
        form.addRow("Konuşma sesi", self.tts_combo)
        self.stt_combo.currentIndexChanged.connect(self._stt_selection_changed)
        self.stt_download.clicked.connect(self._download_stt)
        self._populate_stt_choices(None)
        voice_layout.addLayout(form)
        self.continuous_mode = QCheckBox("Kesintisiz sohbet (Artemis demeden dinle)")
        voice_layout.addWidget(self.continuous_mode)
        self.noise_suppression = QCheckBox("Gürültü engelleme")
        self.noise_suppression.setToolTip(
            "DeepFilterNet3; işlemcide çalışır, ekran kartı gerekmez. Yankı gidermez. "
            "Model pakete dahildir; indirme gerekmez. "
            "Değişiklik ses yeniden başlayınca uygulanır."
        )
        voice_layout.addWidget(self.noise_suppression)
        self.echo_cancellation = QCheckBox("Yankı engelleme (hoparlörle kullanırken)")
        self.echo_cancellation.setToolTip(
            "WebRTC yankı giderici; Jarvis'in hoparlörden çıkan kendi sesini mikrofondan "
            "siler, böylece konuşurken araya girmek ve kendi sesini komut sanmamak güvenilir "
            "olur. Yerelde çalışır. Kulaklıkla gerekmez. Değişiklik ses yeniden başlayınca "
            "uygulanır."
        )
        voice_layout.addWidget(self.echo_cancellation)
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("Gelişmiş ses ayarları")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        voice_layout.addWidget(self.advanced_toggle)
        self.advanced_box = QWidget()
        advanced_layout = QVBoxLayout(self.advanced_box)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(10)
        noise_level_label = QLabel("Gürültü engelleme düzeyi")
        noise_level_label.setObjectName("muted")
        noise_level_label.setStyleSheet("font-size: 12px;")
        self.noise_level = QComboBox()
        for label, key in (("Hafif", "hafif"), ("Orta", "orta"), ("Güçlü", "guclu")):
            self.noise_level.addItem(label, key)
        self.noise_level.setAccessibleName("Gürültü engelleme düzeyi")
        self.noise_level.setToolTip(
            "Düzey bastırma sınırıdır; algılamayı etkiler. Tanıma varsayılan olarak ham sesi alır."
        )
        self.noise_level.setEnabled(False)
        self.noise_suppression.toggled.connect(self.noise_level.setEnabled)
        level_row = QHBoxLayout()
        level_row.addWidget(noise_level_label)
        level_row.addWidget(self.noise_level, 1)
        advanced_layout.addLayout(level_row)
        self.noise_stt = QCheckBox("Tanımaya da temizlenmiş ses ver")
        self.noise_stt.setToolTip(
            "Kapalıyken (önerilen) temizlenmiş ses yalnız konuşma algılamaya, cümle bitişine ve "
            "söz kesmeye gider; konuşma tanıma aynı aralığın ham sesini alır. Ölçümlerde Whisper "
            "temizlenmiş sesle çoğunlukla daha kötü sonuç verdi."
        )
        self.noise_stt.setEnabled(False)
        self.noise_suppression.toggled.connect(self.noise_stt.setEnabled)
        advanced_layout.addWidget(self.noise_stt)
        self.advanced_devices = QCheckBox("Sanal ve diğer ses girişlerini göster")
        self.advanced_devices.toggled.connect(self._toggle_device_detail)
        advanced_layout.addWidget(self.advanced_devices)
        self.refresh_button = QPushButton("Mikrofon listesini yenile")
        self.refresh_button.clicked.connect(self.refresh)
        advanced_layout.addWidget(self.refresh_button)
        voice_layout.addWidget(self.advanced_box)
        self.advanced_box.setVisible(False)
        voice_layout.addStretch(1)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        app_scroll, app_layout = tab_page()
        self.sections.addTab(app_scroll, "Uygulamalar")
        self.all_applications = QCheckBox("Bütün kurulu uygulamalara erişim")
        self.all_applications.toggled.connect(self._sync_application_access)
        app_layout.addWidget(self.all_applications)
        self.computer_auto = QCheckBox("Bilgisayar kullanımında otomatik kip (adımları sormaz)")
        self.computer_auto.setToolTip(
            "Hassas adımlar ve temiz turda başlangıç sorulmaz; Esc her an durdurur. Terminal, "
            "Jarvis ve parola yöneticisi engeli ile dış içerikten sonraki onay yine geçerlidir."
        )
        app_layout.addWidget(self.computer_auto)
        self.application_list = QListWidget()
        self.application_list.setMaximumHeight(140)
        self.application_list.setAccessibleName("İzinli uygulamalar")
        app_layout.addWidget(self.application_list)
        app_actions = QHBoxLayout()
        self.add_application_button = QPushButton("Uygulama ekle")
        self.add_application_button.clicked.connect(self._add_application)
        self.remove_application_button = QPushButton("Listeden çıkar")
        self.remove_application_button.clicked.connect(self._remove_application)
        app_actions.addWidget(self.add_application_button)
        app_actions.addWidget(self.remove_application_button)
        app_layout.addLayout(app_actions)
        self.application_hint = QLabel(
            "Jarvis listedeki uygulamaları açabilir. Yeni uygulama ekleyip Uygula'ya bas."
        )
        self.application_hint.setWordWrap(True)
        app_layout.addWidget(self.application_hint)
        app_layout.addStretch(1)
        github_scroll, github_layout = tab_page()
        self.sections.addTab(github_scroll, "GitHub")
        github_form = QFormLayout()
        self.github_owner = QLineEdit()
        self.github_owner.setAccessibleName("GitHub kullanıcı adı")
        self.github_owner.setPlaceholderText("kullanıcı-adı")
        self.repos_dir = QLineEdit()
        self.repos_dir.setAccessibleName("Repo klasörü")
        self.repos_dir.setPlaceholderText(default_repos_dir())
        self.repos_browse = QPushButton("Klasör seç")
        self.repos_browse.clicked.connect(self._browse_repos_dir)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.repos_dir, 1)
        folder_row.addWidget(self.repos_browse)
        github_form.addRow("Kullanıcı adı", self.github_owner)
        github_form.addRow("Repo klasörü", folder_row)
        github_layout.addLayout(github_form)
        self.github_auto = QCheckBox("Otomatik eşitle")
        self.github_auto.setChecked(True)
        github_layout.addWidget(self.github_auto)
        self.github_button = QPushButton("Bağla ve repoları çek")
        self.github_button.clicked.connect(self._bind_github)
        github_layout.addWidget(self.github_button)
        self.github_status = QLabel(
            "Kendi repoların (fork ve arşivlenmişler hariç) kod ajanının erişimine girer. "
            "Kimlik doğrulamayı gh yapar; Jarvis token görmez."
        )
        self.github_status.setWordWrap(True)
        self.github_status.setTextFormat(Qt.TextFormat.PlainText)
        github_layout.addWidget(self.github_status)
        self.github_list = QListWidget()
        self.github_list.setMaximumHeight(120)
        self.github_list.setAccessibleName("GitHub eşitleme sonuçları")
        github_layout.addWidget(self.github_list)
        github_layout.addStretch(1)
        self.sections.setCurrentIndex(0)
        self.notice = QLabel(
            "Uygula mikrofon, uygulama ve GitHub ayarlarını kaydeder; yeni sohbet başlar."
        )
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.current_device.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice)
        self.apply_button = QPushButton("Uygula")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self._apply)
        layout.addWidget(self.apply_button)
        if demo:
            self.notice.setText("Demo modunda ayarlar değiştirilmez.")
            self.refresh_button.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.add_application_button.setEnabled(False)
            self.remove_application_button.setEnabled(False)
            self.advanced_devices.setEnabled(False)
            self.continuous_mode.setEnabled(False)
            self.noise_suppression.setEnabled(False)
            self.noise_stt.setEnabled(False)
            self.echo_cancellation.setEnabled(False)
            self.stt_combo.setEnabled(False)
            self.tts_combo.setEnabled(False)
            self.stt_download.setEnabled(False)
            self.all_applications.setEnabled(False)
            self.computer_auto.setEnabled(False)
            for widget in (self.github_owner, self.repos_dir, self.repos_browse,
                           self.github_auto, self.github_button):
                widget.setEnabled(False)
        else:
            self.refresh()

    def _toggle_advanced(self, checked: bool) -> None:
        """Gelişmiş ses ayarlarını açar ve ok yönünü duruma göre değiştirir."""
        self.advanced_box.setVisible(checked)
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )

    @staticmethod
    def _stt_size_text(choice: stt_catalog.WhisperChoice) -> str:
        if choice.size_mb < 1000:
            return f"{choice.size_mb} MB"
        return f"{choice.size_mb / 1000:.1f} GB"

    def _populate_stt_choices(self, selected_name: str | None) -> None:
        was_blocked = self.stt_combo.blockSignals(True)
        try:
            self.stt_combo.clear()
            if selected_name is None:
                self.stt_combo.addItem("Özel model (config'teki yol)", None)
            for choice in stt_catalog.CATALOG:
                text = f"{choice.label} · {self._stt_size_text(choice)}"
                if choice.name == self._recommended_stt:
                    text += " · önerilen"
                if stt_catalog.installed_path(choice.name) is not None:
                    text += " · kurulu"
                self.stt_combo.addItem(text, choice.name)
                index = self.stt_combo.count() - 1
                self.stt_combo.setItemData(
                    index, choice.note, Qt.ItemDataRole.ToolTipRole
                )
            if selected_name is None:
                self.stt_combo.setCurrentIndex(0)
            else:
                index = self.stt_combo.findData(selected_name)
                if index >= 0:
                    self.stt_combo.setCurrentIndex(index)
        finally:
            self.stt_combo.blockSignals(was_blocked)
        self._stt_selection_changed()

    def _selected_stt_choice(self) -> stt_catalog.WhisperChoice | None:
        name = self.stt_combo.currentData()
        if not isinstance(name, str):
            return None
        return next(
            (choice for choice in stt_catalog.CATALOG if choice.name == name),
            None,
        )

    def _stt_selection_changed(self, *_args: object) -> None:
        choice = self._selected_stt_choice()
        if choice is None:
            self.stt_status.setText("Config'teki özel model yolu kullanılıyor.")
        else:
            self.stt_status.setText(choice.note)
        self._update_stt_download_button()

    def _update_stt_download_button(self) -> None:
        choice = self._selected_stt_choice()
        running = (
            self.stt_download_thread is not None
            and self.stt_download_thread.isRunning()
        )
        self.stt_download.setEnabled(
            not self._demo
            and choice is not None
            and not running
            and stt_catalog.installed_path(choice.name) is None
        )

    def stt_model_path(self) -> Path | None:
        """Seçili katalog modelinin kullanılabilir yerel yolunu döndürür."""
        choice = self._selected_stt_choice()
        if choice is None:
            return None
        return stt_catalog.installed_path(choice.name)

    def _download_stt(self, *_args: object) -> None:
        if self._demo or self.stt_download_thread is not None:
            return
        choice = self._selected_stt_choice()
        if choice is None:
            return
        if stt_catalog.installed_path(choice.name) is not None:
            self._stt_selection_changed()
            return

        thread = SttDownload(choice.name, self)
        thread.done.connect(self._stt_download_done)
        thread.failed.connect(self._stt_download_failed)
        thread.finished.connect(self._stt_download_finished)
        thread.finished.connect(thread.deleteLater)
        self.stt_download_thread = thread
        self.stt_combo.setEnabled(False)
        self.stt_download.setEnabled(False)
        self.stt_status.setText(
            f"İndiriliyor… ({self._stt_size_text(choice)})"
        )
        thread.start()

    def _stt_download_done(self, _path: Path) -> None:
        choice = self._selected_stt_choice()
        self._populate_stt_choices(choice.name if choice is not None else None)
        self.stt_status.setText("İndirildi; Uygula'ya bas.")

    def _stt_download_failed(self, error_type: str) -> None:
        self.stt_status.setText(f"İndirilemedi: {error_type}")

    def _stt_download_finished(self) -> None:
        self.stt_download_thread = None
        self.stt_combo.setEnabled(not self._demo)
        self._update_stt_download_button()

    def refresh(self) -> None:
        try:
            settings = load_settings(self.config_path)
            self.providers.reload()
            self._refresh_devices(settings)
            self._populate_stt_choices(
                stt_catalog.choice_for_path(settings.voice.stt_model)
            )
            tts_index = self.tts_combo.findData(settings.voice.tts_backend)
            if tts_index >= 0:
                self.tts_combo.setCurrentIndex(tts_index)
            # Yenileme kaydedilmiş değeri gösterir; canlı ses kipini değiştirmemeli.
            self.continuous_mode.blockSignals(True)
            self.continuous_mode.setChecked(settings.voice.continuous_mode)
            self.continuous_mode.blockSignals(False)
            self.noise_suppression.setChecked(settings.voice.noise_suppression)
            self.noise_level.setCurrentIndex(
                max(0, self.noise_level.findData(settings.voice.noise_suppression_level))
            )
            self.noise_level.setEnabled(self.noise_suppression.isChecked() and not self._demo)
            self.noise_stt.setChecked(settings.voice.noise_suppression_stt)
            self.noise_stt.setEnabled(self.noise_suppression.isChecked() and not self._demo)
            self.echo_cancellation.setChecked(settings.voice.echo_cancellation)
            self.applications = dict(settings.applications)
            self._refresh_applications()
            self.all_applications.setChecked(settings.allow_all_applications)
            self.computer_auto.setChecked(settings.computer_use.mode == "auto")
            self._sync_application_access()
            agent = settings.code_agent
            self.github_owner.setText(agent.github_owner or "")
            self.repos_dir.setText(str(agent.repos_dir) if agent.repos_dir else "")
            self.github_auto.setChecked(agent.auto_sync)
        except (OSError, ValueError, KeyError):
            self.notice.setText("Ayarlar okunamadı.")
            return

    def _refresh_devices(self, settings: Settings) -> None:
        self.device_combo.clear()
        self.device_combo.addItem("Sistem varsayılanı", None)
        audio_errors: tuple[type[Exception], ...] = (ImportError, OSError, RuntimeError, ValueError)
        try:
            sd = importlib.import_module("sounddevice")
            audio_errors = (*audio_errors, sd.PortAudioError)
            devices = sd.query_devices()
            hosts = sd.query_hostapis()
            active: Any = settings.voice.input_device
            if active is None:
                active = int(sd.default.device[0])
            active_name = str(active)
            choices = microphone_choices(
                list(devices), list(hosts), active,
                advanced=self.advanced_devices.isChecked(),
            )
            for label, index, indices in choices:
                self.device_combo.addItem(label, index)
                if active in indices or (isinstance(active, str) and active in label):
                    active_name = label
                if settings.voice.input_device in indices:
                    self.device_combo.setCurrentIndex(self.device_combo.count() - 1)
            if active_name == str(active):
                self.device_combo.addItem(f"Mevcut giriş ({active})", settings.voice.input_device)
                self.device_combo.setCurrentIndex(self.device_combo.count() - 1)
            if self.device_combo.count() > 1:
                self.device_combo.setItemText(0, "Sistem varsayılanı")
            self.current_device.setText(active_name)
        except audio_errors:
            self.current_device.setText("Mikrofon aygıtları okunamadı.")

    def _toggle_device_detail(self) -> None:
        if self._demo:
            return
        try:
            self._refresh_devices(load_settings(self.config_path))
        except (OSError, ValueError):
            self.notice.setText("Mikrofon ayarı okunamadı.")

    def _refresh_applications(self) -> None:
        friendly = {"brave": "Brave", "notepad": "Not Defteri", "calculator": "Hesap Makinesi",
                    "explorer": "Dosya Gezgini", "paint": "Paint"}
        self.application_list.clear()
        for alias, executable in self.applications.items():
            self.application_list.addItem(f"{friendly.get(alias, alias)} ({alias})")
            item = self.application_list.item(self.application_list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, alias)
            item.setToolTip(executable)

    def _sync_application_access(self) -> None:
        manual = not self.all_applications.isChecked() and not self._demo
        self.application_list.setEnabled(manual)
        self.application_list.setVisible(manual)
        self.add_application_button.setEnabled(manual)
        self.add_application_button.setVisible(manual)
        self.remove_application_button.setEnabled(manual)
        self.remove_application_button.setVisible(manual)
        self.application_hint.setText(
            "Kurulu uygulamalar otomatik bulunur. Silme, kaldırma ve hassas işlemler onay ister."
            if self.all_applications.isChecked()
            else "Yeni uygulama ekleyip Uygula'ya bas."
        )

    def _add_application(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Uygulama seç", "", "Uygulamalar (*.exe)")
        if not filename:
            return
        path = Path(filename)
        if not path.is_file() or path.suffix.casefold() != ".exe":
            self.notice.setText("Bir uygulamanın .exe dosyasını seç.")
            return
        alias = path.stem.casefold()
        if alias in self.applications and Path(self.applications[alias]) != path:
            self.notice.setText("Bu ad listede kayıtlı; önce mevcut kaydı çıkar.")
            return
        self.applications[alias] = str(path)
        self._refresh_applications()
        self.notice.setText("Uygulama eklendi; kaydetmek için Uygula'ya bas.")

    def _remove_application(self) -> None:
        item = self.application_list.currentItem()
        if item is not None:
            del self.applications[item.data(Qt.ItemDataRole.UserRole)]
            self._refresh_applications()
            self.notice.setText("Uygulama listeden çıkarıldı; kaydetmek için Uygula'ya bas.")

    def _apply(self) -> None:
        self.apply_requested.emit(self.device_combo.currentData())

    def github_values(self) -> dict[str, Any]:
        """save_choices(code_agent=...) için; boş alan None olur ve kaydı siler."""
        owner = self.github_owner.text().strip()
        folder = self.repos_dir.text().strip()
        return {
            "github_owner": owner or None,
            "repos_dir": folder or None,
            "auto_sync": self.github_auto.isChecked(),
        }

    def _browse_repos_dir(self) -> None:
        start = self.repos_dir.text().strip() or default_repos_dir()
        chosen = QFileDialog.getExistingDirectory(self, "Repo klasörü seç", start)
        if chosen:
            self.repos_dir.setText(chosen)

    def _bind_github(self) -> None:
        owner = self.github_owner.text().strip()
        if not GITHUB_OWNER_RE.fullmatch(owner):
            self.github_status.setText("Geçerli bir GitHub kullanıcı adı yaz.")
            return
        if not self.repos_dir.text().strip():
            self.repos_dir.setText(default_repos_dir())
        try:
            save_code_agent(self.config_path, self.github_values())
        except (OSError, ValueError, KeyError, TypeError):
            self.github_status.setText("GitHub ayarları kaydedilemedi.")
            return
        self.run_github_sync(auto=False)

    def start_auto_sync(self) -> None:
        """Masaüstü açılışında bir kez; ayarlar eksik veya kapalıysa hiçbir şey yapmaz."""
        if self._demo:
            return
        try:
            agent = load_settings(self.config_path).code_agent
        except (OSError, ValueError):
            return
        if agent.github_owner and agent.repos_dir and agent.auto_sync:
            self.run_github_sync(auto=True)

    def run_github_sync(self, *, auto: bool) -> None:
        if self.github is not None and self.github.isRunning():
            return
        owner = self.github_owner.text().strip()
        folder = self.repos_dir.text().strip()
        if not owner or not folder:
            return
        self._github_auto = auto
        thread = GitHubSync(owner, Path(folder), self._github_sync, self)
        thread.done.connect(self._github_done)
        thread.failed.connect(self._github_failed)
        thread.finished.connect(lambda: self.github_button.setEnabled(not self._demo))
        self.github = thread
        self.github_button.setEnabled(False)
        self.github_list.clear()
        self.github_status.setText(f"{owner} repoları eşitleniyor…")
        thread.start()

    def _github_failed(self, message: str) -> None:
        self.github_status.setText(f"Eşitleme başarısız: {message}")

    def _github_done(self, report: SyncReport) -> None:
        self.github_list.clear()
        for label, names in (("klonlandı", report.cloned), ("güncellendi", report.fetched)):
            for name in names:
                self.github_list.addItem(f"{label}: {name}")
        for label, pairs in (("atlandı", report.skipped), ("başarısız", report.failed)):
            for name, reason in pairs:
                self.github_list.addItem(f"{label}: {name} — {reason}")
        self.github_status.setText(
            f"{len(report.cloned)} klonlandı, {len(report.fetched)} güncellendi, "
            f"{len(report.skipped)} atlandı, {len(report.failed)} başarısız."
        )
        # Yeniden başlatma yok: kod ajanı projeleri her çağrıda klasörden okur.
        self.github_synced.emit(report, self._github_auto)

    def finish_inventory(self) -> None:
        if self.github is not None and self.github.isRunning():
            self.github.requestInterruption()
            self.github.wait(6000)
