"""Mikrofon, uygulama ve GitHub seçimleri; modeller ProviderSection'dadır."""

from __future__ import annotations

import importlib
import importlib.util
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import QRectF, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.desktop.pet_section import PetSection
from artemis.channels.desktop.pet_sprite import pets_dir_for
from artemis.channels.desktop.projects_panel import CurrentPageStack
from artemis.channels.desktop.provider_section import (
    ProviderSection,
    button_row,
    divider,
    field,
    group,
    meta_label,
)
from artemis.channels.desktop.themes import DEFAULT_THEME, THEME_LABELS, THEMES
from artemis.channels.voice import stt_catalog
from artemis.config.settings import GITHUB_OWNER_RE, Settings, load_settings
from artemis.tools.github_repos import GitHubError, Runner, SyncReport, run_command, sync_repos

SettingPath = tuple[str, ...]

_BAR_HEIGHT = 60  # alt çubuk: 12 + 36 (Uygula) + 12


@dataclass(frozen=True)
class PanelBinding:
    """Paneldeki bir widget ile config'teki tek bir alan arasındaki iki yönlü bağ.

    Yenileme (`write`) ve kaydetme (`read`) aynı satırdan beslenir: bir ayarı yalnız birinde
    unutmak, eskiden panelin varsayılanı gösterip "Uygula"da kayıtlı değeri ezmesine yol
    açabiliyordu. Widget'ın kendisi (etiket, ipucu, yerleşim) yine elle kurulur.
    """

    path: SettingPath
    widget: str  # SettingsPanel öznitelik adı
    read: Callable[[Any], Any]
    write: Callable[[Any, Any], None]
    # Yenilemede sinyal yayılmaz; ör. kesintisiz kip kutusu canlı ses kipini değiştirmemeli.
    quiet: bool = False


def _checkbox(path: SettingPath, widget: str, *, quiet: bool = False) -> PanelBinding:
    return PanelBinding(
        path, widget, lambda box: box.isChecked(), lambda box, value: box.setChecked(bool(value)),
        quiet=quiet,
    )


def _combo(path: SettingPath, widget: str, *, fallback_first: bool) -> PanelBinding:
    """`fallback_first`: kayıtlı değer listede yoksa ilk seçenek gösterilir, yoksa seçim kalır."""

    def write(combo: Any, value: Any) -> None:
        index = combo.findData(value)
        if index >= 0 or fallback_first:
            combo.setCurrentIndex(max(0, index))

    return PanelBinding(path, widget, lambda combo: str(combo.currentData()), write)


def _precision_binding() -> PanelBinding:
    """stt_compute_type: kayıtlı değer iki standart seçenekten biri değilse (örn. `int8`) ayrı bir
    öğe olarak gösterilir; böylece Uygula kullanıcının elle yazdığı değeri sessizce ezmez."""

    def write(combo: Any, value: Any) -> None:
        for index in range(combo.count() - 1, -1, -1):
            if combo.itemData(index) not in _STANDARD_PRECISIONS:
                combo.removeItem(index)
        if combo.findData(value) < 0:
            combo.addItem(f"Özel ({value})", value)
        combo.setCurrentIndex(combo.findData(value))

    return PanelBinding(
        ("voice", "stt_compute_type"), "precision_combo",
        lambda combo: str(combo.currentData()), write,
    )


_STANDARD_PRECISIONS = ("int8_float16", "float16")

# Panelin "Uygula" ile yazdığı ve "yenile" ile okuduğu sade alanlar. Özel mantığı olanlar
# (mikrofon aygıtı, STT model yolu, uygulama listesi, code_agent) save_choices'ta açık kalır.
PANEL_BINDINGS: tuple[PanelBinding, ...] = (
    PanelBinding(
        ("voice", "wake_word"), "wake_name",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    _combo(("voice", "tts_backend"), "tts_combo", fallback_first=False),
    _precision_binding(),
    _checkbox(("voice", "continuous_mode"), "continuous_mode", quiet=True),
    PanelBinding(
        ("voice", "stt_mode"), "hybrid_stt",
        lambda box: "hybrid" if box.isChecked() else "local",
        lambda box, value: box.setChecked(value == "hybrid"),
    ),
    _checkbox(("voice", "barge_in_without_wake"), "barge_in_box"),
    _checkbox(("voice", "followup_listening"), "followup_box"),
    _checkbox(("voice", "live_transcript"), "live_transcript"),
    _checkbox(("voice", "noise_suppression"), "noise_suppression"),
    _combo(("voice", "noise_suppression_level"), "noise_level", fallback_first=True),
    _checkbox(("voice", "noise_suppression_stt"), "noise_stt"),
    _checkbox(("voice", "echo_cancellation"), "echo_cancellation"),
    _checkbox(("allow_all_applications",), "all_applications"),
    PanelBinding(
        ("computer_use", "mode"),
        "computer_auto",
        lambda box: "auto" if box.isChecked() else "accept",
        lambda box, value: box.setChecked(value == "auto"),
    ),
)
_BOUND_PATHS = frozenset(binding.path for binding in PANEL_BINDINGS)


def setting_value(settings: Settings, path: SettingPath) -> Any:
    value: Any = settings
    for key in path:
        value = getattr(value, key)
    return value


def save_choices(
    path: Path, device: int | str | None,
    *, values: Mapping[SettingPath, Any] | None = None,
    applications: dict[str, str] | None = None,
    code_agent: dict[str, Any] | None = None,
    stt_model: Path | None = None,
) -> None:
    """Diğer alanları (model seçimi dahil) korur; doğrulayıp aynı dizinde atomik değiştirir.

    `values` yalnız PANEL_BINDINGS yollarını kabul eder (yazım hatası sessizce yeni alan
    açmasın diye KeyError); verilmeyen yolun kayıtlı değeri korunur.
    """
    unknown = set(values or {}) - _BOUND_PATHS
    if unknown:
        raise KeyError(f"Panel bağı olmayan ayar yolu: {sorted(unknown)}")
    load_settings(path)  # bozuk config'i kaydetmeden önce yakalar
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("voice", {})["input_device"] = device
    for setting_path, value in (values or {}).items():
        section = raw
        for key in setting_path[:-1]:
            section = section.setdefault(key, {})
        section[setting_path[-1]] = value
    if stt_model is not None:
        raw["voice"]["stt_model"] = str(stt_model)
    if applications is not None:
        raw["applications"] = applications
        raw["close_without_confirmation"] = [
            name for name in raw.get("close_without_confirmation", []) if name in applications
        ]
    if code_agent is not None:
        _merge_code_agent(raw, code_agent)
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


class _SectionList(QListWidget):
    """Bölüm listesi; sağdaki tek kaydırma bölgesini (`scroll_area`) de taşır."""

    scroll_area: QScrollArea


# Tema adının yanındaki açıklama; anahtarlar themes.THEME_LABELS ile aynı.
_THEME_HINTS = {
    "light": "Açık gezinme, açık çalışma alanı",
    "dark": "Koyu gezinme, koyu çalışma alanı",
    "light-mixed": "Koyu gezinme, açık çalışma alanı",
    "dark-mixed": "Açık gezinme, koyu çalışma alanı",
}


def _swatch(key: str) -> QPixmap:
    """Gerçek arayüze yakın küçük önizleme: gezinme, çalışma alanı, balon ve birincil düğme."""
    colors = THEMES[key]
    width, height = 128, 76
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0.5, 0.5, width - 1, height - 1), 8, 8)
    painter.setClipPath(clip)
    painter.fillRect(0, 0, width, height, QColor(colors["bg"]))
    painter.fillRect(0, 0, 32, height, QColor(colors["nav"]))
    nav_text = QColor(colors["nav_muted"])
    painter.fillRect(6, 24, 20, 6, QColor(colors["nav_selected"]))  # seçili gezinme satırı
    for top in (10, 24, 36, 48):
        painter.fillRect(8, top + 1, 16, 3, nav_text)
    ink = QColor(colors["muted"])
    painter.fillRect(40, 10, 40, 4, ink)  # asistan cevabı
    painter.fillRect(40, 18, 56, 4, ink)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(colors["accent_soft"]))
    painter.drawRoundedRect(QRectF(66, 30, 54, 16), 6, 6)  # kullanıcı balonu
    painter.setBrush(QColor(colors["accent"]))
    painter.drawRoundedRect(QRectF(88, 56, 32, 11), 4, 4)  # birincil düğme
    painter.setBrush(QColor(colors["surface"]))
    painter.setPen(QColor(colors["border"]))
    painter.drawRoundedRect(QRectF(40, 56, 42, 11), 4, 4)  # ikincil alan
    painter.setClipping(False)
    painter.setPen(QColor(colors["border"]))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(0.5, 0.5, width - 1, height - 1), 8, 8)
    painter.end()
    return pixmap


class _ThemeCard(QFrame):
    """Tema kartı: tek seçim göstergesi 2px vurgu kenarlığıdır; radyo düğmesi yalnız klavye ve
    ekran okuyucu için kalır (göstergesi QSS ile gizli), kart her yerinden tıklanır."""

    def __init__(self, key: str, label: str) -> None:
        super().__init__()
        self.key = key
        self.setObjectName("themeCard")
        self.setProperty("selected", False)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 12, 12, 12)
        row.setSpacing(12)
        swatch = QLabel()
        swatch.setPixmap(_swatch(key))
        swatch.setAccessibleName(f"{label} önizleme")
        self.radio = QRadioButton(label)
        self.radio.toggled.connect(self._selected)
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addStretch(1)
        text.addWidget(self.radio)
        hint = meta_label(_THEME_HINTS.get(key, ""))
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addWidget(hint)
        text.addStretch(1)
        row.addWidget(swatch)
        row.addLayout(text, 1)

    def _selected(self, on: bool) -> None:
        self.setProperty("selected", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        self.radio.click()
        super().mousePressEvent(event)


def _check_hint(text: str) -> QLabel:
    """Onay kutusunun altındaki 12px soluk açıklama; kutu metni uzayıp satırı taşırmasın diye
    ayrıntı buraya (sarılabilir) konur. Girinti = gösterge 16px + boşluk 8px."""
    label = meta_label(text)
    label.setContentsMargins(24, 0, 0, 0)
    return label


class _ApplyButton(QPushButton):
    """Uygula: yalnız değişiklik varken açık. Pencere (`_set_controls`) meşgulken `setEnabled`
    çağırır; o izni saklarız ve kirli durumla birleştiririz, böylece ikisi birbirini ezmez."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self._allowed = True
        self._dirty = False
        self._sync()

    def setEnabled(self, arg__1: bool) -> None:  # noqa: N802
        self._allowed = arg__1
        self._sync()

    def set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self._sync()

    def _sync(self) -> None:
        super().setEnabled(self._allowed and self._dirty)


class SettingsPanel(QWidget):
    theme_changed = Signal(str)
    apply_requested = Signal(object)  # mikrofon aygıtı; model seçimi ProviderSection'da
    # (SyncReport, otomatik_mi): otomatik eşitleme sonucu pencere tarafından baloncukla duyurulur.
    github_synced = Signal(object, bool)

    def __init__(
        self,
        config_path: Path,
        *,
        demo: bool = False,
        github_sync: GitHubSyncFunction = sync_repos,
        pets_dir: Path | None = None,
        codex_pets_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("settingsPanel")
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
        layout.setContentsMargins(24, 0, 24, 24)
        layout.setSpacing(16)
        title = QLabel("Ayarlar")
        title.setObjectName("pageTitle")
        title.setFixedHeight(48)
        layout.addWidget(title)
        body = QHBoxLayout()
        body.setSpacing(16)
        layout.addLayout(body, 1)
        # Sol bölüm listesi + tek kaydırma bölgesi (iç içe kaydırıcı yok).
        self.sections = _SectionList()
        self.sections.setObjectName("sectionList")
        self.sections.setFixedWidth(200)
        self.sections.setAccessibleName("Ayar bölümleri")
        body.addWidget(self.sections)
        self.stack = CurrentPageStack()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.stack)
        self.sections.scroll_area = scroll
        body.addWidget(scroll, 1)

        def page(name: str, description: str) -> QVBoxLayout:
            """Bölüm sayfası: 720px'i aşmayan sütun, solda; başlık + tek satır açıklama."""
            outer = QWidget()
            row = QHBoxLayout(outer)
            # Alt dolgu = yapışkan alt çubuğun yüksekliği: kaydırınca son satır çubuğun hemen
            # altında kırpılı kalmaz, rahatça erişilir.
            row.setContentsMargins(0, 0, 0, _BAR_HEIGHT)
            column = QWidget()
            column.setMaximumWidth(720)
            content = QVBoxLayout(column)
            content.setContentsMargins(0, 0, 0, 0)
            content.setSpacing(24)
            row.addWidget(column, 1)
            row.addStretch(0)
            head = QLabel(name)
            head.setObjectName("sectionTitle")
            heading = QVBoxLayout()
            heading.setSpacing(4)
            heading.addWidget(head)
            heading.addWidget(meta_label(description))
            content.addLayout(heading)
            self.sections.addItem(name)
            self.sections.item(self.sections.count() - 1).setSizeHint(QSize(0, 36))
            self.stack.addWidget(outer)
            return content

        voice = page("Ses", "Uyandırma, tanıma ve konuşma. Değişiklikler Uygula ile kaydedilir.")
        # Uyandırma ve mikrofon
        wake_box, wake = group("Uyandırma ve mikrofon", divided=False)
        voice.addWidget(wake_box)
        self.wake_name = QLineEdit()
        self.wake_name.setText("Artemis")
        self.wake_name.setMaxLength(64)
        self.wake_name.setAccessibleName("Uyandırma adı")
        self.wake_name.setPlaceholderText("Örneğin: Artemis, Ali veya Veli")
        self.wake_name.setToolTip(
            "Sesle çağırmak için kullanacağın ad. Uygulamanın adı Artemis olarak kalır. "
            "Uygula ile ses yeniden başlatılınca geçerli olur."
        )
        field(wake, "Uyandırma adı", self.wake_name,
              "Sesle çağırma adı; uygulamanın adı Artemis kalır.")
        self.device_combo = QComboBox()
        self.device_combo.setAccessibleName("Mikrofon girişi")
        self._no_device()  # demo'da ve yenilemeden önce boş açılır liste görünmesin
        field(wake, "Mikrofon girişi", self.device_combo)
        self.current_device = QLabel("Seçilmedi")
        self.current_device.setWordWrap(True)
        field(wake, "Kullanılan mikrofon", self.current_device)
        # Etiketli seviye ölçeri: düğmeye benzeyen ortalı yazı yerine ince çubuk + değer.
        self.input_level = QProgressBar()
        self.input_level.setObjectName("levelMeter")
        self.input_level.setRange(0, 100)
        self.input_level.setValue(0)
        self.input_level.setTextVisible(False)
        self.input_level.setFixedHeight(6)
        self.input_level.setAccessibleName("Ses girişi seviyesi")
        self.level_value = QLabel("%0")
        self.level_value.setObjectName("meta")
        self.level_value.setMinimumWidth(32)
        self.level_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.input_level.valueChanged.connect(lambda value: self.level_value.setText(f"%{value}"))
        level_row = QHBoxLayout()
        level_row.setSpacing(12)
        level_row.addWidget(self.input_level, 1)
        level_row.addWidget(self.level_value)
        field(wake, "Ses girişi", level_row, "Konuşunca çubuk dolar.")
        self.last_heard = QLabel("Henüz konuşma algılanmadı.")
        self.last_heard.setTextFormat(Qt.TextFormat.PlainText)
        self.last_heard.setWordWrap(True)
        field(wake, "Son duyulan", self.last_heard)
        # Tanıma
        stt_card, stt_layout = group("Tanıma")
        voice.addWidget(stt_card)
        stt_controls = QHBoxLayout()
        stt_controls.setSpacing(8)
        self.stt_combo = QComboBox()
        self.stt_combo.setAccessibleName("Tanıma modeli")
        self.stt_download = QPushButton("İndir")
        self.stt_download.setMinimumWidth(88)
        stt_controls.addWidget(self.stt_combo, 1)
        stt_controls.addWidget(self.stt_download)
        field(stt_layout, "Tanıma modeli", stt_controls)
        self.stt_status = meta_label("")
        stt_layout.addWidget(self.stt_status)
        self.precision_combo = QComboBox()
        self.precision_combo.setAccessibleName("Tanıma hassasiyeti")
        self.precision_combo.setToolTip(
            "Dengeli ölçümde En yüksek hassasiyetle aynı doğrulukta, yaklaşık 1,8 GB daha az "
            "ekran kartı belleği kullanır. İşlemcide çalışırken seçici kapalıdır. Değişiklik ses "
            "yeniden başlayınca uygulanır."
        )
        self.precision_combo.addItem("Dengeli (az bellek)", "int8_float16")
        self.precision_combo.addItem("En yüksek hassasiyet", "float16")
        field(stt_layout, "Tanıma hassasiyeti", self.precision_combo)
        self.hybrid_stt = QCheckBox("Karma tanıma (komutları bulutta çöz)")
        self.hybrid_stt.setToolTip(
            "Uyandırma bu bilgisayarda kalır; komut olan sözün sesi seçtiğin bulut sunucusuna "
            "gider. Değişiklik ses yeniden başlayınca uygulanır."
        )
        stt_layout.addWidget(self.hybrid_stt)
        self.cloud_stt_hint = meta_label("")
        stt_layout.addWidget(self.cloud_stt_hint)
        self.hybrid_stt.toggled.connect(self._update_cloud_hint)
        self._cloud_configured = False
        # Konuşma
        speech_card, speech = group("Konuşma")
        voice.addWidget(speech_card)
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
        field(speech, "Konuşma sesi", self.tts_combo)
        self.continuous_mode = QCheckBox("Kesintisiz sohbet")
        speech.addWidget(self.continuous_mode)
        speech.addWidget(_check_hint("Uyandırma adı söylemeden sürekli dinler."))
        self.barge_in_box = QCheckBox("Konuşurken sesimle sustur")
        speech.addWidget(self.barge_in_box)
        self.followup_box = QCheckBox("Yanıttan sonra adsız dinle")
        speech.addWidget(self.followup_box)
        speech.addWidget(_check_hint(
            "Cevap bitince ya da sesinle susturunca, bir süre uyandırma adı söylemeden dinler."
        ))
        # Ses temizleme ve canlı yazı
        clean_card, clean = group("Canlı yazı ve ses temizleme")
        voice.addWidget(clean_card)
        self.live_transcript = QCheckBox("Canlı yazı (konuşurken)")
        self.live_transcript.setToolTip(
            "Konuşurken metin pet balonunda akar; yalnız gösterimdir, komutu etkilemez. "
            "Kurulum: pip install .[stream] ve ayar dosyasında model/revision. "
            "Değişiklik ses yeniden başlayınca uygulanır."
        )
        clean.addWidget(self.live_transcript)
        # Lisans atfı (mihu Community License): "Powered by mihu" arayüzde görünür kalmalı.
        clean.addWidget(
            meta_label("Yerel, işlemcide çalışır. Powered by mihu · mihu Community License")
        )
        self.noise_suppression = QCheckBox("Gürültü engelleme")
        self.noise_suppression.setToolTip(
            "DeepFilterNet3; işlemcide çalışır, ekran kartı gerekmez. Yankı gidermez. "
            "Model pakete dahildir; indirme gerekmez. "
            "Değişiklik ses yeniden başlayınca uygulanır."
        )
        clean.addWidget(self.noise_suppression)
        self.echo_cancellation = QCheckBox("Yankı engelleme")
        self.echo_cancellation.setToolTip(
            "WebRTC yankı giderici; Artemis'in hoparlörden çıkan kendi sesini mikrofondan "
            "siler, böylece konuşurken araya girmek ve kendi sesini komut sanmamak güvenilir "
            "olur. Yerelde çalışır. Kulaklıkla gerekmez. Değişiklik ses yeniden başlayınca "
            "uygulanır."
        )
        clean.addWidget(self.echo_cancellation)
        clean.addWidget(_check_hint("Hoparlörle kullanırken aç; kulaklıkta gerekmez."))
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("Gelişmiş ses ayarları")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        voice.addWidget(divider())
        voice.addWidget(self.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.advanced_box, advanced_layout = group("Gelişmiş ses ayarları", divided=False)
        self.noise_level = QComboBox()
        for label, key in (("Hafif", "hafif"), ("Orta", "orta"), ("Güçlü", "guclu")):
            self.noise_level.addItem(label, key)
        self.noise_level.setAccessibleName("Gürültü engelleme düzeyi")
        self.noise_level.setToolTip(
            "Düzey bastırma sınırıdır; algılamayı etkiler. Tanıma varsayılan olarak ham sesi alır."
        )
        self.noise_level.setEnabled(False)
        self.noise_suppression.toggled.connect(self.noise_level.setEnabled)
        field(advanced_layout, "Gürültü engelleme düzeyi", self.noise_level)
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
        advanced_layout.addLayout(button_row(self.refresh_button))
        voice.addWidget(self.advanced_box)
        self.advanced_box.setVisible(False)
        voice.addStretch(1)
        self.stt_combo.currentIndexChanged.connect(self._stt_selection_changed)
        self.stt_download.clicked.connect(self._download_stt)
        self._populate_stt_choices(None)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)

        # Model: bağlantılar ve modeller ProviderSection'dadır.
        model_page = page(
            "Model", "Beyin modelini seç. Model ve bağlantı değişiklikleri hemen kaydedilir; "
            "Uygula gerekmez."
        )
        self.providers = ProviderSection(config_path, demo=demo)
        model_page.addWidget(self.providers)
        model_page.addStretch(1)

        apps = page(
            "Uygulamalar",
            "Artemis'in açabileceği uygulamalar ve bilgisayar kullanımı. "
            "Değişiklikler Uygula ile kaydedilir.",
        )
        access_box, access = group("Erişim", divided=False)
        apps.addWidget(access_box)
        self.all_applications = QCheckBox("Bütün kurulu uygulamalara erişim")
        self.all_applications.toggled.connect(self._sync_application_access)
        access.addWidget(self.all_applications)
        self.computer_auto = QCheckBox("Otomatik bilgisayar kullanımı")
        self.computer_auto.setToolTip(
            "Hassas adımlar ve temiz turda başlangıç sorulmaz; Esc her an durdurur. Terminal, "
            "Artemis ve parola yöneticisi engeli ile dış içerikten sonraki onay yine geçerlidir."
        )
        access.addWidget(self.computer_auto)
        access.addWidget(_check_hint(
            "Adımları sormadan yapar; Esc her an durdurur. Üst çubuktaki otomatik onay düğmesi "
            "hemen uygulanır, bu kutu Uygula ile kaydedilir."
        ))
        list_box, listing = group("İzinli uygulamalar")
        apps.addWidget(list_box)
        self.application_list = QListWidget()
        self.application_list.setMaximumHeight(144)
        self.application_list.setAccessibleName("İzinli uygulamalar")
        listing.addWidget(self.application_list)
        self.add_application_button = QPushButton("Uygulama ekle")
        self.add_application_button.setObjectName("primary")
        self.add_application_button.clicked.connect(self._add_application)
        self.remove_application_button = QPushButton("Listeden çıkar")
        self.remove_application_button.clicked.connect(self._remove_application)
        listing.addLayout(button_row(self.remove_application_button, self.add_application_button))
        self.application_hint = meta_label(
            "Artemis listedeki uygulamaları açabilir. Yeni uygulama ekleyip Uygula'ya bas."
        )
        listing.addWidget(self.application_hint)
        apps.addStretch(1)

        github = page(
            "GitHub",
            "Kendi repolarını kod ajanının erişimine bağla. “Bağla” düğmesi hemen kaydeder; "
            "diğer alanlar Uygula ile.",
        )
        account_box, account = group("Hesap", divided=False)
        github.addWidget(account_box)
        self.github_owner = QLineEdit()
        self.github_owner.setAccessibleName("GitHub kullanıcı adı")
        self.github_owner.setPlaceholderText("kullanıcı-adı")
        self.repos_dir = QLineEdit()
        self.repos_dir.setAccessibleName("Repo klasörü")
        self.repos_dir.setPlaceholderText(default_repos_dir())
        self.repos_browse = QPushButton("Klasör seç")
        self.repos_browse.setMinimumWidth(88)
        self.repos_browse.clicked.connect(self._browse_repos_dir)
        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        folder_row.addWidget(self.repos_dir, 1)
        folder_row.addWidget(self.repos_browse)
        field(account, "Kullanıcı adı", self.github_owner)
        field(account, "Repo klasörü", folder_row)
        self.github_auto = QCheckBox("Otomatik eşitle")
        self.github_auto.setChecked(True)
        account.addWidget(self.github_auto)
        self.github_button = QPushButton("Bağla ve repoları çek")
        self.github_button.clicked.connect(self._bind_github)
        account.addLayout(button_row(self.github_button))
        result_box, result = group("Eşitleme")
        github.addWidget(result_box)
        self.github_status = meta_label(
            "Kendi repoların (fork ve arşivlenmişler hariç) kod ajanının erişimine girer. "
            "Giriş GitHub CLI (gh) ile yapılır; Artemis anahtarını görmez."
        )
        result.addWidget(self.github_status)
        self.github_list = QListWidget()
        self.github_list.setMaximumHeight(144)
        self.github_list.setAccessibleName("GitHub eşitleme sonuçları")
        result.addWidget(self.github_list)
        github.addStretch(1)

        # Pet bölümü ayarlarda gösterilmez; app.py kendi sayfasında barındırır.
        self.pet = PetSection(
            pets_dir if pets_dir is not None else pets_dir_for(config_path),
            codex_pets_dir,
            demo=demo,
            config_path=config_path,
        )

        look = page(
            "Görünüm",
            "Arayüz renkleri. Hemen uygulanır; Uygula gerekmez, sohbet yeniden başlamaz.",
        )
        theme_box, theme = group("Tema", divided=False)
        look.addWidget(theme_box)
        # app.py ve testler bu seçiciyi okur/yazar; kartlar ona bağlı, kendisi görünmez.
        self.theme_combo = QComboBox(self)
        self.theme_combo.setAccessibleName("Arayüz teması")
        self.theme_combo.setVisible(False)
        for key, label in THEME_LABELS.items():
            self.theme_combo.addItem(label, key)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(DEFAULT_THEME))
        self.theme_cards: dict[str, _ThemeCard] = {}
        theme_group = QButtonGroup(self)
        self._theme_grid = QGridLayout()
        self._theme_grid.setSpacing(12)
        for key, label in THEME_LABELS.items():
            theme_card = _ThemeCard(key, label)
            theme_card.radio.toggled.connect(partial(self._theme_picked, key))
            theme_group.addButton(theme_card.radio)
            self.theme_cards[key] = theme_card
        theme.addLayout(self._theme_grid)
        self._theme_columns = 0
        self._layout_theme_cards(2)
        self.theme_combo.currentIndexChanged.connect(
            lambda: self.theme_changed.emit(str(self.theme_combo.currentData()))
        )
        self.theme_combo.currentIndexChanged.connect(self._sync_theme_cards)
        self._sync_theme_cards()
        look.addStretch(1)

        self.sections.currentRowChanged.connect(self._on_section)
        self.sections.setCurrentRow(0)
        self._on_section(0)

        # Yapışkan alt çubuk kaydırma bölgesinin dışındadır: uzun sayfada da görünür kalır.
        bar = QFrame()
        bar.setObjectName("card")
        bar.setFixedHeight(_BAR_HEIGHT)
        bar_row = QHBoxLayout(bar)
        bar_row.setContentsMargins(16, 12, 16, 12)
        bar_row.setSpacing(12)
        self.notice = meta_label(
            "Uygula; ses, uygulama ve GitHub ayarlarını kaydeder ve yeni sohbet başlatır."
        )
        self.current_device.setTextFormat(Qt.TextFormat.PlainText)
        self.dirty_label = QLabel("Kaydedilmemiş değişiklik")
        self.dirty_label.setObjectName("meta")
        self.dirty_label.setVisible(False)
        self.apply_button = _ApplyButton("Uygula")
        self.apply_button.setObjectName("primary")
        self.apply_button.setFixedHeight(36)
        self.apply_button.setMinimumWidth(88)
        self.apply_button.clicked.connect(self._apply)
        bar_row.addWidget(self.notice, 1)
        bar_row.addWidget(self.dirty_label)
        bar_row.addWidget(self.apply_button)
        layout.addWidget(bar)
        if demo:
            self.notice.setText("Demo modunda ayarlar değiştirilmez.")
            self.refresh_button.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.add_application_button.setEnabled(False)
            self.remove_application_button.setEnabled(False)
            self.advanced_devices.setEnabled(False)
            self.stt_combo.setEnabled(False)
            self.stt_download.setEnabled(False)
            for binding in PANEL_BINDINGS:
                getattr(self, binding.widget).setEnabled(False)
            for widget in (self.github_owner, self.repos_dir, self.repos_browse,
                           self.github_auto, self.github_button):
                widget.setEnabled(False)
        else:
            self.refresh()
        # Açılır listeler en uzun öğeye göre genişlemesin: dar pencerede yatay kaydırma çıkarıyordu.
        for combo in self.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
        # Değişiklik izleme yalnız kullanıcı girdisine bakar; yenileme sonunda temiz sayılır.
        for edit in (self.wake_name, self.github_owner, self.repos_dir):
            edit.textEdited.connect(self._mark_dirty)
        for box in (*(getattr(self, b.widget) for b in PANEL_BINDINGS), self.github_auto):
            if isinstance(box, QCheckBox):
                box.toggled.connect(self._mark_dirty)
            elif isinstance(box, QComboBox):
                box.currentIndexChanged.connect(self._mark_dirty)
        for combo in (self.device_combo, self.stt_combo):
            combo.currentIndexChanged.connect(self._mark_dirty)

    def section_names(self) -> list[str]:
        return [self.sections.item(row).text() for row in range(self.sections.count())]

    def show_section(self, index: int) -> None:
        self.sections.setCurrentRow(index)

    def _on_section(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        # Önceki sayfanın kaydırma konumu yeni (kısa) sayfada alta düşmesin; programatik
        # show_section() de buradan geçer.
        self.sections.scroll_area.verticalScrollBar().setValue(0)
        self._sync_theme_cards()

    def set_theme(self, name: str) -> None:
        """Seçimi sinyal yaymadan günceller (pencere temayı zaten kendisi uyguladı)."""
        blocked = self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(name)))
        self.theme_combo.blockSignals(blocked)
        self._sync_theme_cards()

    def _theme_picked(self, key: str, on: bool) -> None:
        if on:
            self.theme_combo.setCurrentIndex(self.theme_combo.findData(key))

    def _sync_theme_cards(self, *_args: object) -> None:
        card_for = self.theme_cards.get(str(self.theme_combo.currentData()))
        if card_for is not None and not card_for.radio.isChecked():
            card_for.radio.setChecked(True)

    def _mark_dirty(self, *_args: object) -> None:
        if not self._demo:
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        """Uygula yalnız değişiklik varken açılır; tema gibi anında uygulananlar buraya uğramaz."""
        self.dirty_label.setVisible(dirty)
        self.apply_button.set_dirty(dirty)

    def _layout_theme_cards(self, columns: int) -> None:
        """İki sütunlu kart ızgarası ~510px ister; dar pencerede tek sütun, yoksa bütün sayfaların
        genişliğini (QStackedWidget en büyük sayfaya uyar) yatay kaydırmaya iter."""
        if columns == self._theme_columns:
            return
        self._theme_columns = columns
        while self._theme_grid.count():
            self._theme_grid.takeAt(0)
        for position, theme_card in enumerate(self.theme_cards.values()):
            self._theme_grid.addWidget(theme_card, position // columns, position % columns)
        # Üst yerleşimlerin önbelleğindeki geniş asgari boyut düşsün.
        # Gizli sayfanın iç içe yerleşimleri tembel güncellenir; ızgaradan yukarı hepsini düşür.
        widget = self._theme_grid.parentWidget()
        while widget is not None and widget is not self.stack:
            layout = widget.layout()
            if layout is not None:
                layout.invalidate()
                layout.activate()
            widget = widget.parentWidget()
        self.stack.updateGeometry()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        """Dar pencerede bölüm listesi 160px'e iner; içerik kırpılmasın."""
        super().resizeEvent(event)
        self.sections.setFixedWidth(160 if self.width() < 760 else 200)
        self._layout_theme_cards(1 if self.width() < 840 else 2)

    def _update_cloud_hint(self, *_args: object) -> None:
        """Karma tanıma seçiliyken verinin nereye gittiğini açıkça yazar (ADR 0046)."""
        if not self.hybrid_stt.isChecked():
            self.cloud_stt_hint.setText("")
            return
        text = (
            "Komut sesi seçtiğin bulut sunucusuna gönderilir; kesintisiz kipte her söz gider. "
            "Teknik ayar: voice.stt_cloud_endpoint."
        )
        if not self._cloud_configured:
            text += (
                " Sunucu ve model config'te (stt_cloud_endpoint, stt_cloud_model) "
                "tanımlı değil; tanımlanmadan Uygula reddedilir."
            )
        self.cloud_stt_hint.setText(text)

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
                self.stt_combo.addItem("Özel model (elle ayarlı)", None)
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
            self.stt_status.setText(
                "Özel model (elle ayarlı): ayar dosyasındaki model yolu kullanılıyor."
            )
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
            self.applications = dict(settings.applications)
            self._refresh_applications()
            # Yenileme kaydedilmiş değeri gösterir; `quiet` bağ canlı durumu değiştirmez.
            for binding in PANEL_BINDINGS:
                widget = getattr(self, binding.widget)
                if binding.quiet:
                    widget.blockSignals(True)
                binding.write(widget, setting_value(settings, binding.path))
                if binding.quiet:
                    widget.blockSignals(False)
            voice = settings.voice
            self._cloud_configured = (
                voice.stt_cloud_endpoint in settings.endpoints and bool(voice.stt_cloud_model)
            )
            self._update_cloud_hint()
            # faster-whisper CPU'da zaten int8 kullanır; seçici orada anlamsız olur.
            self.precision_combo.setEnabled(
                settings.voice.stt_device != "cpu" and not self._demo
            )
            self.noise_level.setEnabled(self.noise_suppression.isChecked() and not self._demo)
            self.noise_stt.setEnabled(self.noise_suppression.isChecked() and not self._demo)
            self._sync_application_access()
            agent = settings.code_agent
            self.github_owner.setText(agent.github_owner or "")
            self.repos_dir.setText(str(agent.repos_dir) if agent.repos_dir else "")
            self.github_auto.setChecked(agent.auto_sync)
            self._set_dirty(False)
        except (OSError, ValueError, KeyError):
            self.notice.setText("Ayarlar okunamadı.")
            return

    def _no_device(self) -> None:
        """Giriş yoksa liste boş kalmaz: soluk, kapalı satır nedenini söyler."""
        self.device_combo.clear()
        self.device_combo.addItem("Mikrofon bulunamadı", None)
        self.device_combo.setEnabled(False)

    def _set_current_device(self, text: str) -> None:
        self.current_device.setText(text.strip() or "Seçilmedi")

    def _refresh_devices(self, settings: Settings) -> None:
        self.device_combo.clear()
        self.device_combo.setEnabled(not self._demo)
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
            if not choices:
                self._no_device()
                self._set_current_device("")
                return
            for label, index, indices in choices:
                self.device_combo.addItem(label, index)
                if active in indices or (isinstance(active, str) and active in label):
                    active_name = label
                if settings.voice.input_device in indices:
                    self.device_combo.setCurrentIndex(self.device_combo.count() - 1)
            if active_name == str(active):
                self.device_combo.addItem(f"Mevcut giriş ({active})", settings.voice.input_device)
                self.device_combo.setCurrentIndex(self.device_combo.count() - 1)
            self._set_current_device(active_name)
        except audio_errors:
            self._no_device()
            self.current_device.setText("Seçilmedi (mikrofon aygıtları okunamadı)")

    def _toggle_device_detail(self) -> None:
        if self._demo:
            return
        was_dirty = not self.dirty_label.isHidden()
        try:
            self._refresh_devices(load_settings(self.config_path))
        except (OSError, ValueError):
            self.notice.setText("Mikrofon ayarı okunamadı.")
        # Listeyi yeniden kurmak kullanıcı değişikliği değildir.
        self._set_dirty(was_dirty)

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
        self._mark_dirty()
        self.notice.setText("Uygulama eklendi; kaydetmek için Uygula'ya bas.")

    def _remove_application(self) -> None:
        item = self.application_list.currentItem()
        if item is not None:
            del self.applications[item.data(Qt.ItemDataRole.UserRole)]
            self._refresh_applications()
            self._mark_dirty()
            self.notice.setText("Uygulama listeden çıkarıldı; kaydetmek için Uygula'ya bas.")

    def _apply(self) -> None:
        self.apply_requested.emit(self.device_combo.currentData())

    def values(self) -> dict[SettingPath, Any]:
        """PANEL_BINDINGS alanlarının panelde görünen değeri; save_choices(values=...) için."""
        return {
            binding.path: binding.read(getattr(self, binding.widget)) for binding in PANEL_BINDINGS
        }

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
