"""Mikrofon, uygulama ve GitHub seçimleri; modeller ProviderSection'dadır."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import (
    QFileSystemWatcher,
    QObject,
    QSettings,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QWidget,
)

from artemis.channels.desktop import (
    mic_wizard,
    security_settings_section,
    settings_nav,
    voice_preview,
    voice_settings_section,
)
from artemis.channels.desktop.calendar_mail_section import CalendarMailSection
from artemis.channels.desktop.code_agent_config import AgentDialog, command_status
from artemis.channels.desktop.file_search_section import FileSearchSection
from artemis.channels.desktop.mcp_section import MCPSection
from artemis.channels.desktop.page_kit import PAGE_MAX_WIDTH, CurrentPageStack
from artemis.channels.desktop.pet_section import PetSection
from artemis.channels.desktop.provider_section import ProviderSection
from artemis.channels.desktop.provider_workers import ConnectionTest
from artemis.channels.desktop.routines_section import RoutinesSection
from artemis.channels.desktop.settings_io import (
    PANEL_BINDINGS,
    SettingPath,
    _same,
    apply_github_report,
    restore_baseline,
    save_code_agent,
    setting_value,
)
from artemis.channels.desktop.settings_io import (
    _write_validated as _write_validated,  # eski içe aktarma yolu
)
from artemis.channels.desktop.settings_io import save_choices as save_choices
from artemis.channels.desktop.settings_io import save_computer_mode as save_computer_mode
from artemis.channels.desktop.settings_nav import (
    _GROUP_TARGET_ROLE as _GROUP_TARGET_ROLE,
)
from artemis.channels.desktop.settings_pages import (
    build_look_page,
    build_model_page,
    build_shell,
    build_shortcuts_page,
    build_voice_page,
    default_repos_dir,
)
from artemis.channels.desktop.settings_pages_extra import (
    build_about_page,
    build_apps_page,
    build_automation_page,
    build_connections_page,
    build_knowledge_page,
)
from artemis.channels.desktop.settings_widgets import (
    _DIRTY_ROLE as _DIRTY_ROLE,
)
from artemis.channels.desktop.settings_widgets import (
    _GROUP_ROLE as _GROUP_ROLE,
)
from artemis.channels.desktop.settings_widgets import (
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
from artemis.channels.desktop.thread_shutdown import (
    DETACHED,
    interrupt_threads,
    shared_budget,
    shutdown_thread,
)
from artemis.channels.desktop.triggers_section import TriggersSection
from artemis.channels.desktop.tts_cloud_box import TtsCloudBox
from artemis.channels.desktop.update_section import UpdateSection
from artemis.channels.desktop.voice_model_manager import VoiceModelManagerWidget
from artemis.channels.desktop.voice_preview import VoicePreviewThread
from artemis.channels.desktop.voice_settings_section import SttDownload as SttDownload
from artemis.channels.desktop.voice_telemetry_card import VoiceTelemetryCard
from artemis.channels.voice import stt_catalog
from artemis.config.settings import (
    BUILTIN_AGENT,
    BUILTIN_AGENTS,
    GITHUB_OWNER_RE,
    EndpointConfig,
    Settings,
    load_settings,
)
from artemis.i18n import t
from artemis.tools.github_repos import GitHubError, Runner, SyncReport, run_command, sync_repos

_BLOCK_MAX_WIDTH = PAGE_MAX_WIDTH  # liste + sayfa bloğu; geniş pencerede ortalanır
# Panel bundan darsa bölüm listesi yerine sayfanın üstünde açılır "Bölüm" listesi çıkar: 200px'lik
# liste sayfa sütununu ~270px'e düşürüyor, düğme satırları kırpılıyordu (UX-01/UX-12).
# 1000 iken varsayılan 1180px pencere (panel 956) bile seçiciye düşüyordu; 760'ta sayfa sütunu
# ~500px kalır ve hiçbir sayfa taşmaz (2026-11-15 ölçüm).
_COMPACT_PANEL_WIDTH = 760


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


_LAST_SECTION_KEY = "settings/section"
# Süresi dolunca ayrılan iş parçacıkları bitene dek GC tarafından toplanmasın diye tutulur.
_DETACHED = DETACHED


class SettingsPanel(QWidget):
    theme_changed = Signal(str)
    # Kayıt isteği (Kaydet ya da çıkış onayı): mikrofon aygıtı; model seçimi
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
    mic_test_button: QPushButton
    current_device: QLabel
    input_level: QProgressBar
    level_value: QLabel
    last_heard: QLabel
    stt_combo: QComboBox
    stt_download: QPushButton
    stt_status: QLabel
    stt_language_combo: QComboBox
    precision_combo: QComboBox
    hybrid_stt: QCheckBox
    direct_audio_input: QCheckBox
    multimodal_screen_box: QWidget
    multimodal_screen_context: QCheckBox
    multimodal_screen_hint: QLabel
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
    tts_piper_hint: QLabel
    tts_edge_box: QWidget
    edge_voice_combo: QComboBox
    tts_piper_box: QWidget
    piper_voice_combo: QComboBox
    tts_preview_button: QPushButton
    tts_preview_status: QLabel
    tts_preview_thread: VoicePreviewThread | None
    tts_cloud_box: QWidget
    tts_cloud_saved: QComboBox
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
    voice_tabs: QTabWidget
    voice_telemetry_card: VoiceTelemetryCard
    voice_model_manager: VoiceModelManagerWidget
    listening_hotkey: QLineEdit
    ptt_enabled: QCheckBox
    ptt_hotkey_box: QWidget
    ptt_hotkey: QLineEdit
    continuous_mode: QCheckBox
    dictation_enabled: QCheckBox
    dictation_hotkey: QLineEdit
    dictation_max_seconds: QSpinBox
    dictation_polish: QCheckBox
    selection_enabled: QCheckBox
    selection_mouse_popup: QCheckBox
    selection_hotkey: QLineEdit
    selection_translate_to: QLineEdit
    barge_in_box: QCheckBox
    followup_box: QCheckBox
    live_transcript: QCheckBox
    noise_suppression: QCheckBox
    game_mode: QCheckBox
    game_mode_target_box: QWidget
    game_mode_target: QComboBox
    echo_cancellation: QCheckBox
    mic_advanced_toggle: QToolButton
    mic_advanced_box: QWidget
    advanced_toggle: QToolButton
    noise_level: QComboBox
    noise_stt: QCheckBox
    advanced_devices: QCheckBox
    refresh_button: QPushButton
    providers: ProviderSection
    pet: PetSection
    theme_combo: QComboBox
    language_combo: QComboBox
    theme_cards: dict[str, _ThemeCard]
    _theme_grid: QGridLayout
    _theme_columns: int
    proactive_enabled: QCheckBox
    automation_tabs: QTabWidget
    routines_enabled: QCheckBox
    routines: RoutinesSection
    triggers_enabled: QCheckBox
    triggers: TriggersSection
    daily_summary_enabled: QCheckBox
    daily_summary_at: QLineEdit
    research_enabled: QCheckBox
    knowledge_tabs: QTabWidget
    connections_tabs: QTabWidget
    telegram: TelegramSection
    telegram_voice: QCheckBox
    telegram_photo: QCheckBox
    telegram_files: QCheckBox
    calendar_enabled: QCheckBox
    calendar_cloud: QCheckBox
    calendar_allow_write: QCheckBox
    calendar_caldav_url: QLineEdit
    calendar_caldav_username: QLineEdit
    mail_enabled: QCheckBox
    mail_cloud: QCheckBox
    mail_allow_send: QCheckBox
    mail_host: QLineEdit
    mail_port: QLineEdit
    mail_username: QLineEdit
    mail_folder: QLineEdit
    mail_smtp_host: QLineEdit
    mail_smtp_port: QLineEdit
    mail_smtp_username: QLineEdit
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
    mcp: MCPSection
    mcp_servers: MCPSection
    skills_enabled: QCheckBox
    skills: SkillsSection
    file_search_enabled: QCheckBox
    file_search: FileSearchSection
    file_search_roots: QListWidget
    all_applications: QCheckBox
    computer_auto: QCheckBox
    computer_observation: QComboBox
    application_list: QListWidget
    close_without_confirmation_list: QListWidget
    add_application_button: QPushButton
    remove_application_button: QPushButton
    application_hint: QLabel
    allow_cloud_file_content: QCheckBox
    allow_cloud_clipboard: QCheckBox
    memory_encrypt_at_rest: QCheckBox
    advanced_box: QWidget
    update_section: UpdateSection
    about_section: QWidget

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
        self._gpu_mb: int | None = None  # nvidia-smi arka planda sorulur (V1)
        self._recommended_stt = stt_catalog.recommended(None)
        self._stt_installed: dict[str, Path | None] = {}
        self.gpu_probe: voice_settings_section.GpuProbe | None = None
        self._device_snapshot: tuple[Any, Any, Any] | None = None
        self.github: GitHubSync | None = None
        self.stt_download_thread: SttDownload | None = None
        self._github_sync = github_sync
        self._github_auto = False
        self.applications: dict[str, str] = {}
        self._dirty = False
        self._failed = False
        self._loading = False
        self._baseline_bytes: bytes | None = None
        self._baseline_dump: dict[str, Any] | None = None
        # Tek vuruşlu zamanlayıcı bağlı yöntemle çalışır (lambda kendini yakalarsa GC çöker).
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
        self._canonical_saved_notes: dict[str, _SavedNote] = {}
        self._page_tabs: dict[str, QTabWidget] = {}
        build_voice_page(self)
        build_shortcuts_page(self)
        self._sync_voice_conditional_boxes()
        build_model_page(self, pets_dir, codex_pets_dir)
        build_look_page(self)
        build_automation_page(self)
        build_knowledge_page(self)
        build_connections_page(self)
        build_apps_page(self)
        build_about_page(self)
        self.voice_model_manager.models_changed.connect(self._on_voice_models_changed)
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
            self.notice.setText(t("Demo modunda ayarlar değiştirilmez."))
            self.refresh_button.setEnabled(False)
            self.add_application_button.setEnabled(False)
            self.remove_application_button.setEnabled(False)
            self.advanced_devices.setEnabled(False)
            self.mic_test_button.setEnabled(False)
            self.stt_combo.setEnabled(False)
            self.stt_download.setEnabled(False)
            for binding in PANEL_BINDINGS:
                getattr(self, binding.widget).setEnabled(False)
            for widget in (self.github_owner, self.repos_dir, self.repos_browse,
                           self.github_auto, self.github_button, self.default_agent_combo,
                           self.add_custom_agent_button):
                widget.setEnabled(False)
            self._sync_voice_output_controls()
        else:
            # Önce işçi başlar: refresh() PortAudio'yu GUI'de değil, işçinin sonucuyla doldurur.
            voice_settings_section.start_gpu_probe(self)
            self.refresh()
        # Açılır listeler en uzun öğeye göre genişlemesin: dar pencerede yatay kaydırma çıkarıyordu.
        for combo in self.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
        # Değişiklik izleme yalnız kullanıcı girdisine bakar; yenileme sonunda temiz sayılır.
        for edit in (
            self.wake_name, self.listening_hotkey, self.ptt_hotkey, self.dictation_hotkey,
            self.selection_hotkey,
            self.selection_translate_to, self.github_owner, self.repos_dir,
            self.mail_host, self.mail_port, self.mail_username, self.mail_folder,
            self.daily_summary_at,
        ):
            edit.textEdited.connect(self._mark_dirty)
        for combo in (self.cloud_model, self.edge_voice_combo, self.piper_voice_combo):
            line = combo.lineEdit()
            if line is not None:
                line.textEdited.connect(self._mark_dirty)
        for box in (*(getattr(self, b.widget) for b in PANEL_BINDINGS), self.github_auto):
            if isinstance(box, QCheckBox):
                box.toggled.connect(self._mark_dirty)
            elif isinstance(box, QComboBox):
                box.currentIndexChanged.connect(self._mark_dirty)
            elif isinstance(box, QSpinBox):
                box.valueChanged.connect(self._mark_dirty)
        self.close_without_confirmation_list.itemChanged.connect(self._mark_dirty)
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

    def select_section(self, name: str) -> bool:
        """Sayfa, eski bölüm veya alt sekme adına göre (TR/EN) ilgili sayfa ve sekmeyi açar."""
        names = self.section_names()
        direct = t(name)
        if direct in names:
            self.show_section(names.index(direct))
            return True
        if name in names:
            self.show_section(names.index(name))
            return True
        routes = settings_nav.SECTION_ROUTES
        route = routes.get(_fold(name)) or routes.get(_fold(direct))
        if route is None:
            return False
        page_tr, subtab = route
        candidates = (t(page_tr), page_tr)
        row = next((i for i, item in enumerate(names) if item in candidates), None)
        if row is None:
            folded = {_fold(c) for c in candidates}
            row = next((i for i, item in enumerate(names) if _fold(item) in folded), None)
        if row is None:
            return False
        self.show_section(row)
        if subtab is not None:
            tabs = self._page_tabs.get(page_tr)
            if tabs is not None and 0 <= subtab < tabs.count():
                tabs.setCurrentIndex(subtab)
        return True

    def _build_section_combo(self) -> None:
        settings_nav.build_section_combo(self)

    def _combo_picked(self, index: int) -> None:
        settings_nav.combo_picked(self, index)

    def _sync_combo(self, row: int) -> None:
        settings_nav.sync_combo(self, row)

    def _set_compact(self, compact: bool) -> None:
        settings_nav.set_compact(self, compact)

    def _page_text(self, row: int) -> str:
        return settings_nav.page_text(self, row)

    def _filter_sections(self, text: str) -> None:
        settings_nav.filter_sections(self, text)

    def _pick_first_match(self) -> None:
        settings_nav.pick_first_match(self)

    def _refresh_dirty_dots(self) -> None:
        settings_nav.refresh_dirty_dots(self)

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
        """Dosyaya yazmaz: kutuyu işaretler (toggled -> kirli), kaydı Kaydet yapar."""
        self.proactive_enabled.setChecked(True)

    @property
    def has_unsaved(self) -> bool:
        """Kaydedilmemiş kullanıcı değişikliği var mı (demo modunda her zaman False)."""
        return self._dirty and not self._demo

    def _owner_row(self, source: object) -> int | None:
        if not isinstance(source, QObject):
            return None
        widget: QWidget | None = source if isinstance(source, QWidget) else None
        cur: QObject | None = source
        while widget is None and cur is not None:
            cur = cur.parent()
            if isinstance(cur, QWidget):
                widget = cur
        while widget is not None:
            for row in range(self.stack.count()):
                if self.stack.widget(row) is widget:
                    return row
            widget = widget.parentWidget()
        return None

    def _mark_dirty(self, *_args: object) -> None:
        if not self._demo and not self._loading:
            owner = self._owner_row(self.sender())
            self._dirty_rows.add(owner if owner is not None else self.stack.currentIndex())
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        """Tema gibi anında uygulananlar buraya uğramaz. Temizlenince hata ve kirli noktalar da
        sıfırlanır."""
        self._dirty = dirty
        if not dirty:
            self._failed = False
            self.retry_button.setVisible(False)
            self._dirty_rows.clear()
        self._refresh_dirty_dots()
        self._update_revert()

    def _apply(self) -> None:
        self.apply_requested.emit(self.device_combo.currentData())

    def flush(self) -> None:
        """Bekleyen değişiklik varsa hemen kaydettirir (Kaydet düğmesi ve çıkış onayı)."""
        if self._demo:
            return
        had_pending = self.providers.has_pending
        if had_pending and not self.providers.commit_pending():
            self.save_failed(t("Model seçimi kaydedilemedi; mevcut ayarlar korunuyor."))
            return
        if self._dirty:
            # Yalnız model rolleri değiştiyse ve apply_requested dinleyicisi yoksa
            # (veya diğer alanlar zaten kayıtlıysa) _apply sonrası kirli kalmasın.
            self._apply()
            if had_pending and self._dirty and not self._failed:
                self.mark_saved()

    def discard_changes(self) -> None:
        """Kaydedilmemiş düzenlemeleri atar ve tüm denetimleri kayıtlı config'ten yeniler."""
        if self._demo:
            return
        self.providers.drop_pending()
        self.refresh()
        self._set_dirty(False)
        self.notice.setText("")

    def mark_saved(self, written: bool = True) -> None:
        """Pencere kaydı yazdı: temiz say."""
        self._set_dirty(False)
        if written:
            self.notice.setText(t("Kaydedildi"))

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
        try:
            restore_baseline(self.config_path, baseline)
        except (OSError, ValueError, yaml.YAMLError):
            # Kirli kalır (düğme görünür, yeniden denenebilir); yarım yazılmış dosya yok.
            self.notice.show_error(t("Değişiklikler geri alınamadı; mevcut ayarlar korunuyor."))
            self._update_revert()
            return
        self.providers.drop_pending()
        self._set_dirty(False)
        self.refresh()
        self.telegram.refresh()
        self._watch()
        self._update_revert()
        self.notice.setText(t("Değişiklikler geri alındı"))
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
        voice_settings_section.update_cloud_hint(self)

    def _cloud_endpoint(self) -> tuple[str, EndpointConfig | None]:
        return voice_settings_section.cloud_endpoint(self)

    def _save_cloud_key(self) -> None:
        voice_settings_section.save_cloud_key(self)

    def _fetch_cloud_models(self) -> None:
        voice_settings_section.fetch_cloud_models(self)

    def _cloud_models_done(self, message: str, models: list[str]) -> None:
        voice_settings_section.cloud_models_done(self, message, models)

    def _load_cloud_servers(self) -> None:
        voice_settings_section.load_cloud_servers(self)

    def _cloud_server_picked(self, *_args: object) -> None:
        voice_settings_section.cloud_server_picked(self)

    def _toggle_mic_advanced(self, checked: bool) -> None:
        voice_settings_section.toggle_mic_advanced(self, checked)

    def _toggle_advanced(self, checked: bool) -> None:
        voice_settings_section.toggle_advanced(self, checked)

    def _sync_voice_conditional_boxes(self, *_args: object) -> None:
        voice_settings_section.sync_voice_conditional_boxes(self)

    @staticmethod
    def _stt_size_text(choice: stt_catalog.WhisperChoice) -> str:
        return voice_settings_section.stt_size_text(choice)

    def _populate_stt_choices(self, selected_name: str | None) -> None:
        voice_settings_section.populate_stt_choices(self, selected_name)

    def _selected_stt_choice(self) -> stt_catalog.WhisperChoice | None:
        return voice_settings_section.selected_stt_choice(self)

    def _stt_selection_changed(self, *_args: object) -> None:
        voice_settings_section.stt_selection_changed(self)

    def _update_stt_download_button(self) -> None:
        voice_settings_section.update_stt_download_button(self)

    def stt_model_path(self) -> Path | None:
        """Seçili katalog modelinin kullanılabilir yerel yolunu döndürür."""
        return voice_settings_section.stt_model_path(self)

    def _gpu_probe_done(self, gpu_mb: object) -> None:
        voice_settings_section.gpu_probe_done(self, gpu_mb)

    def _device_scan_done(self, snapshot: object) -> None:
        voice_settings_section.device_scan_done(self, snapshot)

    def _stt_scan_done(self, installed: object) -> None:
        voice_settings_section.stt_scan_done(self, installed)

    def _download_stt(self, *_args: object) -> None:
        voice_settings_section.download_stt(self)

    def _stt_download_done(self, _path: Path) -> None:
        voice_settings_section.stt_download_done(self, _path)

    def _stt_download_failed(self, error_type: str) -> None:
        voice_settings_section.stt_download_failed(self, error_type)

    def _stt_download_finished(self) -> None:
        voice_settings_section.stt_download_finished(self)

    def refresh(self) -> None:
        self._loading = True
        try:
            settings = load_settings(self.config_path)
            self.providers.reload()
            self._load_cloud_servers()
            self._refresh_devices(settings)
            self._populate_stt_choices(
                voice_settings_section.selected_stt_name(self, settings.voice.stt_model)
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
            self._sync_voice_conditional_boxes()
            if isinstance(self.tts_cloud_box, TtsCloudBox):
                self.tts_cloud_box.reload_servers()
            self._sync_voice_output_controls()
            self.voice_telemetry_card.refresh()
            self.voice_model_manager.trigger_refresh(quiet=True)
            self.routines.refresh()
            self.skills.refresh()
            self.triggers.refresh()
            self.file_search.refresh()
            self.mcp.refresh()
            # faster-whisper CPU'da zaten int8 kullanır; seçici orada anlamsız olur.
            self.precision_combo.setEnabled(
                settings.voice.stt_device != "cpu" and not self._demo
            )
            self.noise_level.setEnabled(self.noise_suppression.isChecked() and not self._demo)
            self.noise_stt.setEnabled(self.noise_suppression.isChecked() and not self._demo)
            self.selection_mouse_popup.setEnabled(
                self.selection_enabled.isChecked() and not self._demo
            )
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
            self.notice.setText(t("Ayarlar okunamadı."))
            return
        finally:
            self._loading = False

    def _no_device(self) -> None:
        voice_settings_section.no_device(self)

    def _set_current_device(self, text: str) -> None:
        voice_settings_section.set_current_device(self, text)

    def _refresh_devices(self, settings: Settings) -> None:
        voice_settings_section.refresh_devices(self, settings)

    def _toggle_device_detail(self) -> None:
        voice_settings_section.toggle_device_detail(self)

    def _refresh_applications(self) -> None:
        friendly = {"brave": "Brave", "notepad": t("Not Defteri"), "paint": "Paint",
                    "calculator": t("Hesap Makinesi"), "explorer": t("Dosya Gezgini")}
        self.application_list.clear()
        for alias, executable in self.applications.items():
            self.application_list.addItem(f"{friendly.get(alias, alias)} ({alias})")
            item = self.application_list.item(self.application_list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, alias)
            item.setToolTip(executable)
        security_settings_section.refresh_close_without_confirmation(self)

    def _sync_application_access(self) -> None:
        manual = not self.all_applications.isChecked() and not self._demo
        self.application_list.setEnabled(manual)
        self.application_list.setVisible(manual)
        self.add_application_button.setEnabled(manual)
        self.add_application_button.setVisible(manual)
        self.remove_application_button.setEnabled(manual)
        self.remove_application_button.setVisible(manual)
        security_settings_section.sync_close_without_confirmation_access(self)
        self.application_hint.setText(
            t("Kurulu uygulamalar otomatik bulunur. Silme, kaldırma ve hassas işlemler onay ister.")
            if self.all_applications.isChecked()
            else t("Artemis listedeki uygulamaları açabilir.")
        )

    def _add_application(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, t("Uygulama seç"), "", t("Uygulamalar (*.exe)")
        )
        if not filename:
            return
        path = Path(filename)
        if not path.is_file() or path.suffix.casefold() != ".exe":
            self.notice.setText(t("Bir uygulamanın .exe dosyasını seç."))
            return
        alias = path.stem.casefold()
        if alias in self.applications and Path(self.applications[alias]) != path:
            self.notice.setText(t("Bu ad listede kayıtlı; önce mevcut kaydı çıkar."))
            return
        self.applications[alias] = str(path)
        self._refresh_applications()
        self._mark_dirty()
        self.notice.setText(t("Uygulama eklendi; Kaydet ile kaydedilir."))

    def _remove_application(self) -> None:
        item = self.application_list.currentItem()
        if item is not None:
            del self.applications[item.data(Qt.ItemDataRole.UserRole)]
            self._refresh_applications()
            self._mark_dirty()
            self.notice.setText(t("Uygulama listeden çıkarıldı; Kaydet ile kaydedilir."))

    def mcp_values(self) -> list[dict[str, Any]]:
        return self.mcp.values()

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
            note = (
                t(" · yerleşik")
                if name in BUILTIN_AGENTS and name not in self.code_agents
                else ""
            )
            self.agent_list.addItem(f"{name} · {kind} · {command or '—'} · {status}{note}")
        blocked = self.default_agent_combo.blockSignals(True)
        self.default_agent_combo.clear()
        for name, _kind, _command in rows:
            self.default_agent_combo.addItem(name, name)
        current = self.default_agent_combo.findData(self.default_agent)
        self.default_agent_combo.setCurrentIndex(max(0, current))
        self.default_agent_combo.blockSignals(blocked)
        self.agent_hint.setText(
            t("Giriş her ajanın kendi CLI'sindedir (Claude/Codex login); Artemis sır saklamaz.")
        )

    def _default_agent_picked(self, index: int) -> None:
        self.default_agent = str(self.default_agent_combo.itemData(index))
        self._mark_dirty()

    def _stage_agent(self, name: str, profile: dict[str, Any]) -> None:
        self.code_agents[name] = profile
        self._refresh_code_agents()
        self._mark_dirty()
        self.agent_hint.setText(t("'{name}' eklendi; Kaydet ile kaydedilir.").format(name=name))

    def _add_custom_agent(self) -> None:
        dialog = AgentDialog((*BUILTIN_AGENTS, *self.code_agents), self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_profile:
            self._stage_agent(*dialog.result_profile)

    def _browse_repos_dir(self) -> None:
        start = self.repos_dir.text().strip() or default_repos_dir()
        chosen = QFileDialog.getExistingDirectory(self, t("Repo klasörü seç"), start)
        if chosen:
            self.repos_dir.setText(chosen)

    def flash_saved_note(self, canonical_name: str) -> None:
        """Sayfanın 'Kaydedildi' etiketini etkin dile bağlı olmadan yanıp söndürür."""
        note = (
            self._canonical_saved_notes.get(canonical_name)
            or self.saved_notes.get(t(canonical_name))
            or self.saved_notes.get(canonical_name)
        )
        if note is not None:
            note.flash()

    def _bind_github(self) -> None:
        owner = self.github_owner.text().strip()
        if not GITHUB_OWNER_RE.fullmatch(owner):
            self.github_status.setText(t("Geçerli bir GitHub kullanıcı adı yaz."))
            return
        if not self.repos_dir.text().strip():
            self.repos_dir.setText(default_repos_dir())
        try:
            save_code_agent(self.config_path, self.github_values())
        except (OSError, ValueError, KeyError, TypeError):
            self.github_status.setText(t("GitHub ayarları kaydedilemedi."))
            return
        self.flash_saved_note("Bağlantılar")
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
        thread.finished.connect(self._github_finished)
        self.github = thread
        self.github_button.setEnabled(False)
        self.github_list.clear()
        self.github_status.setText(t("{owner} repoları eşitleniyor…").format(owner=owner))
        thread.start()

    def _github_finished(self) -> None:
        self.github_button.setEnabled(not self._demo)

    def _github_failed(self, message: str) -> None:
        self.github_status.setText(t("Eşitleme başarısız: {message}").format(message=message))

    def _github_done(self, report: SyncReport) -> None:
        apply_github_report(self, report)

    def _open_mic_wizard(self) -> None:
        mic_wizard.open_mic_wizard(self)

    def _sync_voice_output_controls(self, *_args: object) -> None:
        voice_preview.sync_voice_output_controls(self)

    def _toggle_voice_preview(self) -> None:
        voice_preview.toggle_voice_preview(self)

    def _voice_preview_done(self, message: str) -> None:
        self.tts_preview_status.setText(message)

    def _voice_preview_failed(self, message: str) -> None:
        self.tts_preview_status.setText(message)

    def _voice_preview_finished(self) -> None:
        self.tts_preview_thread = None
        self._sync_voice_output_controls()

    def _on_voice_models_changed(self, _entries: object = None) -> None:
        voice_settings_section.sync_voice_models_from_manager(self)

    def finish_inventory(self) -> None:
        # Önce hepsine kesme istenir, sonra tek toplam bütçeyle beklenir (kapanış GUI'yi dondurmaz).
        interrupt_threads(
            self.github, self._cloud_fetch, self.stt_download_thread, self.gpu_probe,
            self.tts_preview_thread,
        )
        with shared_budget():
            if shutdown_thread(
                self.github,
                ("done", self._github_done),
                ("failed", self._github_failed),
                ("finished", self._github_finished),
            ):
                self.github = None
            # Model listesi isteği (en çok 5 sn zaman aşımı) çalışırken panel yıkılmasın.
            if shutdown_thread(self._cloud_fetch, ("done", self._cloud_models_done)):
                self._cloud_fetch = None
            if shutdown_thread(
                self.stt_download_thread,
                ("done", self._stt_download_done),
                ("failed", self._stt_download_failed),
                ("finished", self._stt_download_finished),
            ):
                self.stt_download_thread = None
            if shutdown_thread(
                self.gpu_probe, ("done", self._gpu_probe_done), ("scanned", self._stt_scan_done),
                ("devices", self._device_scan_done),
            ):
                self.gpu_probe = None
            if shutdown_thread(
                self.tts_preview_thread,
                ("done", self._voice_preview_done),
                ("failed", self._voice_preview_failed),
                ("finished", self._voice_preview_finished),
            ):
                self.tts_preview_thread = None
            self.telegram.finish()
            self.update_section.finish()
            self.voice_model_manager.finish()
            if isinstance(self.tts_cloud_box, TtsCloudBox):
                self.tts_cloud_box.finish()
            self.file_search.finish()
            self.mcp.finish()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.finish_inventory()
        super().closeEvent(event)
