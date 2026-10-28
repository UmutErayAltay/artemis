"""Onaylanan taslak değişmezdir; yalnız kimlik göstermek yetki kapsamını saklamaz."""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AUTHORITY_BOUNDARY = (
    "Yalnız güvenilen yerel depo; dosya düzenleme ve yerel commit. Claude Code Bash, "
    "alt ajanlar, kullanıcı hooks/MCP ayarları Artemis SafetyGate dışında çalışabilir. "
    "Push yasağı CLI deseni/talimattır; OS izolasyonu veya harcama garantisi değildir. "
    "İptalde yarım değişiklikler kalır; otomatik geri alma yapılmaz."
)


def approval_text(values: dict[str, Any]) -> str:
    """Onay ekranında görünen ajan/klasör özeti; SafetyGate'e dokunmadan spec ile gider."""
    agent = values.get("agent", "claude")
    workdir = values.get("workdir", "proje")
    repo = values.get("repo_path") or "(çözülmedi)"
    folder = values.get("workdir_path") or repo
    lines = [
        f"Ajan: {agent}",
        f"Başlangıç klasörü: {workdir} -> {folder}",
        f"Hedef repo: {repo}",
    ]
    if workdir != "proje":
        lines.append(
            "Ajan bu klasörde başlar ve oradaki kuralları yükler; "
            f"hedef repo: {repo}. Bu klasördeki dosyaları değiştirebilir."
        )
    return "\n".join(lines)


class ProjectSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_id: str
    project: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    task: str = Field(min_length=1, max_length=4000, repr=False)
    scope: tuple[str, ...] = Field(min_length=1, max_length=16, repr=False)
    acceptance: tuple[str, ...] = Field(min_length=1, max_length=16, repr=False)
    # Ajan ve başlangıç klasörü yalnız yapılandırılmış TAKMA ADLARDIR (ADR 0049); yol
    # alanları onay ekranında çözülmüş mutlak yolu göstermek için hazırlıkta yazılır ve
    # başlatmada yeniden çözülüp karşılaştırılır (ayar değiştiyse iş başlamaz).
    agent: str = Field(default="claude", pattern=r"^[a-z0-9_-]{1,32}$")
    workdir: str = Field(default="proje", pattern=r"^[a-z0-9_-]{1,32}$")
    repo_path: str = Field(default="", max_length=1024)
    workdir_path: str = Field(default="", max_length=1024)
    # Türetilir: model ne yazarsa yazsın doğrulamada yeniden hesaplanır, böylece onay
    # ekranındaki metin ajan/klasör/yol alanlarından ayrışamaz.
    approval_text: str = Field(default="", max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def derive_approval_text(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = {**data, "approval_text": approval_text(data)}
        return data

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
