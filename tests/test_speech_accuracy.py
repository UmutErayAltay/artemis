"""STT ölçüm hattı gerçek dosya biçimiyle ama sahte tanıyıcıyla sınanır."""

import wave
from pathlib import Path

import pytest
from pydantic import ValidationError

from artemis.config.settings import VoiceConfig
from bench.speech_accuracy import (
    Corpus,
    CorpusItem,
    app_mentions,
    edit_distance,
    load_corpus,
    measure,
    normalize,
    read_audio,
)


class FakeRecognizer:
    def __init__(self, predictions: list[str]) -> None:
        self.predictions = iter(predictions)
        self.calls: list[bytes] = []

    async def transcribe(self, pcm: bytes) -> str:
        self.calls.append(pcm)
        return next(self.predictions)


def make_wav(path: Path, pcm: bytes = b"\x00\x00" * 160) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(pcm)


def test_voice_stt_defaults_and_bounds() -> None:
    config = VoiceConfig()
    assert (config.stt_language, config.stt_beam_size, config.stt_partial_beam_size) == (
        "tr", 5, 1
    )
    assert config.stt_hotwords == ()
    assert VoiceConfig(stt_language="auto", stt_hotwords=("artemis", "Visual Studio Code"))
    for values in (
        {"stt_beam_size": 0},
        {"stt_beam_size": 11},
        {"stt_partial_beam_size": 0},
        {"stt_language": "en"},
        {"stt_hotwords": ["bad\nword"]},
        {"stt_hotwords": ["x" * 65]},
        {"stt_hotwords": ["ok"] * 101},
    ):
        with pytest.raises(ValidationError):
            VoiceConfig.model_validate(values)


@pytest.mark.asyncio
async def test_measure_reads_real_wav_and_reports_metrics_without_text(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    make_wav(audio)
    corpus = Corpus(
        apps=["Visual Studio Code", "Discord"],
        items=[
            CorpusItem(
                id="mixed", category="mixed_apps", speaker_id="s1", split="dev",
                audio="speech.wav", transcript="İlk dosyayı Visual Studio Code'da aç",
                expected_apps=["Visual Studio Code"],
            ),
            CorpusItem(
                id="quiet", category="silence", speaker_id="s2", split="dev",
                audio="speech.wav", transcript="",
            ),
        ],
    )
    recognizer = FakeRecognizer(["İlk dosyayı Visual Studio Code'da aç", ""])
    report = await measure(
        corpus, tmp_path, recognizer, mode="sahte", model_metadata={"profile": "fake"}
    )
    assert report["totals"]["wer"] == 0
    assert report["totals"]["cer"] == 0
    assert report["totals"]["app_name_correct"] == 1
    assert report["totals"]["silence_hallucinations"] == 0
    assert report["category_breakdown"]["silence"]["wer"] is None
    assert report["items"][1]["app_names_correct"] is None
    assert "transcript" not in str(report)
    assert len(recognizer.calls) == 2


@pytest.mark.asyncio
async def test_empty_reference_reports_hallucination_and_undefined_rates(tmp_path: Path) -> None:
    make_wav(tmp_path / "quiet.wav")
    corpus = Corpus(
        apps=["Discord"],
        items=[CorpusItem(id="quiet", category="silence", audio="quiet.wav", transcript="")],
    )
    report = await measure(
        corpus, tmp_path, FakeRecognizer(["hello"]), mode="sahte", model_metadata={}
    )
    assert report["totals"]["wer"] is None
    assert report["totals"]["cer"] is None
    assert report["totals"]["app_name_accuracy"] is None
    assert report["totals"]["silence_hallucinations"] == 1


@pytest.mark.asyncio
async def test_corpus_path_traversal_is_rejected_before_transcription(tmp_path: Path) -> None:
    corpus_path = tmp_path / "corpus.yaml"
    corpus_path.write_text(
        "apps: [Discord]\nitems:\n  - id: bad\n    category: speech\n"
        "    audio: ../outside.wav\n    transcript: test\n",
        encoding="utf-8",
    )
    corpus, root = load_corpus(corpus_path)
    recognizer = FakeRecognizer(["must not run"])
    with pytest.raises(ValueError):
        await measure(corpus, root, recognizer, mode="sahte", model_metadata={})
    assert recognizer.calls == []


def test_turkish_normalization_handles_case_and_punctuation() -> None:
    assert normalize("İSTANBUL'da, IŞIK! ğüşöç") == "istanbulda ışık ğüşöç"
    assert normalize("I\u0307stanbul") == "istanbul"


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("DISCORD'u aç", "Discord"),
        ("Chrome'da ara", "Chrome"),
        ("Steam'i başlat", "Steam"),
        ("Visual Studio Code'u aç", "Visual Studio Code"),
    ],
)
def test_app_match_handles_turkish_suffix_and_case(phrase: str, expected: str) -> None:
    assert app_mentions(
        phrase, ["Discord", "Chrome", "Steam", "Visual Studio Code"]
    ) == {expected}


def test_app_match_respects_word_boundaries() -> None:
    assert not app_mentions("ChromeX ve Steamworks", ["Chrome", "Steam"])


@pytest.mark.asyncio
async def test_edit_metrics_count_substitution_deletion_and_insertion(tmp_path: Path) -> None:
    make_wav(tmp_path / "words.wav")
    corpus = Corpus(
        apps=["Discord", "Chrome"],
        items=[CorpusItem(
            id="edits", category="mixed", audio="words.wav",
            transcript="alpha beta gamma delta", expected_apps=["Chrome"],
        )],
    )
    report = await measure(
        corpus, tmp_path, FakeRecognizer(["alpha DISCORD'u delta extra"]),
        mode="sahte", model_metadata={},
    )
    # beta -> Discord substitution, gamma deletion, extra insertion.
    assert report["items"][0]["word_errors"] == 3
    assert report["totals"]["wer"] == 3 / 4
    assert report["items"][0]["unexpected_app_names"] == ["discord"]
    assert report["items"][0]["app_names_correct"] is False
    assert report["totals"]["cer"] == edit_distance(
        list("alpha beta gamma delta"), list("alpha discordu delta extra")
    ) / len("alpha beta gamma delta")


def test_invalid_or_truncated_wav_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "truncated.wav"
    make_wav(path)
    path.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(ValueError, match="eksik veya bozuk"):
        read_audio(path)


def test_extra_hotwords_are_validated_and_do_not_touch_config() -> None:
    from artemis.config.settings import Settings
    from bench.speech_accuracy import with_extra_hotwords

    base = Settings(voice=VoiceConfig(stt_hotwords=("Artemis",)))
    assert with_extra_hotwords(base, []) is base
    extended = with_extra_hotwords(base, ["Brave", "Artemis"])
    assert extended.voice.stt_hotwords == ("Artemis", "Brave")
    assert base.voice.stt_hotwords == ("Artemis",)
    with pytest.raises(ValidationError):
        with_extra_hotwords(base, ["x" * 65])
