"""PySide6 paneli; bütün model ve araç işleri Qt'siz köprüde kalır."""

from __future__ import annotations

import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError
from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QPalette,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from artemis.brain.providers import provider_problem
from artemis.channels.autostart import Autostart
from artemis.channels.desktop import gui_gc, pet_sprite, shell_assets, themes
from artemis.channels.desktop.approval_dialog import ApprovalDialog
from artemis.channels.desktop.automation_panel import AutomationPanel
from artemis.channels.desktop.bridge import (
    Activity,
    ActivityGap,
    Approval,
    BridgeEvent,
    DesktopBridge,
    Failed,
    Finished,
    Proactive,
    State,
    ToolFinished,
    VoiceCommand,
)
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
from artemis.channels.desktop.data_panel import DataPanel
from artemis.channels.desktop.hotkey import GlobalHotkey
from artemis.channels.desktop.instance import InstanceServer, claim_instance, instance_name
from artemis.channels.desktop.operations import (
    OperationLog,
)
from artemis.channels.desktop.page_kit import center_page
from artemis.channels.desktop.pet import PetWidget, level_from_message
from artemis.channels.desktop.pet_sprite import pets_dir_for
from artemis.channels.desktop.projects_panel import ProjectsPanel, status_label
from artemis.channels.desktop.selection_popup import SelectionPopup
from artemis.channels.desktop.settings_io import _same, save_choices, save_computer_mode
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.desktop.steps import step_line
from artemis.channels.desktop.telegram_service import TelegramService, should_run
from artemis.channels.desktop.text_viewer import open_text_file
from artemis.channels.desktop.widgets import PetFace
from artemis.channels.desktop.window_layout import (
    _NAV_COMPACT_WIDTH,
    _NAV_WIDTH,
    build_chat_page,
    build_sidebar,
    build_topbar,
    page_frame,
)
from artemis.channels.runtime import Runtime, create_runtime
from artemis.config.settings import load_settings, same_hotkey
from artemis.proactive.daily_summary import VAULT_TEXT_LIMIT, parse_day, summary_path
from artemis.safety.audit import AuditLog
from artemis.tools.plugins.memory import AppendArguments
from artemis.tools.plugins.routines import RoutineNameArguments
from artemis.tools.toast import ToastOutcome, show_toast

_STYLE = themes.stylesheet(themes.DEFAULT_THEME)
PROJECTS_POLL_ACTIVE_MS = 2000
PROJECTS_POLL_IDLE_MS = 15000




# Durum ve ses noktaları tema renk anahtarıyla okunur; hex değer koda gömülmez.
_STATUS_TONES = {"ready": "success", "running": "warning", "error": "error"}
_VOICE_TONES = {
    "off": "muted",
    "preparing": "warning",
    "listening": "success",
    "awaiting_command": "success",
    "thinking": "warning",
    "speaking": "success",
    "error": "error",
}
# Bu pencere genişliğinin altında adım paneli gizlenir, üst çubuk daralır.
_STEP_PANEL_MIN_WINDOW = 1100
_COMPACT_HEADER_WINDOW = 1000
_COMPACT_NAV_WINDOW = 900  # bunun altında yan gezinme yalnız ikon (UX-01)
# Adım blokları yalnız son birkaç isteğin canlı bağlantısını tutar.
_STEP_TURNS_KEPT = 8
_STEP_EVENTS_PER_TURN = 200


def _dot_style(color: str) -> str:
    """Durum noktasının rengi; metin ayrı etikette düz kalır."""
    return f"QLabel {{ color: {color}; font-size: 12px; background: transparent; }}"


class DesktopWindow(QMainWindow):
    # Kısayol kendi iş parçacığından yayar; Qt bunu ana iş parçacığına sıraya alır.
    _hotkey_pressed = Signal()
    _dictation_pressed = Signal()
    _selection_pressed = Signal()
    _toast_failed = Signal(str)
    # Alanları window_layout kurar; burada yalnız tipleri bildirilir.
    brand_pet: PetFace
    demo_chip: QLabel | None
    model_caption: QLabel
    model_value: QPushButton
    model_box: QPushButton
    voice_dot: QLabel
    voice_state: QLabel
    voice_button: QPushButton
    auto_button: QPushButton
    status_pill: QFrame
    status_dot: QLabel
    status_label: QLabel
    quick_action: QPushButton
    new_chat_button: QPushButton
    operations_button: QPushButton
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
        self,
        config_path: Path,
        *,
        demo: bool = False,
        voice: bool = False,
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
        if demo:
            self._temporary = tempfile.TemporaryDirectory(prefix="artemis-demo-")
            temp_root = Path(self._temporary.name)

            def factory(path: Path, approve: Any, on_record: Any) -> Runtime:
                from artemis.channels.desktop.demo import create_demo_parts

                settings, provider, registry, audit = create_demo_parts(temp_root, on_record)
                runtime = create_runtime(
                    path,
                    approve,
                    settings=settings,
                    provider=provider,
                    registry=registry,
                    audit=audit,
                )
                runtime.context.services["demo_records"] = {"sample": "Demo kitaplığı: örnek metin"}
                return runtime

            self.bridge = DesktopBridge(config_path, factory)
        else:
            self.bridge = DesktopBridge(config_path)
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
        self._build_pet()
        self._build_ui()
        self._build_overlay()
        self._build_tray()
        self._hotkey: GlobalHotkey | None = None
        self._dictation_hotkey: GlobalHotkey | None = None
        self._selection_hotkey: GlobalHotkey | None = None
        # Seçili metin eylemleri (ADR 0062): pencere yalnız düğme basışlarını köprüye iletir.
        self.selection_popup = SelectionPopup(
            self,
            lambda action: self.bridge.selection_run(action),
            lambda: self.bridge.selection_write_back(),
        )
        self.selection_popup.apply_theme(themes.THEMES[self._theme])
        self._selection_pressed.connect(lambda: self.bridge.selection_hotkey())
        # Köprü ayar uygulanınca yenilenir; sinyal her seferinde güncel olanı çağırır.
        self._dictation_pressed.connect(lambda: self.bridge.toggle_dictation())
        if not demo:
            self._refresh_welcome()
            self._start_hotkey()
            self._start_dictation_hotkey()
            self._start_selection_hotkey()
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
            on_toggle_voice=self.voice_button_click,
            on_show_panel=self._show_panel,
            on_quick=self._show_overlay,
            on_approval=self._focus_approval,
            on_quit=self._request_exit,
            on_submit=lambda text: self._submit_text(text),
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
                self._pet_cache[size] = (
                    pet_sprite.preview(info, size) if info is not None else None
                )
            except pet_sprite.PetError:
                self._pet_cache[size] = None
        return self._pet_cache[size]

    def _refresh_brand_pet(self) -> None:
        """Selected pet beside the immutable Artemis product name."""
        self._pet_cache.clear()
        self.brand_pet.set_pixmap(self._pet_pixmap(32))
        self.chat.refresh_pet()

    def _apply_theme(self, name: str) -> None:
        self._theme = name if name in themes.THEMES else themes.DEFAULT_THEME
        style = themes.stylesheet(self._theme)
        self.setStyleSheet(style)
        # Her yoldan gelen tema değişimi Görünüm kartlarını da eşitler; set_theme sinyal yaymaz.
        self.settings_panel.set_theme(self._theme)
        self.settings_panel.providers.set_theme(themes.THEMES[self._theme])
        self.automation_panel.set_theme(themes.THEMES[self._theme])
        palette = themes.THEMES[self._theme]
        for block in self.findChildren(StepBlock):
            block.apply_theme(palette)
        self.chat.apply_theme(palette)
        self.brand_pet.set_palette_tokens(palette)
        for request_id in list(self._op_widgets):
            self._render_card(request_id)
        if hasattr(self, "overlay"):
            self.overlay.setStyleSheet(style)
            self._theme_placeholders(self.overlay)
        # Masaüstü pet ve yanındaki hızlı kutu ayrı pencereler; aynı jetonlarla boyanır
        # (pet.apply_theme hızlı kutuyu da günceller; sabit lacivert yok).
        self.pet.apply_theme(self._theme)
        if hasattr(self, "selection_popup"):
            self.selection_popup.apply_theme(palette)
        self._theme_placeholders(self)
        self._refresh_tones()
        self._refresh_icons()
        dark = self._theme.startswith("dark")
        self._theme_toggle_text = "Açık tema" if dark else "Koyu tema"
        compact = self.sidebar.width() == _NAV_COMPACT_WIDTH
        self.theme_toggle.setText("" if compact else self._theme_toggle_text)
        self.theme_toggle.setAccessibleName("Açık temaya geç" if dark else "Koyu temaya geç")
        if not self._demo:
            self._appearance.setValue("theme", self._theme)

    def _token(self, key: str) -> str:
        return themes.THEMES[self._theme][key]

    def _refresh_tones(self) -> None:
        """Durum ve ses noktaları tema değişince yeni renk anahtarıyla yeniden boyanır."""
        self.status_dot.setStyleSheet(_dot_style(self._token(_STATUS_TONES[self._status_tone])))
        tone = _VOICE_TONES.get(self._voice_state, _VOICE_TONES["off"])
        self.voice_dot.setStyleSheet(_dot_style(self._token(tone)))

    def _refresh_icons(self) -> None:
        """İkonlar temanın rengini çalışma anında alır; menü ve çalışma alanı ayrı olabilir."""
        nav_color = self._token("nav_text")
        for button, name in self._nav_icon_names.items():
            button.setIcon(shell_assets.icon(name, nav_color))
        dark = self._theme.startswith("dark")
        self.theme_toggle.setIcon(shell_assets.icon("sun" if dark else "moon", nav_color, 16))
        muted, accent = self._token("muted"), self._token("accent")
        for button, name in (
            (self.voice_button, "mic"),
            (self.composer_voice, "mic"),
            (self.auto_button, "shield"),
        ):
            button.setIcon(shell_assets.icon(name, muted, 18, accent))

    def _theme_placeholders(self, root: QWidget) -> None:
        color = QColor(themes.THEMES[self._theme]["muted"])
        for widget in [*root.findChildren(QLineEdit), *root.findChildren(QPlainTextEdit)]:
            palette = widget.palette()
            palette.setColor(QPalette.ColorRole.PlaceholderText, color)
            widget.setPalette(palette)

    def _start_hotkey(self) -> None:
        try:
            text = load_settings(self._config_path).voice.listening_hotkey.strip()
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            return
        if not text:
            return
        try:
            hotkey = GlobalHotkey(text, self._hotkey_pressed.emit)
        except ValueError:
            self._hotkey_problem(f"Ses kısayolu tanınmadı: {text}")
            return
        self._hotkey_pressed.connect(self.voice_button_click)
        if hotkey.start():
            self._hotkey = hotkey
        elif sys.platform == "win32":
            self._hotkey_problem(f"Ses kısayolu ({text}) başka bir programda kayıtlı.")

    def _start_dictation_hotkey(self) -> None:
        """Dikte kısayolu (ADR 0057): kapalıyken hiç kaydedilmez; ayar uygulanınca yenilenir."""
        if self._dictation_hotkey is not None:
            self._dictation_hotkey.stop()
            self._dictation_hotkey = None
        try:
            config = load_settings(self._config_path).dictation
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            return
        if not config.enabled:
            return
        text = config.hotkey.strip()
        try:
            hotkey = GlobalHotkey(text, self._dictation_pressed.emit)
        except ValueError:
            self._hotkey_problem(f"Dikte kısayolu tanınmadı: {text}")
            return
        if hotkey.start():
            self._dictation_hotkey = hotkey
        elif sys.platform == "win32":
            self._hotkey_problem(f"Dikte kısayolu ({text}) başka bir programda kayıtlı.")

    def _start_selection_hotkey(self) -> None:
        """Seçili metin kısayolu (ADR 0062): kapalıyken kaydedilmez; ayar uygulanınca yenilenir."""
        if self._selection_hotkey is not None:
            self._selection_hotkey.stop()
            self._selection_hotkey = None
        try:
            config = load_settings(self._config_path).selection_actions
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            return
        if not config.enabled:
            return
        text = config.hotkey.strip()
        try:
            hotkey = GlobalHotkey(text, self._selection_pressed.emit)
        except ValueError:
            self._hotkey_problem(f"Seçili metin kısayolu tanınmadı: {text}")
            return
        if hotkey.start():
            self._selection_hotkey = hotkey
        elif sys.platform == "win32":
            self._hotkey_problem(f"Seçili metin kısayolu ({text}) başka bir programda kayıtlı.")

    def _show_selection(self, kind: str, message: str) -> None:
        """Seçili metin eylemi durumu: pencere imleç yanında açılır, sonuç orada görünür."""
        if kind == "ready":
            self.selection_popup.show_ready(message)
        elif kind == "working":
            self.selection_popup.show_working()
        elif kind == "result":
            self.selection_popup.show_result(message)
        elif kind == "message" and message:
            self.selection_popup.show_message(message)
            self._set_status(message, "ready")

    def _show_dictation(self, kind: str, message: str) -> None:
        """Dikte durumu (ADR 0057): kayıt sürerken görünür gösterge, bitince normal duruma dönüş."""
        if kind in {"listening", "working"}:
            line = "Dikte dinliyor…" if kind == "listening" else "Dikte yazıya çevriliyor…"
            self._set_status(line, "running")
            self.overlay.set_busy(True, line)
            self.pet.set_voice("listening" if kind == "listening" else "thinking")
        elif kind == "idle":
            if self._busy:
                self._set_status("Çalışıyor…", "running")
            else:
                self._set_status("Hazır", "ready")
            self.overlay.set_busy(self._busy)
            self.pet.set_voice(self._voice_state)
        elif kind == "message" and message:
            self._set_status(message, "ready")
        elif kind == "text":
            # Yazılamayan metin kaybolmasın; pano değil panel (konuşma geçmişine girmez).
            self._append_chat("Artemis", f"Dikte metni (yazılmadı ya da yarım kaldı): {message}")

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
        self.tabs.addTab(self.chat_page, "Sohbet")
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
            self._config_path,
            demo=self._demo,
            state=QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                            "Artemis", "appearance"),
        )
        self.settings_panel.apply_requested.connect(self._apply_settings)
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
        self.settings_scroll = self.settings_panel
        # Ctrl+, her yerden Ayarlar'ı açar (pencere kapsamlı).
        self._settings_shortcut = QShortcut(QKeySequence("Ctrl+,"), self)
        self._settings_shortcut.activated.connect(self._show_settings)
        self.tabs.addTab(self.settings_scroll, "Ayarlar")
        self.projects_page = page_frame(self.projects_panel)
        self.tabs.addTab(self.projects_page, "Projeler")
        self.data_panel = DataPanel(demo=self._demo)
        self.data_panel.tool_requested.connect(self._submit_project_tool)
        self.data_page = page_frame(self.data_panel)
        self.tabs.addTab(self.data_page, "Geçmiş")
        self.automation_panel = AutomationPanel(demo=self._demo, config_path=self._config_path)
        self.automation_panel.tool_requested.connect(self._submit_automation_tool)
        self.automation_panel.settings_requested.connect(self._open_settings_section)
        self.automation_page = page_frame(self.automation_panel)
        self.tabs.addTab(self.automation_page, "Otomasyon")
        # Pet atölyesi günlük ayarlardan ayrı sayfadır; PetSection kendi başlığını taşır
        # ama kaydırıcısı yoktur.
        self.pet_scroll = QScrollArea()
        self.pet_scroll.setWidgetResizable(True)
        self.pet_scroll.setFrameShape(QFrame.Shape.NoFrame)
        center_page(self.pet_scroll)
        self.pet_scroll.setWidget(self.settings_panel.pet)
        self.pet_page = page_frame(self.pet_scroll)
        self.tabs.addTab(self.pet_page, "Petler")
        self.tabs.tabBar().hide()
        sidebar = build_sidebar(self, [
            ("Sohbet", self.chat_page), ("Projeler", self.projects_page),
            ("Otomasyon", self.automation_page), ("Petler", self.pet_page),
            ("Geçmiş", self.data_page),
            ("Ayarlar", self.settings_scroll),
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
        self._describe_voice(_VOICE_LABELS["off"], False)
        self._set_controls()
        self._fit_shell()
        QTimer.singleShot(0, self.prompt.setFocus)
        # Açılışta bir kez, arka plan thread'inde; CLI'da eşitleme yoktur.
        QTimer.singleShot(0, self.settings_panel.start_auto_sync)
        if not self._demo:
            self._check_provider(open_settings=True)

    def _check_provider(self, *, open_settings: bool) -> None:
        """Birincil model çağrılamayacaksa ilk tur patlamadan Ayarlar'ı sağlayıcı bölümünde açar."""
        try:
            reason = provider_problem(load_settings(self._config_path))
        except (OSError, ValueError):
            return
        self.settings_panel.providers.show_problem(reason)
        if reason and open_settings:
            self.tabs.setCurrentWidget(self.settings_scroll)
            # Sağlayıcı eksikse Ayarlar doğrudan Model bölümünde açılır.
            names = self.settings_panel.section_names()
            self.settings_panel.show_section(names.index("Model") if "Model" in names else 1)
            QTimer.singleShot(
                0,
                lambda: self.settings_panel.sections.scroll_area.ensureWidgetVisible(
                    self.settings_panel.providers
                ),
            )

    def _show_settings(self) -> None:
        self.tabs.setCurrentWidget(self.settings_scroll)

    def _open_settings_section(self, name: str) -> None:
        names = self.settings_panel.section_names()
        if name in names:
            self.settings_panel.show_section(names.index(name))
        self.tabs.setCurrentWidget(self.settings_scroll)

    def _sync_navigation(self, *_args: object) -> None:
        for page, button in self.nav_buttons.items():
            button.setChecked(page is self.tabs.currentWidget())

    def _settings_tab_changed(self, *_args: object) -> None:
        """Ayarlar'dan çıkış: bekleyen düzenleme yazılır, sonra tek yeniden kurulum. Girişte
        "geri al" taban çizgisi o anki kayıtlı dosyadan yenilenir."""
        is_open = self.tabs.currentWidget() is self.settings_scroll
        if is_open == self._settings_open:
            return
        self._settings_open = is_open
        if is_open:
            self.settings_panel.begin_session()
            return
        self.settings_panel.flush()
        if self._settings_restart_pending:
            self._request_restart(self.settings_panel.notice)

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
        if self.settings_panel.pet.creator is not None:
            self.settings_panel.pet.creator.reload_models()
        self._check_provider(open_settings=False)
        if self._exiting or self._demo:
            self.settings_panel.providers.status.setText(
                "Kaydedildi; işlem bitince yeni ayarlar devreye girer."
            )
            return
        self._request_restart(self.settings_panel.providers.status)

    def _telegram_changed(self) -> None:
        """chat_id/token değişti: proaktif Telegram teslimi yeniden kurulumla yeni durumu görür."""
        status = self.settings_panel.telegram.message
        if self._exiting or self._demo:
            self._sync_telegram_service()
            status.setText("Kaydedildi; işlem bitince uygulama yeniden başlayınca geçerli olur.")
            return
        # Kanal servisi çalışma zamanından bağımsızdır: yeni sohbet/token hemen eşitlenir.
        # Yeniden kurulum hemen yapıldıysa o zaten eşitlemiştir (çift yeniden başlatma olmasın);
        # ertelendiyse (meşgul) ya da sürüyorsa burada eşitlenir.
        self._telegram_resynced = False
        self._request_restart(status)
        if not self._telegram_resynced:
            self._sync_telegram_service()

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
        """Projeler'den yazılan ajan/klasör ayarı çalışma zamanına yeniden kurulumla ulaşır.

        Takma adları çalışma zamanının ayarı çözer; yeniden kurulmazsa yeni klasör taslakta
        "bilinmeyen klasör" olur. Meşgulken sahte bir uygulama iddiası yerine dürüst not düşülür.
        """
        if self._exiting or self._demo:
            self.projects_panel.notice.setText(
                "Kaydedildi; işlem bitince uygulama yeniden başlayınca geçerli olur."
            )
            return
        self._request_restart()

    def _request_restart(self, notice: QLabel | None = None) -> None:
        """Config değişince çalışma zamanını kurmanın TEK girişi (Uygula, sağlayıcı, kod ajanı).

        Yeniden kurulum çalışan turu/arka plan işini sessizce iptal eder ve bekleyen onayı
        geçersiz kılar; bu yüzden hepsi boşta olana dek ertelenir (bayrak tek seferliktir).
        Config zaten yazılmıştır: ertelenen yalnız canlı kuruluştur, ayar kaybolmaz.
        """
        # Bu istek o anki config'le kurar; sekmeden çıkış aynı içerik için ikinci kez kurmaz.
        self._runtime_dump = self.settings_panel.config_dump()
        if self._restart_settings:
            # Sürmekte olan kuruluş config'i durma bittikten sonra okur; ikincisi gereksiz.
            return
        self._restart_notice = notice
        self._config_restart_pending = True
        self._config_restart_serial = self.projects_panel.status_serial
        self._maybe_restart_for_config()

    def _config_restart_blocker(self) -> str:
        """Yeniden kurulumu engelleyen neden ('' = boşta)."""
        if self._approval_dialog is not None:
            return "Kaydedildi; bekleyen onay bitince uygulama yeniden başlayınca geçerli olur."
        # Panel listesi bayat olabilir (sekme açık değilken yoklanmaz); sohbetten başlayan iş
        # için doğrudan kaynak olan işçi tarafı sayımına da bakılır.
        if self.projects_panel.active_job_count() or self.bridge.active_job_count():
            return "Kaydedildi; çalışan iş bitince uygulama yeniden başlayınca geçerli olur."
        if self._busy:
            return "Kaydedildi; işlem bitince uygulama yeniden başlayınca geçerli olur."
        return ""

    def _maybe_restart_for_config(self) -> None:
        if not self._config_restart_pending or self._exiting:
            return
        reason = self._config_restart_blocker()
        if reason:
            (self._restart_notice or self.projects_panel.notice).setText(reason)
            return
        self._config_restart_pending = False
        self._restart_runtime()

    def _github_synced(self, report: Any, auto: bool) -> None:
        if not (auto and report.cloned):
            return
        text = (
            f"{len(report.cloned)} yeni GitHub reposu çekildi; "
            "Ayarlar'dan çıkınca kullanıma girer."
        )
        self.pet.say(text, "job")
        if self._tray is not None:
            self._tray.showMessage("Artemis", text, QSystemTrayIcon.MessageIcon.Information, 6000)

    def _build_overlay(self) -> None:
        self.overlay = QuickOverlay(self._submit_overlay, self._cancel_active, self._show_panel)
        self.overlay.setStyleSheet(themes.stylesheet(self._theme))
        self._theme_placeholders(self.overlay)

    def _apply_settings(self, device: int | str | None) -> None:
        """Otomatik kayıt: doğrular ve yazar, yeniden kurmaz (sekmeden çıkınca kurulur)."""
        panel = self.settings_panel
        if self._demo or self._exiting:
            panel.save_failed("Ayarları değiştirmek için işlemin bitmesini bekle.")
            return
        values = panel.values()
        try:
            voice_key = load_settings(self._config_path).voice.listening_hotkey
            dictation_key = str(values[("dictation", "hotkey")])
            selection_key = str(values[("selection_actions", "hotkey")])
            clash = same_hotkey(dictation_key, voice_key)
            selection_clash = same_hotkey(selection_key, voice_key) or same_hotkey(
                selection_key, dictation_key
            )
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            clash = selection_clash = False  # okunamayan config aşağıdaki kayıtta yakalanır
        if clash:
            panel.save_failed("Dikte kısayolu ses kısayoluyla aynı olamaz; başka bir tuş seç.")
            return
        if selection_clash:
            panel.save_failed("Seçili metin kısayolu ses ve dikte kısayolundan farklı olmalı.")
            return
        try:
            written = save_choices(
                self._config_path,
                device,
                values=values,
                applications=panel.applications,
                code_agent=panel.code_agent_values(),
                stt_model=panel.stt_model_path(),
            )
        except (OSError, ValueError, KeyError, TypeError):
            panel.save_failed("Ayarlar kaydedilemedi; mevcut ayarlar korunuyor.")
            return
        panel.mark_saved(written)

    def _restart_runtime(self) -> None:
        """Config değişince çalışma zamanını yeniden kurar (Uygula ve sağlayıcı kaydı)."""
        if self.settings_panel.pet.creator is not None:
            self.settings_panel.pet.creator.reload_models()
        # Gömülü Telegram kanalı kendi runtime'ını açılışta okunan ayarla kurmuştu (örn. izinli
        # uygulamalar); yeniden kurulmazsa eski, gevşek politikayla çalışmaya devam ederdi.
        self._sync_telegram_service()
        self._telegram_resynced = True
        self._voice_on_ready = self.bridge.voice_active
        self._restart_settings = True
        self._worker_ready = False
        self._busy = True
        self.bridge.shutdown()
        self._set_status("Ayarlar uygulanıyor…", "running")
        self._set_controls()

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(app_icon(), self)
        self._tray.setToolTip("Artemis")
        menu = QMenu(self)
        open_action = menu.addAction("Paneli aç")
        quick_action = menu.addAction("Hızlı komut")
        pet_action = menu.addAction("Peti göster/gizle")
        if sys.platform == "win32" and not self._demo:
            # ADR 0030: kayıt defteri değişikliği yalnız kullanıcının bu tıklamasıyla olur.
            self._autostart_action = menu.addAction("Windows açılışında başlat")
            self._autostart_action.setCheckable(True)
            self._autostart_action.setChecked(self._autostart_state() == "on")
            self._autostart_action.toggled.connect(self._toggle_autostart)
        menu.addSeparator()
        exit_action = menu.addAction("Çıkış")
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
        autostart = self._autostart()
        return autostart.status().state if autostart is not None else "off"

    def _toggle_autostart(self, enabled: bool) -> None:
        autostart = self._autostart()
        if autostart is None:
            message, success = "Ayarlar okunamadı; açılış kaydı değiştirilmedi.", False
        else:
            result = autostart.enable() if enabled else autostart.disable()
            message, success = result.message, result.success
        if not success and self._autostart_action is not None:
            # Seçim kutusu gerçek durumu göstermeli: başarısızlıkta eski haline döner.
            self._autostart_action.blockSignals(True)
            self._autostart_action.setChecked(not enabled)
            self._autostart_action.blockSignals(False)
        if self._tray is not None:
            self._tray.showMessage(
                "Artemis", message, QSystemTrayIcon.MessageIcon.Information, 6000
            )

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
        self.tabs.setCurrentWidget(self.settings_scroll)
        # Model, bölüm listesinin ikinci satırıdır (Ses, Model, Uygulamalar, GitHub, Görünüm).
        self.settings_panel.sections.setCurrentRow(1)

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
        self.model_box.setVisible(width >= 800)
        self.model_caption.setVisible(width >= 800)
        self._show_model(self._model_full)
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
        limit = 120 if self.width() < _COMPACT_HEADER_WINDOW else 240
        metrics = QFontMetrics(self.model_value.font())
        shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, limit)
        self.model_value.setText(shown)
        self.model_value.setToolTip(
            f"Kullanılan beyin modeli: {text}. Değiştirmek için Ayarlar'ı aç."
            if text else "Kullanılan beyin modeli"
        )
        # Çipin 8px yan dolgusu iki yanda 16px ekler; 8px pay ölçü sapmasına karşıdır.
        self.model_value.setFixedWidth(max(60, metrics.horizontalAdvance(shown) + 24))

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
            self._set_status("Hazır değil veya önceki işlem temizleniyor.", "running")
            return False
        self._active_request = request_id
        self._busy = True
        self._begin_card(request_id, text)
        self._append_chat("Siz", text)
        self._set_status("Çalışıyor…", "running")
        self._set_controls()
        self.overlay.set_busy(True)
        return True

    def _show_routine_notice(
        self, name: str, text: str, suggest: str = "", summary_day: str = ""
    ) -> None:
        """Ana düğme: rutin "Çalıştır", tetikleyici önerisi "Yaz" (ADR 0061), gün özeti "Aç";
        özet için vault açıksa ikinci düğme "Vault'a ekle" (ADR 0066)."""
        self._routine_name = name
        self._routine_suggest = suggest
        self._summary_day = summary_day
        self.routine_run_button.setText(
            "Aç" if summary_day else "Yaz" if suggest else "Çalıştır"
        )
        self.summary_vault_button.setVisible(bool(summary_day) and self._vault_note() is not None)
        self.routine_label.setText(text)
        self.routine_bar.show()

    def _vault_note(self) -> str | None:
        """Vault kapalıysa ya da config okunamazsa None, değilse eklenecek not adı."""
        try:
            settings = load_settings(self._config_path)
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            return None
        if settings.memory.vault_path is None:
            return None
        return settings.daily_summary.vault_note

    def _summary_file(self) -> Path | None:
        day = parse_day(self._summary_day)
        if day is None:
            return None
        try:
            return summary_path(load_settings(self._config_path).state_dir, day)
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            return None

    def _append_summary_to_vault(self) -> None:
        """Normal, onaylı `vault_append` turu: onay penceresi eklenecek metni gösterir."""
        path, note = self._summary_file(), self._vault_note()
        self.routine_bar.hide()
        if path is None or note is None:
            return
        try:
            text = path.read_text(encoding="utf-8")[:VAULT_TEXT_LIMIT].strip()
        except (OSError, UnicodeError):
            self._set_status("Özet dosyası okunamadı.", "running")
            return
        if text:
            self._submit_routine_tool("vault_append", AppendArguments(path=note, text=text))

    def _run_routine_notice(self) -> None:
        self.routine_bar.hide()
        if self._summary_day:
            path = self._summary_file()
            if path is not None and path.is_file():
                open_text_file(path, "Gün özeti", self)
        elif self._routine_suggest:
            # Gönderilmez: metin yalnız sohbet kutusuna yazılır, kullanıcı okuyup kendisi yollar.
            self.prompt.setPlainText(self._routine_suggest)
            self.prompt.setFocus()
        elif self._routine_name:
            self._submit_routine_tool("routine_run", RoutineNameArguments(name=self._routine_name))

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
            notice.setText("Başka işlem sürüyor; çalıştırılmadı.")
            self._set_status("Başka işlem sürüyor; rutin çalıştırılmadı.", "running")
            return
        self._active_request = request_id
        self._busy = True
        self._begin_card(request_id, self._routine_tool_title(name, arguments))
        self._set_controls()

    def _routine_tool_title(self, name: str, arguments: BaseModel) -> str:
        """İşlem kartı başlığı: ham araç adı yerine insan okur ad (kapı yolu değişmez)."""
        if name == "vault_append":
            return "Gün özetini vault'a ekle"
        if name == "research_cancel":
            return "Araştırmayı durdur"
        if name == "routine_run":
            label = self.bridge.labels().get(name, ("Rutin çalıştır", ""))[0]
            return f"{label}: {getattr(arguments, 'name', '')}".rstrip(": ")
        return f"Rutin işlemi: {name}"

    def _submit_project_tool(self, name: str, arguments: BaseModel) -> None:
        # Durum yoklaması salt okunur ve 2 sn'de bir gelir: kart, meşgul durumu ve pet
        # tepkisi üretmez (aksi halde "Taslak hazırla"ya basılmış gibi titrer).
        quiet = name == "code_agent_status"
        request_id = self.bridge.submit_tool(name, arguments, quiet=quiet)
        if request_id is None:
            if not quiet:
                panel = self.projects_panel if name.startswith("code_agent_") else self.data_panel
                panel.notice.setText("Başka işlem sürüyor; istek çalıştırılmadı.")
            return
        self._active_request = request_id
        if quiet:
            return
        self._busy = True
        self._begin_card(request_id, f"Proje işlemi: {name}")
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
        if (
            self.tabs.currentWidget() is self.projects_page
            and self._worker_ready
            and not self._busy
        ):
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

    def _toggle_auto_mode(self, checked: bool) -> None:
        """Üst çubuktaki düğme: kip anında değişir ve config'e yazılır.

        Kip oturum içi onayı belirler; tarayıcıdan sonra "Uygula" beklemek istenmeyen
        sürpriz olurdu, bu yüzden düğme hem runtime'a hem config'e anında yazar.
        """
        mode = "auto" if checked else "accept"
        saved = True
        if checked and not self._demo and not self._confirm_auto_mode():
            # Vazgeçilirse iki denetim de kapalı kalır; kip hiç değişmez.
            for control in (self.settings_panel.computer_auto, self.auto_button):
                control.blockSignals(True)
                control.setChecked(False)
                control.blockSignals(False)
            self._show_auto_state(False)
            return
        if not self._demo:
            # Önce config: yazılamayan kip runtime'a uygulanırsa güvenlik ayarı sessizce
            # diskten ayrışırdı (yeniden açılışta eski kip döner).
            try:
                save_computer_mode(self._config_path, mode)
            except (OSError, ValueError):
                saved = False
                self._set_status("Kip kaydedilemedi", "error")
            else:
                if not self.bridge.set_computer_mode(mode):
                    # Runtime hazır değilse yalnız config yazılmıştır; açılışta geçerli olur.
                    self._set_status("Kip kaydedilecek", "running")
        shown = checked if saved else not checked
        # İki denetim de sinyalsiz eşitlenir: düğme ve ayar kutusu aynı kipi göstermeli.
        for control in (self.settings_panel.computer_auto, self.auto_button):
            control.blockSignals(True)
            control.setChecked(shown)
            control.blockSignals(False)
        self._show_auto_state(shown)
        if saved and not self._demo:
            self._append_chat("Sistem", f"Bilgisayar kullanımı kipi: {mode}.")

    def _auto_mode_box(self) -> tuple[QMessageBox, QPushButton]:
        """Uyarı kutusu ve "aç" düğmesi; ayrı kurulur ki README görüntüsü bloklamadan alınsın."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Otomatik onayı aç?")
        box.setText(
            "Otomatik onayda Artemis ekranı okur, tıklar ve yazar; hassas adımları da "
            "sana sormadan yapar. Esc her an durdurur."
        )
        box.setInformativeText(
            "Yanlış anlaşılan bir komut ya da ekrandaki zararlı bir metin, istemediğin bir "
            "işlemi tetikleyebilir. Terminal, Artemis ve parola yöneticisi yine engelli."
        )
        yes = box.addButton("Riski anlıyorum, aç", QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton("Vazgeç", QMessageBox.ButtonRole.RejectRole)
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
        self.auto_button.setAccessibleDescription(f"Oto onay {'açık' if on else 'kapalı'}")
        mode = "otomatik" if on else "her işlemde sor"
        self.auto_button.setToolTip(
            f"Onay: {mode}. Oto onay açıkken bilgisayar kullanımı adımları sormadan çalışır; "
            "Esc yine durdurur. Değişiklik hemen geçerli, Uygula gerekmez."
        )

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
        self.status_label.setText(text)
        self._status_tone = tone
        self.status_dot.setStyleSheet(_dot_style(self._token(_STATUS_TONES[tone])))
        self.status_pill.adjustSize()

    def _show_voice_state(self, state: str) -> None:
        self._voice_state = state
        if state == "off":
            self._continuous = False
        text = _VOICE_LABELS.get(state, _VOICE_LABELS["off"])
        if self._continuous:
            text += " · Kesintisiz"
        self.voice_state.setText(text)
        self.overlay.voice_state.setText(text)
        on = state in {"preparing", "listening", "awaiting_command", "thinking", "speaking"}
        self._describe_voice(text, on)
        self._refresh_tones()
        self.pet.set_voice(state)
        if state == "off":
            self.settings_panel.input_level.setValue(0)

    def _describe_voice(self, text: str, on: bool) -> None:
        """Üst çubuk ve yazma alanındaki ses anahtarı aynı durumu ve açıklamayı taşır."""
        if self._demo:
            tip = "Demo modunda ses yok"
        elif on:
            tip = f"{text} · kapatmak için tıkla"
        else:
            tip = f"{text} · açmak için tıkla; mikrofonu yalnız sen açarsın"
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
            status = "çalışıyor" if self._busy else "hazır"
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
                self._append_chat("Sistem", "Ayarlar uygulandı; yeni sohbet başladı.")
                self._show_voice_state("off")
                self.pet.set_busy(False)
                self.bridge = DesktopBridge(self._config_path)
                if not self._demo:
                    self._refresh_welcome()
                    self._start_dictation_hotkey()
                    self._start_selection_hotkey()
                self.settings_panel.refresh()
                self.settings_panel.notice.setText(
                    "Ayarlar uygulandı; seçilen model ve mikrofon kullanılıyor."
                )
            else:
                self._finish_exit()

    def _handle_event(self, event: BridgeEvent) -> None:
        if isinstance(event, State):
            if event.request_id is not None and event.request_id != self._active_request:
                return
            if event.state == "ready":
                if (
                    event.request_id is None
                    and not self._busy
                    and event.message.startswith("Hazır · ")
                ):
                    self._show_model(event.message.removeprefix("Hazır · "))
                if event.request_id is not None or not self._busy:
                    self._worker_ready = True
                    self._set_status("Hazır", "ready")
                    self._busy = False
                    self._active_request = None
                    self._set_controls()
                    self.overlay.set_busy(False, "Hazır")
                if self._voice_on_ready and self._worker_ready:
                    self._voice_on_ready = False
                    self.bridge.start_voice()
                if event.request_id is not None:
                    self._dismiss_approval_for(event.request_id)
            elif event.state == "running":
                self._busy = True
                self._set_status("Çalışıyor…", "running")
                self._set_controls()
                self.overlay.set_busy(True)
            elif event.state == "computer":
                # Bilgisayar kullanımı oturumu (ADR 0039): ne yaptığı ve nasıl durdurulacağı
                # hep görünür; boş metin oturum bitti demektir, normal duruma dönülür.
                if event.message:
                    line = f"Bilgisayarı kullanıyorum · {event.message} · durdurmak için Esc"
                    self._set_status(line, "running")
                    self.overlay.set_busy(True, line)
                elif self._busy:
                    self._set_status("Çalışıyor…", "running")
                    self.overlay.set_busy(True)
                else:
                    self._set_status("Hazır", "ready")
            elif event.state.startswith("selection_"):
                self._show_selection(event.state.removeprefix("selection_"), event.message)
            elif event.state.startswith("dictation_"):
                self._show_dictation(event.state.removeprefix("dictation_"), event.message)
            elif event.state == "cleanup_pending":
                self._busy = True
                self._set_status("Önceki işlem temizleniyor…", "running")
                self.overlay.set_busy(True, "Temizleniyor…")
                self._set_controls()
            elif event.state == "voice":
                self._show_voice_state(event.message)
            elif event.state == "voice_mode":
                self._show_continuous(event.message == "on")
            elif event.state == "voice_level":
                level = level_from_message(event.message)
                if level is not None:
                    self.settings_panel.input_level.setValue(round(level * 100))
                    self.pet.set_level(level)
            elif event.state == "voice_heard":
                self.settings_panel.last_heard.setText(event.message)
                self.pet.heard(event.message)
            elif event.state == "voice_live":
                # Kullanıcı konuşurken pete bakar; ayarlar paneli yalnız nihai metni gösterir.
                # Her güncelleme balon süresini yeniler, nihai `voice_heard` üzerine yazar.
                self.pet.live(event.message)
            elif event.state == "voice_live_unavailable":
                kind, _, reason = event.message.partition(":")
                if kind == "runtime":
                    # Çalışırken düşen canlı yazı bir yapılandırma sorunu değildir.
                    self._append_chat(
                        "Artemis",
                        "Canlı yazı çalışırken hata verdi ve bu oturumda kapatıldı; "
                        "ses canlı yazısız sürüyor.",
                    )
                else:
                    fix = (
                        'Paketleri pip install -e ".[stream]" ile kurun.'
                        if kind == "deps"
                        else "Ayarlar > Ses bölümündeki canlı yazı modelini kontrol et."
                    )
                    self._append_chat(
                        "Artemis",
                        f"Canlı yazı açılamadı: {reason}. {fix} Ses canlı yazısız sürüyor.",
                    )
            elif event.state == "voice_denoise":
                if event.message.startswith("unavailable:"):
                    kind, _, reason = event.message.removeprefix("unavailable:").partition(":")
                    fix = (
                        'Paketleri pip install -e ".[voice]" ile kurun.'
                        if kind == "deps"
                        else "Artemis'i yeniden kurun ya da Ayarlar > Ses bölümündeki "
                        "ses temizleme ayarlarını kontrol et."
                    )
                    self._append_chat(
                        "Artemis",
                        f"Gürültü engelleme açılamadı: {reason}. {fix} Ses işlenmeden sürüyor.",
                    )
                elif event.message == "off":
                    self._append_chat(
                        "Artemis",
                        "Gürültü engelleme hata verdi ve bu oturumda kapatıldı; "
                        "ses işlenmeden devam ediyor.",
                    )
            elif event.state == "voice_echo":
                if event.message == "off":
                    self._append_chat(
                        "Artemis",
                        "Yankı engelleme hata verdi ve bu oturumda kapatıldı; "
                        "ses yankı gidermeden sürüyor.",
                    )
            elif event.state == "voice_error":
                self._append_chat("Artemis", event.message)
                self.pet.flash_error()
                self._show_voice_state("off")
            elif event.state == "context_reset":
                # Adım blokları eski balonlarla birlikte silinir; kayıt da sıfırlanır.
                # Karşılama geri gelir; işlem kartları da sıfırlanır (denetim günlüğü değişmez).
                self._clear_chat_view()
            elif event.state == "cancelling":
                self._set_status("İptal ediliyor…", "running")
            elif event.state == "stopping":
                self._set_status("Temizleniyor…", "running")
            elif event.state == "stopped":
                self._finish_exit()
            elif event.state == "cancelled":
                self._set_status("İptal edildi", "ready")
            return
        if isinstance(event, VoiceCommand):
            self._active_request = event.request_id
            self._busy = True
            self._begin_card(event.request_id, event.text)
            self._append_chat("Siz (ses)", event.text)
            self._set_status("Çalışıyor…", "running")
            self._set_controls()
            self.overlay.set_busy(True)
            return
        if isinstance(event, Approval):
            self._show_approval(event)
            return
        if isinstance(event, ToolFinished):
            if event.request_id == self._active_request:
                self._dismiss_approval_for(event.request_id)
                if event.tool_name.startswith("routine_"):
                    self.settings_panel.routines.accept_result(event.tool_name, event.result)
                    self._append_chat("Artemis", event.result.message)
                    return
                if event.tool_name == "research_cancel":
                    self.automation_panel.accept_result(event.tool_name, event.result)
                    self._append_chat("Artemis", event.result.message)
                    return
                panel = (self.projects_panel if event.tool_name.startswith("code_agent_")
                         else self.data_panel)
                panel.accept_result(event.tool_name, event.result)
                # Silinen geçmişin balonları ekranda kalırsa kullanıcı silmenin başarısız
                # olduğunu sanır; "Yeni sohbet" ile aynı temizlik yapılır.
                if (
                    event.tool_name == "conversation_history"
                    and event.result.success
                    and event.result.data.get("action") == "delete"
                ):
                    self._clear_chat_view()
            return
        if isinstance(event, Finished):
            if event.request_id == self._active_request:
                self._dismiss_approval_for(event.request_id)
                self._append_chat(
                    "Artemis", event.result.text, self._step_block_for(event.request_id)
                )
                self.pet.answer(event.result.text)
                self.overlay.status.setText("Yanıt hazır")
            return
        if isinstance(event, Failed):
            if not event.request_id:
                self._set_status(f"Başlatılamadı · {event.error_type}", "error")
                self._append_chat("Artemis", event.message)
            elif event.request_id == self._active_request:
                self._dismiss_approval_for(event.request_id)
                self._ops.fail(event.request_id)
                self._render_card(event.request_id)
                self._append_chat(
                    "Artemis",
                    f"İstek tamamlanamadı ({event.error_type}).",
                    self._step_block_for(event.request_id),
                )
                self.pet.flash_error()
            return
        if isinstance(event, Proactive):
            # Kendiliğinden mesaj ayrı konuşmacıyla görünür; modelin geçmişine eklenmez.
            self._append_chat("Artemis (kendiliğinden)", f"{event.title}\n{event.body}")
            self.pet.answer(event.body)
            if event.routine or event.suggest or event.summary_day:
                self._show_routine_notice(
                    event.routine, event.body, event.suggest, event.summary_day
                )
            return
        if isinstance(event, ActivityGap):
            self._ops.dropped += event.dropped_count
            self.activity_notice.setText(
                f"{self._ops.dropped} eski etkinlik görünümden düştü; kartlar eksik olabilir."
            )
            self.activity_notice.show()
            return
        if isinstance(event, Activity):
            self._pet_activity(event)
            self._feed_card(event)
            self._collect_step(event)

    def _clear_chat_view(self) -> None:
        self._turn_steps.clear()
        self._reset_cards()
        self.chat.clear()
        if not self._busy:
            self._set_status("Hazır", "ready")
        self._set_controls()

    def _pet_activity(self, event: Activity) -> None:
        self.pet.code_agent_event(event.event, event.fields)
        if event.event in {"code_agent_started", "code_agent_waiting", "code_agent_done"}:
            # Liste yoklaması etkin işte 2 sn, değilse 15 sn; durum değişince hemen hızlanır.
            self._projects_timer.setInterval(PROJECTS_POLL_ACTIVE_MS)
            self._projects_timer.start()
        if event.event in {"code_agent_waiting", "code_agent_done"}:
            self._notify_job(event.fields)

    def _notify_job(self, fields: dict[str, Any]) -> None:
        """Sekme açık olmasa da görünür bildirim: sohbet satırı + Windows toast.

        Tepsi balonu Windows bildirimleri kapalıyken sonuçsuz kayboluyordu; toast sonucu verir.

        Aynı iş/durum çifti bir kez bildirilir (kilit bırakma ve zaman aşımı yolları da
        `code_agent_done` yazar). Kullanıcının kendi iptali bildirim gerektirmez.
        """
        status = str(fields.get("status", ""))
        key = (str(fields.get("job_id", "")), status, str(fields.get("question_id") or ""))
        if status == "cancelled" or key in self._notified_jobs:
            return
        self._notified_jobs.add(key)
        project = str(fields.get("project") or "kod ajanı")
        summary = str(fields.get("summary") or "").strip()
        text = f"Kod işi · {project}: {status_label(fields)}" + (f". {summary}" if summary else "")
        # Makbuz ipucu yalnız sohbet satırında; toast metni değişmez.
        hint = " Makbuz için: “bu işin makbuzunu yaz”." if status == "done" else ""
        self._append_chat("Artemis", text + hint)
        toast = self._toast
        if toast is None:
            return

        def deliver() -> None:
            # powershell çalıştırır (15 sn'ye dek): arayüz iş parçacığını dondurmamak için ayrı
            # iş parçacığı; sonuç sinyalle ana iş parçacığına döner.
            outcome = toast("Artemis", text[:240])
            if outcome.status != "ok":
                self._toast_failed.emit(outcome.message)

        threading.Thread(target=deliver, name="job-toast", daemon=True).start()

    def _show_toast_failure(self, message: str) -> None:
        # Sohbet satırı zaten gösterildi; bildirimin neden görünmediği dürüstçe eklenir.
        self._append_chat("Sistem", f"(Windows bildirimi gösterilmedi: {message})")

    def _show_approval(self, item: Approval) -> None:
        if item.request_id != self._active_request:
            self.bridge.decide(item.request_id, item.turn_id, item.approval_id, False)
            return
        if self._approval_dialog is not None:
            self._approval_dialog.reject()
        self.overlay.hide()
        self._show_panel()
        dialog = ApprovalDialog(item, self._decide_approval, self)
        self._approval_dialog = dialog
        dialog.finished.connect(lambda _result, current=dialog: self._clear_approval(current))
        dialog.open()
        self.pet.set_approval(True)

    def _decide_approval(
        self, item: Approval, approved: bool, remember_site: bool = False
    ) -> bool:
        return self.bridge.decide(
            item.request_id, item.turn_id, item.approval_id, approved, remember_site
        )

    def _dismiss_approval_for(self, request_id: str) -> None:
        dialog = self._approval_dialog
        if dialog is not None and dialog.item.request_id == request_id:
            dialog.reject()

    def _clear_approval(self, dialog: ApprovalDialog) -> None:
        if self._approval_dialog is dialog:
            self._approval_dialog = None
            self.pet.set_approval(False)

    def _begin_card(self, request_id: str, text: str) -> None:
        self._ops.begin(request_id, text)
        self._render_card(request_id)

    def _feed_card(self, event: Activity) -> None:
        # Etiket tablosu işçi açılınca dolar; her olayda güncel kopya okunur.
        card = self._ops.feed(event.request_id, event.event, event.fields, self.bridge.labels())
        if card is not None:
            self._render_card(card.request_id)

    def _drop_card(self, widget: OperationCard) -> None:
        """Kartı hemen kaldırır. `deleteLater` tek başına yetmez: silme olay döngüsüne kalır ve
        o ana dek widget eski konumunda çizilmeye devam eder (kartın altında hayalet kopya).
        Bu yüzden önce gizlenir ve ebeveynden koparılır."""
        self.activity_cards.removeWidget(widget)
        widget.hide()
        widget.setParent(None)
        widget.deleteLater()

    def _render_card(self, request_id: str) -> None:
        """Kartı (yeniden) kurar; yeni kart listenin en üstüne, 20'yi aşan eskiler düşer."""
        card = self._ops.cards.get(request_id)
        if card is None:
            return
        if not self.activity_empty.isHidden():
            self.activity_empty.hide()
            self.activity.show()
        widget = OperationCard(card, themes.THEMES[self._theme])
        old = self._op_widgets.get(request_id)
        index = 0
        if old is not None:
            index = self.activity_cards.indexOf(old)
            self._drop_card(old)
        self._op_widgets[request_id] = widget
        self.activity_cards.insertWidget(index, widget)
        self._sync_activity_panel()
        for stale in [key for key in self._op_widgets if key not in self._ops.cards]:
            self._drop_card(self._op_widgets.pop(stale))

    def _reset_cards(self) -> None:
        """Yeni sohbette işlem kartları temizlenir; denetim günlüğüne dokunulmaz."""
        for widget in self._op_widgets.values():
            self._drop_card(widget)
        self._op_widgets.clear()
        self._ops.clear()
        self.activity_notice.hide()
        self.activity.hide()
        self.activity_empty.show()
        self._sync_activity_panel()

    def _append_chat(self, speaker: str, text: str, steps: StepBlock | None = None) -> None:
        self.chat.append(speaker, text, steps)

    def _turn_for(self, request_id: str) -> _TurnSteps:
        turn = self._turn_steps.get(request_id)
        if turn is None:
            turn = _TurnSteps()
            self._turn_steps[request_id] = turn
            while len(self._turn_steps) > _STEP_TURNS_KEPT:
                del self._turn_steps[next(iter(self._turn_steps))]
        return turn

    def _collect_step(self, event: Activity) -> None:
        if event.request_id is None:
            return
        turn = self._turn_for(event.request_id)
        if len(turn.activities) >= _STEP_EVENTS_PER_TURN:
            return
        turn.activities.append(event)
        if turn.block is not None:
            self._add_step(turn.block, event)

    def _step_block_for(self, request_id: str) -> StepBlock:
        """Yanıt balonunun üstündeki blok; bloğa henüz satır gelmediyse gizli kalır."""
        turn = self._turn_for(request_id)
        if turn.block is None:
            block = StepBlock(themes.THEMES[self._theme])
            for activity in turn.activities:
                self._add_step(block, activity)
            turn.block = block
        return turn.block

    def _add_step(self, block: StepBlock, activity: Activity) -> None:
        # Etiket tablosu işçi açılınca dolar; her satırda güncel kopya okunur.
        line = step_line(activity.event, activity.fields, self.bridge.labels())
        if line is not None:
            block.add_line(activity, line)

    def _request_exit(self) -> None:
        # Bekleyen düzenleme kaybolmasın; _exiting kayıtları reddeder, o yüzden önce.
        self.settings_panel.flush()
        self._exiting = True
        if self._approval_dialog is not None:
            self._approval_dialog.reject()
        self.bridge.shutdown()
        self._set_status("Temizleniyor…", "running")
        self._set_controls()
        self.overlay.set_busy(True, "Temizleniyor…")

    def _finish_exit(self) -> None:
        if not self._exiting:
            return
        # Kanal kendi runtime'ını kapatır; iş parçacığı çıkıştan sonra kalmasın.
        self._telegram_service.stop()
        self._timer.stop()
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None
        if self._dictation_hotkey is not None:
            self._dictation_hotkey.stop()
            self._dictation_hotkey = None
        if self._selection_hotkey is not None:
            self._selection_hotkey.stop()
            self._selection_hotkey = None
        self.selection_popup.close()
        self.settings_panel.finish_inventory()
        if self._tray is not None:
            self._tray.hide()
        self.overlay.close()
        self.pet.close()
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        self._allow_close = True
        self.close()
        self._app.quit()

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


def build_window(
    config_path: Path,
    *,
    demo: bool = False,
    voice: bool = False,
    app: QApplication | None = None,
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


def _set_app_user_model_id() -> None:
    """Görev çubuğu pythonw'ın simgesi yerine pencere simgesini göstersin diye (yalnız Windows)."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("UmutErayAltay.Artemis")
    except (AttributeError, OSError):
        return  # simge yalnız kozmetik; kimlik atanamazsa uygulama yine açılır


def run_desktop(
    config_path: Path, *, demo: bool = False, voice: bool = False, background: bool = False
) -> int:
    _set_app_user_model_id()
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
                print("Artemis zaten açık; panel öne getirildi.")
            return 0
        if not server.is_listening():
            print("Uyarı: tek örnek koruması kurulamadı; ikinci Artemis açılırsa çakışabilir.")
    app.setApplicationName("Artemis")
    app.setWindowIcon(app_icon())
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(_STYLE)
    window = build_window(config_path, demo=demo, voice=voice, app=app)
    if server is not None:
        server.setParent(window)
        server.show_requested.connect(window.show_from_instance)
    window.present(background=background)
    return app.exec()
