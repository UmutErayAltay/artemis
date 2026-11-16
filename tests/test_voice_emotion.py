"""Bulut TTS duygu etiketleri: doğrulama, temizleme, istem, geçmiş ve yedek TTS."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic import ValidationError

from artemis.brain.emotion import emotion_tags_active, strip_emotion_tags
from artemis.brain.models import Completion
from artemis.channels.runner import CommandRunner
from artemis.channels.voice.adapters import FallbackSynthesizer
from artemis.channels.voice.core import PCMChunk
from artemis.config.settings import EndpointConfig, VoiceConfig
from tests.test_loop import make_loop


def test_strip_emotion_tags_behavior() -> None:
    tags = ("[laugh]", "[sigh]")
    assert strip_emotion_tags("Harika [laugh] !", tags) == "Harika!"
    assert strip_emotion_tags("Bak [1] ve [link] [LAUGH].", tags) == "Bak [1] ve [link]."
    assert strip_emotion_tags("Tabii [sigh] hemen.", tags) == "Tabii hemen."
    assert strip_emotion_tags("[laugh] Merhaba", tags) == "Merhaba"
    assert strip_emotion_tags("Harika [laugh] !", ()) == "Harika [laugh] !"


def test_emotion_tags_active_only_for_cloud_with_flag() -> None:
    tags = ("[laugh]", "[sigh]")
    assert emotion_tags_active(VoiceConfig(tts_backend="piper", tts_cloud_emotion_tags=True)) == ()
    assert emotion_tags_active(VoiceConfig(tts_backend="cloud", tts_cloud_emotion_tags=False)) == ()
    assert (
        emotion_tags_active(
            VoiceConfig(
                tts_backend="cloud",
                tts_cloud_emotion_tags=True,
                tts_cloud_emotion_tag_list=tags,
            )
        )
        == tags
    )


async def test_prompt_contains_emotion_tags_only_when_active(tmp_path) -> None:
    endpoints = {"bulut": EndpointConfig(base_url="https://example.com/v1")}

    loop_on, provider_on = make_loop(
        tmp_path / "on",
        [Completion(content="Tamam.")],
        endpoints=endpoints,
        voice=VoiceConfig(
            tts_backend="cloud",
            tts_cloud_endpoint="bulut",
            tts_cloud_emotion_tags=True,
            tts_cloud_emotion_tag_list=("[laugh]", "[whisper]"),
        ),
    )
    await loop_on.run("selam")
    system_on = provider_on.requests[0][0].content
    assert "yalnız şu etiketleri kullanabilirsin: [laugh], [whisper]." in system_on

    loop_off, provider_off = make_loop(
        tmp_path / "off",
        [Completion(content="Tamam.")],
        endpoints=endpoints,
        voice=VoiceConfig(
            tts_backend="cloud",
            tts_cloud_endpoint="bulut",
            tts_cloud_emotion_tags=False,
        ),
    )
    await loop_off.run("selam")
    system_off = provider_off.requests[0][0].content
    assert "yalnız şu etiketleri kullanabilirsin" not in system_off


async def test_runner_strips_result_and_recorded_history(tmp_path) -> None:
    loop, _ = make_loop(
        tmp_path,
        [Completion(content="Tabii [laugh] hemen.")],
        endpoints={"bulut": EndpointConfig(base_url="https://example.com/v1")},
        voice=VoiceConfig(
            tts_backend="cloud",
            tts_cloud_endpoint="bulut",
            tts_cloud_emotion_tags=True,
            tts_cloud_emotion_tag_list=("[laugh]",),
        ),
    )
    runner = CommandRunner(loop)
    result = await runner.run("selam")
    assert result.text == "Tabii hemen."
    assert "[laugh]" not in result.text
    assert result.record is not None
    assert "[laugh]" not in result.record.answer
    assert all("[laugh]" not in msg.content for msg in runner.conversation.render().messages)


async def test_fallback_synthesizer_strips_tags_for_local() -> None:
    seen: dict[str, list[str]] = {"primary": [], "local": []}

    class FailingPrimary:
        async def stream(self, text: str, cancelled: asyncio.Event) -> AsyncIterator[PCMChunk]:
            seen["primary"].append(text)
            if False:
                yield PCMChunk(b"", 16000, 1, 2)
            raise RuntimeError("kota doldu")

        async def close(self) -> None:
            return None

    class FakeLocal:
        async def stream(self, text: str, cancelled: asyncio.Event) -> AsyncIterator[PCMChunk]:
            seen["local"].append(text)
            yield PCMChunk(b"\x01\x00", 16000, 1, 2)

        async def close(self) -> None:
            return None

    synth = FallbackSynthesizer(FailingPrimary(), FakeLocal(), strip_tags=("[laugh]",))
    chunks = [chunk async for chunk in synth.stream("Harika [laugh] !", asyncio.Event())]
    assert len(chunks) == 1
    assert seen["primary"] == ["Harika [laugh] !"]
    assert seen["local"] == ["Harika!"]


@pytest.mark.parametrize(
    "bad_tags",
    [
        ("[Laugh]",),
        ("laugh",),
        ("[laugh]", "[laugh]"),
    ],
)
def test_voice_config_rejects_invalid_emotion_tags(bad_tags: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        VoiceConfig(tts_cloud_emotion_tag_list=bad_tags)
