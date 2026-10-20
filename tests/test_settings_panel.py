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


def test_save_microphone_preserves_settings_and_never_touches_model(tmp_path: Path) -> None:
    path = config(tmp_path)
    save_choices(path, 7)
    settings = load_settings(path)
    assert settings.primary_model == "first" and list(settings.models) == ["first"]
    assert settings.voice.input_device == 7
    assert settings.voice.stt_model == tmp_path / "models/whisper"
    assert settings.close_without_confirmation == ["brave"]
    assert settings.applications == {"brave": "brave.exe"}
    assert not list(tmp_path.glob(".settings-*"))
    save_choices(path, None)
    assert load_settings(path).primary_model == "first"
    assert load_settings(path).voice.input_device is None


def test_settings_panel_has_no_model_combo_or_inventory(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from jarvis.channels.desktop import settings_panel

    app = QApplication.instance() or QApplication([])
    assert not hasattr(settings_panel, "ModelInventory")
    panel = settings_panel.SettingsPanel(config(tmp_path))
    assert not hasattr(panel, "model_combo") and not hasattr(panel, "inventory")
    panel.deleteLater()
    app.processEvents()


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
    save_choices(path, None, applications={"notepad": "notepad.exe"})
    settings = load_settings(path)
    assert settings.applications == {"notepad": "notepad.exe"}
    assert settings.close_without_confirmation == []
    assert settings.primary_model == "first"


def test_global_app_access_is_saved_without_expanding_manual_list(tmp_path: Path) -> None:
    path = config(tmp_path)
    save_choices(path, 2, allow_all_applications=True)
    settings = load_settings(path)
    assert settings.allow_all_applications
    assert settings.applications == {"brave": "brave.exe"}


def test_continuous_mode_choice_is_saved_and_defaults_off(tmp_path: Path) -> None:
    path = config(tmp_path)
    assert load_settings(path).voice.continuous_mode is False
    save_choices(path, None, continuous_mode=True)
    assert load_settings(path).voice.continuous_mode is True
    save_choices(path, None)
    assert load_settings(path).voice.continuous_mode is True
    save_choices(path, None, continuous_mode=False)
    assert load_settings(path).voice.continuous_mode is False


def test_noise_suppression_defaults_off_and_choices_are_saved(tmp_path: Path) -> None:
    path = config(tmp_path)
    voice = load_settings(path).voice
    assert (voice.noise_suppression, voice.noise_suppression_level) == (False, "orta")
    from jarvis.config.model_paths import DEFAULT_NOISE_MODEL_DIR

    assert voice.noise_model_dir == DEFAULT_NOISE_MODEL_DIR  # config konumundan bağımsız
    save_choices(path, None, noise_suppression=True, noise_suppression_level="guclu")
    voice = load_settings(path).voice
    assert (voice.noise_suppression, voice.noise_suppression_level) == (True, "guclu")
    save_choices(path, None)  # verilmeyen anahtarlar korunur
    assert load_settings(path).voice.noise_suppression is True
    save_choices(path, None, noise_suppression=False)
    voice = load_settings(path).voice
    assert (voice.noise_suppression, voice.noise_suppression_level) == (False, "guclu")
    assert voice.noise_suppression_stt is False  # varsayılan: STT ham sesi alır
    save_choices(path, None, noise_suppression_stt=True)
    assert load_settings(path).voice.noise_suppression_stt is True
    save_choices(path, None)  # verilmeyen anahtar korunur
    assert load_settings(path).voice.noise_suppression_stt is True


def test_echo_cancellation_defaults_off_and_choice_is_saved(tmp_path: Path) -> None:
    path = config(tmp_path)
    assert load_settings(path).voice.echo_cancellation is False

    save_choices(path, None, echo_cancellation=True)
    assert load_settings(path).voice.echo_cancellation is True

    save_choices(path, None)
    assert load_settings(path).voice.echo_cancellation is True

    save_choices(path, None, echo_cancellation=False)
    assert load_settings(path).voice.echo_cancellation is False


def test_noise_model_dir_is_resolved_relative_to_config(tmp_path: Path) -> None:
    path = config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["voice"]["noise_model_dir"] = "../models/deepfilternet3"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    expected = (tmp_path / "../models/deepfilternet3").resolve()
    assert load_settings(path).voice.noise_model_dir == expected


def test_invalid_noise_level_is_rejected(tmp_path: Path) -> None:
    path = config(tmp_path)
    with pytest.raises(ValueError):
        save_choices(path, None, noise_suppression_level="cok-guclu")
