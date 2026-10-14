"""Onaylanan taslak değişmezdir; yalnız kimlik göstermek yetki kapsamını saklamaz."""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AUTHORITY_BOUNDARY = (
    "Yalnız güvenilen yerel depo; dosya düzenleme ve yerel commit. Claude Code Bash, "
    "alt ajanlar, kullanıcı hooks/MCP ayarları Jarvis SafetyGate dışında çalışabilir. "
    "Push yasağı CLI deseni/talimattır; OS izolasyonu veya harcama garantisi değildir. "
    "İptalde yarım değişiklikler kalır; otomatik geri alma yapılmaz."
)


class ProjectSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_id: str
    project: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    task: str = Field(min_length=1, max_length=4000, repr=False)
    scope: tuple[str, ...] = Field(min_length=1, max_length=16, repr=False)
    acceptance: tuple[str, ...] = Field(min_length=1, max_length=16, repr=False)

    @field_validator("spec_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        UUID(value)
        return value

    @field_validator("scope", "acceptance")
    @classmethod
    def valid_items(cls, items: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 1000 for item in items):
            raise ValueError("Kapsam ve kabul ölçütleri 1-1000 karakter olmalı.")
        return items

    @field_validator("task")
    @classmethod
    def valid_task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Görev boş olamaz.")
        return value


class SegmentOutput(BaseModel):
    """Claude structured_output içindeki sabit tamamlanma/soru sözleşmesi."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["question", "completed"]
    summary: str = Field(max_length=2000, repr=False)
    question: str | None = Field(max_length=4000, repr=False)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.kind == "question" and (self.question is None or not self.question.strip()):
            raise ValueError("Soru metni gerekli.")
        if self.kind == "completed" and self.question is not None:
            raise ValueError("Tamamlanma soru taşıyamaz.")
        return self
