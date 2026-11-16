"""OpenAI uyumlu ve Gemini bulut konuşma sesi (TTS) adaptörü."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from artemis.brain.providers import stored_endpoint_key
from artemis.channels.voice.contracts import PCMChunk
from artemis.config.settings import EndpointConfig, VoiceConfig
from artemis.safety.audit import AuditLog

CLOUD_TTS_TIMEOUT_SECONDS = 20.0
CLOUD_TTS_CONNECT_TIMEOUT_SECONDS = 5.0
_RATE_RE = re.compile(r"rate\s*=\s*(\d+)", re.IGNORECASE)


def _parse_mime_rate(mime_type: object, default_rate: int) -> int:
    if isinstance(mime_type, str):
        match = _RATE_RE.search(mime_type)
        if match:
            rate = int(match.group(1))
            if rate > 0:
                return rate
    return default_rate


def _extract_gemini_parts(raw_json: str) -> list[tuple[bytes, object]]:
    """Tek SSE `data:` satırından (ham PCM baytları, mimeType) çiftlerini çözer."""
    try:
        event = json.loads(raw_json)
    except ValueError:
        raise RuntimeError("Bulut konuşma yanıtı çözülemedi.") from None
    if not isinstance(event, dict):
        raise RuntimeError("Bulut konuşma yanıtı çözülemedi.")
    candidates = event.get("candidates")
    if not isinstance(candidates, list):
        return []
    extracted: list[tuple[bytes, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData")
            if not isinstance(inline, dict):
                continue
            b64_data = inline.get("data")
            if not isinstance(b64_data, str) or not b64_data:
                continue
            try:
                raw_pcm = base64.b64decode(b64_data, validate=True)
            except ValueError:
                raise RuntimeError("Bulut konuşma yanıtı çözülemedi.") from None
            extracted.append((raw_pcm, inline.get("mimeType")))
    return extracted


class CloudSpeechSynthesizer:
    """OpenAI `/audio/speech` ve Gemini `:streamGenerateContent?alt=sse` PCM akış adaptörü.

    Hata metnine URL, yanıt gövdesi veya anahtar konmaz; istisna zinciri de taşınmaz
    (sır sızdırmasın). Yönlendirme takip edilmez: anahtar başka sunucuya taşınamaz.
    """

    def __init__(
        self,
        config: VoiceConfig,
        endpoint_id: str,
        endpoint: EndpointConfig,
        audit: AuditLog,
    ) -> None:
        if not config.allow_cloud_tts_text:
            raise ValueError("Bulut konuşma sesi için allow_cloud_tts_text açık olmalı.")
        if not config.tts_cloud_model.strip():
            raise ValueError("Bulut konuşma sesi için tts_cloud_model açıkça ayarlanmalı.")
        gemini = config.tts_cloud_protocol == "gemini"
        if (
            endpoint.api_key
            and (gemini or not config.tts_cloud_model.endswith(":free"))
            and not config.tts_cloud_allow_paid
        ):
            note = " (Google bu anahtarı ücretlendirebilir)" if gemini else ""
            raise ValueError(
                f"Ücretli ses modeli için tts_cloud_allow_paid açık olmalı{note}."
            )
        self.config = config
        self.endpoint_id = endpoint_id
        self.endpoint = endpoint
        self.audit = audit

    async def stream(self, text: str, cancelled: asyncio.Event) -> AsyncIterator[PCMChunk]:
        if cancelled.is_set():
            return
        gemini = self.config.tts_cloud_protocol == "gemini"
        headers: dict[str, str] = {}
        if self.endpoint.api_key:
            key = stored_endpoint_key(self.endpoint_id)
            if not key:
                raise RuntimeError("Bulut konuşma sesi için API anahtarı kayıtlı değil.")
            if gemini:
                headers["x-goog-api-key"] = key
            else:
                headers["Authorization"] = f"Bearer {key}"

        # Gizlilik denetimi metni göndermeden önce yazılır; ham metin kaydedilmez.
        self.audit.emit(
            "cloud_tts_transfer",
            service=self.endpoint_id,
            model=self.config.tts_cloud_model,
            content_kind="assistant_response_text",
            allowed=True,
        )

        if gemini:
            url = (
                f"{self.endpoint.base_url}/models/"
                f"{self.config.tts_cloud_model}:streamGenerateContent?alt=sse"
            )
            generation_config: dict[str, Any] = {"responseModalities": ["AUDIO"]}
            if self.config.tts_cloud_voice:
                generation_config["speechConfig"] = {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": self.config.tts_cloud_voice,
                        }
                    }
                }
            payload: dict[str, Any] = {
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": generation_config,
            }
        else:
            url = f"{self.endpoint.base_url}/audio/speech"
            payload = {
                "model": self.config.tts_cloud_model,
                "input": text,
                "response_format": "pcm",
            }
            if self.config.tts_cloud_voice:
                payload["voice"] = self.config.tts_cloud_voice

        timeout = httpx.Timeout(
            CLOUD_TTS_TIMEOUT_SECONDS,
            connect=CLOUD_TTS_CONNECT_TIMEOUT_SECONDS,
        )
        try:
            async with (
                httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client,
                client.stream("POST", url, json=payload, headers=headers) as response,
            ):
                if response.status_code != 200:
                    if response.status_code == 429:
                        raise RuntimeError(
                            "Bulut konuşma sesi hız/kota sınırına takıldı (HTTP 429)."
                        )
                    raise RuntimeError(
                        f"Bulut konuşma sesi başarısız (HTTP {response.status_code})."
                    )
                content_type = response.headers.get("content-type", "").lower()
                if gemini:
                    if content_type and not content_type.startswith("text/event-stream"):
                        raise RuntimeError("Bulut konuşma yanıtı ses değil.")
                    async for chunk in self._stream_gemini(response, cancelled):
                        yield chunk
                else:
                    if not content_type.startswith("audio/"):
                        raise RuntimeError("Bulut konuşma yanıtı ses değil.")
                    async for chunk in self._stream_openai(response, cancelled):
                        yield chunk
        except httpx.HTTPError:
            raise RuntimeError("Bulut konuşma sesi uca ulaşamadı veya süre doldu.") from None

    async def _stream_openai(
        self, response: httpx.Response, cancelled: asyncio.Event
    ) -> AsyncIterator[PCMChunk]:
        pending = b""
        yielded = False
        async for chunk in response.aiter_bytes():
            if cancelled.is_set():
                return
            if not chunk:
                continue
            data = pending + chunk
            # 16-bit PCM örnekleri 2 bayttır; tek kalan son bayt sonraki parçaya taşınır.
            even_len = len(data) - (len(data) % 2)
            if even_len:
                yielded = True
                yield PCMChunk(data[:even_len], self.config.tts_cloud_sample_rate, 1, 2)
            pending = data[even_len:]
        if cancelled.is_set():
            return
        if not yielded:
            raise RuntimeError("Bulut konuşma sesi boş döndü.")

    async def _stream_gemini(
        self, response: httpx.Response, cancelled: asyncio.Event
    ) -> AsyncIterator[PCMChunk]:
        pending = b""
        yielded = False
        async for line in response.aiter_lines():
            if cancelled.is_set():
                return
            if not line.startswith("data:"):
                continue
            raw_json = line[5:].strip()
            if not raw_json or raw_json == "[DONE]":
                continue
            for raw_pcm, mime in _extract_gemini_parts(raw_json):
                if cancelled.is_set():
                    return
                if not raw_pcm:
                    continue
                rate = _parse_mime_rate(mime, self.config.tts_cloud_sample_rate)
                data = pending + raw_pcm
                even_len = len(data) - (len(data) % 2)
                if even_len:
                    yielded = True
                    yield PCMChunk(data[:even_len], rate, 1, 2)
                pending = data[even_len:]
        if cancelled.is_set():
            return
        if not yielded:
            raise RuntimeError("Bulut konuşma sesi boş döndü.")

    async def close(self) -> None:
        return None
