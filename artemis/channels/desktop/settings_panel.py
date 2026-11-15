"""Mikrofon, uygulama ve GitHub seçimleri; modeller ProviderSection'dadır."""

from __future__ import annotations

import importlib
import importlib.util
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from PySide6.QtCore import (
    QFileSystemWatcher,
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QToolButton,
    QWidget,
)

from artemis.brain.providers import stored_endpoint_key
from artemis.channels.desktop.calendar_mail_section import CalendarMailSection
from artemis.channels.desktop.code_agent_config import AgentDialog, command_status
from artemis.channels.desktop.file_search_section import FileSearchSection
from artemis.channels.desktop.page_kit import PAGE_MAX_WIDTH, CurrentPageStack
from artemis.channels.desktop.pet_section import PetSection
from artemis.channels.desktop.provider_section import (
    ConnectionTest,
    ProviderSection,
    store_endpoint_key,
)
from artemis.channels.desktop.routines_section import RoutinesSection
from artemis.channels.desktop.settings_io import (
    PANEL_BINDINGS,
    SettingPath,
    _same,
    microphone_choices,
    save_code_agent,
    setting_value,
)
from artemis.channels.desktop.settings_io import (
    _write_validated as _write_validated,  # eski içe aktarma yolu
)
from artemis.channels.desktop.settings_io import (
    save_choices as save_choices,
)
from artemis.channels.desktop.settings_io import (
    save_computer_mode as save_computer_mode,
)
from artemis.channels.desktop.settings_pages import (
    build_apps_page,
    build_calendar_mail_page,
    build_files_page,
    build_github_page,
    build_look_page,
    build_model_page,
    build_research_page,
    build_routines_page,
    build_shell,
    build_skills_page,
    build_summary_page,
    build_telegram_page,
    build_triggers_page,
    build_voice_page,
    default_repos_dir,
)
from artemis.channels.desktop.settings_widgets import (
    _DIRTY_A11Y,
    _DIRTY_ROLE,
    _GROUP_ROLE,
    _HEADER_HEIGHT,
    _ITEM_HEIGHT,
    _fold,
    _install_wheel_guard,
    _Notice,
    _SavedNote,
    _SearchBox,
    _SectionDelegate,
    _SectionList,
    _ThemeCard,
)
from artemis.channels.desktop.skills_section import SkillsSection
from artemis.channels.desktop.telegram_section import TelegramSection
from artemis.channels.desktop.themes import DEFAULT_THEME, THEMES
from artemis.channels.desktop.triggers_section import TriggersSection
from artemis.channels.voice import stt_catalog
from artemis.config.settings import (
    BUILTIN_AGENT,
    BUILTIN_AGENTS,
    GITHUB_OWNER_RE,
    EndpointConfig,
    Settings,
    load_settings,
)
from artemis.tools.github_repos import GitHubError, Runner, SyncReport, run_command, sync_repos

_BLOCK_MAX_WIDTH = PAGE_MAX_WIDTH  # liste + sayfa bloğu; geniş pencerede ortalanır
# Panel bundan darsa bölüm listesi yerine sayfanın üstünde açılır "Bölüm" listesi çıkar: 200px'lik
# liste sayfa sütununu ~270px'e düşürüyor, düğme satırları kırpılıyordu (UX-01/UX-12).
# 1000 iken varsayılan 1180px pencere (panel 956) bile seçiciye düşüyordu; 760'ta sayfa sütunu
# ~500px kalır ve hiçbir sayfa taşmaz (2026-11-15 ölçüm).
_COMPACT_PANEL_WIDTH = 760
_GROUP_TARGET_ROLE = Qt.ItemDataRole.UserRole + 2  # seçicide grup başlığı -> ilk sayfa satırı
_AUTOSAVE_MS = 800  # son düzenlemeden sonra bu kadar durulunca kaydedilir



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


_LAST_SECTION_KEY = "settings/section"




class SettingsPanel(QWidget):
    theme_changed = Signal(str)
    # Otomatik kayıt isteği (düzenleme durulunca ya da flush): mikrofon aygıtı; model seçimi
    # ProviderSection'da. Pencere yazar, sonra mark_saved()/save_failed() ile geri bildirir.
    apply_requested = Signal(object)
    save_requested = Signal()  # "Kaydet": yazıldı, beklemeden uygula
    reverted = Signal()  # config dosyası oturum başındaki hâline döndü; canlı kuruluş gerekir
    # (SyncReport, otomatik_mi): otomatik eşitleme sonucu pencere tarafından baloncukla duyurulur.
    github_synced = Signal(object, bool)
    # Alanları settings_pages kurar; burada yalnız tipleri bildirilir.
    notice: _Notice
    retry_button: QPushButton
    revert_button: QPushButton
    save_button: QPushButton
    title_row: QHBoxLayout
    sections: _SectionList
    _section_delegate: _SectionDelegate
    stack: CurrentPageStack
    section_combo: QComboBox
    _compact: bool
    nav: QWidget
    search: _SearchBox
    no_results: QLabel
    wake_name: QLineEdit
    device_combo: QComboBox
    current_device: QLabel
    input_level: QProgressBar
    level_value: QLabel
    last_heard: QLabel
    stt_combo: QComboBox
    stt_download: QPushButton
    stt_status: QLabel
    precision_combo: QComboBox
    hybrid_stt: QCheckBox
    cloud_box: QWidget
    cloud_server: QComboBox
    cloud_protocol: QComboBox
    cloud_key: QLineEdit
    cloud_key_button: QPushButton
    cloud_model: QComboBox
    cloud_fetch_button: QPushButton
    _cloud_fetch: ConnectionTest | None
    _cloud_fetch_for: str
    cloud_stt_hint: QLabel
    tts_combo: QComboBox
    tts_cloud_box: QWidget
    tts_cloud_server: QComboBox
    tts_cloud_protocol: QComboBox
    tts_cloud_model: QComboBox
    tts_cloud_fetch_button: QPushButton
    tts_cloud_voice: QComboBox
    tts_cloud_consent: QCheckBox
    tts_cloud_paid: QCheckBox
    tts_cloud_emotion: QCheckBox
    tts_cloud_emotion_tags: QLineEdit
    tts_cloud_hint: QLabel
    continuous_mode: QCheckBox
    dictation_enabled: QCheckBox
    dictation_hotkey: QLineEdit
    selection_enabled: QCheckBox
    selection_hotkey: QLineEdit
    selection_translate_to: QLineEdit
    barge_in_box: QCheckBox
    followup_box: QCheckBox
    live_transcript: QCheckBox
    noise_suppression: QCheckBox
    game_mode: QCheckBox
    echo_cancellation: QCheckBox
    advanced_toggle: QToolButton
    noise_level: QComboBox
    noise_stt: QCheckBox
    advanced_devices: QCheckBox
    refresh_button: QPushButton
    providers: ProviderSection
    pet: PetSection
    theme_combo: QComboBox
    theme_cards: dict[str, _ThemeCard]
    _theme_grid: QGridLayout
    _theme_columns: int
    proactive_enabled: QCheckBox
    routines_enabled: QCheckBox
    routines: RoutinesSection
    triggers_enabled: QCheckBox
    triggers: TriggersSection
    daily_summary_enabled: QCheckBox
    daily_summary_at: QLineEdit
    research_enabled: QCheckBox
    telegram: TelegramSection
    telegram_voice: QCheckBox
    telegram_photo: QCheckBox
    telegram_files: QCheckBox
    calendar_enabled: QCheckBox
    calendar_cloud: QCheckBox
    mail_enabled: QCheckBox
    mail_cloud: QCheckBox
    mail_host: QLineEdit
    mail_port: QLineEdit
    mail_username: QLineEdit
    mail_folder: QLineEdit
    calendar_mail: CalendarMailSection
    github_owner: QLineEdit
    repos_dir: QLineEdit
    repos_browse: QPushButton
    github_auto: QCheckBox
    github_button: QPushButton
    github_status: QLabel
    github_list: QListWidget
    agent_list: QListWidget
    default_agent_combo: QComboBox
    add_custom_agent_button: QPushButton
    agent_hint: QLabel
    code_agents: dict[str, dict[str, Any]]
    default_agent: str
    _builtin_command: str
    skills_enabled: QCheckBox
    skills: SkillsSection
    file_search_enabled: QCheckBox
    file_search: FileSearchSection
    file_search_roots: QListWidget
    all_applications: QCheckBox
    computer_auto: QCheckBox
    computer_observation: QComboBox
    application_list: QListWidget
    add_application_button: QPushButton
    remove_application_button: QPushButton
    application_hint: QLabel
    advanced_box: QWidget

    def __init__(
        self,
        config_path: Path,
        *,
        demo: bool = False,
        github_sync: GitHubSyncFunction = sync_repos,
        pets_dir: Path | None = None,
        codex_pets_dir: Path | None = None,
        state: QSettings | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("settingsPanel")
        _install_wheel_guard()
        self._state = None if demo else state  # son bölüm yalnız burada hatırlanır
        self._navigating = False
        self._row_groups: list[tuple[str, bool]] = []  # (grup, grubun ilk sayfası mı)
        self._dirty_rows: set[int] = set()
        self.config_path = config_path
        self._demo = demo
        self._gpu_mb = stt_catalog.gpu_memory_mb()
        self._recommended_stt = stt_catalog.recommended(self._gpu_mb)
        self.github: GitHubSync | None = None
        self.stt_download_thread: SttDownload | None = None
        self._github_sync = github_sync
        self._github_auto = False
        self.applications: dict[str, str] = {}
        self._dirty = False
        self._failed = False
        self._baseline_bytes: bytes | None = None
        self._baseline_dump: dict[str, Any] | None = None
        # Tek vuruşlu zamanlayıcılar bağlı yöntemle çalışır (lambda kendini yakalarsa GC çöker).
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(_AUTOSAVE_MS)
        self._save_timer.timeout.connect(self._apply)
        self._disk_timer = QTimer(self)
        self._disk_timer.setSingleShot(True)
        self._disk_timer.setInterval(200)
        self._disk_timer.timeout.connect(self._disk_changed)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._disk_timer.start)
        self._watcher.directoryChanged.connect(self._disk_timer.start)
        build_shell(self)
        self._current_group = ""
        self.saved_notes: dict[str, _SavedNote] = {}
        build_voice_page(self)
        build_model_page(self, pets_dir, codex_pets_dir)
        build_look_page(self)
        build_routines_page(self)
        build_triggers_page(self)
        build_summary_page(self)
        build_research_page(self)
        build_telegram_page(self)
        build_calendar_mail_page(self)
        build_github_page(self)
        build_skills_page(self)
        build_files_page(self)
        build_apps_page(self)
        self._build_section_combo()
        self.sections.currentRowChanged.connect(self._on_section)
        self.section_combo.activated.connect(self._combo_picked)
        start = 0
        if self._state is not None:
            saved = str(self._state.value(_LAST_SECTION_KEY, ""))
            if saved in self.section_names():  # anahtar artık yoksa ilk bölüm
                start = self.section_names().index(saved)
        self._navigating = True  # açılış seçimi yeniden yazılmaz
        self.sections.setCurrentRow(start)
        self._navigating = False
        self._on_section(start)
        self.search.textChanged.connect(self._filter_sections)
        self.search.returnPressed.connect(self._pick_first_match)

        self.current_device.setTextFormat(Qt.TextFormat.PlainText)
        if demo:
            self.notice.setText("Demo modunda ayarlar değiştirilmez.")
            self.refresh_button.setEnabled(False)
            self.add_application_button.setEnabled(False)
            self.remove_application_button.setEnabled(False)
            self.advanced_devices.setEnabled(False)
            self.stt_combo.setEnabled(False)
            self.stt_download.setEnabled(False)
            for binding in PANEL_BINDINGS:
                getattr(self, binding.widget).setEnabled(False)
            for widget in (self.github_owner, self.repos_dir, self.repos_browse,
                           self.github_auto, self.github_button, self.default_agent_combo,
                           self.add_custom_agent_button):
                widget.setEnabled(False)
        else:
            self.refresh()
        # Açılır listeler en uzun öğeye göre genişlemesin: dar pencerede yatay kaydırma çıkarıyordu.
        for combo in self.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
        # Değişiklik izleme yalnız kullanıcı girdisine bakar; yenileme sonunda temiz sayılır.
        for edit in (
            self.wake_name, self.dictation_hotkey, self.selection_hotkey,
            self.selection_translate_to, self.github_owner, self.repos_dir,
            self.mail_host, self.mail_port, self.mail_username, self.mail_folder,
            self.daily_summary_at,
        ):
            edit.textEdited.connect(self._mark_dirty)
        self.cloud_model.lineEdit().textEdited.connect(self._mark_dirty)  # type: ignore[union-attr]
        for box in (*(getattr(self, b.widget) for b in PANEL_BINDINGS), self.github_auto):
            if isinstance(box, QCheckBox):
                box.toggled.connect(self._mark_dirty)
            elif isinstance(box, QComboBox):
                box.currentIndexChanged.connect(self._mark_dirty)
        for combo in (self.device_combo, self.stt_combo):
            combo.currentIndexChanged.connect(self._mark_dirty)
        roots_model = self.file_search_roots.model()
        roots_model.rowsInserted.connect(self._mark_dirty)
        roots_model.rowsRemoved.connect(self._mark_dirty)
        if not demo:
            self.begin_session()

    def section_names(self) -> list[str]:
        return [self.sections.item(row).text() for row in range(self.sections.count())]

    def section_groups(self) -> dict[str, list[str]]:
        """Grup başlığı -> sayfa adları (listede sırayla). Başlıklar satır değildir."""
        groups: dict[str, list[str]] = {}
        for row, (title, _first) in enumerate(self._row_groups):
            groups.setdefault(title, []).append(self.sections.item(row).text())
        return groups

    def show_section(self, index: int) -> None:
        self._navigating = True  # kodla gezinme (ör. model uyarısı) son seçimi değiştirmez
        try:
            self.sections.setCurrentRow(index)
        finally:
            self._navigating = False

    def _build_section_combo(self) -> None:
        """Bölüm listesinin açılır kopyası: sayfa satırı veri=satır. Grup başlığı kalın görünür;
        tıklanınca ölü kalmaz, grubun ilk sayfasına gider (Umut 2026-11-15: "bir şey olmuyor")."""
        bold = QFont(self.section_combo.font())
        bold.setBold(True)
        for row, (group_name, first) in enumerate(self._row_groups):
            if first:
                self.section_combo.addItem(group_name)
                index = self.section_combo.count() - 1
                self.section_combo.setItemData(index, row, _GROUP_TARGET_ROLE)
                self.section_combo.setItemData(index, bold, Qt.ItemDataRole.FontRole)
            self.section_combo.addItem(self.sections.item(row).text(), row)

    def _combo_picked(self, index: int) -> None:
        row = self.section_combo.itemData(index)
        if not isinstance(row, int):
            row = self.section_combo.itemData(index, _GROUP_TARGET_ROLE)
        if isinstance(row, int):
            self.sections.setCurrentRow(row)
            self._sync_combo(row)  # başlık seçildiyse kutuda sayfa adı görünsün

    def _sync_combo(self, row: int) -> None:
        self.section_combo.setCurrentIndex(self.section_combo.findData(row))

    def _set_compact(self, compact: bool) -> None:
        """Dar panelde liste ve arama gizlenir, yerine "Bölüm" seçici gelir; arama süzgeci
        temizlenir ki gizli kutu görünmeyen bir süzgeci sürdürmesin."""
        if compact == self._compact:
            return
        self._compact = compact
        if compact:
            self.search.clear()
        self.nav.setVisible(not compact)
        self.section_combo.setVisible(compact)

    def _page_text(self, row: int) -> str:
        """Sayfada görünen metinler: başlık, etiketler, kutu ve grup adları."""
        page = self.stack.widget(row)
        assert page is not None
        parts = [self.sections.item(row).text()]
        parts += [label.text() for label in page.findChildren(QLabel)]
        parts += [button.text() for button in page.findChildren(QAbstractButton)]
        parts += [box.title() for box in page.findChildren(QGroupBox)]
        return _fold("\n".join(parts))

    def _filter_sections(self, text: str) -> None:
        needle = _fold(text.strip())
        shown = [
            row for row in range(self.sections.count())
            if not needle or needle in self._page_text(row)
        ]
        seen: set[str] = set()
        for row in range(self.sections.count()):
            item = self.sections.item(row)
            item.setHidden(row not in shown)
            group = self._row_groups[row][0]
            header = row in shown and group not in seen  # boş grubun başlığı da gizlenir
            if row in shown:
                seen.add(group)
            item.setData(_GROUP_ROLE, group if header else None)
            item.setSizeHint(QSize(0, _ITEM_HEIGHT + (_HEADER_HEIGHT if header else 0)))
        self.no_results.setVisible(bool(needle) and not shown)
        if shown and self.sections.currentRow() not in shown:
            self.sections.setCurrentRow(shown[0])
        if needle and len(shown) == 1:
            self.sections.setCurrentRow(shown[0])

    def _pick_first_match(self) -> None:
        for row in range(self.sections.count()):
            if not self.sections.item(row).isHidden():
                self.sections.setCurrentRow(row)
                return

    def _refresh_dirty_dots(self) -> None:
        for row in range(self.sections.count()):
            item = self.sections.item(row)
            dirty = row in self._dirty_rows
            item.setData(_DIRTY_ROLE, dirty)
            spoken = f"{item.text()}, {self._row_groups[row][0]}"
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                spoken + (f", {_DIRTY_A11Y}" if dirty else ""),
            )
            item.setToolTip(_DIRTY_A11Y if dirty else "")
        self.sections.viewport().update()

    def _on_section(self, row: int) -> None:
        if row < 0:
            return
        if self._state is not None and not self._navigating:
            self._state.setValue(_LAST_SECTION_KEY, self.sections.item(row).text())
        self._sync_combo(row)
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

    def _theme_picked(self, on: bool) -> None:
        radio = self.sender()
        if on and radio is not None:
            self.theme_combo.setCurrentIndex(self.theme_combo.findData(radio.property("theme")))

    def _emit_theme(self, *_args: object) -> None:
        self.theme_changed.emit(str(self.theme_combo.currentData()))

    def _sync_theme_cards(self, *_args: object) -> None:
        tokens = THEMES.get(str(self.theme_combo.currentData()), THEMES[DEFAULT_THEME])
        self._section_delegate.muted = QColor(tokens["muted"])
        self._section_delegate.accent = QColor(tokens["accent"])
        self.sections.viewport().update()
        card_for = self.theme_cards.get(str(self.theme_combo.currentData()))
        if card_for is not None and not card_for.radio.isChecked():
            card_for.radio.setChecked(True)

    def _triggers_saved(self) -> None:
        """Form `triggers.enabled`ı açmış olabilir; kutu eski kalırsa kayıt geri kapatırdı."""
        try:
            enabled = load_settings(self.config_path).triggers.enabled
        except (OSError, ValueError):
            return
        blocked = self.triggers_enabled.blockSignals(True)
        self.triggers_enabled.setChecked(enabled)
        self.triggers_enabled.blockSignals(blocked)

    def _tick_proactive(self) -> None:
        """Dosyaya yazmaz: kutuyu işaretler (toggled -> kirli), kaydı otomatik kayıt yapar."""
        self.proactive_enabled.setChecked(True)

    def _mark_dirty(self, *_args: object) -> None:
        if not self._demo:
            # ponytail: kirli bölüm = o an açık olan; değişiklik yalnız görünen sayfada olur.
            self._dirty_rows.add(self.stack.currentIndex())
            self._set_dirty(True)
            # Her düzenleme sayacı yeniden başlatır: yazma duraksayınca tek kez olur.
            self._save_timer.start()

    def _set_dirty(self, dirty: bool) -> None:
        """Tema gibi anında uygulananlar buraya uğramaz. Temizlenince bekleyen otomatik kayıt da
        düşer: kodla yenileme/geri alma sonradan eski değeri dosyaya yazmasın."""
        self._dirty = dirty
        if not dirty:
            self._save_timer.stop()
            self._failed = False
            self.retry_button.setVisible(False)
            self._dirty_rows.clear()
        self._refresh_dirty_dots()
        self._update_revert()

    def _apply(self) -> None:
        self.apply_requested.emit(self.device_combo.currentData())

    def flush(self) -> None:
        """Bekleyen değişiklik varsa debounce'u beklemeden kaydettirir (sekme/pencere çıkışı)."""
        if self._dirty and not self._demo:
            self._save_timer.stop()
            self._apply()

    def mark_saved(self, written: bool = True) -> None:
        """Pencere kaydı yazdı: temiz say. Yeniden kurulum sekmeden çıkınca yapılır."""
        self._set_dirty(False)
        if written:
            self.notice.setText("Kaydedildi · Ayarlar'dan çıkınca uygulanır")

    def save_failed(self, text: str) -> None:
        """Kayıt reddedildi: değişiklik kirli kalır (bölüm noktaları durur), hata kalıcıdır."""
        self._failed = True
        self.notice.show_error(text)
        self.retry_button.setVisible(not self._demo)
        self._update_revert()

    # Oturum başlangıcına dönüş ---------------------------------------------------------------

    def config_dump(self) -> dict[str, Any] | None:
        """Kayıtlı config'in doğrulanmış dökümü; okunamazsa None."""
        return self._dump()

    def _dump(self) -> dict[str, Any] | None:
        try:
            return load_settings(self.config_path).model_dump(mode="json")
        except (OSError, ValueError, yaml.YAMLError):
            return None

    def begin_session(self) -> None:
        """Ayarlar açıldığındaki kayıtlı hâli "geri al" için saklar (sekmeye her girişte).

        Yalnız config dosyası alınır; kasadaki parolalar bilerek bellekte tutulmaz."""
        if self._demo:
            return
        self.flush()  # bekleyen düzenleme yeni taban çizgisine karışmasın
        try:
            self._baseline_bytes = self.config_path.read_bytes()
            self._baseline_dump = load_settings(self.config_path).model_dump(mode="json")
        except (OSError, ValueError, yaml.YAMLError):
            self._baseline_bytes = self._baseline_dump = None
        self._watch()
        self._update_revert()

    def _watch(self) -> None:
        """Dosya + klasör izlenir: atomik değiştirme (os.replace) dosya izini düşürür."""
        wanted = [str(self.config_path), str(self.config_path.parent)]
        have = set(self._watcher.files()) | set(self._watcher.directories())
        missing = [path for path in wanted if path not in have and Path(path).exists()]
        if missing:
            self._watcher.addPaths(missing)

    def _disk_changed(self) -> None:
        self._watch()
        self._update_revert()

    def _update_revert(self) -> None:
        """Düğme: kirli ya da kayıtlı hâl taban çizgisinden (tür-katı) farklıysa görünür."""
        if self._demo or self._baseline_dump is None or self._baseline_bytes is None:
            self.revert_button.setVisible(False)
            self.save_button.setVisible(False)
            return
        # Okunamayan/geçersiz config "değişti" sayılır: kullanıcı geri alabilsin.
        changed = self._dirty or not _same(self._dump(), self._baseline_dump)
        self.revert_button.setVisible(changed)
        self.save_button.setVisible(changed)

    def _save_now(self) -> None:
        """Kaydet: bekleyen düzenleme hemen yazılır, pencere sekmeden çıkmayı beklemeden uygular."""
        self.flush()
        if not self._failed:
            self.save_requested.emit()

    def _revert(self) -> None:
        baseline = self._baseline_bytes
        if baseline is None or self._demo:
            return
        self._save_timer.stop()  # bekleyen otomatik kayıt geri almanın üstüne yazmasın
        temporary = ""
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".settings-", suffix=".yaml", dir=self.config_path.parent
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(baseline)
            load_settings(Path(temporary))
            os.replace(temporary, self.config_path)
        except (OSError, ValueError, yaml.YAMLError):
            # Kirli kalır (düğme görünür, yeniden denenebilir); yarım yazılmış dosya yok.
            self.notice.show_error("Değişiklikler geri alınamadı; mevcut ayarlar korunuyor.")
            self._update_revert()
            return
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
        self._set_dirty(False)
        self.refresh()
        self.telegram.refresh()
        self._watch()
        self._update_revert()
        self.notice.setText("Değişiklikler geri alındı")
        self.reverted.emit()

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

    def resizeEvent(self, event: Any) -> None:
        """Dar pencerede bölüm listesi 160px'e iner; içerik kırpılmasın."""
        super().resizeEvent(event)
        self._set_compact(self.width() < _COMPACT_PANEL_WIDTH)
        self._layout_theme_cards(1 if self.width() < 840 else 2)
        # Geniş pencerede başlık, gövde ve alt çubuk tek bloktur: en çok _BLOCK_MAX_WIDTH, ortada.
        # Dar pencerede yan boşluk 0 kalır, yerleşim eskisi gibidir.
        side = max(0, (self.width() - 48 - _BLOCK_MAX_WIDTH) // 2)
        self.layout().setContentsMargins(24 + side, 0, 24 + side, 24)  # type: ignore[union-attr]

    def _update_cloud_hint(self, *_args: object) -> None:
        """Karma tanıma seçiliyken verinin nereye gittiğini açıkça yazar (ADR 0046)."""
        on = self.hybrid_stt.isChecked()
        xai = self.cloud_protocol.currentData() == "xai"
        self.cloud_box.setVisible(on)
        live = on and not self._demo
        self.cloud_model.setEnabled(live and not xai)
        self.cloud_fetch_button.setEnabled(live and not xai and bool(self._cloud_endpoint()[0]))
        edit = self.cloud_model.lineEdit()
        if edit is not None:
            edit.setPlaceholderText("xAI için model gerekmez" if xai else "Örneğin: whisper-1")
        name, endpoint = self._cloud_endpoint()
        needs_key = endpoint is not None and endpoint.api_key
        self.cloud_key.setEnabled(live and needs_key)
        self.cloud_key_button.setEnabled(live and needs_key)
        self.cloud_key.setPlaceholderText(
            "" if not name else "Bu sunucu anahtar istemiyor" if not needs_key
            else "kayıtlı" if stored_endpoint_key(name) else ""
        )
        if not on:
            self.cloud_stt_hint.setText("")
            return
        text = (
            "Komut sesi seçtiğin bulut sunucusuna gönderilir; kesintisiz kipte her söz gider. "
            "Sunucu eklemek için Model sağlayıcıları bölümünü kullan."
        )
        if xai:
            text += (
                " Grok tanıma Türkçeyi resmi olarak doğrulamıyor; hata olursa yerel metne düşer."
            )
        if needs_key and not stored_endpoint_key(name) and not self.cloud_key.text().strip():
            text += (
                " Bu sunucu için API anahtarı kayıtlı değil; yukarıya yazıp "
                "'Anahtarı kaydet'e bas, yoksa tanıma çalışmaz."
            )
        self.cloud_stt_hint.setText(text)

    def _cloud_endpoint(self) -> tuple[str, EndpointConfig | None]:
        """Seçili sunucunun kimliği ve config kaydı; seçilmemiş ya da okunamazsa (\"\", None)."""
        name = str(self.cloud_server.currentData() or "")
        if not name:
            return "", None
        try:
            return name, load_settings(self.config_path).endpoints.get(name)
        except (OSError, ValueError):
            return name, None

    def _save_cloud_key(self) -> None:
        """Anahtarı kasaya yazar; YAML, bağlar ve Uygula'ya girmez, alan hemen temizlenir."""
        name, endpoint = self._cloud_endpoint()
        typed = self.cloud_key.text().strip()
        if not name or endpoint is None or not endpoint.api_key or not typed:
            self.cloud_stt_hint.setText("Önce bir sunucu seç ve anahtarı yaz.")
            return
        failure = store_endpoint_key(name, typed)
        self.cloud_key.clear()
        self._update_cloud_hint()
        self.cloud_stt_hint.setText(
            f"Anahtar saklanamadı; {failure}" if failure else f"{name} için anahtar kaydedildi."
        )

    def _fetch_cloud_models(self) -> None:
        """Seçili sunucunun /models listesini arka planda getirir; bir seferde tek istek."""
        name, endpoint = self._cloud_endpoint()
        if endpoint is None or (self._cloud_fetch is not None and self._cloud_fetch.isRunning()):
            return
        key = None
        if endpoint.api_key:
            key = self.cloud_key.text().strip() or stored_endpoint_key(name)
        self.cloud_stt_hint.setText("Model listesi alınıyor…")
        self._cloud_fetch_for = name
        self._cloud_fetch = ConnectionTest(endpoint.base_url, key, self)
        self._cloud_fetch.done.connect(self._cloud_models_done)
        self._cloud_fetch.start()

    def _cloud_models_done(self, message: str, models: list[str]) -> None:
        # İstek sürerken sunucu değiştiyse eski sunucunun listesi yeni sunucunun önerisi olmaz.
        if self._cloud_fetch_for != str(self.cloud_server.currentData() or ""):
            return
        self.cloud_stt_hint.setText(message)
        if not models:
            return
        current = self.cloud_model.currentText()
        self.cloud_model.blockSignals(True)  # liste yenilemesi kirli sayılmasın
        self.cloud_model.clear()
        self.cloud_model.addItems(models)
        self.cloud_model.setCurrentIndex(max(0, self.cloud_model.findText(current)))
        if current:
            self.cloud_model.setEditText(current)
        self.cloud_model.blockSignals(False)

    def _load_cloud_servers(self) -> None:
        """Sunucu listesini config'teki endpoints'ten yeniler; seçimi korur."""
        try:
            endpoints = list(load_settings(self.config_path).endpoints)
        except (OSError, ValueError):
            return
        current = self.cloud_server.currentData()
        self.cloud_server.blockSignals(True)
        self.cloud_server.clear()
        self.cloud_server.addItem("Seçilmedi", "")
        for name in endpoints:
            self.cloud_server.addItem(name, name)
        self.cloud_server.setCurrentIndex(max(0, self.cloud_server.findData(current)))
        self.cloud_server.blockSignals(False)
        self._update_cloud_hint()

    def _cloud_server_picked(self, *_args: object) -> None:
        """xAI sunucusu seçilince protokol xAI'ye önayarlanır (kullanıcı değiştirebilir)."""
        name = str(self.cloud_server.currentData() or "")
        try:
            endpoint = load_settings(self.config_path).endpoints.get(name)
        except (OSError, ValueError):
            return
        host = urlparse(endpoint.base_url).hostname or "" if endpoint else ""
        if name == "xai" or host == "x.ai" or host.endswith(".x.ai"):
            self.cloud_protocol.setCurrentIndex(self.cloud_protocol.findData("xai"))

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
        self.stt_status.setText("İndirildi; kendiliğinden kaydedilir.")

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
            self._load_cloud_servers()
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
            self._update_cloud_hint()
            self.routines.refresh()
            self.skills.refresh()
            self.triggers.refresh()
            self.file_search.refresh()
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
            self.code_agents = {
                name: profile.model_dump(mode="json", exclude_defaults=True)
                for name, profile in agent.agents.items()
            }
            self.default_agent = agent.default_agent
            self._builtin_command = agent.command
            self._refresh_code_agents()
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
        was_dirty = self._dirty
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
            else "Artemis listedeki uygulamaları açabilir."
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
        self.notice.setText("Uygulama eklendi; kendiliğinden kaydedilir.")

    def _remove_application(self) -> None:
        item = self.application_list.currentItem()
        if item is not None:
            del self.applications[item.data(Qt.ItemDataRole.UserRole)]
            self._refresh_applications()
            self._mark_dirty()
            self.notice.setText("Uygulama listeden çıkarıldı; kendiliğinden kaydedilir.")

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

    def code_agent_values(self) -> dict[str, Any]:
        """Uygula için: GitHub alanları + ajan profilleri. None değer anahtarı config'ten siler."""
        return {
            **self.github_values(),
            "agents": dict(self.code_agents) or None,
            "default_agent": None if self.default_agent == BUILTIN_AGENT else self.default_agent,
        }

    def _refresh_code_agents(self) -> None:
        self.agent_list.clear()
        # Yerleşikler (claude, codex) her zaman listededir; kullanıcı codex'i tanımladıysa onun
        # profili (örn. açık mutlak yol) gösterilir. Ardından özel ajanlar.
        rows = [(BUILTIN_AGENT, "claude", self._builtin_command)]
        codex_command = (self.code_agents.get("codex") or {}).get("command", "codex")
        rows.append(("codex", "codex", codex_command))
        rows += [
            (n, p["kind"], p.get("command"))
            for n, p in self.code_agents.items()
            if n not in BUILTIN_AGENTS
        ]
        for name, kind, command in rows:
            # Yalnız PATH/önbellek bakılır; arayüz iş parçacığında süreç başlatılmaz.
            status = command_status(command)
            note = " · yerleşik" if name in BUILTIN_AGENTS and name not in self.code_agents else ""
            self.agent_list.addItem(f"{name} · {kind} · {command or '—'} · {status}{note}")
        blocked = self.default_agent_combo.blockSignals(True)
        self.default_agent_combo.clear()
        for name, _kind, _command in rows:
            self.default_agent_combo.addItem(name, name)
        current = self.default_agent_combo.findData(self.default_agent)
        self.default_agent_combo.setCurrentIndex(max(0, current))
        self.default_agent_combo.blockSignals(blocked)
        self.agent_hint.setText(
            "Giriş her ajanın kendi CLI'sindedir (Claude/Codex login); Artemis sır saklamaz."
        )

    def _default_agent_picked(self, index: int) -> None:
        self.default_agent = str(self.default_agent_combo.itemData(index))
        self._mark_dirty()

    def _stage_agent(self, name: str, profile: dict[str, Any]) -> None:
        self.code_agents[name] = profile
        self._refresh_code_agents()
        self._mark_dirty()
        self.agent_hint.setText(f"'{name}' eklendi; kendiliğinden kaydedilir.")

    def _add_custom_agent(self) -> None:
        dialog = AgentDialog((*BUILTIN_AGENTS, *self.code_agents), self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_profile:
            self._stage_agent(*dialog.result_profile)

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
        self.saved_notes["GitHub"].flash()
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
        # Model listesi isteği (en çok 5 sn zaman aşımı) çalışırken panel yıkılmasın.
        if self._cloud_fetch is not None and self._cloud_fetch.isRunning():
            self._cloud_fetch.wait(6000)
        self.telegram.finish()
