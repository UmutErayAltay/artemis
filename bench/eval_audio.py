"""Değerlendirme corpus'ları için ortak ses yardımcıları (konuşma kalitesi planı aşama 2).

Ham ses depoya girmez: çıktı klasörü depo içinde olamaz (plan: "depoya WAV ekleme yok").
Bozmalar (gürültü, yankı, hız, telefon bandı) yalnız numpy ile ve sabit tohumla yapılır;
aynı girdi aynı WAV'ı üretir, ölçümler tekrarlanabilir.
"""

from __future__ import annotations

import subprocess
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np
import numpy.typing as npt

from artemis.tools.platform import child_environment

SAMPLE_RATE = 16_000
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = Path.home() / ".cache" / "artemis-eval"

Signal = npt.NDArray[np.float32]
Decoder = Callable[[bytes], bytes]


def ensure_outside_repo(path: Path) -> Path:
    """Ses çıktısı depo içine yazılmaz; yanlışlıkla commit edilmesin."""
    resolved = path.expanduser().resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise ValueError("Ses corpus'u depo içine yazılamaz; depo dışında bir klasör seçin.")
    return resolved


def to_float(pcm: bytes) -> Signal:
    return (np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0).astype(np.float32)


def to_pcm(signal: Signal) -> bytes:
    clipped = np.clip(signal, -1.0, 1.0)
    return (clipped * 32767.0).round().astype("<i2").tobytes()


def resample(signal: Signal, source_rate: int, target_rate: int = SAMPLE_RATE) -> Signal:
    """Frekans alanında bant sınırlı yeniden örnekleme (scipy gerektirmez)."""
    if source_rate == target_rate or not len(signal):
        return signal.astype(np.float32)
    count = max(1, round(len(signal) * target_rate / source_rate))
    spectrum = np.fft.rfft(signal)
    bins = count // 2 + 1
    if bins <= len(spectrum):
        spectrum = spectrum[:bins]
    else:
        spectrum = np.pad(spectrum, (0, bins - len(spectrum)))
    result = np.fft.irfft(spectrum, n=count) * (count / len(signal))
    return result.astype(np.float32)


def write_wav(path: Path, signal: Signal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(to_pcm(signal))


def ffmpeg_decoder(ffmpeg: str = "ffmpeg", timeout: float = 30.0) -> Decoder:
    """Sıkıştırılmış sesi (MP3 vb.) 16 kHz mono PCM16'ya çevirir; argv sabittir."""

    def decode(data: bytes) -> bytes:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
            input=data, capture_output=True, timeout=timeout, env=child_environment(),
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            raise ValueError("Ses çözülemedi (ffmpeg)")
        return result.stdout

    return decode


def _rms(signal: Signal) -> float:
    return float(np.sqrt(np.mean(np.square(signal)))) if len(signal) else 0.0


def add_noise(signal: Signal, noise: Signal, snr_db: float) -> Signal:
    """Gürültüyü verilen sinyal/gürültü oranında ekler; gürültü gerekirse tekrarlanır."""
    if not len(noise) or _rms(noise) == 0.0:
        raise ValueError("Gürültü sinyali boş")
    repeated = np.resize(noise, len(signal)).astype(np.float32)
    gain = _rms(signal) / (_rms(repeated) * 10 ** (snr_db / 20)) if _rms(signal) else 0.0
    return (signal + gain * repeated).astype(np.float32)


def pink_noise(samples: int, seed: int) -> Signal:
    """1/f gürültü: beyaz gürültünün spektrumu frekansın kökü ile bölünür."""
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal(samples))
    frequencies = np.arange(len(spectrum), dtype=np.float64)
    frequencies[0] = 1.0
    shaped = np.fft.irfft(spectrum / np.sqrt(frequencies), n=samples)
    return (shaped / (np.max(np.abs(shaped)) or 1.0)).astype(np.float32)


def reverb(signal: Signal, rt60: float, seed: int) -> Signal:
    """Sentetik oda yankısı: üstel sönen gürültü dürtü yanıtı ile evrişim."""
    rng = np.random.default_rng(seed)
    length = int(rt60 * SAMPLE_RATE)
    times = np.arange(length) / SAMPLE_RATE
    impulse = rng.standard_normal(length) * np.exp(-6.9 * times / rt60)
    impulse[0] = 1.0
    impulse /= np.sqrt(np.sum(np.square(impulse)))
    size = len(signal) + length - 1
    wet = np.fft.irfft(np.fft.rfft(signal, size) * np.fft.rfft(impulse, size), n=size)
    wet = wet[: len(signal)]
    peak = np.max(np.abs(wet)) or 1.0
    return (wet / peak * (np.max(np.abs(signal)) or 1.0)).astype(np.float32)


def change_speed(signal: Signal, factor: float) -> Signal:
    """Konuşma hızını (ve perdeyi) değiştirir: 1.1 daha hızlı ve tiz."""
    if factor <= 0:
        raise ValueError("Hız katsayısı pozitif olmalı")
    return resample(signal, round(SAMPLE_RATE * factor), SAMPLE_RATE)


def telephone_band(signal: Signal) -> Signal:
    """300–3400 Hz dışını kırpar: zayıf kulaklık/telefon mikrofonu benzetimi."""
    spectrum = np.fft.rfft(signal)
    frequencies = np.fft.rfftfreq(len(signal), 1 / SAMPLE_RATE)
    spectrum[(frequencies < 300) | (frequencies > 3400)] = 0
    return np.fft.irfft(spectrum, n=len(signal)).astype(np.float32)


def babble(samples: int, pool: list[Signal], seed: int, talkers: int = 3) -> Signal:
    """Arka plan konuşması: havuzdan rastgele birkaç gerçek kaydın toplamı (kafe/ofis)."""
    if not pool:
        raise ValueError("Babble havuzu boş")
    rng = np.random.default_rng(seed)
    mixed = np.zeros(samples, dtype=np.float32)
    for index in rng.choice(len(pool), size=min(talkers, len(pool)), replace=False):
        clip = pool[int(index)]
        offset = int(rng.integers(0, max(1, len(clip))))
        mixed += np.resize(np.roll(clip, -offset), samples)
    return mixed


CONDITIONS: dict[str, Callable[[Signal, int], Signal]] = {
    "clean": lambda signal, _seed: signal,
    "pink_snr15": lambda signal, seed: add_noise(signal, pink_noise(len(signal), seed), 15),
    "pink_snr5": lambda signal, seed: add_noise(signal, pink_noise(len(signal), seed), 5),
    "reverb_0.5s": lambda signal, seed: reverb(signal, 0.5, seed),
    "speed_0.9": lambda signal, _seed: change_speed(signal, 0.9),
    "speed_1.15": lambda signal, _seed: change_speed(signal, 1.15),
    "telephone": lambda signal, _seed: telephone_band(signal),
}


def apply_condition(name: str, signal: Signal, seed: int) -> Signal:
    try:
        return CONDITIONS[name](signal, seed)
    except KeyError as error:
        raise ValueError(f"Bilinmeyen koşul: {name}") from error
