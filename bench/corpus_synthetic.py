"""Açık kaynak/ücretsiz TTS ile etiketli sentetik komut corpus'u kurar (ADR 0045).

Her ses bir "konuşmacı"dır; sesler ve uygulama adları ``dev``/``test`` arasında ayrıktır.
Her örnek temiz hâliyle ve sabit tohumla seçilmiş bir bozmayla (gürültü, yankı, hız,
telefon bandı) yazılır. Sentetik ses gerçek konuşmadan temizdir: buradaki WER ayar
karşılaştırması ve gerileme içindir, gerçek kullanıcı doğruluğu kanıtı değildir.

Sesler: Piper (yerel, ``--piper model.onnx``) ve isteğe bağlı Edge TTS (``--edge SES``,
metin Microsoft'a gider; yalnız bu sentetik şablon metni gider, ``--bulut-tts-izni`` şart).
Çıktı depo dışına yazılır.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.eval_audio import (
    CONDITIONS,
    DEFAULT_OUTPUT_ROOT,
    Signal,
    apply_condition,
    ensure_outside_repo,
    resample,
    to_float,
    write_wav,
)

TEMPLATES_PATH = Path(__file__).with_name("synthetic_commands.yaml")
AUGMENTATIONS = tuple(condition for condition in CONDITIONS if condition != "clean")


class App(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    spoken: list[str] = Field(min_length=1)


class Template(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    text: str
    wake: bool
    intent: str
    target: bool = False
    negation: bool = False
    correction: bool = False
    category: str


class Templates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    wake_word: str
    apps: dict[str, list[App]]
    templates: list[Template]
    phrases: list[Template] = Field(default_factory=list)


@dataclass(frozen=True)
class Utterance:
    """Seslendirilecek metin ve ölçümün referansı/etiketleri."""

    id: str
    spoken: str
    transcript: str
    split: str
    labels: dict[str, Any]


Synthesize = Callable[[str], Awaitable[tuple[Signal, int]]]


@dataclass(frozen=True)
class Voice:
    id: str
    split: str
    synthesize: Synthesize


def load_templates(path: Path = TEMPLATES_PATH) -> Templates:
    templates = Templates.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    dev = {app.name for app in templates.apps.get("dev", [])}
    test = {app.name for app in templates.apps.get("test", [])}
    if not dev or not test or dev & test:
        raise ValueError("dev ve test uygulama listeleri dolu ve ayrık olmalı")
    return templates


def utterances(templates: Templates, seed: int = 0) -> list[Utterance]:
    """Her ayrım için şablon × uygulama; düzeltmede vazgeçilen uygulama aynı ayrımdandır."""
    rng = random.Random(seed)
    wake = templates.wake_word
    result: list[Utterance] = []
    for split in ("dev", "test"):
        apps = templates.apps[split]
        for template in templates.templates:
            for index, app in enumerate(apps):
                other = apps[(index + 1) % len(apps)]
                spoken_app = rng.choice(app.spoken)
                spoken_other = rng.choice(other.spoken)
                result.append(Utterance(
                    id=f"{split}_{template.id}_{index}",
                    spoken=template.text.format(wake=wake, app=spoken_app, other=spoken_other),
                    transcript=template.text.format(wake=wake, app=app.name, other=other.name),
                    split=split,
                    labels={
                        "category": template.category, "wake": template.wake,
                        "intent": template.intent,
                        "target_app": app.name if template.target else None,
                        "expected_apps": sorted(
                            {app.name, other.name} if template.correction else {app.name}
                        ),
                        "negation": template.negation, "correction": template.correction,
                    },
                ))
        for phrase in templates.phrases:
            text = phrase.text.format(wake=wake)
            result.append(Utterance(
                id=f"{split}_{phrase.id}", spoken=text, transcript=text, split=split,
                labels={
                    "category": phrase.category, "wake": phrase.wake, "intent": phrase.intent,
                    "target_app": None, "expected_apps": [], "negation": False,
                    "correction": False,
                },
            ))
    return result


def assign_splits(voice_ids: Sequence[str]) -> dict[str, str]:
    """Sesler sıralanıp dönüşümlü dağıtılır; en az bir dev ve bir test sesi şarttır."""
    ordered = sorted(voice_ids)
    if len(ordered) < 2:
        raise ValueError("Konuşmacı ayrımı için en az iki ses gerekli")
    return {voice: ("dev" if index % 2 == 0 else "test") for index, voice in enumerate(ordered)}


async def build(
    templates: Templates,
    voices: Sequence[Voice],
    output: Path,
    *,
    conditions: Sequence[str] = AUGMENTATIONS,
    seed: int = 0,
) -> dict[str, Any]:
    output = ensure_outside_repo(output)
    rng = random.Random(seed)
    items: list[dict[str, Any]] = []
    failures: list[str] = []
    for voice in voices:
        for utterance in (u for u in utterances(templates, seed) if u.split == voice.split):
            try:
                signal, rate = await voice.synthesize(utterance.spoken)
            except (OSError, RuntimeError, ValueError) as error:
                failures.append(f"{voice.id}/{utterance.id}: {type(error).__name__}")
                continue
            clean = resample(signal, rate)
            for condition in ("clean", rng.choice(list(conditions))):
                item_id = f"{voice.id}_{utterance.id}_{condition}"
                audio = apply_condition(condition, clean, rng.randrange(2**31))
                write_wav(output / "audio" / f"{item_id}.wav", audio)
                items.append({
                    "id": item_id, "category": utterance.labels["category"],
                    "speaker_id": voice.id, "split": voice.split,
                    "audio": f"audio/{item_id}.wav", "transcript": utterance.transcript,
                    "expected_apps": utterance.labels["expected_apps"],
                    "source": "synthetic", "condition": condition,
                    "wake": utterance.labels["wake"], "intent": utterance.labels["intent"],
                    "target_app": utterance.labels["target_app"],
                    "negation": utterance.labels["negation"],
                    "correction": utterance.labels["correction"],
                })
    if not items:
        raise ValueError("Hiç örnek sentezlenemedi")
    apps = sorted({app.name for split in templates.apps.values() for app in split})
    corpus = {"apps": apps, "wake_word": templates.wake_word, "items": items}
    (output / "corpus.yaml").write_text(
        yaml.safe_dump(corpus, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    note = {
        "source": "synthetic", "voices": {voice.id: voice.split for voice in voices},
        "items": len(items), "failures": failures,
        "splits": {s: sum(item["split"] == s for item in items) for s in ("dev", "test")},
        "warning": "Sentetik ses; gerçek konuşmacı doğruluğu kanıtı değildir.",
        "deletion": "Bu klasörü silmek corpus'u tamamen kaldırır.",
    }
    (output / "DATASET_NOTE.json").write_text(
        json.dumps(note, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return note


def _collector(synthesizer: Any) -> Synthesize:
    """Artemis'in kendi TTS adaptörlerinden tam metnin PCM'ini toplar (tek kod yolu)."""

    async def synthesize(text: str) -> tuple[Signal, int]:
        chunks: list[Signal] = []
        rate = 0
        async for chunk in synthesizer.stream(text, asyncio.Event()):
            if chunk.channels != 1 or chunk.sample_width != 2:
                raise ValueError("TTS mono PCM16 üretmedi")
            rate = chunk.sample_rate
            chunks.append(to_float(chunk.pcm))
        if not chunks:
            raise ValueError("TTS ses üretmedi")
        return np.concatenate(chunks).astype(np.float32), rate

    return synthesize


XTTS_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"


def voice_slug(name: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in name.casefold()).strip("_")


def xtts_voices(speakers: Sequence[str]) -> dict[str, Synthesize]:
    """XTTS-v2'nin hazır stüdyo sesleri Türkçe konuşturulur (gerçek kişi klonlanmaz).

    Model Coqui Public Model License altındadır (ticari olmayan kullanım); yalnız
    ``--xtts-lisans-kabul`` ile indirilir. torch gerektirir: ayrı ortamda kurulması önerilir.
    """
    import threading

    from TTS.api import TTS  # lazy: ağır bağımlılık yalnız bu seçenekte yüklenir

    engine = TTS(XTTS_MODEL, progress_bar=False)
    known = set(engine.speakers or [])
    unknown = sorted(set(speakers) - known)
    if unknown:
        raise ValueError(f"XTTS'te olmayan ses: {', '.join(unknown)}")
    rate = int(engine.synthesizer.output_sample_rate)
    lock = threading.Lock()  # model tek; çağrılar sırayla

    def make(speaker: str) -> Synthesize:
        def run(text: str) -> Any:
            with lock:
                return engine.tts(text=text, speaker=speaker, language="tr")

        async def synthesize(text: str) -> tuple[Signal, int]:
            audio = np.asarray(await asyncio.to_thread(run, text), dtype=np.float32)
            if not audio.size:
                raise ValueError("XTTS ses üretmedi")
            return audio, rate

        return synthesize

    return {"xtts_" + voice_slug(speaker): make(speaker) for speaker in speakers}


def real_voices(
    piper_models: Sequence[Path], edge_voices: Sequence[str], audit_path: Path,
    xtts_speakers: Sequence[str] = (),
) -> list[Voice]:
    from artemis.channels.voice.adapters import EdgeTTSynthesizer, PiperSynthesizer
    from artemis.config.settings import VoiceConfig
    from artemis.safety.audit import AuditLog

    named: dict[str, Synthesize] = {}
    for model in piper_models:
        synthesizer = PiperSynthesizer(VoiceConfig(piper_model=model))
        synthesizer.prepare()
        named["piper_" + model.stem] = _collector(synthesizer)
    for voice in edge_voices:
        config = VoiceConfig(tts_backend="edge", edge_voice=voice, allow_cloud_tts_text=True)
        # Bulut aktarımı uygulamadaki gibi denetime yazılır (metin yazılmaz).
        named["edge_" + voice] = _collector(EdgeTTSynthesizer(config, AuditLog(audit_path)))
    if xtts_speakers:
        named.update(xtts_voices(xtts_speakers))
    splits = assign_splits(list(named))
    return [Voice(name, splits[name], synthesize) for name, synthesize in named.items()]


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sentetik komut ses corpus'u")
    parser.add_argument("--cikti", type=Path, default=DEFAULT_OUTPUT_ROOT / "synthetic_tr")
    parser.add_argument("--piper", type=Path, action="append", default=[], metavar="MODEL")
    parser.add_argument("--edge", action="append", default=[], metavar="SES")
    parser.add_argument("--bulut-tts-izni", action="store_true",
                        help="Edge TTS şablon metnini Microsoft'a gönderir")
    parser.add_argument("--xtts", action="append", default=[], metavar="SES",
                        help="XTTS-v2 hazır stüdyo sesi (ör. 'Ana Florence'); tekrarlanabilir")
    parser.add_argument("--xtts-lisans-kabul", action="store_true",
                        help="Coqui Public Model License (ticari olmayan) koşullarını kabul eder")
    parser.add_argument("--sablonlar", type=Path, default=TEMPLATES_PATH)
    parser.add_argument("--tohum", type=int, default=0)
    args = parser.parse_args(arguments)
    if args.edge and not args.bulut_tts_izni:
        parser.error("--edge metni buluta gönderir; --bulut-tts-izni ile açıkça izin verin")
    if args.xtts and not args.xtts_lisans_kabul:
        parser.error("--xtts modeli CPML lisanslıdır; --xtts-lisans-kabul ile açıkça kabul edin")
    if args.xtts:
        # Coqui indirme sırasında etkileşimli lisans sorusu yerine bu açık bayrağı okur.
        os.environ["COQUI_TOS_AGREED"] = "1"
    output = ensure_outside_repo(args.cikti)
    voices = real_voices(args.piper, args.edge, output / "tts_audit.jsonl", args.xtts)
    note = asyncio.run(build(load_templates(args.sablonlar), voices, output, seed=args.tohum))
    print(json.dumps(note, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
