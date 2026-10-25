"""Mozilla Common Voice Türkçe'den gerçek konuşmacı corpus'u kurar (plan aşama 2).

Kaynak: Common Voice 17.0 (CC0-1.0), Hugging Face aynası ``fixie-ai/common_voice_17_0``;
Mozilla'nın kendi HF deposu artık dosya sunmuyor. Okunmuş cümlelerdir, Artemis komutu
değildir: genel Türkçe WER/CER ve Whisper ayar karşılaştırması içindir; uyandırma ve
uygulama adı ölçmez.

Kurallar: çıktı depo dışına yazılır; konuşmacı kimliği zaten anonim olan ``client_id``'nin
SHA-256 önekiyle kısaltılır, konuşmacıyı tanımlamaya çalışılmaz (CV kullanım koşulu).
Ayrım konuşmacıya göredir: bir konuşmacının bütün örnekleri ya ``dev`` ya ``test``'tedir.
Silmek için çıktı klasörünü silmek yeterlidir; başka bir yere kopya bırakılmaz.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.eval_audio import (
    DEFAULT_OUTPUT_ROOT,
    Decoder,
    ensure_outside_repo,
    ffmpeg_decoder,
    to_float,
    write_wav,
)

DATASET = "fixie-ai/common_voice_17_0"
SHARD_URL = (
    "https://huggingface.co/datasets/" + DATASET + "/resolve/main/tr/test/"
    "chunk-range-000000000-000011290/test-{index:05d}-of-00008.parquet"
)
SHARDS = 8
LICENSE = "CC0-1.0 (Mozilla Common Voice 17.0)"
MAX_CLIP_SECONDS = 15.0


@dataclass(frozen=True)
class Clip:
    client_id: str
    sentence: str
    audio: bytes
    down_votes: int


def speaker_key(client_id: str) -> str:
    return "cv_" + hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:10]


def speaker_split(client_id: str, test_share: float = 0.5) -> str:
    """Konuşmacının ayrımı kimliğin özetinden gelir: aynı kişi iki tarafa düşemez."""
    bucket = int(hashlib.sha256(("split:" + client_id).encode("utf-8")).hexdigest()[:8], 16)
    return "test" if bucket / 0xFFFFFFFF < test_share else "dev"


def read_shard(path: Path) -> list[Clip]:
    import pyarrow.parquet as parquet  # lazy: yalnız corpus kurarken gerekir

    table = parquet.read_table(path, columns=["client_id", "sentence", "audio", "down_votes"])
    clips: list[Clip] = []
    for row in table.to_pylist():
        audio = row.get("audio") or {}
        data = audio.get("bytes") if isinstance(audio, dict) else None
        if isinstance(row.get("client_id"), str) and isinstance(row.get("sentence"), str) \
                and isinstance(data, bytes):
            clips.append(Clip(row["client_id"], row["sentence"], data,
                              int(row.get("down_votes") or 0)))
    return clips


def select(
    clips: Iterable[Clip], *, samples: int, per_speaker: int, test_share: float = 0.5
) -> list[tuple[Clip, str]]:
    """Konuşmacı başına sınırlı örnek; reddedilmiş (down_votes>0) klip alınmaz.

    Konuşmacılar sabit sırayla (özet) dolaşılır, her turda her konuşmacıdan bir örnek
    alınır: az sayıda çok konuşan kişi corpus'a hâkim olmaz.
    """
    by_speaker: dict[str, list[Clip]] = defaultdict(list)
    for clip in clips:
        if clip.down_votes == 0 and clip.sentence.strip():
            by_speaker[clip.client_id].append(clip)
    order = sorted(by_speaker, key=speaker_key)
    chosen: list[tuple[Clip, str]] = []
    for round_index in range(per_speaker):
        for client_id in order:
            if len(chosen) >= samples:
                return chosen
            pool = by_speaker[client_id]
            if round_index < len(pool):
                chosen.append((pool[round_index], speaker_split(client_id, test_share)))
    return chosen


def download(index: int, cache: Path, client: httpx.Client) -> Path:
    if not 0 <= index < SHARDS:
        raise ValueError(f"Parça 0-{SHARDS - 1} arasında olmalı")
    target = cache / f"tr-test-{index:05d}.parquet"
    if target.is_file():
        return target
    cache.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    with client.stream("GET", SHARD_URL.format(index=index), follow_redirects=True) as response:
        response.raise_for_status()
        with partial.open("wb") as stream:
            for chunk in response.iter_bytes():
                stream.write(chunk)
    partial.replace(target)
    return target


def build(
    clips: Sequence[tuple[Clip, str]], output: Path, decode: Decoder, *, sources: list[str]
) -> dict[str, Any]:
    output = ensure_outside_repo(output)
    items: list[dict[str, Any]] = []
    skipped = 0
    for index, (clip, split) in enumerate(clips):
        try:
            signal = to_float(decode(clip.audio))
        except ValueError:
            skipped += 1
            continue
        if not len(signal) or len(signal) > MAX_CLIP_SECONDS * 16_000:
            skipped += 1
            continue
        item_id = f"cv_{index:05d}"
        write_wav(output / "audio" / f"{item_id}.wav", signal)
        items.append({
            "id": item_id, "category": "cv_read", "speaker_id": speaker_key(clip.client_id),
            "split": split, "audio": f"audio/{item_id}.wav", "transcript": clip.sentence,
            "source": "common_voice", "condition": "clean",
        })
    if not items:
        raise ValueError("Hiç örnek çözülemedi")
    corpus = {"apps": [], "items": items}
    (output / "corpus.yaml").write_text(
        yaml.safe_dump(corpus, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    note = {
        "source": DATASET, "license": LICENSE, "shards": sources,
        "items": len(items), "skipped": skipped,
        "speakers": len({item["speaker_id"] for item in items}),
        "splits": {s: sum(item["split"] == s for item in items) for s in ("dev", "test")},
        "deletion": "Bu klasörü silmek corpus'u tamamen kaldırır.",
        "privacy": "client_id SHA-256 önekiyle kısaltıldı; konuşmacı tanımlanmaya çalışılmaz.",
    }
    (output / "DATASET_NOTE.json").write_text(
        json.dumps(note, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return note


def main(arguments: Sequence[str] | None = None,
         *, client_factory: Callable[[], httpx.Client] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Common Voice Türkçe değerlendirme corpus'u")
    parser.add_argument("--cikti", type=Path, default=DEFAULT_OUTPUT_ROOT / "common_voice_tr")
    parser.add_argument("--parca", type=int, action="append", help="Parquet parça no (0-7)")
    parser.add_argument("--ornek", type=int, default=300)
    parser.add_argument("--konusmaci-basi", type=int, default=5)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args(arguments)
    if args.ornek < 1 or args.konusmaci_basi < 1:
        parser.error("örnek ve konuşmacı başı pozitif olmalı")
    output = ensure_outside_repo(args.cikti)
    shards = args.parca or [0]
    factory = client_factory or (lambda: httpx.Client(timeout=120.0))
    clips: list[Clip] = []
    with factory() as client:
        for index in shards:
            clips.extend(read_shard(download(index, output / "cache", client)))
    chosen = select(clips, samples=args.ornek, per_speaker=args.konusmaci_basi)
    note = build(chosen, output, ffmpeg_decoder(args.ffmpeg),
                 sources=[f"tr/test #{index}" for index in shards])
    print(json.dumps({**note, "corpus": str(output / "corpus.yaml")}, ensure_ascii=False,
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
