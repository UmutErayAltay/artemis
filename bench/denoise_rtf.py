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

from jarvis.channels.voice.denoise import DeepFilterNetDenoiser
from jarvis.config.model_paths import DEFAULT_NOISE_MODEL_DIR

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
    args = parser.parse_args(argv)
    denoiser = DeepFilterNetDenoiser(args.model_dir)
    data = _load(args.wav, args.seconds)
    frames = [data[i : i + FRAME * 2] for i in range(0, len(data) - FRAME * 2 + 1, FRAME * 2)]
    for frame in frames[:50]:  # ısınma
        denoiser.process_frame(frame)
    denoiser.reset()
    durations: list[float] = []
    cpu_start, wall_start = time.process_time(), time.perf_counter()
    for frame in frames:
        begin = time.perf_counter()
        denoiser.process_frame(frame)
        durations.append(time.perf_counter() - begin)
    wall, cpu = time.perf_counter() - wall_start, time.process_time() - cpu_start
    audio = len(frames) * FRAME / 16000
    mean_ms = 1000 * sum(durations) / len(durations)
    print(f"durumlu adım modeli, gecikme={denoiser.delay_samples / 16:.0f} ms")
    worst_ms = 1000 * max(durations)
    print(f"kare başına ort. {mean_ms:.2f} ms, en kötü {worst_ms:.2f} ms (20 ms bütçe)")
    print(f"RTF {wall / audio:.3f}; CPU {100 * cpu / audio:.1f}% (tek çekirdek = %100)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
