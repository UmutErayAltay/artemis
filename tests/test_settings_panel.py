"""Seçimlerin doğrulanarak kaydı; testler ağ ve gerçek ses aygıtı kullanmaz."""

import os
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from jarvis.channels.desktop.settings_panel import microphone_choices, save_choices
from jarvis.config.settings import load_settings


def config(tmp_path: Path, *, local: bool = True) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "workspace": "desktop", "state_dir": "state", "audit_path": "audit.jsonl",
        "primary_model": "first",
        "openrouter_base_url": "http://127.0.0.1:8317/v1" if local else "https://example.com/v1",
        "applications": {"brave": "brave.exe"}, "close_without_confirmation": ["brave"],
        "models": {"first": {"provider": "openrouter", "model": "first-model",
                             "input_per_million": "0", "output_per_million": "0"}},
        "voice": {"stt_device": "cpu", "stt_model": "models/whisper"},
    }), encoding="utf-8")
    return path


def test_save_new_local_model_and_microphone_preserves_settings(tmp_path: Path) -> None:
    path = config(tmp_path)
    save_choices(path, "selected-model", 7)
    settings = load_settings(path)
    assert settings.models[settings.primary_model].model == "selected-model"
    assert settings.voice.input_device == 7
    assert settings.voice.stt_model == tmp_path / "models/whisper"
    assert settings.close_without_confirmation == ["brave"]
    assert settings.applications == {"brave": "brave.exe"}
    assert not list(tmp_path.glob(".settings-*"))
    save_choices(path, "first-model", None)
    assert load_settings(path).primary_model == "first"
    assert load_settings(path).voice.input_device is None


def test_unknown_remote_model_cannot_gain_zero_price(tmp_path: Path) -> None:
    path = config(tmp_path, local=False)
    previous = path.read_bytes()
    with pytest.raises(ValueError):
        save_choices(path, "unknown", None)
    assert path.read_bytes() == previous


def test_microphone_driver_copies_collapse_and_selected_device_is_preserved() -> None:
    hosts = [{"name": "MME"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
    devices = [
        {"name": "Mikrofon (Kulaklık Uzun", "hostapi": 0, "max_input_channels": 1},
        {"name": "Mikrofon (Kulaklık Uzun Ad)", "hostapi": 1, "max_input_channels": 1},
        {"name": "Mikrofon (Kulaklık Uzun Ad)", "hostapi": 2, "max_input_channels": 1},
        {"name": "Microphone Array (Laptop)", "hostapi": 1, "max_input_channels": 1},
        {"name": "Sonar virtual", "hostapi": 2, "max_input_channels": 1},
    ]
    normal = microphone_choices(devices, hosts, 0)
    assert len(normal) == 2
    assert normal[0] == ("Mikrofon — Kulaklık Uzun Ad", 0, [0, 1, 2])
    assert normal[1][0] == "Dahili mikrofon — Laptop"
    advanced = microphone_choices(devices, hosts, 0, advanced=True)
    assert len(advanced) == 3
    assert advanced[-1][0] == "Sonar virtual"


def test_application_list_save_preserves_model_and_removes_obsolete_exceptions(
    tmp_path: Path,
) -> None:
    path = config(tmp_path)
    save_choices(path, "first-model", None, applications={"notepad": "notepad.exe"})
    settings = load_settings(path)
    assert settings.applications == {"notepad": "notepad.exe"}
    assert settings.close_without_confirmation == []
    assert settings.primary_model == "first"


def test_global_app_access_is_saved_without_expanding_manual_list(tmp_path: Path) -> None:
    path = config(tmp_path)
    save_choices(path, "first-model", 2, allow_all_applications=True)
    settings = load_settings(path)
    assert settings.allow_all_applications
    assert settings.applications == {"brave": "brave.exe"}
