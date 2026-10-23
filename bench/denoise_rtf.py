"""Gürültü engelleme akış yolunun gerçek zaman çarpanı (ADR 0037).

Kullanım: python -m bench.denoise_rtf [--model-dir DİZİN] [--wav dosya.wav]
Varsayılan model pakete dahil durumlu adım modelidir (``voice.noise_model_dir`` ile aynı dizin).
Kare başına ms (ortalama ve en kötü), RTF ve süreç CPU yüzdesi (tek çekirdek = %100) yazar.
"""

from __future__ import annotations

import argparse
import importlib
import time
import wave
from pathlib import Path
from typing import Any

from artemis.channels.voice.denoise import DeepFilterNetDenoiser
from artemis.config.model_paths import DEFAULT_NOISE_MODEL_DIR

FRAME = 320  # 20 ms, 16 kHz


def _load(path: Path | None, seconds: float) -> bytes:
    if path is None:
        np: Any = importlib.import_module("numpy")
        rng = np.random.default_rng(0)
        pcm = (rng.standard_normal(int(16000 * seconds)) * 1500).astype("<i2")
        return bytes(pcm.tobytes())
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != 16000 or handle.getnchannels() != 1:
            raise SystemExit("WAV 16 kHz mono olmalı")
        return handle.readframes(handle.getnframes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_NOISE_MODEL_DIR)
    parser.add_argument("--wav", type=Path)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument(
        "--rate", type=int, choices=(16000, 48000), default=16000,
        help="mikrofon yakalama hızı: 48000 = yerel 48 kHz (yükseltici atlanır)",
    )
    args = parser.parse_args(argv)
    denoiser = DeepFilterNetDenoiser(args.model_dir)
    data = _load(args.wav, args.seconds)
    frames = [data[i : i + FRAME * 2] for i in range(0, len(data) - FRAME * 2 + 1, FRAME * 2)]
    process = denoiser.process_frame
    delay = denoiser.delay_samples
    if args.rate == 48000:
        # 16 kHz kaynak sinyal 48 kHz'e yükseltilip 960 örneklik karelere bölünür (yalnız bench).
        up: Any = importlib.import_module("artemis.channels.voice.denoise").Upsampler3()
        np: Any = importlib.import_module("numpy")
        samples = np.frombuffer(data, dtype="<i2").astype("float32") / 32768.0
        wide = np.clip(np.rint(up.process(samples) * 32768.0), -32768, 32767).astype("<i2")
        step = FRAME * 3
        frames = [wide[i : i + step].tobytes() for i in range(0, wide.size - step + 1, step)]
        process = denoiser.process_frame_48k
        delay = denoiser.delay_samples_48k
    for frame in frames[:50]:  # ısınma
        process(frame)
    denoiser.reset()
    durations: list[float] = []
    cpu_start, wall_start = time.process_time(), time.perf_counter()
    for frame in frames:
        begin = time.perf_counter()
        process(frame)
        durations.append(time.perf_counter() - begin)
    wall, cpu = time.perf_counter() - wall_start, time.process_time() - cpu_start
    audio = len(frames) * FRAME / 16000
    mean_ms = 1000 * sum(durations) / len(durations)
    print(f"durumlu adım modeli, gecikme={delay / 16:.0f} ms, yakalama {args.rate} Hz")
    worst_ms = 1000 * max(durations)
    print(f"kare başına ort. {mean_ms:.2f} ms, en kötü {worst_ms:.2f} ms (20 ms bütçe)")
    print(f"RTF {wall / audio:.3f}; CPU {100 * cpu / audio:.1f}% (tek çekirdek = %100)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
