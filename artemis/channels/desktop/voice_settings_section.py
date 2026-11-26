"""Ses ve STT ayar bölümü yardımcıları (SettingsPanel ses denetleyicisi)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import QWidget

from artemis.brain.providers import stored_endpoint_key
from artemis.channels.desktop import provider_section, voice_preview
from artemis.channels.desktop.provider_section import store_endpoint_key
from artemis.channels.desktop.provider_workers import ConnectionTest
from artemis.channels.desktop.settings_io import refresh_device_combo
from artemis.channels.voice import stt_catalog
from artemis.config.settings import EndpointConfig, Settings, load_settings
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.settings_panel import SettingsPanel


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


class GpuProbe(QThread):
    """nvidia-smi (en çok 5 sn) GUI iş parçacığını bekletmesin diye ayrı çalışır."""

    done = Signal(object)

    def run(self) -> None:
        self.done.emit(stt_catalog.gpu_memory_mb())


def start_gpu_probe(panel: SettingsPanel) -> None:
    """Panel varsayılan öneriyle açılır; GPU belleği gelince öneri güncellenir."""
    probe = GpuProbe(panel)
    probe.done.connect(panel._gpu_probe_done)
    probe.finished.connect(probe.deleteLater)
    panel.gpu_probe = probe
    probe.start()


def gpu_probe_done(panel: SettingsPanel, gpu_mb: object) -> None:
    panel.gpu_probe = None
    panel._gpu_mb = gpu_mb if isinstance(gpu_mb, int) else None
    recommended = stt_catalog.recommended(panel._gpu_mb)
    if recommended == panel._recommended_stt:
        return
    panel._recommended_stt = recommended
    selected = panel.stt_combo.currentData()
    populate_stt_choices(panel, selected if isinstance(selected, str) else None)


def _installed(panel: SettingsPanel, name: str) -> Path | None:
    """Kurulu model yolunu yenileme başına bir kez çözer (arayüz iş parçacığında dosya araması)."""
    cache = panel._stt_installed
    if name not in cache:
        cache[name] = stt_catalog.installed_path(name)
    return cache[name]


def update_cloud_hint(panel: SettingsPanel) -> None:
    """Karma tanıma seçiliyken verinin nereye gittiğini açıkça yazar (ADR 0079)."""
    gm = getattr(panel, "game_mode", None)
    gt = getattr(panel, "game_mode_target", None)
    if gm is not None and gt is not None:
        gt.setEnabled(gm.isChecked() and not panel._demo)
    gt_box = getattr(panel, "game_mode_target_box", None)
    if gm is not None and gt_box is not None:
        gt_box.setVisible(gm.isChecked())
    game_cloud = bool(gm and gm.isChecked() and gt and gt.currentData() == "cloud")
    hybrid = panel.hybrid_stt.isChecked()
    on = hybrid or game_cloud
    xai = panel.cloud_protocol.currentData() == "xai"
    panel.cloud_box.setVisible(on)
    live = on and not panel._demo
    panel.cloud_model.setEnabled(live and not xai)
    panel.cloud_fetch_button.setEnabled(live and not xai and bool(cloud_endpoint(panel)[0]))
    edit = panel.cloud_model.lineEdit()
    if edit is not None:
        edit.setPlaceholderText(
            t("xAI için model gerekmez") if xai else t("Örneğin: whisper-1")
        )
    name, endpoint = cloud_endpoint(panel)
    needs_key = endpoint is not None and endpoint.api_key
    panel.cloud_key.setEnabled(live and needs_key)
    panel.cloud_key_button.setEnabled(live and needs_key)
    panel.cloud_key.setPlaceholderText(
        "" if not name else t("Bu sunucu anahtar istemiyor") if not needs_key
        else t("kayıtlı") if stored_endpoint_key(name) else ""
    )
    if not on:
        panel.cloud_stt_hint.setText("")
        return
    if hybrid:
        text = t(
            "Komut sesi seçtiğin bulut sunucusuna gönderilir; kesintisiz kipte her söz gider. "
            "Sunucu eklemek için Ayarlar > Model > Bağlantılar bölümünü kullan."
        )
    else:
        text = t(
            "Oyun modunda bulut hedefi seçili: tam ekran oyun açıldığında büyük yerel model "
            "VRAM'den bırakılır ve tanıma seçtiğin bulut sunucusuna geçer."
        )
    if xai:
        text += t(
            " Grok tanıma Türkçeyi resmi olarak doğrulamıyor; hata olursa yerel metne düşer."
        )
    if needs_key and not stored_endpoint_key(name) and not panel.cloud_key.text().strip():
        text += t(
            " Bu sunucu için API anahtarı kayıtlı değil; yukarıya yazıp "
            "'Anahtarı kaydet'e bas, yoksa tanıma çalışmaz."
        )
    panel.cloud_stt_hint.setText(text)


def cloud_endpoint(panel: SettingsPanel) -> tuple[str, EndpointConfig | None]:
    """Seçili sunucunun kimliği ve config kaydı; seçilmemiş ya da okunamazsa (\"\", None)."""
    name = str(panel.cloud_server.currentData() or "")
    if not name:
        return "", None
    try:
        return name, load_settings(panel.config_path).endpoints.get(name)
    except (OSError, ValueError):
        return name, None


def save_cloud_key(panel: SettingsPanel) -> None:
    """Anahtarı kasaya yazar; YAML, bağlar ve Uygula'ya girmez, alan hemen temizlenir."""
    name, endpoint = cloud_endpoint(panel)
    typed = panel.cloud_key.text().strip()
    if not name or endpoint is None or not endpoint.api_key or not typed:
        panel.cloud_stt_hint.setText(t("Önce bir sunucu seç ve anahtarı yaz."))
        return
    failure = store_endpoint_key(name, typed, provider_section.write_secret)
    panel.cloud_key.clear()
    update_cloud_hint(panel)
    panel.cloud_stt_hint.setText(
        t("Anahtar saklanamadı; {failure}").format(failure=failure)
        if failure
        else t("{name} için anahtar kaydedildi.").format(name=name)
    )


def fetch_cloud_models(panel: SettingsPanel) -> None:
    """Seçili sunucunun /models listesini arka planda getirir; bir seferde tek istek."""
    name, endpoint = cloud_endpoint(panel)
    if endpoint is None or (panel._cloud_fetch is not None and panel._cloud_fetch.isRunning()):
        return
    key = None
    if endpoint.api_key:
        key = panel.cloud_key.text().strip() or stored_endpoint_key(name)
    panel.cloud_stt_hint.setText(t("Model listesi alınıyor…"))
    panel._cloud_fetch_for = name
    panel._cloud_fetch = ConnectionTest(
        endpoint.base_url, key, panel, provider_section.probe_models
    )
    panel._cloud_fetch.done.connect(panel._cloud_models_done)
    panel._cloud_fetch.start()


def cloud_models_done(panel: SettingsPanel, message: str, models: list[str]) -> None:
    # İstek sürerken sunucu değiştiyse eski sunucunun listesi yeni sunucunun önerisi olmaz.
    if panel._cloud_fetch_for != str(panel.cloud_server.currentData() or ""):
        return
    panel.cloud_stt_hint.setText(message)
    if not models:
        return
    current = panel.cloud_model.currentText()
    panel.cloud_model.blockSignals(True)  # liste yenilemesi kirli sayılmasın
    panel.cloud_model.clear()
    panel.cloud_model.addItems(models)
    panel.cloud_model.setCurrentIndex(max(0, panel.cloud_model.findText(current)))
    if current:
        panel.cloud_model.setEditText(current)
    panel.cloud_model.blockSignals(False)


def load_cloud_servers(panel: SettingsPanel) -> None:
    """Sunucu listesini config'teki endpoints'ten yeniler; seçimi korur."""
    try:
        endpoints = list(load_settings(panel.config_path).endpoints)
    except (OSError, ValueError):
        return
    current = panel.cloud_server.currentData()
    panel.cloud_server.blockSignals(True)
    panel.cloud_server.clear()
    panel.cloud_server.addItem(t("Seçilmedi"), "")
    for name in endpoints:
        panel.cloud_server.addItem(name, name)
    panel.cloud_server.setCurrentIndex(max(0, panel.cloud_server.findData(current)))
    panel.cloud_server.blockSignals(False)
    update_cloud_hint(panel)


def cloud_server_picked(panel: SettingsPanel) -> None:
    """xAI sunucusu seçilince protokol xAI'ye önayarlanır (kullanıcı değiştirebilir)."""
    name = str(panel.cloud_server.currentData() or "")
    try:
        endpoint = load_settings(panel.config_path).endpoints.get(name)
    except (OSError, ValueError):
        return
    host = urlparse(endpoint.base_url).hostname or "" if endpoint else ""
    if name == "xai" or host == "x.ai" or host.endswith(".x.ai"):
        panel.cloud_protocol.setCurrentIndex(panel.cloud_protocol.findData("xai"))


def _toggle_disclosure(toggle: object, box: QWidget, checked: bool) -> None:
    box.setVisible(checked)
    toggle.setArrowType(  # type: ignore[attr-defined]
        Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
    )


def toggle_mic_advanced(panel: SettingsPanel, checked: bool) -> None:
    """Gelişmiş mikrofon ayarlarını açar ve ok yönünü duruma göre değiştirir."""
    _toggle_disclosure(panel.mic_advanced_toggle, panel.mic_advanced_box, checked)


def toggle_advanced(panel: SettingsPanel, checked: bool) -> None:
    """Gelişmiş tanıma ayarlarını açar ve ok yönünü duruma göre değiştirir."""
    _toggle_disclosure(panel.advanced_toggle, panel.advanced_box, checked)


def sync_voice_conditional_boxes(panel: SettingsPanel) -> None:
    """Bağımlı ses ve kısayol alanlarının görünürlüğünü üst onay kutusuna bağlar."""
    ptt = getattr(panel, "ptt_enabled", None)
    ptt_box = getattr(panel, "ptt_hotkey_box", None)
    if ptt is not None and ptt_box is not None:
        ptt_on = ptt.isChecked()
        ptt_box.setVisible(ptt_on)
        panel.ptt_hotkey.setEnabled(ptt_on and not panel._demo)
    gm = getattr(panel, "game_mode", None)
    gm_box = getattr(panel, "game_mode_target_box", None)
    if gm is not None and gm_box is not None:
        gm_on = gm.isChecked()
        gm_box.setVisible(gm_on)
        panel.game_mode_target.setEnabled(gm_on and not panel._demo)
    direct = getattr(panel, "direct_audio_input", None)
    mm_box = getattr(panel, "multimodal_screen_box", None)
    if direct is not None and mm_box is not None:
        mm_on = direct.isChecked() or panel.multimodal_screen_context.isChecked()
        mm_box.setVisible(mm_on)


def stt_size_text(choice: stt_catalog.WhisperChoice) -> str:
    if choice.size_mb < 1000:
        return f"{choice.size_mb} MB"
    return f"{choice.size_mb / 1000:.1f} GB"


def populate_stt_choices(panel: SettingsPanel, selected_name: str | None) -> None:
    panel._stt_installed = {}  # her yenilemede önbellek tazelenir
    was_blocked = panel.stt_combo.blockSignals(True)
    try:
        panel.stt_combo.clear()
        if selected_name is None:
            panel.stt_combo.addItem(t("Özel model (elle ayarlı)"), None)
        for choice in stt_catalog.CATALOG:
            text = f"{t(choice.label)} · {stt_size_text(choice)}"
            if choice.name == panel._recommended_stt:
                text += t(" · önerilen")
            if _installed(panel, choice.name) is not None:
                text += t(" · kurulu")
            panel.stt_combo.addItem(text, choice.name)
            index = panel.stt_combo.count() - 1
            panel.stt_combo.setItemData(
                index, t(choice.note), Qt.ItemDataRole.ToolTipRole
            )
        if selected_name is None:
            panel.stt_combo.setCurrentIndex(0)
        else:
            index = panel.stt_combo.findData(selected_name)
            if index >= 0:
                panel.stt_combo.setCurrentIndex(index)
    finally:
        panel.stt_combo.blockSignals(was_blocked)
    stt_selection_changed(panel)


def selected_stt_choice(panel: SettingsPanel) -> stt_catalog.WhisperChoice | None:
    name = panel.stt_combo.currentData()
    if not isinstance(name, str):
        return None
    return next(
        (choice for choice in stt_catalog.CATALOG if choice.name == name),
        None,
    )


def stt_selection_changed(panel: SettingsPanel) -> None:
    choice = selected_stt_choice(panel)
    if choice is None:
        panel.stt_status.setText(
            t("Özel model (elle ayarlı): ayar dosyasındaki model yolu kullanılıyor.")
        )
    else:
        panel.stt_status.setText(t(choice.note))
    update_stt_download_button(panel)


def update_stt_download_button(panel: SettingsPanel) -> None:
    choice = selected_stt_choice(panel)
    running = (
        panel.stt_download_thread is not None
        and panel.stt_download_thread.isRunning()
    )
    panel.stt_download.setEnabled(
        not panel._demo
        and choice is not None
        and not running
        and _installed(panel, choice.name) is None
    )


def stt_model_path(panel: SettingsPanel) -> Path | None:
    """Seçili katalog modelinin kullanılabilir yerel yolunu döndürür."""
    choice = selected_stt_choice(panel)
    if choice is None:
        return None
    return _installed(panel, choice.name)


def download_stt(panel: SettingsPanel) -> None:
    if panel._demo or panel.stt_download_thread is not None:
        return
    choice = selected_stt_choice(panel)
    if choice is None:
        return
    if _installed(panel, choice.name) is not None:
        stt_selection_changed(panel)
        return

    thread = SttDownload(choice.name, panel)
    thread.done.connect(panel._stt_download_done)
    thread.failed.connect(panel._stt_download_failed)
    thread.finished.connect(panel._stt_download_finished)
    thread.finished.connect(thread.deleteLater)
    panel.stt_download_thread = thread
    panel.stt_combo.setEnabled(False)
    panel.stt_download.setEnabled(False)
    panel.stt_status.setText(
        t("İndiriliyor… ({size})").format(size=stt_size_text(choice))
    )
    thread.start()


def stt_download_done(panel: SettingsPanel, _path: Path) -> None:
    choice = selected_stt_choice(panel)
    populate_stt_choices(panel, choice.name if choice is not None else None)
    panel.stt_status.setText(t("İndirildi; Kaydet ile kaydedilir."))
    panel._mark_dirty()


def stt_download_failed(panel: SettingsPanel, error_type: str) -> None:
    panel.stt_status.setText(t("İndirilemedi: {error_type}").format(error_type=error_type))


def stt_download_finished(panel: SettingsPanel) -> None:
    panel.stt_download_thread = None
    panel.stt_combo.setEnabled(not panel._demo)
    update_stt_download_button(panel)


def no_device(panel: SettingsPanel) -> None:
    """Giriş yoksa liste boş kalmaz: soluk, kapalı satır nedenini söyler."""
    panel.device_combo.clear()
    panel.device_combo.addItem(t("Mikrofon bulunamadı"), None)
    panel.device_combo.setEnabled(False)


def set_current_device(panel: SettingsPanel, text: str) -> None:
    panel.current_device.setText(text.strip() or t("Seçilmedi"))


def refresh_devices(panel: SettingsPanel, settings: Settings) -> None:
    refresh_device_combo(panel, settings)


def toggle_device_detail(panel: SettingsPanel) -> None:
    if panel._demo:
        return
    was_dirty = panel._dirty
    try:
        panel._refresh_devices(load_settings(panel.config_path))
    except (OSError, ValueError):
        panel.notice.setText(t("Mikrofon ayarı okunamadı."))
    # Listeyi yeniden kurmak kullanıcı değişikliği değildir.
    panel._set_dirty(was_dirty)


def sync_voice_models_from_manager(
    panel: SettingsPanel,
    _sender: object = None,
    _entries: object = None,
) -> None:
    """Model yöneticisindeki değişiklik sonrası Dinleme ve Yanıt ses seçicilerini yeniler."""
    was_dirty = panel._dirty
    selected_stt = panel.stt_combo.currentData()
    populate_stt_choices(
        panel, selected_stt if isinstance(selected_stt, str) else None
    )

    current_piper = panel.piper_voice_combo.currentText()
    was_blocked = panel.piper_voice_combo.blockSignals(True)
    try:
        panel.piper_voice_combo.clear()
        panel.piper_voice_combo.addItems(
            voice_preview.discover_piper_voices(panel.config_path)
        )
        if current_piper:
            panel.piper_voice_combo.setEditText(current_piper)
    finally:
        panel.piper_voice_combo.blockSignals(was_blocked)
    panel._set_dirty(was_dirty)
