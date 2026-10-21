"""Türkçe Whisper model kataloğu ve faster-whisper indirme yardımcıları."""

from __future__ import annotations

import importlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WhisperChoice:
    """Bir faster-whisper modelinin katalog bilgisi."""

    name: str  # faster-whisper boyut adı
    label: str  # Türkçe arayüz etiketi
    size_mb: int  # yaklaşık indirme boyutu
    vram_mb: int  # float16 için yaklaşık GPU belleği
    note: str  # kısa hız ve doğruluk açıklaması


CATALOG: tuple[WhisperChoice, ...] = (
    WhisperChoice(
        "tiny",
        "Tiny",
        75,
        400,
        "En hızlı, Türkçede çok hata yapar; yalnız deneme için.",
    ),
    WhisperChoice(
        "base",
        "Base",
        145,
        500,
        "Çok hızlı, Türkçe doğruluğu düşük.",
    ),
    WhisperChoice(
        "small",
        "Small",
        484,
        1000,
        "Hızlı, zayıf makine için makul.",
    ),
    WhisperChoice(
        "medium",
        "Medium",
        1530,
        2600,
        "Orta hız, iyi doğruluk.",
    ),
    WhisperChoice(
        "large-v3-turbo",
        "Large v3 Turbo",
        1620,
        2500,
        "Large v3'e yakın doğruluk, birkaç kat hızlı. Önerilen.",
    ),
    WhisperChoice(
        "large-v3",
        "Large v3",
        3100,
        4500,
        "En doğru, turbodan belirgin yavaş.",
    ),
)

_CATALOG_NAMES = frozenset(choice.name for choice in CATALOG)


def recommended(vram_mb: int | None) -> str:
    """GPU belleğine göre etkileşimli asistan için varsayılan modeli seçer."""
    if vram_mb is None or vram_mb < 2500:
        return "small"
    return "large-v3-turbo"


def gpu_memory_mb() -> int | None:
    """İlk NVIDIA GPU'nun toplam belleğini megabayt olarak döndürür."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None

    if getattr(result, "returncode", 0) != 0:
        return None
    try:
        first_line = result.stdout.splitlines()[0].strip()
        return int(first_line)
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def installed_path(name: str) -> Path | None:
    """Yerel faster-whisper önbelleğinde tamamlanmış modeli bulur."""
    try:
        utils = importlib.import_module("faster_whisper.utils")
        result = utils.download_model(name, local_files_only=True)
        path = Path(result)
        if path.is_dir() and (path / "model.bin").is_file():
            return path
        return None
    except ImportError:
        return None
    except Exception:
        return None


def download(name: str) -> Path:
    """Katalogdaki faster-whisper modelini indirir."""
    if name not in _CATALOG_NAMES:
        raise ValueError(f"Katalogda olmayan Whisper modeli: {name}")
    utils = importlib.import_module("faster_whisper.utils")
    return Path(utils.download_model(name))


def choice_for_path(path: Path | None) -> str | None:
    """Yerel model yolundan katalogdaki model adını bulur."""
    if path is None:
        return None

    path_text = str(path)
    for choice in CATALOG:
        systran_marker = f"models--Systran--faster-whisper-{choice.name}"
        if systran_marker in path_text or (
            choice.name == "large-v3-turbo"
            and "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo" in path_text
        ):
            return choice.name

    resolved_path = path.resolve()
    for choice in CATALOG:
        installed = installed_path(choice.name)
        if installed is not None and installed.resolve() == resolved_path:
            return choice.name
    return None
