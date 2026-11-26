"""Ayar paneli bağlamaları ve config.yaml yazımı; Qt'siz, panel dışında da denenebilir."""

from __future__ import annotations

import copy
import importlib
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from artemis.config.settings import (
    MCPServerConfig,
    Settings,
    load_settings,
)
from artemis.i18n import t

SettingPath = tuple[str, ...]


def _same(a: Any, b: Any) -> bool:
    """Tür-katı eşitlik: True != 1, 1 != 1.0; sözlükte anahtar sırası önemsiz, listede önemli.

    Python'un `==`i bool/int/float'ı eşit sayar; "geri al" ve "yazma gereksiz" kararı bu yüzden
    sessizce yanlış çıkardı (örn. 1 yerine True kaydedilmiş sayılırdı)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[key], b[key]) for key in a)
    if isinstance(a, list | tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


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
            combo.addItem(t("Özel ({value})").format(value=value), value)
        combo.setCurrentIndex(combo.findData(value))

    return PanelBinding(
        ("voice", "stt_compute_type"), "precision_combo",
        lambda combo: str(combo.currentData()), write,
    )


def _write_tts_cloud_server(combo: Any, value: Any) -> None:
    # Liste kutunun kendisinde yenilenir; kayıtlı ama listede olmayan sunucu kaybolmasın.
    if value and combo.findData(value) < 0:
        combo.addItem(str(value), value)
    combo.setCurrentIndex(max(0, combo.findData(value)))


def _read_checked_list(widget: Any) -> list[str]:
    from PySide6.QtCore import Qt

    selected: list[str] = []
    for index in range(widget.count()):
        item = widget.item(index)
        if item is not None and item.checkState() == Qt.CheckState.Checked:
            alias = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(alias, str) and alias:
                selected.append(alias)
    return selected


def _write_checked_list(widget: Any, value: Any) -> None:
    from PySide6.QtCore import Qt

    selected = {str(item) for item in (value or ())}
    blocked = widget.blockSignals(True)
    try:
        for index in range(widget.count()):
            item = widget.item(index)
            if item is None:
                continue
            alias = str(item.data(Qt.ItemDataRole.UserRole) or "")
            item.setCheckState(
                Qt.CheckState.Checked if alias in selected else Qt.CheckState.Unchecked
            )
    finally:
        widget.blockSignals(blocked)


_STANDARD_PRECISIONS = ("int8_float16", "float16")

# Panelin "Uygula" ile yazdığı ve "yenile" ile okuduğu sade alanlar. Özel mantığı olanlar
# (mikrofon aygıtı, STT model yolu, uygulama listesi, code_agent) save_choices'ta açık kalır.
PANEL_BINDINGS: tuple[PanelBinding, ...] = (
    _combo(("language",), "language_combo", fallback_first=True),
    PanelBinding(
        ("voice", "wake_word"), "wake_name",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    PanelBinding(
        ("voice", "listening_hotkey"), "listening_hotkey",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    _checkbox(("voice", "ptt_enabled"), "ptt_enabled"),
    PanelBinding(
        ("voice", "ptt_hotkey"), "ptt_hotkey",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    _combo(("voice", "tts_backend"), "tts_combo", fallback_first=False),
    PanelBinding(
        ("voice", "edge_voice"),
        "edge_voice_combo",
        lambda combo: combo.currentText().strip(),
        lambda combo, value: combo.setEditText(str(value)),
    ),
    PanelBinding(
        ("voice", "piper_voice"),
        "piper_voice_combo",
        lambda combo: combo.currentText().strip(),
        lambda combo, value: combo.setEditText(str(value)),
    ),
    _combo(("voice", "stt_language"), "stt_language_combo", fallback_first=False),
    PanelBinding(
        ("voice", "tts_cloud_endpoint"), "tts_cloud_server",
        lambda combo: str(combo.currentData() or ""),
        _write_tts_cloud_server,
    ),
    _combo(("voice", "tts_cloud_protocol"), "tts_cloud_protocol", fallback_first=True),
    PanelBinding(
        ("voice", "tts_cloud_model"), "tts_cloud_model",
        lambda combo: combo.currentText().strip(),
        lambda combo, value: combo.setEditText(str(value)),
    ),
    PanelBinding(
        ("voice", "tts_cloud_voice"), "tts_cloud_voice",
        lambda combo: combo.currentText().strip(),
        lambda combo, value: combo.setEditText(str(value)),
    ),
    _checkbox(("voice", "allow_cloud_tts_text"), "tts_cloud_consent"),
    _checkbox(("voice", "tts_cloud_allow_paid"), "tts_cloud_paid"),
    _checkbox(("voice", "tts_cloud_emotion_tags"), "tts_cloud_emotion"),
    PanelBinding(
        ("voice", "tts_cloud_emotion_tag_list"),
        "tts_cloud_emotion_tags",
        lambda edit: tuple(edit.text().split()),
        lambda edit, value: edit.setText(" ".join(str(v) for v in value)),
    ),
    _precision_binding(),
    _checkbox(("voice", "continuous_mode"), "continuous_mode", quiet=True),
    PanelBinding(
        ("voice", "stt_mode"), "hybrid_stt",
        lambda box: "hybrid" if box.isChecked() else "local",
        lambda box, value: box.setChecked(value == "hybrid"),
    ),
    _checkbox(("voice", "direct_audio_input"), "direct_audio_input"),
    _checkbox(("voice", "multimodal_screen_context"), "multimodal_screen_context"),
    PanelBinding(
        ("voice", "stt_cloud_endpoint"), "cloud_server",
        lambda combo: str(combo.currentData() or ""),
        lambda combo, value: combo.setCurrentIndex(max(0, combo.findData(value))),
    ),
    _combo(("voice", "stt_cloud_protocol"), "cloud_protocol", fallback_first=True),
    PanelBinding(
        ("voice", "stt_cloud_model"), "cloud_model",
        lambda combo: combo.currentText().strip(),
        lambda combo, value: combo.setEditText(str(value)),
    ),
    _checkbox(("voice", "barge_in_without_wake"), "barge_in_box"),
    _checkbox(("voice", "followup_listening"), "followup_box"),
    _checkbox(("voice", "live_transcript"), "live_transcript"),
    _checkbox(("voice", "noise_suppression"), "noise_suppression"),
    _checkbox(("voice", "game_mode"), "game_mode"),
    _combo(("voice", "game_mode_target"), "game_mode_target", fallback_first=True),
    _combo(("voice", "noise_suppression_level"), "noise_level", fallback_first=True),
    _checkbox(("voice", "noise_suppression_stt"), "noise_stt"),
    _checkbox(("voice", "echo_cancellation"), "echo_cancellation"),
    _checkbox(("dictation", "enabled"), "dictation_enabled"),
    PanelBinding(
        ("dictation", "hotkey"), "dictation_hotkey",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    PanelBinding(
        ("dictation", "max_seconds"),
        "dictation_max_seconds",
        lambda box: float(box.value()),
        lambda box, value: box.setValue(int(round(float(value)))),
        quiet=True,
    ),
    _checkbox(("dictation", "polish_punctuation"), "dictation_polish"),
    _checkbox(("selection_actions", "enabled"), "selection_enabled"),
    _checkbox(("selection_actions", "mouse_popup"), "selection_mouse_popup"),
    PanelBinding(
        ("selection_actions", "hotkey"), "selection_hotkey",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    PanelBinding(
        ("selection_actions", "translate_to"), "selection_translate_to",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    _checkbox(("proactive", "enabled"), "proactive_enabled"),
    _checkbox(("routines", "enabled"), "routines_enabled"),
    _checkbox(("calendar", "enabled"), "calendar_enabled"),
    _checkbox(("calendar", "allow_cloud"), "calendar_cloud"),
    _checkbox(("calendar", "allow_write"), "calendar_allow_write"),
    PanelBinding(
        ("calendar", "caldav_url"), "calendar_caldav_url",
        lambda edit: edit.text().strip().rstrip("/"),
        lambda edit, value: edit.setText(str(value)),
    ),
    PanelBinding(
        ("calendar", "caldav_username"), "calendar_caldav_username",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    _checkbox(("mail", "enabled"), "mail_enabled"),
    _checkbox(("mail", "allow_cloud"), "mail_cloud"),
    _checkbox(("mail", "allow_send"), "mail_allow_send"),
    _checkbox(("skills", "enabled"), "skills_enabled"),
    _checkbox(("triggers", "enabled"), "triggers_enabled"),
    _checkbox(("research", "enabled"), "research_enabled"),
    _checkbox(("daily_summary", "enabled"), "daily_summary_enabled"),
    PanelBinding(
        ("daily_summary", "at"), "daily_summary_at",
        lambda edit: edit.text().strip() or "22:30", lambda edit, value: edit.setText(str(value)),
    ),
    _checkbox(("file_search", "enabled"), "file_search_enabled"),
    PanelBinding(
        ("file_search", "roots"), "file_search_roots",
        lambda box: [box.item(row).text() for row in range(box.count())],
        lambda box, value: (box.clear(), box.addItems([str(v) for v in value]))[0],
    ),
    _checkbox(("telegram", "accept_voice"), "telegram_voice"),
    _checkbox(("telegram", "accept_photo"), "telegram_photo"),
    _checkbox(("telegram", "accept_files"), "telegram_files"),
    PanelBinding(
        ("mail", "host"), "mail_host",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    PanelBinding(
        ("mail", "username"), "mail_username",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    PanelBinding(
        ("mail", "port"), "mail_port",
        lambda edit: int(edit.text()) if edit.text().strip().isdecimal() else 993,
        lambda edit, value: edit.setText(str(value)),
    ),
    PanelBinding(
        ("mail", "folder"), "mail_folder",
        lambda edit: edit.text().strip() or "INBOX", lambda edit, value: edit.setText(str(value)),
    ),
    PanelBinding(
        ("mail", "smtp_host"), "mail_smtp_host",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    PanelBinding(
        ("mail", "smtp_port"), "mail_smtp_port",
        lambda edit: int(edit.text()) if edit.text().strip().isdecimal() else 465,
        lambda edit, value: edit.setText(str(value)),
    ),
    PanelBinding(
        ("mail", "smtp_username"), "mail_smtp_username",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    _checkbox(("allow_all_applications",), "all_applications"),
    PanelBinding(
        ("close_without_confirmation",), "close_without_confirmation_list",
        _read_checked_list, _write_checked_list,
    ),
    _checkbox(("allow_cloud_file_content",), "allow_cloud_file_content"),
    _checkbox(("allow_cloud_clipboard",), "allow_cloud_clipboard"),
    _checkbox(("memory", "encrypt_at_rest"), "memory_encrypt_at_rest"),
    PanelBinding(
        ("computer_use", "mode"),
        "computer_auto",
        lambda box: "auto" if box.isChecked() else "accept",
        lambda box, value: box.setChecked(value == "auto"),
    ),
    _combo(("computer_use", "observation"), "computer_observation", fallback_first=True),
)
_BOUND_PATHS = frozenset(binding.path for binding in PANEL_BINDINGS)


def setting_value(settings: Settings, path: SettingPath) -> Any:
    value: Any = settings
    for key in path:
        value = getattr(value, key)
    return value


def dump_mcp_server(server: MCPServerConfig) -> dict[str, Any]:
    """MCP sunucusunu YAML doğrulamasından geçebilecek JSON uyumlu sözlüğe çevirir."""
    return server.model_dump(mode="json", exclude_none=True)


def save_choices(
    path: Path, device: int | str | None,
    *, values: Mapping[SettingPath, Any] | None = None,
    applications: dict[str, str] | None = None,
    code_agent: dict[str, Any] | None = None,
    stt_model: Path | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
) -> bool:
    """Diğer alanları (model seçimi dahil) korur; doğrulayıp aynı dizinde atomik değiştirir.

    Dosya içeriği değişmeyecekse yazmaz ve False döner (otomatik kayıt her duraksamada dosyayı
    yeniden yazıp izleyicileri ve yeniden başlatmayı boşuna tetiklemesin); yazdıysa True.

    `values` yalnız PANEL_BINDINGS yollarını kabul eder (yazım hatası sessizce yeni alan
    açmasın diye KeyError); verilmeyen yolun kayıtlı değeri korunur.
    """
    unknown = set(values or {}) - _BOUND_PATHS
    if unknown:
        raise KeyError(f"Panel bağı olmayan ayar yolu: {sorted(unknown)}")
    load_settings(path)  # bozuk config'i kaydetmeden önce yakalar
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    before = copy.deepcopy(raw)
    raw.setdefault("voice", {})["input_device"] = device
    for setting_path, value in (values or {}).items():
        section = raw
        for key in setting_path[:-1]:
            if not isinstance(section.get(key), dict):
                section[key] = {}
            section = section[key]
        # Config tuple alanları panelde tuple okunur; YAML yalnız liste yazabilir.
        section[setting_path[-1]] = list(value) if isinstance(value, tuple) else value
    if stt_model is not None:
        raw["voice"]["stt_model"] = str(stt_model)
    if applications is not None:
        raw["applications"] = applications
        raw["close_without_confirmation"] = [
            name for name in raw.get("close_without_confirmation", []) if name in applications
        ]
    if code_agent is not None:
        _merge_code_agent(raw, code_agent)
    if mcp_servers is not None and (mcp_servers or "mcp" in raw):
        raw["mcp"] = {"servers": mcp_servers}
    if _same(before, raw):
        return False
    _write_validated(path, raw)
    return True


def save_computer_mode(path: Path, mode: str) -> None:
    """Yalnız bilgisayar kullanımı kipini yazar; üst çubuktaki düğme Uygula beklemez."""
    if mode not in {"accept", "auto"}:
        raise ValueError(f"Geçersiz bilgisayar kullanımı kipi: {mode!r}")
    load_settings(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("computer_use", {})["mode"] = mode
    _write_validated(path, raw)


def save_work_profile(path: Path, profile: str) -> None:
    """Yalnız çalışma profilini atomik olarak yazar; üst çubuk seçicisi Uygula beklemez."""
    from artemis.config.work_profile import is_valid_work_profile

    if not is_valid_work_profile(profile):
        raise ValueError(f"Geçersiz çalışma profili: {profile!r}")
    load_settings(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["work_profile"] = profile
    _write_validated(path, raw)


def save_code_agent(path: Path, code_agent: dict[str, Any]) -> None:
    """Yalnız code_agent alanları; model/cihaz seçimine bağlı değil (GitHub bağı hemen kalıcı)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    _merge_code_agent(raw, code_agent)
    _write_validated(path, raw)


def save_triggers(
    path: Path,
    items: list[dict[str, Any]],
    *,
    enable: bool = False,
    chains: list[dict[str, Any]] | None = None,
) -> None:
    """Tetikleyici ve öneri zinciri listesini yazar (ADR 0061, ADR 0086).

    Yalnız Ayarlar formundan, kullanıcı eylemiyle çağrılır; hiçbir araç ya da model ulaşamaz.
    Diğer anahtarlar ve atomik yazım `_write_validated`ta.
    """
    load_settings(path)  # bozuk config'i kaydetmeden önce yakalar
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = raw.get("triggers") or {}
    section["items"] = items
    if chains is not None:
        section["chains"] = chains
    if enable:
        section["enabled"] = True
    raw["triggers"] = section
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


def restore_baseline(config_path: Path, baseline: bytes) -> None:
    """Kaydedilmiş başlangıç baytlarını doğrulayıp config_path üzerine atomik olarak yazar."""
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".settings-", suffix=".yaml", dir=config_path.parent
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(baseline)
        load_settings(Path(temporary))
        os.replace(temporary, config_path)
    finally:
        if temporary:
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
            name = t("Dahili mikrofon — ") + name[len("Microphone Array ("):].rstrip(")")
        elif name.startswith("Mikrofon ("):
            name = t("Mikrofon — ") + name[len("Mikrofon ("):].rstrip(")")
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


def refresh_device_combo(panel: Any, settings: Settings, *, reuse: bool = False) -> None:
    """Mikrofon açılır listesini doldurur (SettingsPanel'den taşındı: dosya boyutu sınırı)."""
    panel.device_combo.clear()
    panel.device_combo.setEnabled(not panel._demo)
    panel.device_combo.addItem(t("Sistem varsayılanı"), None)
    audio_errors: tuple[type[Exception], ...] = (ImportError, OSError, RuntimeError, ValueError)
    try:
        sd = importlib.import_module("sounddevice")
        audio_errors = (*audio_errors, sd.PortAudioError)
        snapshot = getattr(panel, "_device_snapshot", None)
        if snapshot is None or not reuse:
            # PortAudio sorgusu yavaş olabilir: yalnız yenilemede sorulur, "ayrıntı"
            # anahtarı aynı anlık görüntüyü yeniden kullanır.
            default_device = getattr(sd.default, "device", None)
            snapshot = (
                sd.query_devices(),
                sd.query_hostapis(),
                default_device[0] if default_device else None,
            )
            panel._device_snapshot = snapshot
        devices, hosts, default_in = snapshot
        active: Any = settings.voice.input_device
        if active is None and default_in is not None:
            # Sistem varsayılan girişi yokken sounddevice (None, None) dönebilir.
            active = int(default_in)
        active_name = "" if active is None else str(active)
        choices = microphone_choices(
            list(devices), list(hosts), active,
            advanced=panel.advanced_devices.isChecked(),
        )
        if not choices:
            panel._no_device()
            panel._set_current_device("")
            return
        for label, index, indices in choices:
            panel.device_combo.addItem(label, index)
            if active in indices or (isinstance(active, str) and active in label):
                active_name = label
            if settings.voice.input_device in indices:
                panel.device_combo.setCurrentIndex(panel.device_combo.count() - 1)
        if active is not None and active_name == str(active):
            panel.device_combo.addItem(
                t("Mevcut giriş ({active})").format(active=active),
                settings.voice.input_device,
            )
            panel.device_combo.setCurrentIndex(panel.device_combo.count() - 1)
        panel._set_current_device(active_name)
    except audio_errors:
        panel._no_device()
        panel.current_device.setText(t("Seçilmedi (mikrofon aygıtları okunamadı)"))


def apply_github_report(panel: Any, report: Any) -> None:
    """GitHub eşitleme sonucunu panele yazar (SettingsPanel'den taşındı: dosya boyutu sınırı)."""
    panel.github_list.clear()
    for label, names in ((t("klonlandı"), report.cloned), (t("güncellendi"), report.fetched)):
        for name in names:
            panel.github_list.addItem(f"{label}: {name}")
    for label, pairs in ((t("atlandı"), report.skipped), (t("başarısız"), report.failed)):
        for name, reason in pairs:
            panel.github_list.addItem(f"{label}: {name} — {reason}")
    summary = t(
        "{cloned} klonlandı, {fetched} güncellendi, {skipped} atlandı, {failed} başarısız."
    )
    panel.github_status.setText(
        summary.format(
            cloned=len(report.cloned), fetched=len(report.fetched),
            skipped=len(report.skipped), failed=len(report.failed),
        )
    )
    # Yeniden başlatma yok: kod ajanı projeleri her çağrıda klasörden okur.
    panel.github_synced.emit(report, panel._github_auto)
