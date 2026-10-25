"""Gürültüde uyandırma ölçümü için mevcut corpus'tan bozulmuş kopya kurar (ADR 0045).

Kaynak corpus'un temiz örnekleri (uyandırma etiketi olanlar) seçilir ve her biri
pembe gürültü, gerçek arka plan konuşması (Common Voice kayıtlarından babble) ve telefon
bandıyla yeniden yazılır. Etiketler korunur; çıktı depo dışına yazılır.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artemis.safety.paths import safe_join
from bench.eval_audio import (
    DEFAULT_OUTPUT_ROOT,
    Signal,
    add_noise,
    babble,
    ensure_outside_repo,
    pink_noise,
    telephone_band,
    to_float,
    write_wav,
)
from bench.speech_accuracy import load_corpus

Condition = Callable[[Signal, int, list[Signal]], Signal]
NOISE_CONDITIONS: dict[str, Condition] = {
    "pink_snr10": lambda s, seed, _p: add_noise(s, pink_noise(len(s), seed), 10),
    "pink_snr5": lambda s, seed, _p: add_noise(s, pink_noise(len(s), seed), 5),
    "babble_snr10": lambda s, seed, pool: add_noise(s, babble(len(s), pool, seed), 10),
    "babble_snr5": lambda s, seed, pool: add_noise(s, babble(len(s), pool, seed), 5),
    "telephone": lambda s, _seed, _p: telephone_band(s),
}


def _read(path: Path) -> Signal:
    with wave.open(str(path), "rb") as stream:
        return to_float(stream.readframes(stream.getnframes()))


def build(
    source: Path, output: Path, *, babble_corpus: Path | None, per_class: int, seed: int = 0,
    conditions: Sequence[str] = tuple(NOISE_CONDITIONS),
) -> dict[str, Any]:
    output = ensure_outside_repo(output)
    corpus, root = load_corpus(source)
    pool: list[Signal] = []
    if babble_corpus is not None:
        babble_items, babble_root = load_corpus(babble_corpus)
        pool = [_read(safe_join(babble_root, item.audio, allow_symlinks=False))
                for item in babble_items.items[:60]]
    needs_pool = any(name.startswith("babble") for name in conditions)
    if needs_pool and not pool:
        raise ValueError("babble koşulları için --babble corpus'u gerekli")
    rng = random.Random(seed)
    clean = [item for item in corpus.items if item.condition == "clean" and item.wake is not None]
    chosen = []
    for label in (True, False):
        group = [item for item in clean if item.wake is label]
        chosen += rng.sample(group, min(per_class, len(group)))
    items: list[dict[str, Any]] = []
    for item in chosen:
        signal = _read(safe_join(root, item.audio, allow_symlinks=False))
        for condition in conditions:
            noisy = NOISE_CONDITIONS[condition](signal, rng.randrange(2**31), pool)
            item_id = f"{item.id.removesuffix('_clean')}_{condition}"
            write_wav(output / "audio" / f"{item_id}.wav", noisy)
            items.append({**item.model_dump(), "id": item_id, "condition": condition,
                          "audio": f"audio/{item_id}.wav"})
    data = {"apps": corpus.apps, "wake_word": corpus.wake_word, "items": items}
    (output / "corpus.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    note = {"source": str(source), "items": len(items), "conditions": list(conditions),
            "babble_clips": len(pool),
            "deletion": "Bu klasörü silmek corpus'u tamamen kaldırır."}
    (output / "DATASET_NOTE.json").write_text(
        json.dumps(note, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return note


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gürültüde uyandırma corpus'u")
    parser.add_argument("--kaynak", type=Path, required=True, help="Etiketli corpus.yaml")
    parser.add_argument("--babble", type=Path, help="Arka plan konuşması için corpus.yaml")
    parser.add_argument("--cikti", type=Path, default=DEFAULT_OUTPUT_ROOT / "wake_noise_tr")
    parser.add_argument("--sinif-basi", type=int, default=40)
    parser.add_argument("--tohum", type=int, default=0)
    args = parser.parse_args(arguments)
    note = build(args.kaynak, args.cikti, babble_corpus=args.babble,
                 per_class=args.sinif_basi, seed=args.tohum)
    print(json.dumps(note, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
