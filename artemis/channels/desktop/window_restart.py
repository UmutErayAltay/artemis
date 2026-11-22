"""Ana pencerenin ayar kaydı ve çalışma zamanını yeniden kurma akışı (DesktopWindow'dan ayrıldı).

Config değişince çalışma zamanını kurmanın tek girişi `request_restart`tir; meşgulken ertelenir.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError
from PySide6.QtCore import QProcess, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QSystemTrayIcon

from artemis.brain.providers import provider_problem
from artemis.channels.desktop.budget_bar import refresh_budget
from artemis.channels.desktop.hotkey_manager import validate_hotkeys
from artemis.channels.desktop.retranslate import retranslate_widgets
from artemis.channels.desktop.settings_io import save_choices
from artemis.config.settings import load_settings
from artemis.i18n import current_language, set_language, t

if TYPE_CHECKING:
    from artemis.channels.desktop.app import DesktopWindow


def check_provider(window: DesktopWindow, *, open_settings: bool) -> None:
    """Birincil model çağrılamayacaksa ilk tur patlamadan Ayarlar'ı sağlayıcı bölümünde açar."""
    try:
        reason = provider_problem(load_settings(window._config_path))
    except (OSError, ValueError):
        return
    window.settings_panel.providers.show_problem(reason)
    if reason and open_settings:
        window.tabs.setCurrentWidget(window.settings_scroll)
        # Sağlayıcı eksikse Ayarlar doğrudan Model bölümünde açılır.
        window.settings_panel.select_section("Model")
        QTimer.singleShot(
            0,
            lambda: window.settings_panel.sections.scroll_area.ensureWidgetVisible(
                window.settings_panel.providers
            ),
        )


def providers_changed(window: DesktopWindow) -> None:
    if window.settings_panel.pet.creator is not None:
        window.settings_panel.pet.creator.reload_models()
    window._check_provider(open_settings=False)
    if window._exiting or window._demo:
        window.settings_panel.providers.status.setText(
            t("Kaydedildi; işlem bitince yeni ayarlar devreye girer.")
        )
        return
    window._request_restart(window.settings_panel.providers.status)


def telegram_changed(window: DesktopWindow) -> None:
    """chat_id/token değişti: proaktif Telegram teslimi yeniden kurulumla yeni durumu görür."""
    status = window.settings_panel.telegram.message
    if window._exiting or window._demo:
        window._sync_telegram_service()
        status.setText(
            t("Kaydedildi; işlem bitince uygulama yeniden başlayınca geçerli olur.")
        )
        return
    # Kanal servisi çalışma zamanından bağımsızdır: yeni sohbet/token hemen eşitlenir.
    # Yeniden kurulum hemen yapıldıysa o zaten eşitlemiştir (çift yeniden başlatma olmasın);
    # ertelendiyse (meşgul) ya da sürüyorsa burada eşitlenir.
    window._telegram_resynced = False
    window._request_restart(status)
    if not window._telegram_resynced:
        window._sync_telegram_service()


def code_agent_config_saved(window: DesktopWindow) -> None:
    """Projeler'den yazılan ajan/klasör ayarı çalışma zamanına yeniden kurulumla ulaşır.

    Takma adları çalışma zamanının ayarı çözer; yeniden kurulmazsa yeni klasör taslakta
    "bilinmeyen klasör" olur. Meşgulken sahte bir uygulama iddiası yerine dürüst not düşülür.
    """
    if window._exiting or window._demo:
        window.projects_panel.notice.setText(
            t("Kaydedildi; işlem bitince uygulama yeniden başlayınca geçerli olur.")
        )
        return
    window._request_restart()


def request_restart(window: DesktopWindow, notice: QLabel | None = None) -> None:
    """Config değişince çalışma zamanını kurmanın TEK girişi (Uygula, sağlayıcı, kod ajanı).

    Yeniden kurulum çalışan turu/arka plan işini sessizce iptal eder ve bekleyen onayı
    geçersiz kılar; bu yüzden hepsi boşta olana dek ertelenir (bayrak tek seferliktir).
    Config zaten yazılmıştır: ertelenen yalnız canlı kuruluştur, ayar kaybolmaz.
    """
    # Bu istek o anki config'le kurar; sekmeden çıkış aynı içerik için ikinci kez kurmaz.
    window._runtime_dump = window.settings_panel.config_dump()
    if window._restart_settings:
        # Sürmekte olan kuruluş config'i durma bittikten sonra okur; ikincisi gereksiz.
        return
    window._restart_notice = notice
    window._config_restart_pending = True
    window._config_restart_serial = window.projects_panel.status_serial
    window._maybe_restart_for_config()


def config_restart_blocker(window: DesktopWindow) -> str:
    """Yeniden kurulumu engelleyen neden ('' = boşta)."""
    if window._approval_dialog is not None:
        return t("Kaydedildi; bekleyen onay bitince uygulama yeniden başlayınca geçerli olur.")
    # Panel listesi bayat olabilir (sekme açık değilken yoklanmaz); sohbetten başlayan iş
    # için doğrudan kaynak olan işçi tarafı sayımına da bakılır.
    if window.projects_panel.active_job_count() or window.bridge.active_job_count():
        return t("Kaydedildi; çalışan iş bitince uygulama yeniden başlayınca geçerli olur.")
    if window._busy:
        return t("Kaydedildi; işlem bitince uygulama yeniden başlayınca geçerli olur.")
    return ""


def maybe_restart_for_config(window: DesktopWindow) -> None:
    if not window._config_restart_pending or window._exiting:
        return
    reason = config_restart_blocker(window)
    if reason:
        (window._restart_notice or window.projects_panel.notice).setText(reason)
        return
    window._config_restart_pending = False
    window._restart_runtime()


def github_synced(window: DesktopWindow, report: Any, auto: bool) -> None:
    if not (auto and report.cloned):
        return
    text = t(
        "{count} yeni GitHub reposu çekildi; Ayarlar'dan çıkınca kullanıma girer."
    ).format(count=len(report.cloned))
    window.pet.say(text, "job")
    if window._tray is not None:
        window._tray.showMessage("Artemis", text, QSystemTrayIcon.MessageIcon.Information, 6000)


def _prompt_unsaved(window: DesktopWindow) -> str:
    """Kaydedilmemiş değişiklik sorusu: 'save' | 'discard' | 'cancel' döndürür."""
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(t("Kaydedilmemiş değişiklikler"))
    box.setText(t("Ayarlarda kaydedilmemiş değişiklikler var. Ne yapılsın?"))
    save_btn = box.addButton(t("Kaydet"), QMessageBox.ButtonRole.AcceptRole)
    discard_btn = box.addButton(t("Değişiklikleri at"), QMessageBox.ButtonRole.DestructiveRole)
    cancel_btn = box.addButton(t("Vazgeç"), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(save_btn)
    box.setEscapeButton(cancel_btn)
    box.exec()
    clicked = box.clickedButton()
    if clicked is save_btn:
        return "save"
    if clicked is discard_btn:
        return "discard"
    return "cancel"


_ask_unsaved: Callable[[DesktopWindow], str] = _prompt_unsaved


def leave_settings(window: DesktopWindow) -> bool:
    """Ayarlar'dan çıkarken ya da uygulamadan çıkarken kaydedilmemiş değişiklikleri çözer."""
    panel = window.settings_panel
    if not panel.has_unsaved:
        return True
    choice = _ask_unsaved(window)
    if choice == "save":
        panel.flush()
        return not panel.has_unsaved
    if choice == "discard":
        panel.discard_changes()
        return True
    return False


def apply_settings(window: DesktopWindow, device: int | str | None) -> None:
    """Ayar kaydı: doğrular ve yazar, yeniden kurmaz (Kaydet ya da sekmeden çıkınca kurulur)."""
    panel = window.settings_panel
    if window._demo or window._exiting:
        panel.save_failed(t("Ayarları değiştirmek için işlemin bitmesini bekle."))
        return
    values = panel.values()
    old_language = current_language()
    hotkey_error: str | None = None
    try:
        loaded = load_settings(window._config_path)
        old_language = loaded.language
        voice_key = str(values.get(("voice", "listening_hotkey"), loaded.voice.listening_hotkey))
        dictation_key = str(values.get(("dictation", "hotkey"), loaded.dictation.hotkey))
        selection_key = str(
            values.get(("selection_actions", "hotkey"), loaded.selection_actions.hotkey)
        )
        hotkey_error = validate_hotkeys(
            listening=voice_key,
            dictation=dictation_key,
            selection=selection_key,
        )
    except (OSError, ValueError, KeyError, ValidationError, yaml.YAMLError):
        hotkey_error = None  # okunamayan config aşağıdaki kayıtta yakalanır
    if hotkey_error:
        panel.save_failed(hotkey_error)
        return
    try:
        written = save_choices(
            window._config_path,
            device,
            values=values,
            applications=panel.applications,
            code_agent=panel.code_agent_values(),
            stt_model=panel.stt_model_path(),
        )
    except (OSError, ValueError, KeyError, TypeError):
        panel.save_failed(t("Ayarlar kaydedilemedi; mevcut ayarlar korunuyor."))
        return
    panel.mark_saved(written)
    reload_dictation = getattr(
        getattr(window, "bridge", None), "reload_dictation_settings", None
    )
    if written and callable(reload_dictation):
        with contextlib.suppress(OSError, ValueError, ValidationError, yaml.YAMLError):
            reload_dictation(load_settings(window._config_path).dictation)
    reload_hotkeys = getattr(window, "_reload_hotkeys", None)
    if written and not window._demo and callable(reload_hotkeys):
        reload_hotkeys()
    new = str(values.get(("language",), old_language))
    if new != current_language():
        set_language(new)
        roots: list[Any] = list(QApplication.topLevelWidgets())
        if window._tray is not None and window._tray.contextMenu() is not None:
            roots.append(window._tray.contextMenu())
        retranslate_widgets(roots, new)
    refresh_budget(window)


def install_update(window: DesktopWindow, path: object) -> None:
    """İndirilmiş ve SHA-256 ile doğrulanmış kurucuyu başlatır; önce kaydedilmemiş ayar sorulur.

    Neden: kurucu ayrı süreçte açılır ve uygulama kapanır; kaydedilmemiş ayarlar kaybolmasın
    diye Ayarlar'dan çıkış kontrolü önce yapılır.
    """
    if not leave_settings(window):
        return
    if not QProcess.startDetached(str(path), []):
        window.settings_panel.update_section.result_label.setText(t("Kurucu başlatılamadı."))
        return
    window._request_exit()


def restart_runtime(window: DesktopWindow) -> None:
    """Config değişince çalışma zamanını yeniden kurar (Uygula ve sağlayıcı kaydı)."""
    if window.settings_panel.pet.creator is not None:
        window.settings_panel.pet.creator.reload_models()
    # Gömülü Telegram kanalı kendi runtime'ını açılışta okunan ayarla kurmuştu (örn. izinli
    # uygulamalar); yeniden kurulmazsa eski, gevşek politikayla çalışmaya devam ederdi.
    window._sync_telegram_service()
    window._telegram_resynced = True
    window._voice_on_ready = window.bridge.voice_active
    window._restart_settings = True
    window._worker_ready = False
    window._busy = True
    window.bridge.shutdown()
    window._set_status(t("Ayarlar uygulanıyor…"), "running")
    window._set_controls()


def stay_in_settings(window: DesktopWindow) -> None:
    """Çıkış iptal edildi: Ayarlar sekmesine, sekme değişimi işleyicisini tetiklemeden dönülür."""
    blocked = window.tabs.blockSignals(True)
    window.tabs.setCurrentWidget(window.settings_scroll)
    window.tabs.blockSignals(blocked)
    window._settings_open = True
    window._sync_navigation()


def finish_exit(window: DesktopWindow) -> None:
    """Çıkış onaylandıktan ve köprü durduktan sonra tüm pencere kaynaklarını kapatır."""
    if not window._exiting:
        return
    # Kanal kendi runtime'ını kapatır; iş parçacığı çıkıştan sonra kalmasın.
    window._telegram_service.stop()
    window._timer.stop()
    if window._hotkey is not None:
        window._hotkey.stop()
        window._hotkey = None
    if window._dictation_hotkey is not None:
        window._dictation_hotkey.stop()
        window._dictation_hotkey = None
    if window._selection_hotkey is not None:
        window._selection_hotkey.stop()
        window._selection_hotkey = None
    window.selection_popup.close()
    window.settings_panel.finish_inventory()
    window.settings_panel.providers.shutdown()
    if window._tray is not None:
        window._tray.hide()
    window.overlay.close()
    window.pet.close()
    if window._temporary is not None:
        window._temporary.cleanup()
        window._temporary = None
    window._allow_close = True
    window.close()
    window._app.quit()
