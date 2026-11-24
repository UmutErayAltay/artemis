"""PySide6 paneli; bütün model ve araç işleri Qt'siz köprüde kalır."""

from __future__ import annotations

import contextlib
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError
from PySide6.QtCore import QPoint, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QFont,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from artemis.channels.autostart import Autostart
from artemis.channels.desktop import gui_gc, hotkey_manager, pet_sprite, themes
from artemis.channels.desktop.approval_dialog import ApprovalDialog
from artemis.channels.desktop.automation_panel import AutomationPanel
from artemis.channels.desktop.bridge import (
    Activity,
    Approval,
    BridgeEvent,
    DesktopBridge,
    RuntimeFactory,
)
from artemis.channels.desktop.budget_bar import refresh_budget
from artemis.channels.desktop.chat_widgets import (
    _VOICE_LABELS,
    ChatTranscript,
    OperationCard,
    PromptEdit,
    QuickOverlay,
    StepBlock,
    _Drawer,
    _TurnSteps,
    welcome_suggestions,
)
from artemis.channels.desktop.connect_wizard import maybe_connect
from artemis.channels.desktop.data_panel import DataPanel
from artemis.channels.desktop.dictation_history import (
    DictationHistory,
    handle_dictation_ui,
    on_dictation_history_action,
)
from artemis.channels.desktop.hotkey import GlobalHotkey, PushToTalkHotkey
from artemis.channels.desktop.instance import (
    InstanceServer,
    claim_instance,
    instance_name,
    set_app_user_model_id,
)
from artemis.channels.desktop.operations import OperationLog
from artemis.channels.desktop.page_kit import center_page
from artemis.channels.desktop.pet import PetWidget
from artemis.channels.desktop.pet_sprite import pets_dir_for
from artemis.channels.desktop.power_events import is_resume_native_event
from artemis.channels.desktop.profile_bar import refresh_profile_button, switch_work_profile
from artemis.channels.desktop.projects_panel import ProjectsPanel
from artemis.channels.desktop.selection_mouse import SelectionCandidate, SelectionMouseWatcher
from artemis.channels.desktop.selection_popup import SelectionBadge, SelectionPopup
from artemis.channels.desktop.settings_io import _same, save_computer_mode
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.desktop.telegram_service import TelegramService, should_run
from artemis.channels.desktop.widgets import PetFace
from artemis.channels.desktop.window_autostart import autostart_state, toggle_autostart
from artemis.channels.desktop.window_events import (
    add_step,
    append_chat,
    append_summary_to_vault,
    begin_card,
    decide_approval,
    handle_event,
    notify_job,
    render_card,
    reset_cards,
    run_routine_notice,
    show_toast_failure,
)
from artemis.channels.desktop.window_layout import (
    _NAV_COMPACT_WIDTH,
    _NAV_WIDTH,
    build_chat_page,
    build_sidebar,
    build_topbar,
    fit_topbar,
    page_frame,
)
from artemis.channels.desktop.window_restart import (
    apply_settings,
    check_provider,
    code_agent_config_saved,
    finish_exit,
    github_synced,
    install_update,
    leave_settings,
    maybe_restart_for_config,
    providers_changed,
    request_restart,
    restart_runtime,
    stay_in_settings,
    telegram_changed,
)
from artemis.channels.desktop.window_theme import (
    STATUS_TONES,
    apply_theme,
    dot_style,
    refresh_tones,
    theme_placeholders,
)
from artemis.channels.runtime import Runtime, create_runtime
from artemis.config.settings import load_settings
from artemis.i18n import set_language, t
from artemis.safety.audit import AuditLog
from artemis.tools.toast import ToastOutcome, show_toast

_STYLE = themes.stylesheet(themes.DEFAULT_THEME)
PROJECTS_POLL_ACTIVE_MS = 2000
PROJECTS_POLL_IDLE_MS = 15000


# Bu pencere genişliğinin altında adım paneli gizlenir, üst çubuk daralır.
_STEP_PANEL_MIN_WINDOW = 1100
_COMPACT_HEADER_WINDOW = 1000
_COMPACT_NAV_WINDOW = 900  # bunun altında yan gezinme yalnız ikon (UX-01)


class DesktopWindow(QMainWindow):
    # Kısayol kendi iş parçacığından yayar; Qt bunu ana iş parçacığına sıraya alır.
    _hotkey_pressed = Signal()
    _dictation_pressed = Signal()
    _selection_pressed = Signal()
    _selection_mouse_candidate = Signal(object)
    _selection_mouse_dismiss = Signal()
    _ptt_pressed = Signal()
    _ptt_released = Signal()
    _toast_failed = Signal(str)
    # Alanları window_layout kurar; burada yalnız tipleri bildirilir.
    _topbar: QFrame
    brand_pet: PetFace
    _theme_toggle_text: str
    demo_chip: QLabel | None
    model_caption: QLabel
    model_value: QPushButton
    model_box: QPushButton
    voice_dot: QLabel
    voice_state: QLabel
    budget_chip: QLabel
    profile_button: QPushButton
    _profile_actions: dict[str, QAction]
    _work_profile: str
    voice_button: QPushButton
    auto_button: QPushButton
    status_pill: QFrame
    status_dot: QLabel
    status_label: QLabel
    quick_action: QPushButton
    new_chat_button: QPushButton
    operations_button: QPushButton
    dictation_history_button: QPushButton
    dictation_history_menu: QMenu
    _dictation_history: DictationHistory
    _chat_row: QHBoxLayout
    chat: ChatTranscript
    _routine_name: str
    _routine_suggest: str
    _summary_day: str
    routine_bar: QFrame
    routine_label: QLabel
    routine_run_button: QPushButton
    routine_dismiss_button: QPushButton
    summary_vault_button: QPushButton
    prompt: PromptEdit
    composer_voice: QPushButton
    cancel_button: QPushButton
    send_button: QPushButton
    activity_panel: QFrame
    activity_close: QPushButton
    activity: QScrollArea
    activity_cards: QVBoxLayout
    activity_notice: QLabel
    activity_empty: QLabel
    _panel_in_drawer: bool
    drawer: _Drawer
    sidebar: QFrame
    nav_buttons: dict[QWidget, QPushButton]
    _nav_icon_names: dict[QPushButton, str]
    theme_toggle: QPushButton

    def __init__(
        self, config_path: Path, *, demo: bool = False, voice: bool = False,
        app: QApplication | None = None,
    ) -> None:
        super().__init__()
        app_instance = app if app is not None else QApplication.instance()
        if not isinstance(app_instance, QApplication):
            raise RuntimeError("Önce QApplication oluşturulmalı.")
        self._app = app_instance
        self._config_path = config_path
        self._restart_settings = False
        self._telegram_resynced = False
        self._config_restart_pending = False
        self._config_restart_serial = -1
        # Ertelenen yeniden kurulumun "neden bekliyor" notu isteği yapan panelde görünür.
        self._restart_notice: QLabel | None = None
        self._demo = demo
        self._continuous = False
        self._voice_state = "off"
        # Demo hiçbir koşulda mikrofon açmaz; --ses bayrağı da demoda yok sayılır.
        self._voice_on_ready = voice and not demo
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._runtime_factory: RuntimeFactory | None = None
        if demo:
            self._temporary = tempfile.TemporaryDirectory(prefix="artemis-demo-")
            temp_root = Path(self._temporary.name)

            def factory(path: Path, approve: Any, on_record: Any) -> Runtime:
                from artemis.channels.desktop.demo import create_demo_parts

                settings, provider, registry, audit = create_demo_parts(temp_root, on_record)
                runtime = create_runtime(
                    path, approve, settings=settings,
                    provider=provider, registry=registry, audit=audit,
                )
                runtime.context.services["demo_records"] = {
                    "sample": t("Demo kitaplığı: örnek metin")
                }
                return runtime

            self._runtime_factory = factory
        self.bridge = DesktopBridge(config_path, self._runtime_factory)
        self.setWindowTitle("Artemis")
        self.resize(1180, 780)
        self.setMinimumSize(720, 560)
        self._busy = False
        self._worker_ready = False
        self._exiting = False
        self._allow_close = False
        self._tray: QSystemTrayIcon | None = None
        self._autostart_action: QAction | None = None
        self._tray_attempts = 0
        self._approval_dialog: ApprovalDialog | None = None
        self._active_request: str | None = None
        self._turn_steps: dict[str, _TurnSteps] = {}
        self._ops = OperationLog()
        self._op_widgets: dict[str, OperationCard] = {}
        # Pet görseli diskten okunur; boyut başına bir kez, pet değişince boşaltılır.
        self._pet_cache: dict[int, QPixmap | None] = {}
        self._theme = themes.DEFAULT_THEME
        self._status_tone = "running"
        self._model_full = ""
        self._work_profile = "work"
        if not demo:
            with contextlib.suppress(OSError, ValueError, ValidationError, yaml.YAMLError):
                self._work_profile = load_settings(self._config_path).work_profile
        self._build_pet()
        self._build_ui()
        self._build_overlay()
        self._build_tray()
        self._hotkey: GlobalHotkey | None = None
        self._dictation_hotkey: GlobalHotkey | None = None
        self._selection_hotkey: GlobalHotkey | None = None
        self._selection_mouse: SelectionMouseWatcher | None = None
        self._selection_anchor: QPoint | None = None
        self._ptt_hotkey: PushToTalkHotkey | None = None
        # Seçili metin eylemleri (ADR 0062): parent=None ki Windows'ta sahip ana pencereyi
        # öne çekmesin (`finish_exit` ikisini de kapatır); düğmeler köprüye iletilir.
        self.selection_badge = SelectionBadge(None, self._on_selection_badge_clicked)
        self.selection_badge.apply_theme(themes.THEMES[self._theme])
        self.selection_popup = SelectionPopup(
            None, lambda action: self.bridge.selection_run(action),
            lambda: self.bridge.selection_write_back(),
        )
        self.selection_popup.apply_theme(themes.THEMES[self._theme])
        self._hotkey_pressed.connect(self.voice_button_click)
        self._selection_pressed.connect(lambda: self.bridge.selection_hotkey())
        self._selection_mouse_candidate.connect(self._on_selection_mouse_candidate)
        self._selection_mouse_dismiss.connect(self.selection_badge.hide)
        # Köprü ayar uygulanınca yenilenir; sinyal her seferinde güncel olanı çağırır.
        self._dictation_pressed.connect(lambda: self.bridge.toggle_dictation())
        self._ptt_pressed.connect(lambda: self.bridge.ptt_press())
        self._ptt_released.connect(lambda: self.bridge.ptt_release())
        if not demo:
            self._refresh_welcome()
            self._start_hotkey()
            self._start_dictation_hotkey()
            self._start_selection_hotkey()
            self._start_ptt_hotkey()
        self.pet.start()
        self._refresh_brand_pet()
        self.settings_panel.pet.set_current(self.pet.skin)
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._drain_bridge)
        self._timer.start()

    def _build_pet(self) -> None:
        # Pet yalnız durum alır; komut ve onay yolları panelinkiyle aynıdır.
        self.pet = PetWidget(
            on_toggle_voice=self.voice_button_click, on_show_panel=self._show_panel,
            on_quick=self._show_overlay, on_approval=self._focus_approval,
            on_quit=self._request_exit, on_submit=lambda text: self._submit_text(text),
            pets_dir=pets_dir_for(self._config_path),
        )

    def _select_skin(self, pet_id: str) -> None:
        problem = self.pet.set_skin(pet_id)
        self._refresh_brand_pet()
        if problem:
            self.settings_panel.pet.show_problem(problem)

    def _pet_pixmap(self, size: int) -> QPixmap | None:
        """Seçili petin durağan karesi (başlık, karşılama ve balonlar aynı kaynağı kullanır)."""
        if size not in self._pet_cache:
            info = pet_sprite.find_pet(self.pet.skin, pets_dir_for(self._config_path))
            try:
                self._pet_cache[size] = pet_sprite.preview(info, size) if info else None
            except pet_sprite.PetError:
                self._pet_cache[size] = None
        return self._pet_cache[size]

    def _refresh_brand_pet(self) -> None:
        """Selected pet beside the immutable Artemis product name."""
        self._pet_cache.clear()
        self.brand_pet.set_pixmap(self._pet_pixmap(32))
        self.chat.refresh_pet()

    def _apply_theme(self, name: str) -> None:
        apply_theme(self, name)

    def _token(self, key: str) -> str:
        return themes.THEMES[self._theme][key]

    def _start_hotkey(self) -> None:
        hotkey_manager.start_listening_hotkey(self, GlobalHotkey)

    def _start_dictation_hotkey(self) -> None:
        """Dikte kısayolu (ADR 0057): kapalıyken hiç kaydedilmez; ayar uygulanınca yenilenir."""
        hotkey_manager.start_dictation_hotkey(self, GlobalHotkey)

    def _start_selection_hotkey(self) -> None:
        """Seçili metin kısayolu (ADR 0062): kapalıyken kaydedilmez; ayar uygulanınca yenilenir."""
        hotkey_manager.start_selection_hotkey(self, GlobalHotkey)

    def _start_ptt_hotkey(self) -> None:
        """Bas-konuş kısayolu: kapalıyken kaydedilmez; ayar uygulanınca yenilenir."""
        hotkey_manager.start_ptt_hotkey(self, PushToTalkHotkey)

    def _reload_hotkeys(self) -> None:
        hotkey_manager.reload_hotkeys(self, GlobalHotkey, PushToTalkHotkey)

    def _show_selection(self, kind: str, message: str) -> None:
        """Seçili metin eylemi durumu: pencere imleç yanında açılır, sonuç orada görünür."""
        if kind == "ready":
            self.selection_badge.hide()
            anchor, self._selection_anchor = self._selection_anchor, None
            self.selection_popup.show_ready(message, anchor=anchor)
        elif kind == "working":
            self.selection_popup.show_working()
        elif kind == "result":
            self.selection_popup.show_result(message)
        elif kind == "message" and message:
            self._selection_anchor = None
            self.selection_badge.hide()
            self.selection_popup.show_message(message)
            self._set_status(message, "ready")

    def _is_selection_ui_at(self, x: int, y: int) -> bool:
        return (
            self.selection_badge.contains_global(x, y)
            or self.selection_popup.contains_global(x, y)
        )

    def _on_selection_mouse_candidate(self, candidate: SelectionCandidate) -> None:
        if not self._busy and not self.selection_popup.isVisible():
            self.selection_badge.show_for(candidate)

    def _on_selection_badge_clicked(self, candidate: SelectionCandidate) -> None:
        self._selection_anchor = QPoint(candidate.x, candidate.y)
        self.bridge.selection_capture_mouse(candidate.hwnd, candidate.pre_text)

    def _show_dictation(self, kind: str, message: str) -> None:
        """Dikte durumu ve RAM içi son-5 geçmişi (ADR 0057)."""
        handle_dictation_ui(self, kind, message)

    def _on_dictation_history_action(self, action: QAction) -> None:
        on_dictation_history_action(self, action)

    def _hotkey_problem(self, message: str) -> None:
        if self._tray is not None:
            self._tray.showMessage("Artemis", message)
        else:
            self._set_status(message, "error")

    def voice_button_click(self) -> None:
        """Pet tıkı: panelin "Ses" düğmesiyle aynı yol (_toggle_voice); düğme kapalıysa işlemez."""
        self.voice_button.click()

    def _focus_approval(self) -> None:
        dialog = self._approval_dialog
        if dialog is None:
            self._show_panel()
            return
        self._show_panel()
        dialog.raise_()
        dialog.activateWindow()

    def _open_operations(self) -> None:
        if not self._panel_in_drawer:
            return
        self.drawer.setGeometry(self.chat_page.rect())
        self.drawer.raise_()
        self.drawer.show()
        self.drawer.setFocus()

    def _close_operations(self) -> None:
        self.drawer.hide()

    def _dock_operations(self, in_drawer: bool) -> None:
        """Aynı panel iki yerde yaşar: widget taşınır, kartlar ve durum kaybolmaz."""
        if in_drawer == self._panel_in_drawer:
            return
        self._panel_in_drawer = in_drawer
        panel = self.activity_panel
        if in_drawer:
            self._chat_row.removeWidget(panel)
            self.drawer.dock(panel)
        else:
            self.drawer.hide()
            self.drawer.undock(panel)
            self._chat_row.addWidget(panel)
        self.activity_close.setVisible(in_drawer)
        self._sync_activity_panel()

    def _sync_activity_panel(self) -> None:
        """Sağ sütun işlem yokken çöker: boş 280px kolon sohbeti sola iter ve boşa yer tutar.

        Çekmecede (dar pencere) panel hep gösterilir; kullanıcı "İşlemler"e basınca boş
        durum cümlesini görmeli. Sütun gizlenince sohbet sütunu yerleşimde kendiliğinden ortalanır.
        """
        self.activity_panel.setVisible(self._panel_in_drawer or bool(self._op_widgets))

    def _build_ui(self) -> None:
        root = QWidget()
        main = QVBoxLayout(root)
        main.setContentsMargins(16, 16, 16, 16)
        main.setSpacing(16)
        main.addWidget(build_topbar(self))

        self.chat_page = build_chat_page(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.chat_page, t("Sohbet"))
        self.projects_panel = ProjectsPanel(demo=self._demo, config_path=self._config_path)
        self.projects_panel.tool_requested.connect(self._submit_project_tool)
        self.projects_panel.config_saved.connect(self._code_agent_config_saved)
        self._projects_timer = QTimer(self)
        self._notified_jobs: set[tuple[str, str, str]] = set()
        # Demo gerçek makineye bildirim göndermez; testler sahte işlev enjekte eder.
        self._toast: Callable[[str, str], ToastOutcome] | None = None if self._demo else show_toast
        self._toast_failed.connect(self._show_toast_failure)
        self._projects_timer.setInterval(PROJECTS_POLL_ACTIVE_MS)
        self._projects_timer.timeout.connect(self._refresh_projects)
        self._projects_timer.start()
        self.settings_panel = SettingsPanel(
            self._config_path, demo=self._demo,
            state=QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                            "Artemis", "appearance"),
        )
        self.settings_panel.apply_requested.connect(self._apply_settings)
        self.settings_panel.save_requested.connect(self._settings_saved_now)
        # Otomatik kayıt config'i yazar ama çalışma zamanı (= yeni sohbet) yalnız Ayarlar
        # sekmesinden çıkılınca bir kez yeniden kurulur; her düzenlemede sohbet sıfırlanmaz.
        # Karar bayrakla değil içerikle verilir: çalışma zamanının kurulduğu config'in dökümü
        # saklanır. Böylece kendi başına yazan bölümler (tetikleyici formu, beceri klasörü) da
        # kapsanır, geri alma eski hâle dönünce boşuna yeniden kurulmaz.
        self._runtime_dump = self.settings_panel.config_dump()
        self.settings_panel.routines.tool_requested.connect(self._submit_routine_tool)
        self.settings_panel.skills.try_requested.connect(self._try_skill)
        # Üst çubuktaki düğme açılışta config'teki kipi gösterir; sinyal kapalı ki açılış
        # kendiliğinden config yazmasın.
        on = self.settings_panel.computer_auto.isChecked()
        self.auto_button.blockSignals(True)
        self.auto_button.setChecked(on)
        self.auto_button.blockSignals(False)
        self._show_auto_state(on)
        self.settings_panel.continuous_mode.toggled.connect(self._live_continuous)
        self.settings_panel.computer_auto.toggled.connect(self._toggle_auto_mode)
        self.settings_panel.github_synced.connect(self._github_synced)
        self.settings_panel.providers.changed.connect(self._providers_changed)
        self.settings_panel.telegram.changed.connect(self._telegram_changed)
        # İki yönlü kanal masaüstü içinde isteğe bağlı çalışır (ADR 0052); kendi iş parçacığı.
        self._telegram_service = TelegramService(self.settings_panel.telegram.channel_status.emit)
        self.settings_panel.telegram.channel_changed.connect(self._sync_telegram_service)
        self._sync_telegram_service()
        self.settings_panel.pet.skin_selected.connect(self._select_skin)
        self.settings_panel.pet.model_settings_requested.connect(self._open_model_settings)
        self.settings_panel.update_section.install_ready.connect(self._install_update)
        self.settings_scroll = self.settings_panel
        # Ctrl+, her yerden Ayarlar'ı açar (pencere kapsamlı).
        self._settings_shortcut = QShortcut(QKeySequence("Ctrl+,"), self)
        self._settings_shortcut.activated.connect(self._show_settings)
        self.tabs.addTab(self.settings_scroll, t("Ayarlar"))
        self.projects_page = page_frame(self.projects_panel)
        self.tabs.addTab(self.projects_page, t("Projeler"))
        self.data_panel = DataPanel(demo=self._demo)
        self.data_panel.tool_requested.connect(self._submit_project_tool)
        self.data_page = page_frame(self.data_panel)
        self.tabs.addTab(self.data_page, t("Geçmiş"))
        self.automation_panel = AutomationPanel(demo=self._demo, config_path=self._config_path)
        self.automation_panel.tool_requested.connect(self._submit_automation_tool)
        self.automation_panel.settings_requested.connect(self._open_settings_section)
        self.automation_page = page_frame(self.automation_panel)
        self.tabs.addTab(self.automation_page, t("Otomasyon"))
        # Pet atölyesi günlük ayarlardan ayrı sayfadır; PetSection kendi başlığını taşır
        # ama kaydırıcısı yoktur.
        self.pet_scroll = QScrollArea()
        self.pet_scroll.setWidgetResizable(True)
        self.pet_scroll.setFrameShape(QFrame.Shape.NoFrame)
        center_page(self.pet_scroll)
        self.pet_scroll.setWidget(self.settings_panel.pet)
        self.pet_page = page_frame(self.pet_scroll)
        self.tabs.addTab(self.pet_page, t("Petler"))
        self.tabs.tabBar().hide()
        sidebar = build_sidebar(self, [
            ("Sohbet", self.chat_page), ("Projeler", self.projects_page),
            ("Otomasyon", self.automation_page), ("Petler", self.pet_page),
            ("Geçmiş", self.data_page), ("Ayarlar", self.settings_scroll),
        ])
        self.tabs.currentChanged.connect(self._sync_navigation)
        self._settings_open = self.tabs.currentWidget() is self.settings_scroll
        self.tabs.currentChanged.connect(self._settings_tab_changed)
        self._sync_navigation()
        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(sidebar)
        body.addWidget(self.tabs, 1)
        main.addLayout(body, 1)

        self.setCentralWidget(root)
        self._appearance = QSettings(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, "Artemis", "appearance"
        )
        selected_theme = str(self._appearance.value("theme", themes.DEFAULT_THEME))
        if selected_theme not in themes.THEMES:
            selected_theme = themes.DEFAULT_THEME
        self.settings_panel.set_theme(selected_theme)
        self.settings_panel.theme_changed.connect(self._apply_theme)
        self._apply_theme(selected_theme)
        self._describe_voice(t(_VOICE_LABELS["off"]), False)
        self._set_controls()
        self._fit_shell()
        refresh_budget(self)
        QTimer.singleShot(0, self.prompt.setFocus)
        # Açılışta bir kez, arka plan thread'inde; CLI'da eşitleme yoktur.
        QTimer.singleShot(0, self.settings_panel.start_auto_sync)
        if not self._demo:
            self._check_provider(open_settings=True)

    def _check_provider(self, *, open_settings: bool) -> None:
        check_provider(self, open_settings=open_settings)

    def _show_settings(self) -> None:
        self.tabs.setCurrentWidget(self.settings_scroll)

    def _open_settings_section(self, name: str) -> None:
        self.settings_panel.select_section(name)
        self.tabs.setCurrentWidget(self.settings_scroll)

    def _sync_navigation(self, *_args: object) -> None:
        for page, button in self.nav_buttons.items():
            button.setChecked(page is self.tabs.currentWidget())

    def _settings_tab_changed(self, *_args: object) -> None:
        """Ayarlar'dan çıkışta kaydedilmemiş değişiklik sorulur; girişte taban çizgisi alınır."""
        is_open = self.tabs.currentWidget() is self.settings_scroll
        if is_open == self._settings_open:
            return
        self._settings_open = is_open
        if is_open:
            self.settings_panel.begin_session()
            return
        if not leave_settings(self):
            stay_in_settings(self)
            return
        if self._settings_restart_pending:
            self._request_restart(self.settings_panel.notice)

    def _settings_saved_now(self) -> None:
        """Kaydet: sekmeden çıkmayı beklemeden uygular (meşgulse yeniden kurulum ertelenir)."""
        if self._settings_restart_pending:
            self._request_restart(self.settings_panel.notice)

    def _install_update(self, path: object) -> None:
        install_update(self, path)

    @property
    def _settings_restart_pending(self) -> bool:
        """Kayıtlı config, çalışma zamanının son kurulduğu config'ten (tür-katı) farklı mı."""
        return not _same(self.settings_panel.config_dump(), self._runtime_dump)

    def _toggle_theme(self) -> None:
        """Açık/koyu arasında geçer; karma (mixed) türev korunur."""
        name = self._theme
        flipped = name.replace("dark", "light") if name.startswith("dark") else (
            name.replace("light", "dark")
        )
        # Ayarlar'daki seçim sinyal yaymadan eşitlenir; tema tek kez uygulanır.
        self.settings_panel.set_theme(flipped)
        self._apply_theme(flipped)

    def _providers_changed(self) -> None:
        providers_changed(self)

    def _telegram_changed(self) -> None:
        telegram_changed(self)

    def _sync_telegram_service(self) -> None:
        """Config'e göre kanal servisini durdurup (gerekirse) yeniden başlatır; canlı, yeniden
        kurulum gerektirmez. Demo'da ve çıkışta asla başlamaz."""
        service = self._telegram_service
        service.stop()
        if self._demo or self._exiting:
            return
        try:
            config = load_settings(self._config_path).telegram
        except (OSError, ValueError, yaml.YAMLError):
            return
        if should_run(config):
            service.start(self._config_path)

    def _code_agent_config_saved(self) -> None:
        code_agent_config_saved(self)

    def _request_restart(self, notice: QLabel | None = None) -> None:
        request_restart(self, notice)

    def _maybe_restart_for_config(self) -> None:
        maybe_restart_for_config(self)

    def _github_synced(self, report: Any, auto: bool) -> None:
        github_synced(self, report, auto)

    def _build_overlay(self) -> None:
        self.overlay = QuickOverlay(self._submit_overlay, self._cancel_active, self._show_panel)
        self.overlay.setStyleSheet(themes.stylesheet(self._theme))
        theme_placeholders(self, self.overlay)

    def _apply_settings(self, device: int | str | None) -> None:
        apply_settings(self, device)

    def _restart_runtime(self) -> None:
        restart_runtime(self)

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(app_icon(), self)
        self._tray.setToolTip("Artemis")
        menu = QMenu(self)
        open_action = menu.addAction(t("Paneli aç"))
        quick_action = menu.addAction(t("Hızlı komut"))
        pet_action = menu.addAction(t("Peti göster/gizle"))
        if sys.platform == "win32" and not self._demo:
            # ADR 0030: kayıt defteri değişikliği yalnız kullanıcının bu tıklamasıyla olur.
            self._autostart_action = menu.addAction(t("Windows açılışında başlat"))
            self._autostart_action.setCheckable(True)
            self._autostart_action.setChecked(self._autostart_state() == "on")
            self._autostart_action.toggled.connect(self._toggle_autostart)
        menu.addSeparator()
        exit_action = menu.addAction(t("Çıkış"))
        open_action.triggered.connect(self._show_panel)
        quick_action.triggered.connect(self._show_overlay)
        pet_action.triggered.connect(self.pet.toggle_pet)
        exit_action.triggered.connect(self._request_exit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()
        self._app.setQuitOnLastWindowClosed(False)

    def _autostart(self) -> Autostart | None:
        try:
            settings = load_settings(self._config_path)
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            return None
        return Autostart(self._config_path, emit=AuditLog(settings.audit_path).emit)

    def _autostart_state(self) -> str:
        return autostart_state(self)

    def _toggle_autostart(self, enabled: bool) -> None:
        toggle_autostart(self, enabled)

    def present(self, *, background: bool) -> None:
        """`--arka-plan` paneli göstermez; tepsi yoksa görünmez süreç bırakılmaz.

        Windows oturum açılışında Explorer tepsisi Artemis'ten sonra hazır olabilir: arka
        planda tepsi bir süre yeniden denenir, ancak hâlâ yoksa panel gösterilir.
        """
        if not background:
            self.show()
            return
        self._tray_attempts = 0
        self._wait_for_tray()

    def _wait_for_tray(self) -> None:
        if self._tray is None and not self._exiting:
            self._build_tray()
        if self._tray is not None or self._exiting:
            return
        self._tray_attempts += 1
        if self._tray_attempts >= TRAY_RETRY_ATTEMPTS:
            self.show()
            return
        QTimer.singleShot(TRAY_RETRY_MS, self, self._wait_for_tray)

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_panel()

    def show_from_instance(self) -> None:
        if not self._exiting:
            self._show_panel()

    def _open_model_settings(self) -> None:
        """Pet atölyesinin boş durumundaki "Ayarlar'da model ekle" düğmesi: Ayarlar > Model."""
        self._open_settings_section("Model")

    def _show_panel(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _show_overlay(self) -> None:
        self.overlay.show()
        self.overlay.raise_()
        self.overlay.activateWindow()
        self.overlay.input.setFocus()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._fit_shell()

    def _fit_shell(self) -> None:
        """Pencere genişliğine göre üst çubuğu daraltır; işlem kartlarını sütun ya da sayfa yapar.

        900px'te hiçbir şey kırpılmaz: model adı "…" ile kesilir, çok dar pencerede model
        çipi tamamen çekilir. İşlem kartları ≥1100px'te sağ sütundur; daha darda aynı panel
        "İşlemler" düğmesiyle açılan yan sayfaya taşınır, böylece her genişlikte erişilir.
        """
        width = self.width()
        self._compact_nav(width < _COMPACT_NAV_WINDOW)
        wide = width >= _STEP_PANEL_MIN_WINDOW
        self._show_model(self._model_full)
        fit_topbar(self)
        self.operations_button.setVisible(not wide)
        self._dock_operations(not wide)
        self._sync_activity_panel()
        if self.drawer.isVisible():
            self.drawer.setGeometry(self.chat_page.rect())

    def _compact_nav(self, compact: bool) -> None:
        """Dar pencerede yan gezinme yalnız ikon olur (UX-01): içerik ~110px kazanır.

        Ad erişilebilir ad ve ipucu olarak kalır; böylece ekran okuyucu ve fare etiketi yitirmez."""
        if compact == (self.sidebar.width() == _NAV_COMPACT_WIDTH):
            return
        self.sidebar.setFixedWidth(_NAV_COMPACT_WIDTH if compact else _NAV_WIDTH)
        for button in self.nav_buttons.values():
            button.setText("" if compact else button.accessibleName())
        self.theme_toggle.setText("" if compact else self._theme_toggle_text)
        self.theme_toggle.setToolTip(self.theme_toggle.accessibleName())

    def _show_model(self, text: str) -> None:
        self._model_full = text
        win_w = self.width()
        if win_w < _COMPACT_HEADER_WINDOW:
            limit = 120
        elif win_w < _STEP_PANEL_MIN_WINDOW:
            limit = 180
        else:
            limit = 240
        metrics = QFontMetrics(self.model_value.font())
        shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, limit)
        self.model_value.setText(shown)
        self.model_value.setToolTip(
            t("Kullanılan beyin modeli: {text}. Değiştirmek için Ayarlar'ı aç.").format(text=text)
            if text else t("Kullanılan beyin modeli")
        )
        # Çipin 8px yan dolgusu iki yanda 16px ekler; 8px pay ölçü sapmasına karşıdır.
        self.model_value.setFixedWidth(max(60, metrics.horizontalAdvance(shown) + 24))
        fit_topbar(self)

    def _submit_main(self) -> None:
        text = self.prompt.toPlainText().strip()
        if text and self._submit_text(text):
            self.prompt.clear()

    def _set_suggestion(self, text: str) -> None:
        self.prompt.setPlainText(text)
        self.prompt.setFocus()
        cursor = self.prompt.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.prompt.setTextCursor(cursor)

    def _try_skill(self, name: str) -> None:
        """Ayarlar > Beceriler "Dene": sohbete geçer, /beceri-adı yazar, göndermez."""
        self.tabs.setCurrentWidget(self.chat_page)
        self._set_suggestion(f"/{name} ")

    def _on_welcome_chip(self, text: str) -> None:
        """Demo ve "…" ile biten öneri yalnız yazma alanına yazılır; diğerleri gönderilir."""
        if self._demo or text.endswith("…"):
            self._set_suggestion(text.removesuffix("…"))
        else:
            self._set_suggestion(text)
            self._submit_main()

    def _refresh_welcome(self) -> None:
        """Karşılama önerileri açık özelliklerden kurulur; config okunamazsa varsayılanlar."""
        try:
            settings = load_settings(self._config_path)
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            settings = None
        self.chat.set_suggestions(
            welcome_suggestions(settings),
            settings.dictation.hotkey if settings and settings.dictation.enabled else "",
        )

    def _show_model_settings(self, _event: object = None) -> None:
        self._open_settings_section("Model")

    def _submit_overlay(self) -> None:
        text = self.overlay.input.toPlainText().strip()
        if text and self._submit_text(text):
            self.overlay.input.clear()

    def _submit_text(self, text: str) -> bool:
        request_id = self.bridge.submit(text)
        if request_id is None:
            self._set_status(t("Hazır değil veya önceki işlem temizleniyor."), "running")
            return False
        self._active_request = request_id
        self._busy = True
        self._begin_card(request_id, text)
        self._append_chat("Siz", text)
        self._set_status(t("Çalışıyor…"), "running")
        self._set_controls()
        self.overlay.set_busy(True)
        return True

    def _append_summary_to_vault(self) -> None:
        append_summary_to_vault(self)

    def _run_routine_notice(self) -> None:
        run_routine_notice(self)

    def _submit_automation_tool(self, name: str, arguments: BaseModel) -> None:
        self._submit_routine_tool(name, arguments, from_automation=True)

    def _submit_routine_tool(
        self, name: str, arguments: BaseModel, *, from_automation: bool = False
    ) -> None:
        request_id = self.bridge.submit_tool(name, arguments)
        if request_id is None:
            # Otomasyon sayfasından gelen uyarı kendi satırında kalır; ayar satırı değişmez.
            notice = (
                self.automation_panel.notice if from_automation
                else self.settings_panel.routines.notice
            )
            notice.setText(t("Başka işlem sürüyor; çalıştırılmadı."))
            self._set_status(t("Başka işlem sürüyor; rutin çalıştırılmadı."), "running")
            return
        self._active_request = request_id
        self._busy = True
        self._begin_card(request_id, self._routine_tool_title(name, arguments))
        self._set_controls()

    def _routine_tool_title(self, name: str, arguments: BaseModel) -> str:
        """İşlem kartı başlığı: ham araç adı yerine insan okur ad (kapı yolu değişmez)."""
        if name == "vault_append":
            return t("Gün özetini vault'a ekle")
        if name == "research_cancel":
            return t("Araştırmayı durdur")
        if name == "routine_run":
            label = t(self.bridge.labels().get(name, ("Rutin çalıştır", ""))[0])
            return f"{label}: {getattr(arguments, 'name', '')}".rstrip(": ")
        return t("Rutin işlemi: {name}").format(name=name)

    def _submit_project_tool(self, name: str, arguments: BaseModel) -> None:
        # Durum yoklaması salt okunur ve 2 sn'de bir gelir: kart, meşgul durumu ve pet
        # tepkisi üretmez (aksi halde "Taslak hazırla"ya basılmış gibi titrer).
        quiet = name == "code_agent_status"
        request_id = self.bridge.submit_tool(name, arguments, quiet=quiet)
        if request_id is None:
            if not quiet:
                panel = self.projects_panel if name.startswith("code_agent_") else self.data_panel
                panel.notice.setText(t("Başka işlem sürüyor; istek çalıştırılmadı."))
            return
        self._active_request = request_id
        if quiet:
            return
        self._busy = True
        self._begin_card(request_id, t("Proje işlemi: {name}").format(name=name))
        self._set_controls()

    def _refresh_projects(self) -> None:
        # Her yoklama denetime yazılan tam bir turdur; etkin iş yokken seyrek yoklanır.
        idle = self.projects_panel.active_job_count() == 0 and not self._config_restart_pending
        want = PROJECTS_POLL_IDLE_MS if idle else PROJECTS_POLL_ACTIVE_MS
        if self._projects_timer.interval() != want:
            self._projects_timer.setInterval(want)
        if self._config_restart_pending:
            # Ertelenmiş yeniden kurulum: iş listesi sekmeden bağımsız tazelenir, son durumu
            # (status_serial arttıktan sonra) boşsa tek kez yeniden kurulur.
            if self._worker_ready and not self._busy:
                if self.projects_panel.status_serial == self._config_restart_serial:
                    self.projects_panel.refresh()
                else:
                    self._config_restart_serial = self.projects_panel.status_serial
                    self._maybe_restart_for_config()
            return
        on_projects = self.tabs.currentWidget() is self.projects_page
        if on_projects and self._worker_ready and not self._busy:
            self.projects_panel.refresh()

    def _new_chat(self) -> None:
        # Ekran, işçi sıfırlamayı doğruladığında (context_reset olayı) temizlenir;
        # işçi reddederse görünen sohbet ile modelin bağlamı ayrışmaz.
        if self.bridge.reset_conversation():
            self.new_chat_button.setEnabled(False)

    def _cancel_active(self) -> None:
        if self._active_request is not None:
            self.bridge.cancel(self._active_request)
        if self._approval_dialog is not None:
            self._approval_dialog.reject()

    def _toggle_voice(self, checked: bool) -> None:
        if checked:
            if not self.bridge.start_voice():
                self.voice_button.setChecked(False)
        elif not self.bridge.stop_voice():
            self.voice_button.setChecked(False)

    def _sync_auto_controls(self, on: bool) -> None:
        # İki denetim de sinyalsiz eşitlenir: düğme ve ayar kutusu aynı kipi göstermeli.
        for control in (self.settings_panel.computer_auto, self.auto_button):
            control.blockSignals(True)
            control.setChecked(on)
            control.blockSignals(False)
        self._show_auto_state(on)

    def _on_profile_action(self, action: QAction) -> None:
        profile = action.data()
        if isinstance(profile, str):
            switch_work_profile(self, profile)

    def _toggle_auto_mode(self, checked: bool) -> None:
        """Üst çubuktaki düğme: kip anında değişir ve config'e yazılır."""
        mode = "auto" if checked else "accept"
        if checked and not self._demo and not self._confirm_auto_mode():
            self._sync_auto_controls(False)
            return
        shown = checked
        if not self._demo:
            saved, applied = True, False
            try:
                save_computer_mode(self._config_path, mode)
            except (OSError, ValueError):
                saved = False
            # Kapatma güvenli kip (accept) içindir; disk yazılmasa bile runtime kapanır.
            if saved or not checked:
                applied = self.bridge.set_computer_mode(mode)
            if checked and (not saved or not applied):
                if saved and not applied:
                    # Uygulanamayan auto diskte kalmasın; geri yazılamazsa durum yine hata gösterir.
                    with contextlib.suppress(OSError, ValueError):
                        save_computer_mode(self._config_path, "accept")
                msg = t("Kip kaydedilemedi") if not saved else t("Otomatik onay uygulanamadı")
                self._set_status(msg, "error")
                shown = False
            elif not checked:
                if not saved or not applied:
                    msg = (
                        t("Kip kapatıldı ama kaydedilemedi") if applied
                        else t("Kip çalışma zamanına uygulanamadı")
                    )
                    self._set_status(msg, "error")
                shown = not applied
            if saved and applied:
                self._append_chat(
                    "Sistem", t("Bilgisayar kullanımı kipi: {mode}.").format(mode=mode)
                )
        self._sync_auto_controls(shown)

    def _auto_mode_box(self) -> tuple[QMessageBox, QPushButton]:
        """Uyarı kutusu ve "aç" düğmesi; ayrı kurulur ki README görüntüsü bloklamadan alınsın."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(t("Otomatik onayı aç?"))
        box.setText(t(
            "Otomatik onayda Artemis ekranı okur, tıklar ve yazar; hassas adımları da "
            "sana sormadan yapar. Esc her an durdurur."
        ))
        box.setInformativeText(t(
            "Yanlış anlaşılan bir komut ya da ekrandaki zararlı bir metin, istemediğin bir "
            "işlemi tetikleyebilir; web okuduktan sonra bağlantılar da sorulmadan açılır. "
            "Terminal, Artemis ve parola yöneticisi yine engelli."
        ))
        yes = box.addButton(t("Riski anlıyorum, aç"), QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton(t("Vazgeç"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        return box, yes

    def _confirm_auto_mode(self) -> bool:
        """Otomatik kip bilinçli açılır: varsayılan cevap hayır (kural 2'nin ruhu)."""
        box, yes = self._auto_mode_box()
        box.exec()
        return box.clickedButton() is yes

    def _show_auto_state(self, on: bool) -> None:
        """Simge anahtarı metin taşımaz; durum erişilebilir açıklamada ve ipucunda okunur."""
        self.auto_button.setAccessibleDescription(
            t("Oto onay açık") if on else t("Oto onay kapalı")
        )
        mode = t("otomatik") if on else t("her işlemde sor")
        self.auto_button.setToolTip(t(
            "Onay: {mode}. Oto onay açıkken bilgisayar kullanımı adımları sormadan çalışır; "
            "Esc yine durdurur. Değişiklik hemen geçerli, Uygula gerekmez."
        ).format(mode=mode))

    def _live_continuous(self, checked: bool) -> None:
        # Kayıt Uygula ile olur; ses açıksa kip hemen uygulanır.
        self.bridge.set_continuous(checked)

    def _show_continuous(self, on: bool) -> None:
        self._continuous = on
        box = self.settings_panel.continuous_mode
        box.blockSignals(True)
        box.setChecked(on)
        box.blockSignals(False)
        self._show_voice_state(self._voice_state)

    def _set_status(self, text: str, tone: str) -> None:
        # Durum düz metindir; ton yalnız yanındaki noktanın rengini belirler.
        self._status_full = text
        self.status_label.setText(text)
        self._status_tone = tone
        self.status_dot.setStyleSheet(dot_style(self._token(STATUS_TONES[tone])))
        self.status_pill.adjustSize()
        fit_topbar(self)

    def _show_voice_state(self, state: str) -> None:
        self._voice_state = state
        if state == "off":
            self._continuous = False
        text = t(_VOICE_LABELS.get(state, _VOICE_LABELS["off"]))
        if self._continuous:
            text += " · " + t("Kesintisiz")
        self.voice_state.setText(text)
        self.overlay.voice_state.setText(text)
        on = state in {"preparing", "listening", "awaiting_command", "thinking", "speaking"}
        self._describe_voice(text, on)
        refresh_tones(self)
        self.pet.set_voice(state)
        if state == "off":
            self.settings_panel.input_level.setValue(0)

    def _describe_voice(self, text: str, on: bool) -> None:
        """Üst çubuk ve yazma alanındaki ses anahtarı aynı durumu ve açıklamayı taşır."""
        if self._demo:
            tip = t("Demo modunda ses yok")
        elif on:
            tip = t("{text} · kapatmak için tıkla").format(text=text)
        else:
            tip = t("{text} · açmak için tıkla; mikrofonu yalnız sen açarsın").format(text=text)
        for button in (self.voice_button, self.composer_voice):
            button.setChecked(on)
            button.setAccessibleDescription(text)
            button.setToolTip(tip)

    def _composer_voice_clicked(self) -> None:
        # Yazma alanındaki simge üst çubuktakiyle aynı işleyiciyi çalıştırır.
        self.voice_button.click()
        self.composer_voice.setChecked(self.voice_button.isChecked())

    def _set_controls(self) -> None:
        enabled = self._worker_ready and not self._busy and not self._exiting
        voice_ok = self._worker_ready and not self._exiting and not self._demo
        self.voice_button.setEnabled(voice_ok)
        self.composer_voice.setEnabled(voice_ok)
        # Kip değişimi çalışan turu kesmez; yalnız bitişte kapanmasın diye korunur.
        self.auto_button.setEnabled(not self._exiting and not self._demo)
        self.profile_button.setEnabled(not self._exiting)
        self.projects_panel.set_busy(not enabled)
        self.data_panel.set_busy(not enabled)
        self.automation_panel.set_busy(not enabled)
        self.prompt.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        self.new_chat_button.setEnabled(enabled)
        # İptal yalnız iş sürerken görünür; boştayken yazma satırı sade kalır.
        cancellable = self._busy and not self._exiting
        self.cancel_button.setEnabled(cancellable)
        self.cancel_button.setVisible(cancellable)
        self.pet.set_busy(self._busy and not self._exiting)
        if self._tray is not None:
            status = t("çalışıyor") if self._busy else t("hazır")
            self._tray.setToolTip(f"Artemis · {status}")

    def _drain_bridge(self) -> None:
        for event in self.bridge.poll_events(64):
            self._handle_event(event)
        if self.bridge.stopped:
            if self._restart_settings and not self._exiting:
                self._restart_settings = False
                self._busy = False
                self._active_request = None
                self._turn_steps.clear()
                self._reset_cards()
                self.chat.clear()
                self._append_chat("Sistem", t("Ayarlar uygulandı; yeni sohbet başladı."))
                self._show_voice_state("off")
                self.pet.set_busy(False)
                # Demo kipi yeniden kurulumda da demo fabrikasını korur; gerçek runtime açılmaz.
                self.bridge = DesktopBridge(self._config_path, self._runtime_factory)
                if not self._demo:
                    self._refresh_welcome()
                    self._reload_hotkeys()
                    with contextlib.suppress(OSError, ValueError, ValidationError, yaml.YAMLError):
                        refresh_profile_button(self, load_settings(self._config_path).work_profile)
                self.settings_panel.refresh()
                self.settings_panel.notice.setText(
                    t("Ayarlar uygulandı; seçilen model ve mikrofon kullanılıyor.")
                )
            else:
                self._finish_exit()

    def _handle_event(self, event: BridgeEvent) -> None:
        handle_event(self, event)

    def _pet_activity(self, event: Activity) -> None:
        self.pet.code_agent_event(event.event, event.fields)
        if event.event in {"code_agent_started", "code_agent_waiting", "code_agent_done"}:
            # Liste yoklaması etkin işte 2 sn, değilse 15 sn; durum değişince hemen hızlanır.
            self._projects_timer.setInterval(PROJECTS_POLL_ACTIVE_MS)
            self._projects_timer.start()
        if event.event in {"code_agent_waiting", "code_agent_done"}:
            notify_job(self, event.fields)

    def _show_toast_failure(self, message: str) -> None:
        show_toast_failure(self, message)

    def _decide_approval(
        self, item: Approval, approved: bool, remember_site: bool = False
    ) -> bool:
        return decide_approval(self, item, approved, remember_site)

    def _begin_card(self, request_id: str, text: str) -> None:
        begin_card(self, request_id, text)

    def _render_card(self, request_id: str) -> None:
        render_card(self, request_id)

    def _reset_cards(self) -> None:
        reset_cards(self)

    def _append_chat(self, speaker: str, text: str, steps: StepBlock | None = None) -> None:
        append_chat(self, speaker, text, steps)

    def _add_step(self, block: StepBlock, activity: Activity) -> None:
        add_step(self, block, activity)

    def _request_exit(self) -> None:
        # Kaydedilmemiş düzenleme varsa kullanıcıya sorulur; Vazgeç veya hata çıkışı iptal eder.
        if not leave_settings(self):
            return
        self._exiting = True
        if self._approval_dialog is not None:
            self._approval_dialog.reject()
        self.bridge.shutdown()
        self._set_status(t("Temizleniyor…"), "running")
        self._set_controls()
        self.overlay.set_busy(True, t("Temizleniyor…"))

    def _finish_exit(self) -> None:
        finish_exit(self)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        if self._approval_dialog is not None:
            self._approval_dialog.reject()
        if self._tray is not None and not self._exiting:
            self.hide()
            event.ignore()
            return
        event.ignore()
        self._request_exit()

    def nativeEvent(self, eventType: Any, message: Any) -> object:
        if is_resume_native_event(eventType, message):
            self.bridge.notify_system_resume()
        return super().nativeEvent(eventType, message)


def build_window(
    config_path: Path, *, demo: bool = False, voice: bool = False, app: QApplication | None = None
) -> DesktopWindow:
    """GUI factory; offscreen kabul testleri aynı gerçek pencereyi kullanır."""
    current = app if app is not None else QApplication.instance()
    if isinstance(current, QApplication):
        gui_gc.install(current)  # betikler/testler run_desktop'suz de buradan geçer
    return DesktopWindow(config_path, demo=demo, voice=voice, app=app)


TRAY_RETRY_MS = 2000
TRAY_RETRY_ATTEMPTS = 15


def _instance_key(config_path: Path) -> Path:
    try:
        return load_settings(config_path).state_dir
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        # Bozuk config'te pencere zaten hatayı gösterir; anahtar yine kurulum başına tekildir.
        return config_path.resolve()


def app_icon() -> QIcon:
    """Paketle gelen çok boyutlu simge; tepsi ve tüm pencereler aynı dosyayı kullanır."""
    return QIcon(str(Path(__file__).with_name("artemis.ico")))


def run_desktop(
    config_path: Path, *, demo: bool = False, voice: bool = False, background: bool = False
) -> int:
    set_app_user_model_id()
    current = QApplication.instance()
    app = current if isinstance(current, QApplication) else QApplication([])
    gui_gc.install(app)
    server: InstanceServer | None = None
    if not demo:
        # ADR 0030: aynı durum dizininde ikinci örnek açılmaz; var olanın paneli öne gelir.
        # Pencere kurulmadan önce sahiplenilir: kurulum uzun sürer ve yarış penceresi açar.
        server = claim_instance(instance_name(_instance_key(config_path)), show=not background)
        if server is None:
            if not background:
                print(t("Artemis zaten açık; panel öne getirildi."))
            return 0
        if not server.is_listening():
            print(t("Uyarı: tek örnek koruması kurulamadı; ikinci Artemis açılırsa çakışabilir."))
    app.setApplicationName("Artemis")
    app.setWindowIcon(app_icon())
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(_STYLE)
    if not demo:
        # Ayar okunamazsa arayüz Türkçe kalır.
        with contextlib.suppress(OSError, ValueError, yaml.YAMLError):
            set_language(load_settings(config_path).language)
    maybe_connect(config_path)
    window = build_window(config_path, demo=demo, voice=voice, app=app)
    if server is not None:
        server.setParent(window)
        server.show_requested.connect(window.show_from_instance)
    window.present(background=background)
    return app.exec()
