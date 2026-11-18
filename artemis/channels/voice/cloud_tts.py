"""OpenAI uyumlu ve Gemini bulut konuşma sesi (TTS) adaptörü."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import wave
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
_CHANNELS_RE = re.compile(r"channels\s*=\s*(\d+)", re.IGNORECASE)
_COMPRESSED_MIMES = (
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
    "audio/opus",
    "audio/aac",
    "audio/flac",
    "audio/x-flac",
)
_WAV_MIMES = ("audio/wav", "audio/x-wav")
_COMPRESSED_ERROR = "Bulut konuşma sesi desteklenmeyen sıkıştırılmış biçimde geldi."
_UNSUPPORTED_FORMAT_ERROR = "Bulut konuşma sesi desteklenmeyen ses biçiminde geldi."


def _parse_mime_rate(mime_type: object, default_rate: int) -> int:
    if isinstance(mime_type, str):
        match = _RATE_RE.search(mime_type)
        if match:
            rate = int(match.group(1))
            if rate > 0:
                return rate
    return default_rate


def _parse_mime_channels(mime_type: object, default_channels: int = 1) -> int:
    if isinstance(mime_type, str):
        match = _CHANNELS_RE.search(mime_type)
        if match:
            channels = int(match.group(1))
            if channels > 0:
                return channels
    return default_channels


def _is_mpeg_frame_header(head: bytes) -> bool:
    """4 baytlık geçerli bir MPEG ses çerçeve başlığını doğrular."""
    # Ham PCM de 0xFF baytlarıyla başlayabileceği için yalnızca senkron biti sıkıştırılmış
    # saymaya yetmez; sürüm, katman, bit hızı ve örnekleme alanı da doğrulanır.
    if len(head) < 4:
        return False
    if head[0] != 0xFF or (head[1] & 0xE0) != 0xE0:
        return False
    if ((head[1] >> 3) & 0x03) == 0x01:
        return False
    # Katman 0x00 ayrılmış, 0x03 (Layer I) ise -1 ham PCM örneğiyle (0xFF 0xFF) çakışır;
    # yalnız MP2/MP3 katmanları (0x01, 0x02) MPEG çerçevesi sayılır.
    if ((head[1] >> 1) & 0x03) not in (0x01, 0x02):
        return False
    if (head[2] >> 4) in (0x00, 0x0F):
        return False
    return ((head[2] >> 2) & 0x03) != 0x03


def _detect_audio_format(head: bytes, mime_type: object) -> str:
    """Bayt başlığı ve MIME türüne göre kapsayıcı biçimini (`compressed`, `wav`, `raw`) belirler."""
    mime_lower = mime_type.strip().lower() if isinstance(mime_type, str) else ""
    if any(token in mime_lower for token in _COMPRESSED_MIMES):
        return "compressed"
    if head.startswith((b"ID3", b"OggS", b"fLaC")):
        return "compressed"
    if _is_mpeg_frame_header(head):
        return "compressed"
    base_mime = mime_lower.split(";", 1)[0].strip()
    if (len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE") or (
        base_mime in _WAV_MIMES
    ):
        return "wav"
    return "raw"


def _needs_more_sniff_bytes(buf: bytes) -> bool:
    """İlk baytlar bir kapsayıcı imzasının önekiyse karar için ek bayt bekler."""
    if not buf:
        return True
    if buf.startswith((b"ID3", b"OggS", b"fLaC")):
        return False
    if _is_mpeg_frame_header(buf):
        return False
    if len(buf) < 4 and any(sig.startswith(buf) for sig in (b"RIFF", b"ID3", b"OggS", b"fLaC")):
        return True
    if len(buf) < 12 and buf.startswith(b"RIFF"):
        return True
    if len(buf) == 1 and buf[0] == 0xFF:
        return True
    return len(buf) < 4 and buf[0] == 0xFF and (buf[1] & 0xE0) == 0xE0


def _unwrap_audio(blob: bytes, mime: object, default_rate: int) -> tuple[bytes, int, int]:
    """Ses verisini kapsayıcıdan çözer; sıkıştırılmış veya bozuk veride RuntimeError yükseltir."""
    fmt = _detect_audio_format(blob[:12], mime)
    if fmt == "compressed":
        raise RuntimeError(_COMPRESSED_ERROR)
    if fmt == "wav":
        try:
            with wave.open(io.BytesIO(blob), "rb") as wav:
                sampwidth = wav.getsampwidth()
                if sampwidth != 2:
                    raise RuntimeError(_UNSUPPORTED_FORMAT_ERROR)
                rate = wav.getframerate()
                channels = wav.getnchannels()
                if rate <= 0 or channels <= 0:
                    raise RuntimeError("Bulut konuşma sesi çözülemedi.")
                frames = wav.readframes(wav.getnframes())
        except (wave.Error, EOFError):
            raise RuntimeError("Bulut konuşma sesi çözülemedi.") from None
        return frames, rate, channels
    return blob, _parse_mime_rate(mime, default_rate), _parse_mime_channels(mime, 1)


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
                    async for chunk in self._stream_openai(response, cancelled, content_type):
                        yield chunk
        except httpx.HTTPError:
            raise RuntimeError("Bulut konuşma sesi uca ulaşamadı veya süre doldu.") from None

    def _emit_format_audit(
        self,
        container: str,
        mime: object,
        sample_rate: int,
        channels: int,
        sample_width: int,
    ) -> None:
        # Çözülen ses biçimini gizli içerik (metin/anahtar/ses baytları) olmadan kaydeder.
        mime_str = mime[:64] if isinstance(mime, str) else ""
        self.audit.emit(
            "cloud_tts_format",
            service=self.endpoint_id,
            model=self.config.tts_cloud_model,
            container=container,
            mime=mime_str,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
        )

    async def _stream_openai(
        self,
        response: httpx.Response,
        cancelled: asyncio.Event,
        content_type: str,
    ) -> AsyncIterator[PCMChunk]:
        initial_fmt = _detect_audio_format(b"", content_type)
        if initial_fmt == "compressed":
            self._emit_format_audit("compressed", content_type, 0, 0, 0)
            raise RuntimeError(_COMPRESSED_ERROR)

        mode: str | None = "wav" if initial_fmt == "wav" else None
        sniff_buf = b""
        wav_parts: list[bytes] = []
        rate = self.config.tts_cloud_sample_rate
        channels = 1
        frame_bytes = 2
        pending = b""
        yielded = False

        async for chunk in response.aiter_bytes():
            if cancelled.is_set():
                return
            if not chunk:
                continue
            if mode == "wav":
                wav_parts.append(chunk)
                continue
            if mode is None:
                sniff_buf += chunk
                if _needs_more_sniff_bytes(sniff_buf):
                    continue
                fmt = _detect_audio_format(sniff_buf[:12], content_type)
                if fmt == "compressed":
                    self._emit_format_audit("compressed", content_type, 0, 0, 0)
                    raise RuntimeError(_COMPRESSED_ERROR)
                if fmt == "wav":
                    mode = "wav"
                    wav_parts.append(sniff_buf)
                    sniff_buf = b""
                    continue
                mode = "raw"
                rate = _parse_mime_rate(content_type, self.config.tts_cloud_sample_rate)
                channels = _parse_mime_channels(content_type, 1)
                frame_bytes = channels * 2
                self._emit_format_audit("raw", content_type, rate, channels, 2)
                chunk = sniff_buf
                sniff_buf = b""
            data = pending + chunk
            # 16-bit PCM çerçeveleri (kanal * 2 bayt) tam olmalıdır; kalan baytlar taşınır.
            aligned_len = len(data) - (len(data) % frame_bytes)
            if aligned_len:
                yielded = True
                yield PCMChunk(data[:aligned_len], rate, channels, 2)
            pending = data[aligned_len:]

        if cancelled.is_set():
            return
        if mode is None:
            if not sniff_buf:
                raise RuntimeError("Bulut konuşma sesi boş döndü.")
            fmt = _detect_audio_format(sniff_buf[:12], content_type)
            if fmt == "compressed":
                self._emit_format_audit("compressed", content_type, 0, 0, 0)
                raise RuntimeError(_COMPRESSED_ERROR)
            if fmt == "wav":
                mode = "wav"
                wav_parts.append(sniff_buf)
            else:
                rate = _parse_mime_rate(content_type, self.config.tts_cloud_sample_rate)
                channels = _parse_mime_channels(content_type, 1)
                frame_bytes = channels * 2
                aligned_len = len(sniff_buf) - (len(sniff_buf) % frame_bytes)
                if aligned_len:
                    self._emit_format_audit("raw", content_type, rate, channels, 2)
                    yielded = True
                    yield PCMChunk(sniff_buf[:aligned_len], rate, channels, 2)
        if mode == "wav":
            body = b"".join(wav_parts)
            if not body:
                raise RuntimeError("Bulut konuşma sesi boş döndü.")
            if _detect_audio_format(body[:12], content_type) == "compressed":
                self._emit_format_audit("compressed", content_type, 0, 0, 0)
                raise RuntimeError(_COMPRESSED_ERROR)
            pcm, rate, channels = _unwrap_audio(
                body, content_type, self.config.tts_cloud_sample_rate
            )
            self._emit_format_audit("wav", content_type, rate, channels, 2)
            frame_bytes = channels * 2
            aligned_len = len(pcm) - (len(pcm) % frame_bytes)
            if aligned_len:
                yielded = True
                yield PCMChunk(pcm[:aligned_len], rate, channels, 2)
        if not yielded:
            raise RuntimeError("Bulut konuşma sesi boş döndü.")

    async def _stream_gemini(
        self, response: httpx.Response, cancelled: asyncio.Event
    ) -> AsyncIterator[PCMChunk]:
        pending = b""
        yielded = False
        audited = False
        current_format: tuple[int, int] | None = None
        async for line in response.aiter_lines():
            if cancelled.is_set():
                return
            if not line.startswith("data:"):
                continue
            raw_json = line[5:].strip()
            if not raw_json or raw_json == "[DONE]":
                continue
            for raw_audio, mime in _extract_gemini_parts(raw_json):
                if cancelled.is_set():
                    return
                if not raw_audio:
                    continue
                fmt = _detect_audio_format(raw_audio[:12], mime)
                if fmt == "compressed":
                    if not audited:
                        audited = True
                        self._emit_format_audit("compressed", mime, 0, 0, 0)
                    raise RuntimeError(_COMPRESSED_ERROR)
                pcm, rate, channels = _unwrap_audio(
                    raw_audio, mime, self.config.tts_cloud_sample_rate
                )
                if not audited:
                    audited = True
                    self._emit_format_audit(fmt, mime, rate, channels, 2)
                if not pcm:
                    continue
                if current_format is not None and (rate, channels) != current_format:
                    # Örnekleme hızı veya kanal sayısı değiştiyse yarım çerçeve yeni biçime uymaz.
                    pending = b""
                current_format = (rate, channels)
                frame_bytes = channels * 2
                data = pending + pcm
                aligned_len = len(data) - (len(data) % frame_bytes)
                if aligned_len:
                    yielded = True
                    yield PCMChunk(data[:aligned_len], rate, channels, 2)
                pending = data[aligned_len:]
        if cancelled.is_set():
            return
        if not yielded:
            raise RuntimeError("Bulut konuşma sesi boş döndü.")

    async def close(self) -> None:
        return None
