"""İngilizce ses (ADR 0082): STT 'en' dili ve uyandırma karşılığı çevirisi."""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from artemis.channels.voice.adapters import FasterWhisperRecognizer
from artemis.channels.voice.core import VoiceController
from artemis.config.settings import Settings, VoiceConfig
from artemis.i18n import set_language
from tests.test_voice_cloud_stt import Server, _transcriber
from tests.test_voice_core import (
    FakeMic,
    FakeRecognizer,
    FakeRunner,
    FakeSink,
    FakeTTS,
    FakeVAD,
)

pytest_plugins = ("tests.test_voice_cloud_stt",)


@pytest.fixture(autouse=True)
def _reset_language() -> Iterator[None]:
    set_language("tr")
    try:
        yield
    finally:
        set_language("tr")


def test_settings_accepts_stt_language_en() -> None:
    config = VoiceConfig(stt_language="en")
    assert config.stt_language == "en"
    settings = Settings(voice=config)
    assert settings.voice.stt_language == "en"


@pytest.mark.parametrize(
    ("stt_language", "expected"),
    [("en", "en"), ("tr", "tr"), ("auto", None)],
)
async def test_faster_whisper_passes_configured_stt_language(
    tmp_path: Path, stt_language: str, expected: str | None
) -> None:
    options: list[dict[str, object]] = []

    class Model:
        def transcribe(self, _samples: object, **kwargs: object) -> tuple[list[object], None]:
            options.append(kwargs)
            return ([SimpleNamespace(text="open browser")], None)

    recognizer = FasterWhisperRecognizer(
        VoiceConfig(stt_model=tmp_path, stt_language=stt_language)  # type: ignore[arg-type]
    )
    recognizer._model = Model()
    assert await recognizer.transcribe(b"\x00\x00") == "open browser"
    assert options[0]["language"] == expected


async def test_cloud_transcriber_sends_en_language_field(server: Server) -> None:
    transcriber, _ = _transcriber(server, stt_language="en")
    await transcriber.transcribe(b"\x01\x00" * 160)
    body = server.requests[0][2]
    assert b'name="language"\r\n\r\nen' in body


async def test_wake_ack_translates_with_active_language() -> None:
    spoken: list[str] = []

    class RecordingTTS(FakeTTS):
        async def stream(self, text: str, cancelled):  # type: ignore[no-untyped-def]
            spoken.append(text)
            async for chunk in super().stream(text, cancelled):
                yield chunk

    controller = VoiceController(
        FakeRunner(),
        VoiceConfig(),
        FakeMic(),
        FakeVAD([]),
        FakeRecognizer([]),
        RecordingTTS(),
        FakeSink(),
    )
    try:
        set_language("en")
        await controller._acknowledge_wake()
        assert controller._spoken_text == "I'm listening."

        set_language("tr")
        await controller._acknowledge_wake()
        assert controller._spoken_text == "Seni dinliyorum."

        assert spoken == ["I'm listening.", "Seni dinliyorum."]
    finally:
        await controller.close()
