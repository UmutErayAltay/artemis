"""Tetikleyici ve koşullu rutin öneri zinciri ayar şemaları (ADR 0061, ADR 0086).

Yalnız kullanıcı tarafından config üzerinden tanımlanır; hiçbir LLM aracı tetikleyici
veya öneri zinciri ekleyemez ya da değiştiremez.
"""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TRIGGERS = 10
MAX_CHAINS = 10

TriggerKind = Literal["folder", "app_opened", "calendar_before", "system_resume"]
ChainWorkProfile = Literal["work", "game", "private", "meeting"]

DEFAULT_TRIGGER_MESSAGES: dict[TriggerKind, str] = {
    "folder": "Yeni dosya geldi: {file}",
    "app_opened": "Uygulama açıldı: {app}",
    "calendar_before": "Yaklaşan toplantı: {title} ({minutes} dk)",
    "system_resume": "Sistem uykudan uyandı",
}

_TIME_RANGE_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


class TriggerConfig(BaseModel):
    """Config'te tanımlı tetikleyici (ADR 0061, ADR 0086): yalnız kullanıcı yazar."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    kind: TriggerKind = "folder"
    # kind == "folder" için zorunlu ve mutlak yol; diğer türlerde boş bırakılır.
    folder: str = Field(default="", max_length=500)
    pattern: str = Field(default="*", min_length=1, max_length=100)
    # kind == "app_opened" için hedef uygulama veya süreç adı (ör. code.exe).
    app: str = Field(default="", max_length=120)
    # kind == "calendar_before" için toplantıdan kaç dakika önce ve isteğe bağlı başlık süzgeci.
    minutes_before: int = Field(default=10, ge=1, le=120)
    title_filter: str = Field(default="", max_length=100)
    # kind == "system_resume" (ve genel tetikleyiciler) için yeniden tetikleme bekleme süresi.
    cooldown_minutes: int = Field(default=30, ge=1, le=1440)
    # Sabit şablon metni; varsayılan türe göre otomatik seçilir.
    message: str = Field(default=DEFAULT_TRIGGER_MESSAGES["folder"], min_length=1, max_length=300)
    # En çok bir eylem düğmesi: sohbet kutusuna öneri ya da onaylı rutin.
    suggest: str | None = Field(default=None, min_length=1, max_length=500)
    routine: str | None = Field(default=None, min_length=1, max_length=40)

    @field_validator("folder")
    @classmethod
    def validate_folder(cls, value: str) -> str:
        if chr(0) in value:
            raise ValueError("folder geçersiz boş karakter içeremez")
        return value

    @field_validator("app")
    @classmethod
    def validate_app(cls, value: str) -> str:
        stripped = value.strip()
        if chr(0) in stripped:
            raise ValueError("app geçersiz boş karakter içeremez")
        return stripped

    @field_validator("title_filter")
    @classmethod
    def validate_title_filter(cls, value: str) -> str:
        if chr(0) in value:
            raise ValueError("title_filter geçersiz boş karakter içeremez")
        return value.strip()

    @model_validator(mode="after")
    def validate_kind_and_button(self) -> Self:
        if self.kind == "folder":
            absolute = bool(self.folder) and (
                Path(self.folder).is_absolute() or PureWindowsPath(self.folder).is_absolute()
            )
            if not absolute:
                raise ValueError("folder mutlak bir yol olmalı (ör. C:/Users/ben/Downloads)")
        elif self.kind == "app_opened":
            if not self.app.strip():
                raise ValueError("app_opened tetikleyicisi için app alanı boş olamaz")
        if self.suggest is not None and self.routine is not None:
            raise ValueError("Bir tetikleyici en çok bir düğme taşır: suggest ya da routine")
        if "message" not in self.model_fields_set and self.kind != "folder":
            object.__setattr__(self, "message", DEFAULT_TRIGGER_MESSAGES[self.kind])
        return self


class SuggestionChainConfig(BaseModel):
    """Koşullu rutin öneri zinciri (ADR 0086): deterministik, LLM'siz, tıklamayla çalışır."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    enabled: bool = True
    after_routine: str = Field(default="", max_length=40)
    after_trigger: str = Field(default="", max_length=32)
    work_profiles: tuple[ChainWorkProfile, ...] = ()
    active_app: str = Field(default="", max_length=120)
    time_range: str = Field(default="", max_length=11)
    cooldown_minutes: int = Field(default=60, ge=1, le=1440)
    message: str = Field(default="Sıradaki adım önerisi", min_length=1, max_length=300)
    suggest: str | None = Field(default=None, min_length=1, max_length=500)
    routine: str | None = Field(default=None, min_length=1, max_length=40)

    @field_validator("after_routine", "after_trigger", "active_app")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if chr(0) in stripped:
            raise ValueError("Alan geçersiz boş karakter içeremez")
        return stripped

    @field_validator("time_range")
    @classmethod
    def validate_time_range(cls, value: str) -> str:
        stripped = value.strip()
        if stripped and not _TIME_RANGE_RE.fullmatch(stripped):
            raise ValueError("time_range SS:DD-SS:DD biçiminde olmalı (ör. 09:00-18:00)")
        return stripped

    @model_validator(mode="after")
    def validate_chain(self) -> Self:
        if not self.after_routine and not self.after_trigger:
            raise ValueError(
                "Zincir için after_routine veya after_trigger alanlarından biri gerekli"
            )
        if self.after_routine and self.after_trigger:
            raise ValueError(
                "Zincir yalnız bir kaynak seçebilir: after_routine veya after_trigger"
            )
        if self.suggest is not None and self.routine is not None:
            raise ValueError("Bir öneri zinciri en çok bir düğme taşır: suggest ya da routine")
        if (
            self.after_routine
            and self.routine
            and self.after_routine.casefold() == self.routine.casefold()
        ):
            raise ValueError("Zincir aynı rutini doğrudan yeniden öneremez")
        return self


class TriggersConfig(BaseModel):
    """Tetikleyiciler ve koşullu öneri zincirleri (ADR 0061, ADR 0086). Varsayılan kapalı."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    items: tuple[TriggerConfig, ...] = Field(default=(), max_length=MAX_TRIGGERS)
    chains: tuple[SuggestionChainConfig, ...] = Field(default=(), max_length=MAX_CHAINS)

    @field_validator("items")
    @classmethod
    def unique_names(cls, values: tuple[TriggerConfig, ...]) -> tuple[TriggerConfig, ...]:
        names = [item.name.casefold() for item in values]
        if len(names) != len(set(names)):
            raise ValueError("Tetikleyici adları benzersiz olmalı")
        return values

    @field_validator("chains")
    @classmethod
    def unique_chain_names(
        cls, values: tuple[SuggestionChainConfig, ...]
    ) -> tuple[SuggestionChainConfig, ...]:
        names = [item.name.casefold() for item in values]
        if len(names) != len(set(names)):
            raise ValueError("Öneri zinciri adları benzersiz olmalı")
        return values
