"""Sırların YAML veya repr içine girmemesi için ayarlarda sır alanı bulunmaz."""

from decimal import Decimal
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["openrouter", "anthropic", "ollama"]
    model: str = Field(min_length=1)
    input_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    context_window: int = Field(default=32768, ge=1024)


class VoiceConfig(BaseModel):
    """Önceden kurulmuş yerel ses modellerinin ve ses akışının ayarları."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    wake_word: str = Field(default="artemis", min_length=1, max_length=64)
    followup_seconds: float = Field(default=8.0, ge=0, le=120)
    sample_rate: Literal[16000] = 16000
    frame_ms: Literal[20, 30] = 20
    vad_aggressiveness: int = Field(default=2, ge=0, le=3)
    end_silence_ms: int = Field(default=600, ge=100, le=3000)
    pre_roll_ms: int = Field(default=300, ge=0, le=1000)
    max_utterance_seconds: float = Field(default=15.0, gt=0, le=120)
    queue_frames: int = Field(default=32, ge=2, le=256)
    stt_model: Path | None = None
    stt_device: Literal["cuda", "cpu"] = "cuda"
    stt_compute_type: str = "float16"
    tts_backend: Literal["piper", "edge"] = "piper"
    piper_model: Path | None = None
    piper_config: Path | None = None
    piper_voice: str = ""
    edge_voice: str = ""
    allow_cloud_tts_text: bool = False
    ffmpeg: str = "ffmpeg"

    @field_validator("wake_word")
    @classmethod
    def validate_wake_word(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Wake word boşluk veya kontrol karakteri içeremez")
        return value.strip()


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: Path = Path("desktop")
    state_dir: Path = Path(".jarvis")
    audit_path: Path = Path(".jarvis/audit.jsonl")
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    primary_model: str = ""
    fallback_model: str | None = None
    max_steps: int = Field(default=8, ge=1, le=100)
    max_seconds: float = Field(default=60.0, gt=0, le=3600)
    provider_timeout_seconds: float = Field(default=15.0, gt=0, le=3600)
    max_output_tokens: int = Field(default=1024, ge=1)
    daily_budget_usd: Decimal = Field(default=Decimal("1"), ge=0)
    allow_paid: bool = False
    allow_cloud_file_content: bool = False
    allow_cloud_clipboard: bool = False
    fallback_seconds: float = Field(default=60.0, ge=0)
    applications: dict[str, str] = Field(default_factory=dict)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    ollama_base_url: str = "http://127.0.0.1:11434"

    @field_validator("openrouter_base_url", "anthropic_base_url", "ollama_base_url")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Sağlayıcı adresinde sır, query veya fragment bulunamaz")
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("Geçerli HTTP(S) sağlayıcı adresi gerekli")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Şifresiz HTTP yalnız yerel sunucu için kullanılabilir")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_models(self) -> Self:
        if self.primary_model and self.primary_model not in self.models:
            raise ValueError("Birincil model models içinde tanımlanmalı")
        if self.fallback_model:
            fallback = self.models.get(self.fallback_model)
            if fallback is None or fallback.provider != "ollama":
                raise ValueError("Yedek model tanımlı bir Ollama modeli olmalı")
        return self


def load_settings(path: Path) -> Settings:
    """Göreli yolları config dosyasına göre çözerek çalışma dizini sürprizini önler."""
    with path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ValueError("Config bir YAML eşlemesi olmalı")
    settings = Settings.model_validate(raw)
    base = path.resolve().parent
    updates: dict[str, object] = {}
    for field in ("workspace", "state_dir", "audit_path"):
        value = getattr(settings, field)
        updates[field] = value if value.is_absolute() else (base / value).resolve()
    voice_updates: dict[str, Path] = {}
    for field in ("stt_model", "piper_model", "piper_config"):
        value = getattr(settings.voice, field)
        if value is not None:
            path_value = value
            voice_updates[field] = (
                path_value if path_value.is_absolute() else (base / path_value).resolve()
            )
    if voice_updates:
        updates["voice"] = settings.voice.model_copy(update=voice_updates)
    return settings.model_copy(update=updates)
