"""cloud_tts_model seçili bulut konuşma ayarına göre kayıtlı ses modelini çözer."""

from __future__ import annotations

from decimal import Decimal

from artemis.config.settings import (
    EndpointConfig,
    ModelConfig,
    Settings,
    VoiceConfig,
    cloud_tts_model,
)


def _settings(*, saved_model_id: str, saved_protocol: str = "gemini", cloud_model: str) -> Settings:
    return Settings(
        endpoints={"bulut": EndpointConfig(base_url="https://api.example.com/v1")},
        models={
            "ses": ModelConfig(
                provider="openai_compatible",
                endpoint="bulut",
                model=saved_model_id,
                kind="speech",
                speech_protocol=saved_protocol,  # type: ignore[arg-type]
                input_per_million=Decimal(0),
                output_per_million=Decimal(0),
            )
        },
        voice=VoiceConfig(
            tts_backend="cloud",
            tts_cloud_endpoint="bulut",
            tts_cloud_protocol="gemini",
            tts_cloud_model=cloud_model,
        ),
    )


def test_cloud_tts_model_matches_with_and_without_models_prefix() -> None:
    s1 = _settings(
        saved_model_id="gemini-2.5-flash-preview-tts",
        cloud_model="gemini-2.5-flash-preview-tts",
    )
    assert cloud_tts_model(s1) == s1.models["ses"]

    s2 = _settings(
        saved_model_id="models/gemini-2.5-flash-preview-tts",
        cloud_model="gemini-2.5-flash-preview-tts",
    )
    assert cloud_tts_model(s2) == s2.models["ses"]

    s3 = _settings(
        saved_model_id="gemini-2.5-flash-preview-tts",
        cloud_model="models/gemini-2.5-flash-preview-tts",
    )
    assert cloud_tts_model(s3) == s3.models["ses"]


def test_cloud_tts_model_returns_none_when_protocol_differs() -> None:
    settings = _settings(
        saved_model_id="gemini-2.5-flash-preview-tts",
        saved_protocol="openai",
        cloud_model="gemini-2.5-flash-preview-tts",
    )
    assert cloud_tts_model(settings) is None
