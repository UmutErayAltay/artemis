"""Yerel WAV kayıtlarıyla STT doğruluk ölçümü; araç veya LLM çalıştırmaz."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import re
import statistics
import sys
import time
import unicodedata
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artemis.channels.voice.adapters import FasterWhisperRecognizer
from artemis.config.settings import Settings, VoiceConfig, load_settings
from artemis.safety.paths import safe_join


class CorpusItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=40)
    speaker_id: str = Field(default="unspecified", min_length=1, max_length=80)
    split: str = Field(default="unspecified", min_length=1, max_length=40)
    audio: str = Field(min_length=1, max_length=240)
    transcript: str = Field(max_length=2000)
    expected_apps: list[str] = Field(default_factory=list, max_length=20)


class Corpus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    apps: list[str] = Field(min_length=1, max_length=100)
    items: list[CorpusItem] = Field(min_length=1, max_length=500)


class Recognizer(Protocol):
    async def transcribe(self, pcm: bytes) -> str: ...


def normalize(text: str) -> str:
    """Türkçe büyük harfleri doğru küçültüp noktalama işaretlerini kaldırır."""
    text = unicodedata.normalize("NFKC", text).replace("İ", "i").replace("I", "ı").casefold()
    text = unicodedata.normalize("NFKC", text)
    return " ".join("".join(
        "" if char in {"'", "’", "ʼ"} else (
            " " if unicodedata.category(char).startswith("P") else char
        )
        for char in text
    ).split())


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, ref_token in enumerate(reference, 1):
        current = [row]
        for col, hyp_token in enumerate(hypothesis, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[col] + 1,
                    previous[col - 1] + (ref_token != hyp_token),
                )
            )
        previous = current
    return previous[-1]


def app_mentions(text: str, vocabulary: Sequence[str]) -> set[str]:
    # Uygulama adı karşılaştırmasında büyük/küçük Türkçe i farkı etkisizdir.
    def fold_app_text(value: str) -> str:
        value = unicodedata.normalize("NFKC", value)
        value = value.replace("İ", "i").replace("I", "ı").casefold().replace("ı", "i")
        return " ".join(value.split())

    folded_text = fold_app_text(text)
    suffix = r"(?:['’](?:y?[ıiuüae]|[dt](?:a|e)|[dt](?:an|en)|n(?:ın|in|un|ün)))?"
    found: set[str] = set()
    for app in vocabulary:
        folded_app = fold_app_text(app)
        pattern = rf"(?<!\w){re.escape(folded_app)}{suffix}(?!\w)"
        if re.search(pattern, folded_text):
            found.add(app)
    return found


def load_corpus(path: Path) -> tuple[Corpus, Path]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    corpus = Corpus.model_validate(data)
    ids = [item.id for item in corpus.items]
    apps = {normalize(app) for app in corpus.apps}
    if len(set(ids)) != len(ids):
        raise ValueError("Corpus item id değerleri benzersiz olmalı")
    if len(apps) != len(corpus.apps):
        raise ValueError("Corpus apps yinelenen uygulama adları içeremez")
    known_apps = {normalize(app) for app in corpus.apps}
    for item in corpus.items:
        if any(normalize(app) not in known_apps for app in item.expected_apps):
            raise ValueError(f"{item.id}: expected_apps corpus.apps içinde olmalı")
    return corpus, path.resolve().parent


def read_audio(path: Path) -> bytes:
    try:
        with wave.open(str(path), "rb") as audio:
            if audio.getnframes() > 16_000 * 120:
                raise ValueError(f"WAV en fazla 120 saniye olabilir: {path.name}")
            if (
                audio.getnchannels() != 1
                or audio.getframerate() != 16000
                or audio.getsampwidth() != 2
                or audio.getcomptype() != "NONE"
            ):
                raise ValueError(f"WAV 16 kHz mono PCM16 olmalı: {path.name}")
            frames = audio.getnframes()
            pcm = audio.readframes(frames)
            if len(pcm) != frames * audio.getnchannels() * audio.getsampwidth():
                raise ValueError(f"WAV eksik veya bozuk: {path.name}")
            return pcm
    except (wave.Error, EOFError) as error:
        raise ValueError(f"WAV okunamadı: {path.name}") from error


def _rate(errors: int, units: int) -> float | None:
    return errors / units if units else None


async def measure(
    corpus: Corpus,
    corpus_root: Path,
    recognizer: Recognizer,
    *,
    mode: str,
    model_metadata: dict[str, Any],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total_word_errors = total_ref_words = total_char_errors = total_ref_chars = 0
    app_correct = app_samples = silence_total = silence_hallucinations = 0
    latencies: list[float] = []
    groups: dict[str, dict[str, int]] = {}
    for item in corpus.items:
        audio_path = safe_join(corpus_root, item.audio, allow_symlinks=False)
        pcm = read_audio(audio_path)
        started = time.perf_counter()
        prediction = await recognizer.transcribe(pcm)
        elapsed = time.perf_counter() - started
        latencies.append(elapsed)
        ref_words = normalize(item.transcript).split()
        hyp_words = normalize(prediction).split()
        ref_chars = " ".join(ref_words)
        hyp_chars = " ".join(hyp_words)
        word_errors = edit_distance(ref_words, hyp_words)
        char_errors = edit_distance(list(ref_chars), list(hyp_chars))
        expected = {normalize(app) for app in item.expected_apps}
        found = {normalize(app) for app in app_mentions(prediction, corpus.apps)}
        annotated_apps = bool(item.expected_apps)
        correct = expected == found
        is_silence = item.category.casefold() == "silence" or not ref_words
        hallucinated = is_silence and bool(hyp_words)
        total_word_errors += word_errors
        total_ref_words += len(ref_words)
        total_char_errors += char_errors
        total_ref_chars += len(ref_chars)
        app_correct += int(correct and annotated_apps)
        app_samples += int(annotated_apps)
        silence_total += int(is_silence)
        silence_hallucinations += int(hallucinated)
        for label in (item.category,):
            group = groups.setdefault(label, {
                "items": 0, "word_errors": 0, "reference_words": 0,
                "char_errors": 0, "reference_chars": 0, "apps_correct": 0,
                "app_samples": 0,
                "silence_samples": 0, "silence_hallucinations": 0,
            })
            group["items"] += 1
            group["word_errors"] += word_errors
            group["reference_words"] += len(ref_words)
            group["char_errors"] += char_errors
            group["reference_chars"] += len(ref_chars)
            group["apps_correct"] += int(correct and annotated_apps)
            group["app_samples"] += int(annotated_apps)
            group["silence_samples"] += int(is_silence)
            group["silence_hallucinations"] += int(hallucinated)
        rows.append({
            "id": item.id,
            "category": item.category,
            "speaker_id": item.speaker_id,
            "split": item.split,
            "wer": _rate(word_errors, len(ref_words)),
            "cer": _rate(char_errors, len(ref_chars)),
            "word_errors": word_errors,
            "reference_words": len(ref_words),
            "app_names_correct": correct if annotated_apps else None,
            "unexpected_app_names": sorted(app for app in found if app not in expected),
            "silence_hallucination": hallucinated,
            "latency_ms": round(elapsed * 1000, 2),
        })
    ordered = sorted(latencies)
    p90_index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * 0.9) - 1))
    breakdown = {
        name: {
            "items": values["items"],
            "wer": _rate(values["word_errors"], values["reference_words"]),
            "cer": _rate(values["char_errors"], values["reference_chars"]),
            "app_name_accuracy": (
                values["apps_correct"] / values["app_samples"]
                if values["app_samples"] else None
            ),
            "app_name_samples": values["app_samples"],
            "silence_samples": values["silence_samples"],
            "silence_hallucinations": values["silence_hallucinations"],
        }
        for name, values in sorted(groups.items())
    }
    return {
        "mode": mode,
        "model_profile": model_metadata,
        "corpus_items": len(rows),
        "items": rows,
        "category_breakdown": breakdown,
        "speaker_count": len({item.speaker_id for item in corpus.items}),
        "split_counts": {
            split: sum(item.split == split for item in corpus.items)
            for split in sorted({item.split for item in corpus.items})
        },
        "totals": {
            "wer": _rate(total_word_errors, total_ref_words),
            "cer": _rate(total_char_errors, total_ref_chars),
            "word_errors": total_word_errors,
            "reference_words": total_ref_words,
            "app_name_accuracy": app_correct / app_samples if app_samples else None,
            "app_name_samples": app_samples,
            "app_name_correct": app_correct,
            "silence_samples": silence_total,
            "silence_hallucinations": silence_hallucinations,
            "latency_ms": {
                "median": round(statistics.median(latencies) * 1000, 2) if latencies else None,
                "p90": round(ordered[p90_index] * 1000, 2) if ordered else None,
            },
        },
        "privacy": "Transcripts and audio are omitted; local files stay on this machine.",
        "metric_scope": (
            "Transcript/app-name match only; this does not measure intent or tool execution."
        ),
    }


class EchoRecognizer:
    """Sahte kipte WAV başına referansı döndürerek ölçüm hattını sınar."""

    def __init__(self, items: Sequence[CorpusItem]) -> None:
        self._items = iter(items)

    async def transcribe(self, _pcm: bytes) -> str:
        return next(self._items).transcript


def metadata(settings: Settings) -> dict[str, Any]:
    return {
        "stt_model": str(settings.voice.stt_model) if settings.voice.stt_model else None,
        "stt_device": settings.voice.stt_device,
        "stt_compute_type": settings.voice.stt_compute_type,
        "stt_language": settings.voice.stt_language,
        "stt_beam_size": settings.voice.stt_beam_size,
        "stt_partial_beam_size": settings.voice.stt_partial_beam_size,
        "stt_hotwords_count": len(settings.voice.stt_hotwords),
        # Ölçülen ipuçları içerik değil uygulama adıdır; A/B karşılaştırması için raporlanır.
        "stt_hotwords": list(settings.voice.stt_hotwords),
        "system": platform.platform(),
    }


def with_extra_hotwords(settings: Settings, extra: Sequence[str]) -> Settings:
    """A/B ölçümü için config'i değiştirmeden ek STT ipucu verir; sınırlar yine doğrulanır."""
    if not extra:
        return settings
    words = tuple(dict.fromkeys((*settings.voice.stt_hotwords, *extra)))
    voice = VoiceConfig.model_validate({**settings.voice.model_dump(), "stt_hotwords": words})
    return settings.model_copy(update={"voice": voice})


async def run(args: argparse.Namespace) -> dict[str, Any]:
    corpus, root = load_corpus(args.corpus)
    # Eksik örnek varsa kısmi skor raporlanmaz.
    paths = [safe_join(root, item.audio, allow_symlinks=False) for item in corpus.items]
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Corpus WAV dosyaları eksik ({len(missing)}): {', '.join(missing)}"
        )
    for path in paths:
        read_audio(path)
    if args.sahte:
        fake_settings = Settings()
        recognizer: Recognizer = EchoRecognizer(corpus.items)
        model = metadata(fake_settings)
    else:
        settings = with_extra_hotwords(load_settings(args.config), args.ek_hotword)
        if settings.voice.stt_model is None:
            raise ValueError("Canlı benchmark için voice.stt_model gerekli.")
        recognizer = FasterWhisperRecognizer(settings.voice)
        await asyncio.to_thread(recognizer.prepare)
        model = metadata(settings)
    return await measure(
        corpus, root, recognizer,
        mode="sahte" if args.sahte else "canli",
        model_metadata=model,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Yerel WAV konuşma doğruluğu benchmarkı")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--sahte", action="store_true")
    modes.add_argument("--canli", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument(
        "--ek-hotword", action="append", default=[], metavar="AD",
        help="Yalnız bu ölçümde STT ipucuna eklenir (ör. uygulama adı); tekrarlanabilir",
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as error:
        print(json.dumps(
            {"mode": "canli" if args.canli else "sahte", "failure": str(error)},
            ensure_ascii=False,
            indent=2,
        ))
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
