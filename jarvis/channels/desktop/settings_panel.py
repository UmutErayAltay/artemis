"""Model ve mikrofon seçimleri; sırlar config dosyasına yazılmaz."""

from __future__ import annotations

import importlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.channels.desktop.provider_section import ProviderSection
from jarvis.config.settings import GITHUB_OWNER_RE, Settings, load_settings
from jarvis.tools.github_repos import GitHubError, Runner, SyncReport, run_command, sync_repos


def save_choices(
    path: Path, model_id: str, device: int | str | None,
    *, applications: dict[str, str] | None = None,
    allow_all_applications: bool | None = None,
    code_agent: dict[str, Any] | None = None,
    continuous_mode: bool | None = None,
) -> None:
    """Diğer alanları korur; doğrulamadan sonra aynı dizinde atomik değiştirir."""
    settings = load_settings(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    matches = [alias for alias, item in settings.models.items() if item.model == model_id]
    if matches:
        raw["primary_model"] = matches[0]
    else:
        base = urlparse(settings.openrouter_base_url)
        primary = settings.models[settings.primary_model]
        if (
            base.hostname not in {"localhost", "127.0.0.1", "::1"}
            or primary.provider != "openrouter"
        ):
            raise ValueError("Yeni model yalnız yapılandırılmış yerel API'den seçilebilir")
        raw["models"]["cpa-selected"] = {
            **primary.model_dump(mode="json"), "model": model_id,
        }
        raw["primary_model"] = "cpa-selected"
    raw.setdefault("voice", {})["input_device"] = device
    if continuous_mode is not None:
        raw["voice"]["continuous_mode"] = continuous_mode
    if applications is not None:
        raw["applications"] = applications
        raw["close_without_confirmation"] = [
            name for name in raw.get("close_without_confirmation", []) if name in applications
        ]
    if allow_all_applications is not None:
        raw["allow_all_applications"] = allow_all_applications
    if code_agent is not None:
        _merge_code_agent(raw, code_agent)
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


class ModelInventory(QThread):
    loaded = Signal(list)
    failed = Signal(str)

    def __init__(self, config_path: Path, parent: QWidget) -> None:
        super().__init__(parent)
        self.config_path = config_path

    def run(self) -> None:
        try:
            settings = load_settings(self.config_path)
            # Uzak kataloglara yeni kimlik bilgisi gönderilmez.
            if urlparse(settings.openrouter_base_url).hostname not in {
                "localhost", "127.0.0.1", "::1"
            }:
                self.failed.emit("Model listesi yapılandırılmış modellerden alınır.")
                return
            key = os.environ.get("OPENROUTER_API_KEY", "")
            response = httpx.get(
                settings.openrouter_base_url + "/models",
                headers={"Authorization": "Bearer " + key}, timeout=5, follow_redirects=False,
            )
            response.raise_for_status()
            models = sorted({
                item["id"] for item in response.json()["data"]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            })
            self.loaded.emit(models)
        except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
            self.failed.emit("API model listesi alınamadı; kayıtlı modeller kullanılabilir.")


GitHubSyncFunction = Callable[[str, Path, Runner, Callable[[], bool]], SyncReport]


class GitHubSync(QThread):
    """gh/git alt süreçlerini GUI dışında koşar (ModelInventory ile aynı kalıp)."""

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


def default_repos_dir() -> str:
    return str(Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop" / "Projeler")


class SettingsPanel(QWidget):
    apply_requested = Signal(str, object)
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
        self.inventory: ModelInventory | None = None
        self.github: GitHubSync | None = None
        self._github_sync = github_sync
        self._github_auto = False
        self.applications: dict[str, str] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)
        title = QLabel("Ayarlar")
        title.setObjectName("title")
        layout.addWidget(title)
        self.providers = ProviderSection(config_path, demo=demo)
        layout.addWidget(self.providers)
        form = QFormLayout()
        self.model_combo = QComboBox()
        self.model_combo.setAccessibleName("Beyin modeli")
        self.device_combo = QComboBox()
        self.device_combo.setAccessibleName("Mikrofon girişi")
        self.current_device = QLabel()
        self.current_device.setWordWrap(True)
        form.addRow("Beyin modeli", self.model_combo)
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
        layout.addLayout(form)
        self.continuous_mode = QCheckBox("Kesintisiz sohbet (Artemis demeden dinle)")
        layout.addWidget(self.continuous_mode)
        self.advanced_devices = QCheckBox("Sanal ve diğer ses girişlerini göster")
        self.advanced_devices.toggled.connect(self._toggle_device_detail)
        layout.addWidget(self.advanced_devices)
        app_title = QLabel("Açılabilecek uygulamalar")
        app_title.setObjectName("section")
        layout.addWidget(app_title)
        self.all_applications = QCheckBox("Bütün kurulu uygulamalara erişim")
        self.all_applications.toggled.connect(self._sync_application_access)
        layout.addWidget(self.all_applications)
        self.application_list = QListWidget()
        self.application_list.setMaximumHeight(140)
        self.application_list.setAccessibleName("İzinli uygulamalar")
        layout.addWidget(self.application_list)
        app_actions = QHBoxLayout()
        self.add_application_button = QPushButton("Uygulama ekle")
        self.add_application_button.clicked.connect(self._add_application)
        self.remove_application_button = QPushButton("Listeden çıkar")
        self.remove_application_button.clicked.connect(self._remove_application)
        app_actions.addWidget(self.add_application_button)
        app_actions.addWidget(self.remove_application_button)
        layout.addLayout(app_actions)
        self.application_hint = QLabel(
            "Jarvis listedeki uygulamaları açabilir. Yeni uygulama ekleyip Uygula'ya bas."
        )
        self.application_hint.setWordWrap(True)
        layout.addWidget(self.application_hint)
        github_title = QLabel("GitHub")
        github_title.setObjectName("section")
        layout.addWidget(github_title)
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
        layout.addLayout(github_form)
        self.github_auto = QCheckBox("Otomatik eşitle")
        self.github_auto.setChecked(True)
        layout.addWidget(self.github_auto)
        self.github_button = QPushButton("Bağla ve repoları çek")
        self.github_button.clicked.connect(self._bind_github)
        layout.addWidget(self.github_button)
        self.github_status = QLabel(
            "Kendi repoların (fork ve arşivlenmişler hariç) kod ajanının erişimine girer. "
            "Kimlik doğrulamayı gh yapar; Jarvis token görmez."
        )
        self.github_status.setWordWrap(True)
        self.github_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.github_status)
        self.github_list = QListWidget()
        self.github_list.setMaximumHeight(120)
        self.github_list.setAccessibleName("GitHub eşitleme sonuçları")
        layout.addWidget(self.github_list)
        self.refresh_button = QPushButton("Model ve mikrofon listesini yenile")
        self.refresh_button.clicked.connect(self.refresh)
        layout.addWidget(self.refresh_button)
        self.notice = QLabel("Uygula seçilen modeli ve mikrofonu kaydeder; yeni sohbet başlar.")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.current_device.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice)
        self.apply_button = QPushButton("Uygula")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self._apply)
        layout.addWidget(self.apply_button)
        layout.addStretch(1)
        self._demo = demo
        if demo:
            self.notice.setText("Demo modunda ayarlar değiştirilmez.")
            self.refresh_button.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.add_application_button.setEnabled(False)
            self.remove_application_button.setEnabled(False)
            self.advanced_devices.setEnabled(False)
            self.continuous_mode.setEnabled(False)
            self.all_applications.setEnabled(False)
            for widget in (self.github_owner, self.repos_dir, self.repos_browse,
                           self.github_auto, self.github_button):
                widget.setEnabled(False)
        else:
            self.refresh()

    def refresh(self) -> None:
        try:
            settings = load_settings(self.config_path)
            self.providers.reload()
            primary = settings.models.get(settings.primary_model)
            selected = primary.model if primary else None
            self.model_combo.clear()
            for model in settings.models.values():
                self.model_combo.addItem(model.model, model.model)
            self.model_combo.setCurrentIndex(self.model_combo.findData(selected))
            self._refresh_devices(settings)
            # Yenileme kaydedilmiş değeri gösterir; canlı ses kipini değiştirmemeli.
            self.continuous_mode.blockSignals(True)
            self.continuous_mode.setChecked(settings.voice.continuous_mode)
            self.continuous_mode.blockSignals(False)
            self.applications = dict(settings.applications)
            self._refresh_applications()
            self.all_applications.setChecked(settings.allow_all_applications)
            self._sync_application_access()
            agent = settings.code_agent
            self.github_owner.setText(agent.github_owner or "")
            self.repos_dir.setText(str(agent.repos_dir) if agent.repos_dir else "")
            self.github_auto.setChecked(agent.auto_sync)
        except (OSError, ValueError, KeyError):
            self.notice.setText("Ayarlar okunamadı.")
            return
        if self.inventory is None or not self.inventory.isRunning():
            self.inventory = ModelInventory(self.config_path, self)
            self.inventory.loaded.connect(self._models_loaded)
            self.inventory.failed.connect(self.notice.setText)
            self.inventory.finished.connect(lambda: self.refresh_button.setEnabled(True))
            self.refresh_button.setEnabled(False)
            self.inventory.start()

    def _models_loaded(self, models: list[str]) -> None:
        selected = self.model_combo.currentData()
        for model in models:
            if self.model_combo.findData(model) < 0:
                self.model_combo.addItem(model, model)
        self.model_combo.setCurrentIndex(self.model_combo.findData(selected))

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
        if self.model_combo.currentData() is not None:
            self.apply_requested.emit(
                self.model_combo.currentData(), self.device_combo.currentData()
            )

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
        if self.inventory is not None:
            self.inventory.wait(6000)
