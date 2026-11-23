"""Ses modellerinin (Whisper, Piper, Silero VAD, DFN3) Qt'siz sağlık envanteri ve bakımı.

Bu modül PySide6'ya bağımlı değildir: doğrudan birim testlerinde ve masaüstü arayüzünün
arka plan iş parçacıklarında kullanılır (ADR 0088).
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import ValidationError

from artemis.channels.voice import denoise, silero, stt_catalog
from artemis.config.model_paths import DEFAULT_NOISE_MODEL_DIR
from artemis.config.settings import Settings, load_settings

ModelFamily = Literal["whisper", "piper", "silero", "dfn3"]
ModelStatus = Literal["ok", "missing", "corrupt"]

CURATED_PIPER_VOICES: tuple[str, ...] = (
    "tr_TR-dfki-medium",
    "en_US-lessac-medium",
    "en_GB-alba-medium",
)

_PIPER_VOICE_RE = re.compile(
    r"^(?P<lang_family>[a-z]{2})_(?P<lang_region>[A-Z]{2})-"
    r"(?P<voice_name>[a-zA-Z0-9_]+)-(?P<voice_quality>[a-zA-Z0-9_]+)$"
)
_PIPER_URL_TEMPLATE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "{lang_family}/{lang_code}/{voice_name}/{voice_quality}/"
    "{voice_id}{extension}?download=true"
)
_CHUNK_BYTES = 1024 * 1024

WhisperInstalledFn = Callable[[str], Path | None]
WhisperDownloadFn = Callable[[str], Path]
UrlFetchFn = Callable[[str], bytes]
PiperPinMap = Mapping[str, tuple[str | None, str | None]]
WhisperShaMap = Mapping[str, str]


@dataclass(frozen=True)
class PiperVoiceSpec:
    """Küratörlü Piper TTS ses paketi tanımı."""

    voice_id: str
    label: str
    onnx_url: str
    json_url: str
    approx_size_mb: int
    onnx_sha256: str | None = None
    json_sha256: str | None = None


def _build_piper_spec(
    voice_id: str, label: str, approx_size_mb: int
) -> PiperVoiceSpec:
    match = _PIPER_VOICE_RE.fullmatch(voice_id)
    if match is None:
        raise ValueError(f"Geçersiz Piper ses kimliği: {voice_id}")
    lang_family = match.group("lang_family")
    lang_code = f"{lang_family}_{match.group('lang_region')}"
    args = {
        "lang_family": lang_family,
        "lang_code": lang_code,
        "voice_name": match.group("voice_name"),
        "voice_quality": match.group("voice_quality"),
        "voice_id": voice_id,
    }
    return PiperVoiceSpec(
        voice_id=voice_id,
        label=label,
        onnx_url=_PIPER_URL_TEMPLATE.format(extension=".onnx", **args),
        json_url=_PIPER_URL_TEMPLATE.format(extension=".onnx.json", **args),
        approx_size_mb=approx_size_mb,
    )


PIPER_VOICE_CATALOG: dict[str, PiperVoiceSpec] = {
    "tr_TR-dfki-medium": _build_piper_spec(
        "tr_TR-dfki-medium", "Türkçe · DFKI Medium", 63
    ),
    "en_US-lessac-medium": _build_piper_spec(
        "en_US-lessac-medium", "English (US) · Lessac Medium", 63
    ),
    "en_GB-alba-medium": _build_piper_spec(
        "en_GB-alba-medium", "English (UK) · Alba Medium", 63
    ),
}


@dataclass(frozen=True)
class VoiceModelEntry:
    """Tek bir ses modelinin sağlık, boyut ve eylem yeteneği kaydı."""

    id: str
    family: ModelFamily
    name: str
    label: str
    path: Path | None
    size_bytes: int
    status: ModelStatus
    integrity: str
    bundled: bool
    can_download: bool
    can_clean: bool
    active: bool
    detail: str


@dataclass(frozen=True)
class VoiceHealthSummary:
    """Tüm ses modeli envanterinin toplu sağlık özeti."""

    total: int
    ok_count: int
    missing_count: int
    corrupt_count: int
    total_size_bytes: int


def format_size_bytes(size_bytes: int) -> str:
    """Bayt cinsinden disk boyutunu okunabilir biçime çevirir."""
    if size_bytes <= 0:
        return "0 B"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            hasher.update(chunk)
    return hasher.hexdigest()


def _dir_size_bytes(path: Path) -> int:
    if path.is_file():
        with contextlib.suppress(OSError):
            return path.stat().st_size
        return 0
    if not path.is_dir():
        return 0
    total = 0
    with contextlib.suppress(OSError):
        for child in path.rglob("*"):
            if child.is_file():
                with contextlib.suppress(OSError):
                    total += child.stat().st_size
    return total


def _safe_load_settings(config_path: Path, settings: Settings | None) -> Settings:
    if settings is not None:
        return settings
    try:
        return load_settings(config_path)
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        return Settings()


def _default_hf_cache_roots() -> tuple[Path, ...]:
    try:
        constants = importlib.import_module("huggingface_hub.constants")
        cache = getattr(constants, "HF_HUB_CACHE", None)
        if cache:
            return (Path(str(cache)),)
    except ImportError:
        pass
    return ()


def _whisper_repo_folder_names(name: str) -> tuple[str, ...]:
    names = [f"models--Systran--faster-whisper-{name}"]
    if name == "large-v3-turbo":
        names.append("models--mobiuslabsgmbh--faster-whisper-large-v3-turbo")
    return tuple(names)


def _find_whisper_cache_root(
    path: Path,
    name: str | None,
    hf_cache_roots: Sequence[Path] | None,
) -> Path | None:
    """Yalnız tanınan HuggingFace Whisper önbellek kökünü döndürür; özel klasörü korur."""
    markers = set(_whisper_repo_folder_names(name)) if name else set()
    try:
        resolved = path.resolve()
    except OSError:
        return None
    for candidate in (resolved, *resolved.parents):
        if markers and candidate.name in markers:
            return candidate
        if name is None and candidate.name.startswith(
            ("models--Systran--faster-whisper-", "models--mobiuslabsgmbh--faster-whisper-")
        ):
            return candidate
    if hf_cache_roots:
        for root in hf_cache_roots:
            with contextlib.suppress(OSError):
                root_resolved = root.resolve()
                if resolved != root_resolved and root_resolved in resolved.parents:
                    # En üst doğrudan alt dizini döndür (örn. cache_root/<model_dir>).
                    rel_parts = resolved.relative_to(root_resolved).parts
                    if rel_parts:
                        return root_resolved / rel_parts[0]
    return None


def _broken_hf_cache_path(
    name: str, hf_cache_roots: Sequence[Path]
) -> Path | None:
    for root in hf_cache_roots:
        for folder_name in _whisper_repo_folder_names(name):
            candidate = root / folder_name
            if candidate.is_dir():
                return candidate
    return None


def _check_whisper_dir(
    path: Path, expected_sha256: str | None = None
) -> tuple[ModelStatus, str, str]:
    model_bin = path / "model.bin"
    if not model_bin.is_file():
        return "corrupt", "corrupt", "model.bin eksik."
    try:
        bin_size = model_bin.stat().st_size
    except OSError:
        return "corrupt", "corrupt", "model.bin okunamadı."
    if bin_size <= 0:
        return "corrupt", "corrupt", "model.bin boş (0 B)."
    cfg_path = path / "config.json"
    if not cfg_path.is_file():
        return "corrupt", "corrupt", "config.json eksik."
    try:
        parsed = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "corrupt", "corrupt", "config.json bozuk."
    if not isinstance(parsed, dict):
        return "corrupt", "corrupt", "config.json geçersiz."
    if expected_sha256 is not None:
        try:
            actual = _sha256_file(model_bin)
        except OSError:
            return "corrupt", "corrupt", "model.bin SHA-256 hesaplanamadı."
        if actual.lower() != expected_sha256.lower():
            return (
                "corrupt",
                "sha256_mismatch",
                f"SHA-256 uyuşmuyor ({actual[:12]}…).",
            )
        return "ok", "sha256_ok", f"SHA-256 doğrulandı ({actual[:12]}…)."
    return "ok", "ok", "model.bin ve config.json hazır."


def _whisper_entries(
    settings: Settings,
    *,
    whisper_installed: WhisperInstalledFn,
    hf_cache_roots: Sequence[Path],
    whisper_sha256: WhisperShaMap | None,
) -> list[VoiceModelEntry]:
    entries: list[VoiceModelEntry] = []
    active_stt = stt_catalog.choice_for_path(settings.voice.stt_model)
    active_game = stt_catalog.choice_for_path(settings.voice.game_mode_stt_model)

    for choice in stt_catalog.CATALOG:
        installed = whisper_installed(choice.name)
        is_active = choice.name in (active_stt, active_game)
        expected_sha = whisper_sha256.get(choice.name) if whisper_sha256 else None
        if installed is not None:
            status, integrity, detail = _check_whisper_dir(installed, expected_sha)
            size_bytes = _dir_size_bytes(installed)
            cache_root = _find_whisper_cache_root(installed, choice.name, hf_cache_roots)
            can_clean = cache_root is not None
            if is_active:
                detail = f"Etkin · {detail}"
            entries.append(
                VoiceModelEntry(
                    id=f"whisper:{choice.name}",
                    family="whisper",
                    name=choice.name,
                    label=f"Whisper · {choice.label}",
                    path=installed,
                    size_bytes=size_bytes,
                    status=status,
                    integrity=integrity,
                    bundled=False,
                    can_download=(status != "ok"),
                    can_clean=can_clean,
                    active=is_active,
                    detail=detail,
                )
            )
            continue

        broken = _broken_hf_cache_path(choice.name, hf_cache_roots)
        if broken is not None:
            size_bytes = _dir_size_bytes(broken)
            entries.append(
                VoiceModelEntry(
                    id=f"whisper:{choice.name}",
                    family="whisper",
                    name=choice.name,
                    label=f"Whisper · {choice.label}",
                    path=broken,
                    size_bytes=size_bytes,
                    status="corrupt",
                    integrity="corrupt",
                    bundled=False,
                    can_download=True,
                    can_clean=True,
                    active=is_active,
                    detail="Önbellek eksik veya bozuk.",
                )
            )
        else:
            entries.append(
                VoiceModelEntry(
                    id=f"whisper:{choice.name}",
                    family="whisper",
                    name=choice.name,
                    label=f"Whisper · {choice.label}",
                    path=None,
                    size_bytes=0,
                    status="missing",
                    integrity="missing",
                    bundled=False,
                    can_download=True,
                    can_clean=False,
                    active=is_active,
                    detail=f"Kurulu değil (~{choice.size_mb} MB).",
                )
            )

    seen_custom: set[Path] = set()
    for field_name, custom_path in (
        ("stt_model", settings.voice.stt_model),
        ("game_mode_stt_model", settings.voice.game_mode_stt_model),
    ):
        if custom_path is None or stt_catalog.choice_for_path(custom_path) is not None:
            continue
        resolved = custom_path
        with contextlib.suppress(OSError):
            resolved = custom_path.resolve()
        if resolved in seen_custom:
            continue
        seen_custom.add(resolved)
        if custom_path.is_dir():
            status, integrity, detail = _check_whisper_dir(custom_path)
            size_bytes = _dir_size_bytes(custom_path)
            detail = f"Özel yol (silinmez) · {detail}"
        else:
            status = "missing"
            integrity = "missing"
            size_bytes = 0
            detail = "Özel yol bulunamadı (silinmez)."
        entries.append(
            VoiceModelEntry(
                id=f"whisper:custom:{field_name}",
                family="whisper",
                name=custom_path.name or field_name,
                label=f"Whisper · Özel ({custom_path.name or field_name})",
                path=custom_path,
                size_bytes=size_bytes,
                status=status,
                integrity=integrity,
                bundled=False,
                can_download=False,
                can_clean=False,
                active=True,
                detail=detail,
            )
        )
    return entries


def _piper_search_dirs(
    config_path: Path,
    configured_model: Path | None,
    piper_models_dir: Path | None,
) -> list[Path]:
    dirs: list[Path] = []
    if piper_models_dir is not None:
        dirs.append(piper_models_dir)
    if configured_model is not None:
        dirs.append(configured_model.parent)
    dirs.extend([config_path.parent / "models", config_path.parent.parent / "models"])
    unique: list[Path] = []
    seen: set[Path] = set()
    for d in dirs:
        key = d
        with contextlib.suppress(OSError):
            key = d.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def _managed_piper_roots(
    config_path: Path, piper_models_dir: Path | None
) -> tuple[Path, ...]:
    roots = [config_path.parent / "models", config_path.parent.parent / "models"]
    if piper_models_dir is not None:
        roots.insert(0, piper_models_dir)
    out: list[Path] = []
    for root in roots:
        with contextlib.suppress(OSError):
            out.append(root.resolve())
    return tuple(out)


def _discover_piper_ids(search_dirs: Sequence[Path]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for onnx in sorted(directory.glob("*.onnx")):
            stem = onnx.stem
            if stem and stem not in seen:
                seen.add(stem)
                found.append(stem)
    for preset in CURATED_PIPER_VOICES:
        if preset not in seen:
            seen.add(preset)
            found.append(preset)
    return found


def _same_path(first: Path | None, second: Path | None) -> bool:
    if first is None or second is None:
        return False
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return first == second


def _resolve_piper_pair(
    voice_id: str,
    search_dirs: Sequence[Path],
    configured_model: Path | None,
    configured_config: Path | None,
) -> tuple[Path | None, Path | None]:
    for directory in search_dirs:
        candidate = directory / f"{voice_id}.onnx"
        if candidate.is_file():
            sibling = Path(f"{candidate}.json")
            if sibling.is_file():
                return candidate, sibling
            cfg = configured_config if _same_path(configured_model, candidate) else None
            return candidate, cfg
    if (
        configured_model is not None
        and configured_model.is_file()
        and configured_model.stem == voice_id
    ):
        return configured_model, configured_config
    return None, None


def _check_piper_pair(
    onnx_path: Path,
    json_path: Path | None,
    *,
    expected_onnx_sha256: str | None,
    expected_json_sha256: str | None,
) -> tuple[ModelStatus, str, str, int]:
    try:
        onnx_size = onnx_path.stat().st_size
    except OSError:
        return "corrupt", "corrupt", "ONNX dosyası okunamadı.", 0
    total_size = onnx_size
    if onnx_size <= 0:
        return "corrupt", "corrupt", "ONNX dosyası boş (0 B).", total_size
    if json_path is None or not json_path.is_file():
        return "corrupt", "corrupt", ".onnx.json yapılandırma dosyası eksik.", total_size
    try:
        total_size += json_path.stat().st_size
        parsed = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "corrupt", "corrupt", ".onnx.json geçerli JSON değil.", total_size
    if not isinstance(parsed, dict):
        return "corrupt", "corrupt", ".onnx.json nesne biçiminde değil.", total_size

    try:
        actual_onnx = _sha256_file(onnx_path)
        actual_json = _sha256_file(json_path)
    except OSError:
        return "corrupt", "corrupt", "SHA-256 hesaplanamadı.", total_size

    if expected_onnx_sha256 and actual_onnx.lower() != expected_onnx_sha256.lower():
        return (
            "corrupt",
            "sha256_mismatch",
            f"ONNX SHA-256 uyuşmuyor ({actual_onnx[:12]}…).",
            total_size,
        )
    if expected_json_sha256 and actual_json.lower() != expected_json_sha256.lower():
        return (
            "corrupt",
            "sha256_mismatch",
            f"JSON SHA-256 uyuşmuyor ({actual_json[:12]}…).",
            total_size,
        )
    if expected_onnx_sha256 or expected_json_sha256:
        return (
            "ok",
            "sha256_ok",
            f"SHA-256 doğrulandı ({actual_onnx[:12]}…).",
            total_size,
        )
    return "ok", "ok", f"ONNX + JSON hazır (SHA-256: {actual_onnx[:12]}…).", total_size


def _piper_entries(
    config_path: Path,
    settings: Settings,
    *,
    piper_models_dir: Path | None,
    piper_pins: PiperPinMap | None,
) -> list[VoiceModelEntry]:
    search_dirs = _piper_search_dirs(
        config_path, settings.voice.piper_model, piper_models_dir
    )
    managed_roots = _managed_piper_roots(config_path, piper_models_dir)
    voice_ids = _discover_piper_ids(search_dirs)
    entries: list[VoiceModelEntry] = []

    for voice_id in voice_ids:
        spec = PIPER_VOICE_CATALOG.get(voice_id)
        label = f"Piper · {spec.label}" if spec else f"Piper · {voice_id}"
        onnx_path, json_path = _resolve_piper_pair(
            voice_id, search_dirs, settings.voice.piper_model, settings.voice.piper_config
        )
        pinned = piper_pins.get(voice_id) if piper_pins else None
        exp_onnx = pinned[0] if pinned else (spec.onnx_sha256 if spec else None)
        exp_json = pinned[1] if pinned else (spec.json_sha256 if spec else None)
        is_active = voice_id == settings.voice.piper_voice or (
            settings.voice.piper_model is not None
            and onnx_path is not None
            and onnx_path.resolve() == settings.voice.piper_model.resolve()
        )
        if onnx_path is None:
            size_hint = f" (~{spec.approx_size_mb} MB)" if spec else ""
            entries.append(
                VoiceModelEntry(
                    id=f"piper:{voice_id}",
                    family="piper",
                    name=voice_id,
                    label=label,
                    path=None,
                    size_bytes=0,
                    status="missing",
                    integrity="missing",
                    bundled=False,
                    can_download=(spec is not None),
                    can_clean=False,
                    active=is_active,
                    detail=f"Kurulu değil{size_hint}.",
                )
            )
            continue

        status, integrity, detail, total_size = _check_piper_pair(
            onnx_path,
            json_path,
            expected_onnx_sha256=exp_onnx,
            expected_json_sha256=exp_json,
        )
        is_managed = any(
            root == onnx_path.parent.resolve() for root in managed_roots
        )
        if is_active:
            detail = f"Etkin · {detail}"
        entries.append(
            VoiceModelEntry(
                id=f"piper:{voice_id}",
                family="piper",
                name=voice_id,
                label=label,
                path=onnx_path,
                size_bytes=total_size,
                status=status,
                integrity=integrity,
                bundled=False,
                can_download=(spec is not None and status != "ok"),
                can_clean=is_managed,
                active=is_active,
                detail=detail,
            )
        )
    return entries


def _silero_entry(settings: Settings, silero_dir: Path) -> VoiceModelEntry:
    model_path = silero_dir / silero.SILERO_MODEL_NAME
    active = settings.voice.vad_backend != "webrtc"
    if not model_path.is_file():
        return VoiceModelEntry(
            id="silero:vad",
            family="silero",
            name=silero.SILERO_MODEL_NAME,
            label="Silero VAD (v5.1.2)",
            path=model_path,
            size_bytes=0,
            status="missing",
            integrity="missing",
            bundled=True,
            can_download=False,
            can_clean=False,
            active=active,
            detail="Paket içi model dosyası eksik; Artemis'i yeniden kurun.",
        )
    size_bytes = _dir_size_bytes(model_path)
    try:
        silero.verify_silero_model(model_dir=silero_dir)
    except ValueError:
        return VoiceModelEntry(
            id="silero:vad",
            family="silero",
            name=silero.SILERO_MODEL_NAME,
            label="Silero VAD (v5.1.2)",
            path=model_path,
            size_bytes=size_bytes,
            status="corrupt",
            integrity="sha256_mismatch",
            bundled=True,
            can_download=False,
            can_clean=False,
            active=active,
            detail="SHA-256 doğrulaması başarısız (paket içi model bozulmuş).",
        )
    except OSError:
        return VoiceModelEntry(
            id="silero:vad",
            family="silero",
            name=silero.SILERO_MODEL_NAME,
            label="Silero VAD (v5.1.2)",
            path=model_path,
            size_bytes=size_bytes,
            status="corrupt",
            integrity="corrupt",
            bundled=True,
            can_download=False,
            can_clean=False,
            active=active,
            detail="Model dosyası okunamadı.",
        )
    return VoiceModelEntry(
        id="silero:vad",
        family="silero",
        name=silero.SILERO_MODEL_NAME,
        label="Silero VAD (v5.1.2)",
        path=model_path,
        size_bytes=size_bytes,
        status="ok",
        integrity="sha256_ok",
        bundled=True,
        can_download=False,
        can_clean=False,
        active=active,
        detail=f"Paket içi · SHA-256 doğrulandı ({silero.SILERO_MODEL_SHA256[:12]}…).",
    )


def _dfn3_entry(settings: Settings, dfn3_dir: Path | None) -> VoiceModelEntry:
    resolved_dir = dfn3_dir or settings.voice.noise_model_dir or DEFAULT_NOISE_MODEL_DIR
    is_bundled = resolved_dir.resolve() == DEFAULT_NOISE_MODEL_DIR.resolve()
    model_path = resolved_dir / denoise.STEP_MODEL_NAME
    active = settings.voice.noise_suppression
    prefix = "Paket içi" if is_bundled else "Özel klasör"

    if not model_path.is_file():
        return VoiceModelEntry(
            id="dfn3:step",
            family="dfn3",
            name=denoise.STEP_MODEL_NAME,
            label="DeepFilterNet3 (DFN3)",
            path=model_path,
            size_bytes=0,
            status="missing",
            integrity="missing",
            bundled=is_bundled,
            can_download=False,
            can_clean=False,
            active=active,
            detail=f"{prefix} · model dosyası eksik.",
        )
    size_bytes = _dir_size_bytes(model_path)
    try:
        denoise.verify_step_model(resolved_dir)
    except ValueError:
        return VoiceModelEntry(
            id="dfn3:step",
            family="dfn3",
            name=denoise.STEP_MODEL_NAME,
            label="DeepFilterNet3 (DFN3)",
            path=model_path,
            size_bytes=size_bytes,
            status="corrupt",
            integrity="sha256_mismatch",
            bundled=is_bundled,
            can_download=False,
            can_clean=False,
            active=active,
            detail=f"{prefix} · SHA-256 doğrulaması başarısız.",
        )
    except OSError:
        return VoiceModelEntry(
            id="dfn3:step",
            family="dfn3",
            name=denoise.STEP_MODEL_NAME,
            label="DeepFilterNet3 (DFN3)",
            path=model_path,
            size_bytes=size_bytes,
            status="corrupt",
            integrity="corrupt",
            bundled=is_bundled,
            can_download=False,
            can_clean=False,
            active=active,
            detail=f"{prefix} · model dosyası okunamadı.",
        )
    return VoiceModelEntry(
        id="dfn3:step",
        family="dfn3",
        name=denoise.STEP_MODEL_NAME,
        label="DeepFilterNet3 (DFN3)",
        path=model_path,
        size_bytes=size_bytes,
        status="ok",
        integrity="sha256_ok",
        bundled=is_bundled,
        can_download=False,
        can_clean=False,
        active=active,
        detail=f"{prefix} · SHA-256 doğrulandı ({denoise.STEP_MODEL_SHA256[:12]}…).",
    )


def inventory_voice_models(
    config_path: Path,
    settings: Settings | None = None,
    *,
    whisper_installed: WhisperInstalledFn = stt_catalog.installed_path,
    piper_models_dir: Path | None = None,
    piper_pins: PiperPinMap | None = None,
    whisper_sha256: WhisperShaMap | None = None,
    silero_dir: Path = silero.SILERO_MODEL_DIR,
    dfn3_dir: Path | None = None,
    hf_cache_roots: Sequence[Path] | None = None,
) -> list[VoiceModelEntry]:
    """Dört ses modeli ailesinin (Whisper, Piper, Silero VAD, DFN3) envanterini çıkarır."""
    loaded = _safe_load_settings(config_path, settings)
    cache_roots = tuple(hf_cache_roots) if hf_cache_roots is not None else _default_hf_cache_roots()
    entries: list[VoiceModelEntry] = []
    entries.append(_silero_entry(loaded, silero_dir))
    entries.append(_dfn3_entry(loaded, dfn3_dir))
    entries.extend(
        _piper_entries(
            config_path,
            loaded,
            piper_models_dir=piper_models_dir,
            piper_pins=piper_pins,
        )
    )
    entries.extend(
        _whisper_entries(
            loaded,
            whisper_installed=whisper_installed,
            hf_cache_roots=cache_roots,
            whisper_sha256=whisper_sha256,
        )
    )
    return entries


def summarize_health(entries: Sequence[VoiceModelEntry]) -> VoiceHealthSummary:
    """Envanter kayıtlarından özet sayılar ve toplam disk boyutu üretir."""
    ok_count = sum(1 for e in entries if e.status == "ok")
    missing_count = sum(1 for e in entries if e.status == "missing")
    corrupt_count = sum(1 for e in entries if e.status == "corrupt")
    total_size = sum(e.size_bytes for e in entries)
    return VoiceHealthSummary(
        total=len(entries),
        ok_count=ok_count,
        missing_count=missing_count,
        corrupt_count=corrupt_count,
        total_size_bytes=total_size,
    )


def _find_entry(
    entry_id: str,
    config_path: Path,
    settings: Settings | None,
    **kwargs: Any,
) -> VoiceModelEntry:
    entries = inventory_voice_models(config_path, settings, **kwargs)
    for entry in entries:
        if entry.id == entry_id:
            return entry
    raise ValueError(f"Bilinmeyen ses modeli kimliği: {entry_id}")


def verify_voice_model(
    entry_id: str,
    config_path: Path,
    settings: Settings | None = None,
    *,
    whisper_installed: WhisperInstalledFn = stt_catalog.installed_path,
    piper_models_dir: Path | None = None,
    piper_pins: PiperPinMap | None = None,
    whisper_sha256: WhisperShaMap | None = None,
    silero_dir: Path = silero.SILERO_MODEL_DIR,
    dfn3_dir: Path | None = None,
    hf_cache_roots: Sequence[Path] | None = None,
) -> VoiceModelEntry:
    """Seçili ses modelinin bütünlüğünü ve SHA-256 özetini doğrular."""
    entry = _find_entry(
        entry_id,
        config_path,
        settings,
        whisper_installed=whisper_installed,
        piper_models_dir=piper_models_dir,
        piper_pins=piper_pins,
        whisper_sha256=whisper_sha256,
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=hf_cache_roots,
    )
    # Whisper model.bin açıkça doğrulanırken SHA manifesti yoksa da dosya özetini hesapla.
    if (
        entry.family == "whisper"
        and entry.status == "ok"
        and entry.integrity == "ok"
        and entry.path is not None
        and (entry.path / "model.bin").is_file()
    ):
        digest = _sha256_file(entry.path / "model.bin")
        return VoiceModelEntry(
            id=entry.id,
            family=entry.family,
            name=entry.name,
            label=entry.label,
            path=entry.path,
            size_bytes=entry.size_bytes,
            status="ok",
            integrity="sha256_ok",
            bundled=entry.bundled,
            can_download=entry.can_download,
            can_clean=entry.can_clean,
            active=entry.active,
            detail=f"SHA-256 hesaplandı ({digest[:12]}…).",
        )
    return entry


def _default_url_fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("Yalnız HTTPS adresleri indirilebilir.")
    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def _validate_downloaded_payload(
    filename: str, data: bytes, expected_sha256: str | None
) -> None:
    if not data:
        raise ValueError(f"İndirilen dosya boş: {filename}")
    actual = hashlib.sha256(data).hexdigest()
    if expected_sha256 and actual.lower() != expected_sha256.lower():
        raise ValueError(
            f"SHA-256 doğrulaması başarısız ({filename}): {actual[:12]}…"
        )


def _write_verified_piper_pair(
    onnx_dest: Path,
    onnx_bytes: bytes,
    expected_onnx_sha256: str | None,
    json_dest: Path,
    json_bytes: bytes,
    expected_json_sha256: str | None,
) -> None:
    """ONNX ve JSON yüklerini önce doğrular, sonra iki aşamada atomik yazar."""
    _validate_downloaded_payload(json_dest.name, json_bytes, expected_json_sha256)
    try:
        parsed = json.loads(json_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("İndirilen Piper yapılandırması geçersiz JSON.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("İndirilen Piper yapılandırması geçersiz JSON.")
    _validate_downloaded_payload(onnx_dest.name, onnx_bytes, expected_onnx_sha256)

    onnx_dest.parent.mkdir(parents=True, exist_ok=True)
    json_part = json_dest.with_name(f"{json_dest.name}.part")
    onnx_part = onnx_dest.with_name(f"{onnx_dest.name}.part")
    json_bak = json_dest.with_name(f"{json_dest.name}.bak")
    had_json = json_dest.is_file()
    try:
        json_part.write_bytes(json_bytes)
        onnx_part.write_bytes(onnx_bytes)
        if had_json:
            shutil.copyfile(json_dest, json_bak)
        os.replace(json_part, json_dest)
        try:
            os.replace(onnx_part, onnx_dest)
        except OSError:
            if had_json and json_bak.is_file():
                with contextlib.suppress(OSError):
                    os.replace(json_bak, json_dest)
            elif not had_json:
                with contextlib.suppress(OSError):
                    json_dest.unlink(missing_ok=True)
            raise
    finally:
        for tmp_file in (json_part, onnx_part, json_bak):
            with contextlib.suppress(OSError):
                tmp_file.unlink(missing_ok=True)


def download_voice_model(
    entry_id: str,
    config_path: Path,
    settings: Settings | None = None,
    *,
    whisper_installed: WhisperInstalledFn = stt_catalog.installed_path,
    whisper_downloader: WhisperDownloadFn = stt_catalog.download,
    url_fetcher: UrlFetchFn | None = None,
    piper_models_dir: Path | None = None,
    piper_pins: PiperPinMap | None = None,
    whisper_sha256: WhisperShaMap | None = None,
    silero_dir: Path = silero.SILERO_MODEL_DIR,
    dfn3_dir: Path | None = None,
    hf_cache_roots: Sequence[Path] | None = None,
) -> VoiceModelEntry:
    """İndirilebilir Whisper veya Piper ses modelini atomik olarak indirir."""
    entry = _find_entry(
        entry_id,
        config_path,
        settings,
        whisper_installed=whisper_installed,
        piper_models_dir=piper_models_dir,
        piper_pins=piper_pins,
        whisper_sha256=whisper_sha256,
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=hf_cache_roots,
    )
    if entry.bundled:
        raise ValueError("Paket içi modeller ayrıca indirilemez.")
    if entry.family == "dfn3":
        raise ValueError("DeepFilterNet3 modeli indirme desteklemiyor.")
    if entry.family == "whisper":
        if entry_id.startswith("whisper:custom:"):
            raise ValueError("Özel Whisper model yolu indirilemez.")
        whisper_downloader(entry.name)
    elif entry.family == "piper":
        spec = PIPER_VOICE_CATALOG.get(entry.name)
        if spec is None:
            raise ValueError(f"İndirilebilir Piper kataloğunda yok: {entry.name}")
        target_dir = piper_models_dir or (config_path.parent / "models")
        fetcher = url_fetcher or _default_url_fetch
        pinned = piper_pins.get(entry.name) if piper_pins else None
        exp_onnx = pinned[0] if pinned else spec.onnx_sha256
        exp_json = pinned[1] if pinned else spec.json_sha256

        onnx_bytes = fetcher(spec.onnx_url)
        json_bytes = fetcher(spec.json_url)
        onnx_dest = target_dir / f"{entry.name}.onnx"
        json_dest = target_dir / f"{entry.name}.onnx.json"
        _write_verified_piper_pair(
            onnx_dest,
            onnx_bytes,
            exp_onnx,
            json_dest,
            json_bytes,
            exp_json,
        )
    else:
        raise ValueError(f"Bu model türü indirilemez: {entry.family}")

    return _find_entry(
        entry_id,
        config_path,
        settings,
        whisper_installed=whisper_installed,
        piper_models_dir=piper_models_dir,
        piper_pins=piper_pins,
        whisper_sha256=whisper_sha256,
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=hf_cache_roots,
    )


def clean_voice_model(
    entry_id: str,
    config_path: Path,
    settings: Settings | None = None,
    *,
    whisper_installed: WhisperInstalledFn = stt_catalog.installed_path,
    piper_models_dir: Path | None = None,
    piper_pins: PiperPinMap | None = None,
    whisper_sha256: WhisperShaMap | None = None,
    silero_dir: Path = silero.SILERO_MODEL_DIR,
    dfn3_dir: Path | None = None,
    hf_cache_roots: Sequence[Path] | None = None,
) -> VoiceModelEntry:
    """Yalnız indirilebilir önbellek/model dosyalarını siler; paket içi/özel yolları korur."""
    cache_roots = tuple(hf_cache_roots) if hf_cache_roots is not None else _default_hf_cache_roots()
    entry = _find_entry(
        entry_id,
        config_path,
        settings,
        whisper_installed=whisper_installed,
        piper_models_dir=piper_models_dir,
        piper_pins=piper_pins,
        whisper_sha256=whisper_sha256,
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=cache_roots,
    )
    if entry.bundled:
        raise ValueError("Paket içi modeller silinemez.")
    if not entry.can_clean or entry.path is None:
        raise ValueError("Bu model dosyası temizlenemez (özel konum veya kurulu değil).")

    if entry.family == "whisper":
        cache_dir = _find_whisper_cache_root(entry.path, entry.name, cache_roots)
        if cache_dir is None:
            raise ValueError("Özel Whisper klasörü güvenlik nedeniyle silinmez.")
        shutil.rmtree(cache_dir)
    elif entry.family == "piper":
        managed_roots = _managed_piper_roots(config_path, piper_models_dir)
        if not any(root == entry.path.parent.resolve() for root in managed_roots):
            raise ValueError("Özel Piper konumu güvenlik nedeniyle silinmez.")
        json_sibling = Path(f"{entry.path}.json")
        entry.path.unlink(missing_ok=True)
        json_sibling.unlink(missing_ok=True)
    else:
        raise ValueError("Bu model türü temizlenemez.")

    return _find_entry(
        entry_id,
        config_path,
        settings,
        whisper_installed=whisper_installed,
        piper_models_dir=piper_models_dir,
        piper_pins=piper_pins,
        whisper_sha256=whisper_sha256,
        silero_dir=silero_dir,
        dfn3_dir=dfn3_dir,
        hf_cache_roots=cache_roots,
    )
