"""VAD gürültü ölçümü: konuşmasız gürültüde karelerin yüzde kaçı "konuşma" sayılıyor.

Sentetik ve deterministiktir (kişisel ses yok): beyaz, pembe gürültü ve 50 Hz uğultu, RMS ~3000.
Yanlış "konuşma" kararı segment açar, gereksiz STT çalıştırır ve söz kesmeyi tetikleyebilir.
Kullanım: python -m bench.vad_noise
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artemis.channels.voice.adapters import WebRTCVAD
from artemis.channels.voice.silero import SileroVAD
from artemis.config.settings import VoiceConfig

SAMPLE_RATE = 16000
FRAME_BYTES = SAMPLE_RATE * 20 // 1000 * 2


def noises(seconds: float = 20.0, seed: int = 1) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    spectrum = np.fft.rfft(rng.standard_normal(n)) / np.sqrt(np.arange(1, n // 2 + 2))
    raw = {
        "beyaz": rng.standard_normal(n),
        "pembe": np.fft.irfft(spectrum, n),
        "ugultu": np.sin(2 * np.pi * 50 * t) + 0.3 * np.sin(2 * np.pi * 150 * t),
    }
    return {name: x / np.sqrt(np.mean(x**2)) * 3000 for name, x in raw.items()}


def false_positive_rate(vad: WebRTCVAD | SileroVAD, samples: np.ndarray) -> float:
    pcm = np.clip(samples, -32768, 32767).astype(np.int16).tobytes()
    flags = [
        vad.is_speech(pcm[i : i + FRAME_BYTES])
        for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)
    ]
    return round(sum(flags) / len(flags), 3)


def report() -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for name, samples in noises().items():
        result[name] = {
            "webrtc": false_positive_rate(WebRTCVAD(VoiceConfig()), samples),
            "silero": false_positive_rate(SileroVAD(), samples),
        }
    return result


if __name__ == "__main__":
    print(json.dumps(report(), ensure_ascii=False, indent=2))
