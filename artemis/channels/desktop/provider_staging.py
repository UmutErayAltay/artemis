"""Bekleyen rol ve düzen değişiklikleri: Kaydet'e basılana dek ayarları bellekte tutar."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import yaml

from artemis.channels.desktop.provider_layout import ROLE_TEXTS
from artemis.config.settings import Settings, load_settings
from artemis.i18n import t

if TYPE_CHECKING:
    from artemis.channels.desktop.provider_section import ProviderSection


def loaded_settings(section: ProviderSection) -> Settings:
    """Kayıtlı ayarları okur; bekleyen rol seçimleri varsa görünüme uygular."""
    settings = load_settings(section.config_path)
    if section._pending is None:
        return settings
    pending = section._pending
    primary = str(pending.get("primary_model") or settings.primary_model)
    hard = str(pending["hard_model"]) if pending.get("hard_model") else None
    fallback = str(pending["fallback_model"]) if pending.get("fallback_model") else None
    cu_raw = pending.get("computer_use")
    cu_model = (
        str(cu_raw.get("model") or "")
        if isinstance(cu_raw, dict)
        else settings.computer_use.model
    )
    cu = settings.computer_use.model_copy(update={"model": cu_model})
    return settings.model_copy(
        update={
            "primary_model": primary,
            "hard_model": hard,
            "fallback_model": fallback,
            "computer_use": cu,
        }
    )


def stage(section: ProviderSection, raw: dict[str, object]) -> None:
    """Rol değişikliğini diske yazmadan bekletir ve üst paneli kirli işaretler."""
    section._pending = copy.deepcopy(raw)
    section.reload()
    section.staged.emit()


def commit_pending(section: ProviderSection) -> bool:
    """Bekleyen rol seçimlerini doğrulanmış olarak diske yazar ve çalışma zamanını uyarır."""
    if section._pending is None:
        return True
    pending = copy.deepcopy(section._pending)
    if not section._write(pending):
        return False
    section.reload()
    section.changed.emit()
    return True


def drop_pending(section: ProviderSection) -> None:
    """Bekleyen rol seçimlerini atar ve görünümü kayıtlı ayardan yeniler."""
    if section._pending is None:
        return
    section._pending = None
    section.reload()


def make_primary(section: ProviderSection) -> None:
    """Seçili sohbet modelini beyin modeli olarak bekletir."""
    alias = section._selected_alias()
    if alias is None:
        return
    try:
        raw = section._raw()
    except (OSError, yaml.YAMLError):
        section.status.setText(t("Ayarlar okunamadı."))
        return
    if any(row[0] == alias and row[5] != "chat" for row in section._model_data):
        msg = t("Resim ve ses modelleri beyin modeli olamaz; sohbet modeli seçin.")
        section.status.setText(msg)
        return
    # Takma ada göre yazılır: aynı model kimliğini paylaşan modeller karışmaz.
    raw["primary_model"] = alias
    if raw.get("fallback_model") == alias:
        raw.pop("fallback_model")  # beyin olan model kendi arıza yedeği olamaz
    section._stage(raw)
    section.status.setText(
        t("Beyin modeli: {alias} · Kaydet ile uygulanır.").format(alias=alias)
    )


def toggle_role(section: ProviderSection, key: str) -> None:
    """Seçili modele zor işler/yedek/bilgisayar (computer_use.model) rolü; 2. tık kapatır."""
    alias = section._selected_alias()
    if alias is None:
        return
    name, off, on = ROLE_TEXTS[key]
    try:
        settings = section._loaded_settings()
        raw = section._raw()
    except (OSError, ValueError, yaml.YAMLError):
        section.status.setText(t("Ayarlar okunamadı."))
        return
    model = settings.models.get(alias)
    if model is None or model.kind != "chat":
        section.status.setText(
            t("Resim ve ses modelleri {role} modeli olamaz; sohbet modeli seçin.").format(
                role=t(name).lower()
            )
        )
        return
    if key == "fallback_model" and alias == settings.primary_model:
        section.status.setText(
            t("Bu model zaten beyin; {role} için başka bir model seçin.").format(
                role=t(name).lower()
            )
        )
        return
    # computer_use.model iç içe alandır; diğer roller kök anahtardır.
    target = raw.setdefault("computer_use", {}) if key == "computer_use" else raw
    field = "model" if key == "computer_use" else key
    if not isinstance(target, dict):
        section.status.setText(t("Ayarlar okunamadı."))
        return
    removing = target.get(field) == alias
    if removing:
        target.pop(field, None)
    else:
        target[field] = alias
    section._stage(raw)
    base = t(off) if removing else t(on).format(alias=alias)
    section.status.setText(base.rstrip(".") + t(" · Kaydet ile uygulanır."))


def apply_layout(section: ProviderSection) -> None:
    """Önerilen beyin/zor işler/bilgisayar düzenini bekletir."""
    ana, zor, pc = section._suggested
    if not ana:
        return
    try:
        raw = section._raw()
    except (OSError, yaml.YAMLError):
        section.status.setText(t("Ayarlar okunamadı."))
        return
    raw["primary_model"] = ana
    if zor:
        raw["hard_model"] = zor
    else:
        raw.pop("hard_model", None)
    # Boş computer_use.model ana modeli kullanır; öneri ana modelse boş bırakılır.
    computer = raw.setdefault("computer_use", {})
    if isinstance(computer, dict):
        computer["model"] = pc or ""
    section._stage(raw)
    section.status.setText(
        t("Düzen seçildi. Beyin: {ana}").format(ana=ana)
        + (t(", zor işler: {zor}").format(zor=zor) if zor else "")
        + (t(", bilgisayar kullanımı: {pc}").format(pc=pc) if pc else "")
        + t(" · Kaydet ile uygulanır.")
    )


def light_mode_of(section: ProviderSection, alias: str | None) -> bool | None:
    """Modelin bekleyen (yoksa kayıtlı) `light_mode` değeri; okunamazsa None (Otomatik)."""
    try:
        models = section._raw().get("models")
    except (OSError, yaml.YAMLError):
        return None
    entry = models.get(alias) if isinstance(models, dict) else None
    value = entry.get("light_mode") if isinstance(entry, dict) else None
    return value if isinstance(value, bool) else None


def set_light_mode(section: ProviderSection) -> None:
    """Seçili modelin hafif kip değerini (Otomatik/Açık/Kapalı) bekletir (ADR 0092)."""
    alias = section._selected_alias()
    if alias is None:
        return
    try:
        raw = section._raw()
    except (OSError, yaml.YAMLError):
        section.status.setText(t("Ayarlar okunamadı."))
        return
    models = raw.get("models")
    entry = models.get(alias) if isinstance(models, dict) else None
    if not isinstance(entry, dict):
        section.status.setText(t("Ayarlar okunamadı."))
        return
    value = section.light_combo.currentData()
    if value is None:
        entry.pop("light_mode", None)
    else:
        entry["light_mode"] = value
    section._stage(raw)
    section.status.setText(t("Hafif model kipi değişti · Kaydet ile uygulanır."))
