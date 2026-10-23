"""Seçimlerin doğrulanarak kaydı; testler ağ ve gerçek ses aygıtı kullanmaz."""

import os
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from artemis.channels.desktop.settings_panel import microphone_choices, save_choices
from artemis.config.settings import load_settings


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


def test_stt_model_path_is_saved(tmp_path: Path) -> None:
    path = config(tmp_path)
    model = tmp_path / "m"
    save_choices(path, None, stt_model=model)
    assert load_settings(path).voice.stt_model == model


def test_tts_backend_choice_is_saved(tmp_path: Path) -> None:
    path = config(tmp_path)
    save_choices(path, None, values={("voice", "tts_backend"): "ema"})
    assert load_settings(path).voice.tts_backend == "ema"


def test_settings_panel_has_no_model_combo_or_inventory(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop import settings_panel

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
    save_choices(path, 2, values={("allow_all_applications",): True})
    settings = load_settings(path)
    assert settings.allow_all_applications
    assert settings.applications == {"brave": "brave.exe"}


def test_continuous_mode_choice_is_saved_and_defaults_off(tmp_path: Path) -> None:
    path = config(tmp_path)
    assert load_settings(path).voice.continuous_mode is False
    save_choices(path, None, values={("voice", "continuous_mode"): True})
    assert load_settings(path).voice.continuous_mode is True
    save_choices(path, None)
    assert load_settings(path).voice.continuous_mode is True
    save_choices(path, None, values={("voice", "continuous_mode"): False})
    assert load_settings(path).voice.continuous_mode is False


def test_noise_suppression_defaults_off_and_choices_are_saved(tmp_path: Path) -> None:
    path = config(tmp_path)
    voice = load_settings(path).voice
    assert (voice.noise_suppression, voice.noise_suppression_level) == (False, "orta")
    from artemis.config.model_paths import DEFAULT_NOISE_MODEL_DIR

    assert voice.noise_model_dir == DEFAULT_NOISE_MODEL_DIR  # config konumundan bağımsız
    save_choices(path, None, values={
        ("voice", "noise_suppression"): True, ("voice", "noise_suppression_level"): "guclu",
    })
    voice = load_settings(path).voice
    assert (voice.noise_suppression, voice.noise_suppression_level) == (True, "guclu")
    save_choices(path, None)  # verilmeyen anahtarlar korunur
    assert load_settings(path).voice.noise_suppression is True
    save_choices(path, None, values={("voice", "noise_suppression"): False})
    voice = load_settings(path).voice
    assert (voice.noise_suppression, voice.noise_suppression_level) == (False, "guclu")
    assert voice.noise_suppression_stt is False  # varsayılan: STT ham sesi alır
    save_choices(path, None, values={("voice", "noise_suppression_stt"): True})
    assert load_settings(path).voice.noise_suppression_stt is True
    save_choices(path, None)  # verilmeyen anahtar korunur
    assert load_settings(path).voice.noise_suppression_stt is True


def test_echo_cancellation_defaults_off_and_choice_is_saved(tmp_path: Path) -> None:
    path = config(tmp_path)
    assert load_settings(path).voice.echo_cancellation is False

    save_choices(path, None, values={("voice", "echo_cancellation"): True})
    assert load_settings(path).voice.echo_cancellation is True

    save_choices(path, None)
    assert load_settings(path).voice.echo_cancellation is True

    save_choices(path, None, values={("voice", "echo_cancellation"): False})
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
        save_choices(path, None, values={("voice", "noise_suppression_level"): "cok-guclu"})


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_settings_panel_tabs_are_grouped_by_topic(tmp_path: Path, qt_app) -> None:
    from PySide6.QtWidgets import QFrame, QScrollArea

    from artemis.channels.desktop import settings_panel

    panel = settings_panel.SettingsPanel(config(tmp_path))
    assert [
        panel.sections.tabText(index) for index in range(panel.sections.count())
    ] == ["Ses", "Model", "Uygulamalar", "GitHub"]
    assert panel.sections.currentIndex() == 0
    for index in range(panel.sections.count()):
        page = panel.sections.widget(index)
        assert isinstance(page, QScrollArea)
        assert page.widgetResizable()
        assert page.frameShape() == QFrame.Shape.NoFrame
    panel.deleteLater()
    qt_app.processEvents()


def test_advanced_voice_settings_are_collapsed_by_default(tmp_path: Path, qt_app) -> None:
    from PySide6.QtCore import Qt

    from artemis.channels.desktop import settings_panel

    panel = settings_panel.SettingsPanel(config(tmp_path))
    assert panel.advanced_box.isHidden()
    assert not panel.advanced_box.isVisibleTo(panel)
    assert panel.advanced_toggle.arrowType() == Qt.ArrowType.RightArrow

    panel.advanced_toggle.setChecked(True)
    assert not panel.advanced_box.isHidden()
    assert panel.advanced_box.isVisibleTo(panel)
    assert panel.advanced_toggle.arrowType() == Qt.ArrowType.DownArrow

    panel.advanced_toggle.setChecked(False)
    assert panel.advanced_box.isHidden()
    assert panel.advanced_toggle.arrowType() == Qt.ArrowType.RightArrow
    panel.deleteLater()
    qt_app.processEvents()


def test_noise_controls_are_grouped_in_advanced_voice_settings(
    tmp_path: Path, qt_app
) -> None:
    from artemis.channels.desktop import settings_panel

    panel = settings_panel.SettingsPanel(config(tmp_path))
    assert panel.advanced_box.isAncestorOf(panel.noise_stt)
    assert panel.advanced_box.isAncestorOf(panel.noise_level)
    assert not panel.advanced_box.isAncestorOf(panel.noise_suppression)
    panel.deleteLater()
    qt_app.processEvents()


def test_save_computer_mode_writes_only_the_mode(tmp_path: Path) -> None:
    from artemis.channels.desktop.settings_panel import save_computer_mode

    path = config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8")
        + "computer_use:\n  enabled: true\n  model: first\n",
        encoding="utf-8",
    )
    save_computer_mode(path, "auto")
    settings = load_settings(path)
    assert settings.computer_use.mode == "auto"
    assert settings.computer_use.enabled is True  # komşu alanlar korunur

    with pytest.raises(ValueError):
        save_computer_mode(path, "yolo")
    assert load_settings(path).computer_use.mode == "auto"


# Bilinçli olarak PANEL_BINDINGS dışında kalan seçim widget'ları ve nedenleri. Yeni bir kutu ya da
# liste eklenip bağlanmazsa aşağıdaki test kırılır: ya bağ ekleyin ya da buraya gerekçe yazın.
_UNBOUND_WIDGETS = {
    "device_combo": "input_device: aygıt listesi ve 'sistem varsayılanı' özel yenilenir",
    "stt_combo": "stt_model: katalog seçimi yola çevrilir, indirme durumu taşır",
    "github_auto": "code_agent.auto_sync: github_values ile birlikte, save_code_agent de yazar",
    "advanced_devices": "yalnız görünüm; config'e yazılmaz",
}


def _panel(path: Path):  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.settings_panel import SettingsPanel

    QApplication.instance() or QApplication([])
    return SettingsPanel(path)


def _flip(widget) -> None:  # type: ignore[no-untyped-def]
    """Widget'ı varsayılandan farklı, seçilebilir bir değere getirir."""
    from PySide6.QtWidgets import QCheckBox, QComboBox

    if isinstance(widget, QCheckBox):
        widget.setChecked(not widget.isChecked())
        return
    assert isinstance(widget, QComboBox)
    model = widget.model()
    for index in range(widget.count()):
        if index != widget.currentIndex() and model.item(index).isEnabled():  # type: ignore[attr-defined]
            widget.setCurrentIndex(index)
            return
    raise AssertionError(f"{widget.accessibleName()} için başka seçenek yok")


def test_every_panel_binding_names_a_real_setting_and_widget(tmp_path: Path) -> None:
    from artemis.channels.desktop.settings_panel import PANEL_BINDINGS, setting_value

    path = config(tmp_path)
    settings = load_settings(path)
    panel = _panel(path)
    assert len({binding.path for binding in PANEL_BINDINGS}) == len(PANEL_BINDINGS)
    for binding in PANEL_BINDINGS:
        setting_value(settings, binding.path)  # yol yanlışsa AttributeError
        assert hasattr(panel, binding.widget), binding.widget
    panel.deleteLater()


def test_every_choice_widget_is_bound_or_explicitly_unbound(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QCheckBox, QComboBox

    from artemis.channels.desktop.settings_panel import PANEL_BINDINGS

    panel = _panel(config(tmp_path))
    choices = {
        name for name, value in vars(panel).items() if isinstance(value, QCheckBox | QComboBox)
    }
    bound = {binding.widget for binding in PANEL_BINDINGS}
    assert choices - bound - set(_UNBOUND_WIDGETS) == set()
    assert bound & set(_UNBOUND_WIDGETS) == set()
    panel.deleteLater()


def test_every_bound_setting_survives_save_and_refresh(tmp_path: Path) -> None:
    """Uygula → diske yaz → yeni panel: her bağ değeri aynı göstermeli (yenile/kaydet simetrisi)."""
    from artemis.channels.desktop.settings_panel import PANEL_BINDINGS, setting_value

    path = config(tmp_path)
    first = _panel(path)
    defaults = first.values()
    assert set(defaults) == {binding.path for binding in PANEL_BINDINGS}
    for binding in PANEL_BINDINGS:
        _flip(getattr(first, binding.widget))
    chosen = first.values()
    changed = {key for key in chosen if chosen[key] != defaults[key]}
    assert changed == set(chosen), "her bağ varsayılandan farklı bir değerle denenmeli"
    save_choices(path, None, values=chosen)
    saved = load_settings(path)
    assert {key: setting_value(saved, key) for key in chosen} == chosen  # diske gerçekten yazıldı
    second = _panel(path)
    assert second.values() == chosen
    second.refresh()  # ikinci yenileme de aynı değeri korur
    assert second.values() == chosen
    first.deleteLater()
    second.deleteLater()


def test_save_choices_rejects_paths_without_a_panel_binding(tmp_path: Path) -> None:
    path = config(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(KeyError):
        save_choices(path, None, values={("voice", "echo_cancelation"): True})  # yazım hatası
    assert path.read_text(encoding="utf-8") == before
