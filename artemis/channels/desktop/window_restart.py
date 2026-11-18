"""Ana pencerenin ayar kaydı ve çalışma zamanını yeniden kurma akışı (DesktopWindow'dan ayrıldı).

Config değişince çalışma zamanını kurmanın tek girişi `request_restart`tir; meşgulken ertelenir.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QSystemTrayIcon

from artemis.brain.providers import provider_problem
from artemis.channels.desktop.settings_io import save_choices
from artemis.config.settings import load_settings, same_hotkey

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
        names = window.settings_panel.section_names()
        window.settings_panel.show_section(names.index("Model") if "Model" in names else 1)
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
            "Kaydedildi; işlem bitince yeni ayarlar devreye girer."
        )
        return
    window._request_restart(window.settings_panel.providers.status)


def telegram_changed(window: DesktopWindow) -> None:
    """chat_id/token değişti: proaktif Telegram teslimi yeniden kurulumla yeni durumu görür."""
    status = window.settings_panel.telegram.message
    if window._exiting or window._demo:
        window._sync_telegram_service()
        status.setText("Kaydedildi; işlem bitince uygulama yeniden başlayınca geçerli olur.")
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
            "Kaydedildi; işlem bitince uygulama yeniden başlayınca geçerli olur."
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
        return "Kaydedildi; bekleyen onay bitince uygulama yeniden başlayınca geçerli olur."
    # Panel listesi bayat olabilir (sekme açık değilken yoklanmaz); sohbetten başlayan iş
    # için doğrudan kaynak olan işçi tarafı sayımına da bakılır.
    if window.projects_panel.active_job_count() or window.bridge.active_job_count():
        return "Kaydedildi; çalışan iş bitince uygulama yeniden başlayınca geçerli olur."
    if window._busy:
        return "Kaydedildi; işlem bitince uygulama yeniden başlayınca geçerli olur."
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
    text = (
        f"{len(report.cloned)} yeni GitHub reposu çekildi; "
        "Ayarlar'dan çıkınca kullanıma girer."
    )
    window.pet.say(text, "job")
    if window._tray is not None:
        window._tray.showMessage("Artemis", text, QSystemTrayIcon.MessageIcon.Information, 6000)


def apply_settings(window: DesktopWindow, device: int | str | None) -> None:
    """Otomatik kayıt: doğrular ve yazar, yeniden kurmaz (sekmeden çıkınca kurulur)."""
    panel = window.settings_panel
    if window._demo or window._exiting:
        panel.save_failed("Ayarları değiştirmek için işlemin bitmesini bekle.")
        return
    values = panel.values()
    try:
        voice_key = load_settings(window._config_path).voice.listening_hotkey
        dictation_key = str(values[("dictation", "hotkey")])
        selection_key = str(values[("selection_actions", "hotkey")])
        clash = same_hotkey(dictation_key, voice_key)
        selection_clash = same_hotkey(selection_key, voice_key) or same_hotkey(
            selection_key, dictation_key
        )
    except (OSError, ValueError, KeyError, ValidationError, yaml.YAMLError):
        clash = selection_clash = False  # okunamayan config aşağıdaki kayıtta yakalanır
    if clash:
        panel.save_failed("Dikte kısayolu ses kısayoluyla aynı olamaz; başka bir tuş seç.")
        return
    if selection_clash:
        panel.save_failed("Seçili metin kısayolu ses ve dikte kısayolundan farklı olmalı.")
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
        panel.save_failed("Ayarlar kaydedilemedi; mevcut ayarlar korunuyor.")
        return
    panel.mark_saved(written)


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
    window._set_status("Ayarlar uygulanıyor…", "running")
    window._set_controls()
