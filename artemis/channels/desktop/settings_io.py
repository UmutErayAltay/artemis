"""Ayar paneli bağlamaları ve config.yaml yazımı; Qt'siz, panel dışında da denenebilir."""

from __future__ import annotations

import copy
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from artemis.config.settings import (
    Settings,
    load_settings,
)

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
    _combo(("voice", "noise_suppression_level"), "noise_level", fallback_first=True),
    _checkbox(("voice", "noise_suppression_stt"), "noise_stt"),
    _checkbox(("voice", "echo_cancellation"), "echo_cancellation"),
    _checkbox(("dictation", "enabled"), "dictation_enabled"),
    PanelBinding(
        ("dictation", "hotkey"), "dictation_hotkey",
        lambda edit: edit.text().strip(), lambda edit, value: edit.setText(str(value)),
    ),
    _checkbox(("selection_actions", "enabled"), "selection_enabled"),
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
    _checkbox(("mail", "enabled"), "mail_enabled"),
    _checkbox(("mail", "allow_cloud"), "mail_cloud"),
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
        lambda edit: int(edit.text()) if edit.text().strip().isdigit() else 993,
        lambda edit, value: edit.setText(str(value)),
    ),
    PanelBinding(
        ("mail", "folder"), "mail_folder",
        lambda edit: edit.text().strip() or "INBOX", lambda edit, value: edit.setText(str(value)),
    ),
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


def save_code_agent(path: Path, code_agent: dict[str, Any]) -> None:
    """Yalnız code_agent alanları; model/cihaz seçimine bağlı değil (GitHub bağı hemen kalıcı)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    _merge_code_agent(raw, code_agent)
    _write_validated(path, raw)


def save_triggers(path: Path, items: list[dict[str, Any]], *, enable: bool = False) -> None:
    """Tetikleyici listesini yazar (ADR 0061). Yalnız Ayarlar formundan, kullanıcı eylemiyle
    çağrılır; hiçbir araç ya da model ulaşamaz. Diğer anahtarlar ve atomik yazım
    `_write_validated`ta."""
    load_settings(path)  # bozuk config'i kaydetmeden önce yakalar
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = raw.get("triggers") or {}
    section["items"] = items
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
