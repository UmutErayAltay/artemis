"""Whisper kataloğunun model seçimi ve yerel yardımcılarının testleri."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from artemis.channels.voice import stt_catalog


@pytest.fixture(autouse=True)
def _local_cache_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Testler hiçbir Hugging Face önbelleğine veya ağına dokunmaz."""
    monkeypatch.setattr(stt_catalog, "installed_path", lambda _name: None)


def test_catalog_contains_only_multilingual_models() -> None:
    assert all(not choice.name.endswith(".en") for choice in stt_catalog.CATALOG)
    assert all(not choice.name.startswith("distil-") for choice in stt_catalog.CATALOG)


def test_recommended_model_depends_on_gpu_memory() -> None:
    assert stt_catalog.recommended(None) == "small"
    assert stt_catalog.recommended(6141) == "large-v3"
    assert stt_catalog.recommended(3000) == "large-v3"
    assert stt_catalog.recommended(2999) == "small"
    assert stt_catalog.recommended(2000) == "small"


def test_turbo_note_is_not_recommended_but_large_v3_is() -> None:
    notes = {choice.name: choice.note for choice in stt_catalog.CATALOG}
    assert "Önerilen" not in notes["large-v3-turbo"]
    assert "zayıf" in notes["large-v3-turbo"]
    assert "Önerilen" in notes["large-v3"]


def test_gpu_memory_is_none_when_nvidia_smi_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_command(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(stt_catalog.subprocess, "run", missing_command)
    assert stt_catalog.gpu_memory_mb() is None


def test_gpu_memory_parses_first_line(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="6141\n")

    monkeypatch.setattr(stt_catalog.subprocess, "run", fake_run)
    assert stt_catalog.gpu_memory_mb() == 6141


def test_download_rejects_name_outside_catalog() -> None:
    with pytest.raises(ValueError):
        stt_catalog.download("evil/../x")


def test_choice_for_path_recognizes_hugging_face_cache_folders() -> None:
    turbo = Path(
        "C:/x/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/abc"
    )
    small = Path("C:/x/models--Systran--faster-whisper-small/snapshots/abc")

    assert stt_catalog.choice_for_path(turbo) == "large-v3-turbo"
    assert stt_catalog.choice_for_path(small) == "small"
    assert stt_catalog.choice_for_path(None) is None
