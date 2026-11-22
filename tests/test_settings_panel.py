"""Seçimlerin doğrulanarak kaydı; testler ağ ve gerçek ses aygıtı kullanmaz."""

import os
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from artemis.brain import providers
from artemis.channels.desktop import provider_section as section_module
from artemis.channels.desktop.settings_io import microphone_choices
from artemis.channels.desktop.settings_panel import (
    _same,
    _write_validated,
    save_choices,
)
from artemis.config.settings import load_settings

SECRET = "sk-test-gizli"


@pytest.fixture(autouse=True)
def _vault(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Gerçek kimlik kasasına ne okuma ne yazma yapılır."""
    store: dict[str, str] = {}
    monkeypatch.setattr(providers, "read_secret", lambda target: store.get(target))
    monkeypatch.setattr(section_module, "write_secret", lambda s, t: store.__setitem__(t, s))
    for name in ("BULUT", "BULUT2", "XAI"):
        monkeypatch.delenv(f"ARTEMIS_{name}_API_KEY", raising=False)
    return store


def config(tmp_path: Path, *, local: bool = True, cloud: bool = False) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "workspace": "desktop", "state_dir": "state", "audit_path": "audit.jsonl",
        "primary_model": "first",
        "openrouter_base_url": "http://127.0.0.1:8317/v1" if local else "https://example.com/v1",
        "applications": {"brave": "brave.exe"}, "close_without_confirmation": ["brave"],
        "models": {"first": {"provider": "openrouter", "model": "first-model",
                             "input_per_million": "0", "output_per_million": "0"}},
        # Karma tanıma kutusu açılınca doğrulama uç + model ister (ADR 0079).
        **({"endpoints": {"bulut": {"base_url": "https://api.example.com/v1"},
                          "bulut2": {"base_url": "https://api2.example.com/v1"}}}
           if cloud else {}),
        "voice": {"stt_device": "cpu", "stt_model": "models/whisper",
                  **({"stt_cloud_endpoint": "bulut", "stt_cloud_model": "whisper-x"}
                     if cloud else {})},
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


def test_computer_observation_choice_is_saved(tmp_path: Path, qt_app) -> None:
    path = config(tmp_path)
    panel = _panel(path)
    panel.computer_observation.setCurrentIndex(panel.computer_observation.findData("uia"))
    save_choices(path, None, values=panel.values())
    assert load_settings(path).computer_use.observation == "uia"
    panel.deleteLater()


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


def test_settings_panel_sections_are_grouped_by_topic(tmp_path: Path, qt_app) -> None:
    from PySide6.QtWidgets import QScrollArea

    from artemis.channels.desktop import settings_panel

    panel = settings_panel.SettingsPanel(config(tmp_path))
    assert panel.section_names() == [
        "Ses", "Model", "Görünüm",
        "Rutinler", "Tetikleyiciler", "Gün özeti", "Araştırma",
        "Telegram", "Takvim ve posta", "GitHub",
        "Beceriler", "Dosya araması", "Uygulamalar", "Hakkında",
    ]
    assert panel.section_groups() == {
        "Temel": ["Ses", "Model", "Görünüm"],
        "Otomasyon": ["Rutinler", "Tetikleyiciler", "Gün özeti", "Araştırma"],
        "Bağlantılar": ["Telegram", "Takvim ve posta", "GitHub"],
        "Bilgi": ["Beceriler", "Dosya araması", "Uygulamalar", "Hakkında"],
    }
    assert panel.sections.objectName() == "sectionList"
    assert panel.sections.currentRow() == 0
    # Tek kaydırma bölgesi: sayfa başına ikinci kaydırıcı yok.
    assert len(panel.findChildren(QScrollArea)) == 1
    for index in range(len(panel.section_names())):
        panel.show_section(index)
        assert panel.stack.currentIndex() == index
    assert panel.pet.parent() is not panel.stack.currentWidget()
    panel.deleteLater()
    qt_app.processEvents()


def test_settings_block_is_centred_on_wide_windows_and_full_on_narrow(
    tmp_path: Path, qt_app
) -> None:
    from artemis.channels.desktop import settings_panel

    panel = settings_panel.SettingsPanel(config(tmp_path))
    body = panel.sections.parentWidget().parentWidget()
    panel.resize(1920, 1040)
    panel.show()
    qt_app.processEvents()
    left, right = body.geometry().left(), 1920 - body.geometry().right() - 1
    assert abs(left - right) <= 2
    assert body.width() <= 1240
    panel.resize(900, 700)
    qt_app.processEvents()
    assert body.geometry().left() == 24 and body.width() == panel.width() - 48
    panel.deleteLater()
    qt_app.processEvents()


def test_theme_cards_follow_combo_and_emit_once(tmp_path: Path, qt_app) -> None:
    from artemis.channels.desktop import settings_panel
    from artemis.channels.desktop.themes import THEME_LABELS

    panel = settings_panel.SettingsPanel(config(tmp_path))
    seen: list[str] = []
    panel.theme_changed.connect(seen.append)
    assert set(panel.theme_cards) == set(THEME_LABELS)
    # Varsayılan tema "dark": değişiklik görülsün diye önce "light" seçilir.
    panel.theme_cards["light"].radio.click()
    assert seen == ["light"]
    assert panel.theme_combo.currentData() == "light"
    panel.set_theme("dark")  # sinyal yaymaz, kartı eşler
    assert seen == ["light"]
    assert panel.theme_cards["dark"].radio.isChecked()
    assert panel.theme_cards["dark"].property("selected") is True
    panel.theme_combo.setCurrentIndex(panel.theme_combo.findData("dark-mixed"))
    assert panel.theme_cards["dark-mixed"].radio.isChecked()
    assert seen[-1] == "dark-mixed"
    panel.deleteLater()
    qt_app.processEvents()


def test_dirty_notice_shows_after_edit_and_clears_on_refresh(tmp_path: Path, qt_app) -> None:
    from artemis.channels.desktop import settings_panel

    panel = settings_panel.SettingsPanel(config(tmp_path))
    assert not panel._dirty and not panel.has_unsaved
    assert not hasattr(panel, "_save_timer")
    panel.barge_in_box.setChecked(not panel.barge_in_box.isChecked())
    assert panel._dirty and panel.has_unsaved
    panel.refresh()
    assert not panel._dirty and not panel.has_unsaved
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
    "theme_combo": "yalnız görünüm; QSettings ile anında kaydedilir, ses config'ine yazılmaz",
    "device_combo": "input_device: aygıt listesi ve 'sistem varsayılanı' özel yenilenir",
    "stt_combo": "stt_model: katalog seçimi yola çevrilir, indirme durumu taşır",
    "github_auto": "code_agent.auto_sync: github_values ile birlikte, save_code_agent de yazar",
    "advanced_devices": "yalnız görünüm; config'e yazılmaz",
    "section_combo": "dar pencerede bölüm listesinin yerine geçen gezinme; config'e yazılmaz",
    "default_agent_combo": "code_agent.default_agent: code_agent_values ile ajanlarla yazılır",
    "tts_cloud_saved": (
        "Ses panelinin kayıtlı model seçicisi; config'e yazılmaz, "
        "seçince tts_cloud_* alanları dolar"
    ),
}


def _panel(path: Path):  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.settings_panel import SettingsPanel

    QApplication.instance() or QApplication([])
    return SettingsPanel(path)


def _flip(widget) -> None:  # type: ignore[no-untyped-def]
    """Widget'ı varsayılandan farklı, seçilebilir bir değere getirir."""
    from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QListWidget, QSpinBox

    if isinstance(widget, QSpinBox):
        widget.setValue(5 if widget.value() != 5 else 15)
        return
    if isinstance(widget, QListWidget):  # file_search.roots: mutlak yol yeter, varlığı aranmaz
        widget.addItem(str(Path(widget.accessibleName() or "x").resolve() / "belgeler"))
        return
    if isinstance(widget, QLineEdit):
        name = widget.accessibleName()
        # Kısayol alanları birbirinden farklı olmalı (config çakışmayı reddeder).
        widget.setText(
            "587" if name == "IMAP portu" else "07:15" if name == "Özet saati"
            else "Veli2" if "Seçili" in name else "Veli"
        )
        return
    if isinstance(widget, QComboBox) and widget.isEditable():
        widget.setEditText("Veli")
        return
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

    path = config(tmp_path, cloud=True)
    first = _panel(path)
    defaults = first.values()
    assert set(defaults) == {binding.path for binding in PANEL_BINDINGS}
    for binding in PANEL_BINDINGS:
        widget = getattr(first, binding.widget)
        if binding.widget == "cloud_server":  # "Seçilmedi" karma kipte geçersiz olurdu
            widget.setCurrentIndex(widget.count() - 1)
        elif binding.widget == "tts_cloud_emotion_tags":  # yalnız [etiket] biçimi geçerli
            widget.setText("[laugh] [cry]")
        else:
            _flip(widget)
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


def _set_voice(path: Path, **voice: str) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["voice"].update(voice)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")


def test_precision_combo_round_trips_and_keeps_custom_value(tmp_path: Path) -> None:
    path = config(tmp_path)
    key = ("voice", "stt_compute_type")
    panel = _panel(path)
    assert panel.values()[key] == "float16"  # kod varsayılanı değişmez
    panel.precision_combo.setCurrentIndex(panel.precision_combo.findData("int8_float16"))
    save_choices(path, None, values=panel.values())
    assert load_settings(path).voice.stt_compute_type == "int8_float16"
    # Elle yazılmış eski değer panelde görünür ve Uygula ile ezilmez.
    _set_voice(path, stt_compute_type="int8")
    panel.refresh()
    assert panel.values()[key] == "int8"
    panel.refresh()  # tekrar yenileme özel öğeyi çoğaltmaz
    assert panel.precision_combo.count() == 3
    panel.deleteLater()


def test_precision_combo_disabled_on_cpu_only(tmp_path: Path) -> None:
    path = config(tmp_path)  # stt_device: cpu
    panel = _panel(path)
    assert not panel.precision_combo.isEnabled()
    _set_voice(path, stt_device="cuda")
    panel.refresh()
    assert panel.precision_combo.isEnabled()
    panel.deleteLater()


def test_save_choices_rejects_paths_without_a_panel_binding(tmp_path: Path) -> None:
    path = config(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(KeyError):
        save_choices(path, None, values={("voice", "echo_cancelation"): True})  # yazım hatası
    assert path.read_text(encoding="utf-8") == before


def test_hybrid_stt_checkboxes_round_trip_and_hint(tmp_path: Path) -> None:
    path = config(tmp_path, cloud=True)
    panel = _panel(path)
    assert panel.values()[("voice", "stt_mode")] == "local"
    assert panel.cloud_stt_hint.text() == ""
    panel.hybrid_stt.setChecked(True)
    assert "kesintisiz kipte her söz gider" in panel.cloud_stt_hint.text()
    assert "Ayarlar > Model > Bağlantılar" in panel.cloud_stt_hint.text()
    assert "Grok" not in panel.cloud_stt_hint.text()
    save_choices(path, None, values=panel.values())
    saved = load_settings(path).voice
    assert saved.stt_mode == "hybrid"
    assert _panel(path).hybrid_stt.isChecked()
    panel.deleteLater()


def test_hybrid_without_endpoint_warns_and_save_is_rejected(tmp_path: Path) -> None:
    path = config(tmp_path)
    _set_voice(path, stt_cloud_endpoint="", stt_cloud_model="")
    panel = _panel(path)
    panel.hybrid_stt.setChecked(True)
    assert panel.cloud_server.currentData() == ""
    with pytest.raises(ValueError):
        save_choices(path, None, values=panel.values())
    assert load_settings(path).voice.stt_mode == "local"
    panel.deleteLater()


def test_cloud_stt_controls_visibility_server_list_and_xai_preselect(tmp_path: Path) -> None:
    path = config(tmp_path, cloud=True)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["endpoints"]["xai"] = {"base_url": "https://api.x.ai/v1"}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    panel = _panel(path)
    panel.show()
    assert not panel.cloud_box.isVisible()
    panel.hybrid_stt.setChecked(True)
    assert panel.cloud_box.isVisible()
    servers = [panel.cloud_server.itemData(i) for i in range(panel.cloud_server.count())]
    assert servers == ["", "bulut", "bulut2", "xai"]
    assert panel.cloud_server.itemText(0) == "Seçilmedi"
    assert panel.cloud_model.isEnabled()
    panel.cloud_server.setCurrentIndex(servers.index("xai"))
    panel.cloud_server.activated.emit(servers.index("xai"))
    assert panel.cloud_protocol.currentData() == "xai"
    assert not panel.cloud_model.isEnabled()
    assert "Türkçeyi resmi olarak doğrulamıyor" in panel.cloud_stt_hint.text()
    values = panel.values()
    assert values[("voice", "stt_cloud_endpoint")] == "xai"
    assert values[("voice", "stt_cloud_protocol")] == "xai"
    values[("voice", "stt_cloud_model")] = ""
    save_choices(path, None, values=values)  # xai: model boş olabilir
    voice = load_settings(path).voice
    assert (voice.stt_cloud_endpoint, voice.stt_cloud_protocol) == ("xai", "xai")
    panel.deleteLater()


def test_cloud_server_list_reloads_when_providers_change(tmp_path: Path) -> None:
    path = config(tmp_path, cloud=True)
    panel = _panel(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["endpoints"]["yeni"] = {"base_url": "https://y.example.com/v1"}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    panel.providers.changed.emit()
    assert panel.cloud_server.findData("yeni") >= 0
    panel.deleteLater()


def test_custom_wake_name_roundtrip_and_invalid_value_keeps_file(tmp_path: Path) -> None:
    path = config(tmp_path)
    panel = _panel(path)
    panel.wake_name.setText("  Ali  ")
    save_choices(path, None, values=panel.values())
    assert load_settings(path).voice.wake_word == "Ali"
    panel.refresh()
    assert panel.wake_name.text() == "Ali"
    original = path.read_bytes()
    panel.wake_name.setText("   ")
    with pytest.raises(ValueError):
        save_choices(path, None, values=panel.values())
    assert path.read_bytes() == original
    panel.deleteLater()


# ---- Bulut tanıma: API anahtarı ve model listesi ----


def _hybrid_panel(path: Path, server: str = "bulut"):  # type: ignore[no-untyped-def]
    panel = _panel(path)
    panel.hybrid_stt.setChecked(True)
    panel.cloud_server.setCurrentIndex(panel.cloud_server.findData(server))
    panel._set_dirty(False)
    return panel


def test_cloud_key_is_stored_in_vault_only(tmp_path: Path, _vault: dict[str, str]) -> None:
    path = config(tmp_path, cloud=True)
    panel = _hybrid_panel(path)
    assert panel.cloud_key.isEnabled() and panel.cloud_key.placeholderText() == ""
    assert "kayıtlı değil" in panel.cloud_stt_hint.text()
    panel.cloud_key.setText(SECRET)
    # yazılmış ama saklanmamış anahtar değerlere ve YAML'a girmez
    values = panel.values()
    assert SECRET not in repr(values) and values[("voice", "stt_cloud_model")] == "whisper-x"
    save_choices(path, None, values=values)
    assert SECRET not in path.read_text(encoding="utf-8")
    panel._set_dirty(False)
    panel.cloud_key_button.click()
    assert _vault == {"Artemis/bulut": SECRET}
    assert panel.cloud_key.text() == "" and panel.cloud_key.placeholderText() == "kayıtlı"
    assert "kaydedildi" in panel.cloud_stt_hint.text()
    assert SECRET not in panel.cloud_stt_hint.text()
    assert not panel._dirty  # kayıt gerekmez
    assert SECRET not in repr(panel.values())
    assert SECRET not in path.read_text(encoding="utf-8")
    panel.deleteLater()


def test_cloud_key_follows_server_and_missing_key_hint(
    tmp_path: Path, _vault: dict[str, str]
) -> None:
    path = config(tmp_path, cloud=True)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["endpoints"]["yerel"] = {"base_url": "http://127.0.0.1:1/v1", "api_key": False}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    _vault["Artemis/bulut2"] = SECRET
    panel = _hybrid_panel(path, server="")
    assert not panel.cloud_key.isEnabled() and not panel.cloud_fetch_button.isEnabled()
    panel.cloud_server.setCurrentIndex(panel.cloud_server.findData("bulut2"))
    assert panel.cloud_key.isEnabled() and panel.cloud_key.placeholderText() == "kayıtlı"
    assert "kayıtlı değil" not in panel.cloud_stt_hint.text()
    panel.cloud_server.setCurrentIndex(panel.cloud_server.findData("bulut"))
    assert panel.cloud_key.placeholderText() == ""
    assert "kayıtlı değil" in panel.cloud_stt_hint.text()
    panel.cloud_server.setCurrentIndex(panel.cloud_server.findData("yerel"))
    assert not panel.cloud_key.isEnabled() and not panel.cloud_key_button.isEnabled()
    assert panel.cloud_key.placeholderText() == "Bu sunucu anahtar istemiyor"
    panel.deleteLater()


def test_cloud_key_demo_disabled_and_vault_failure_leaks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.settings_panel import SettingsPanel

    path = config(tmp_path, cloud=True)
    QApplication.instance() or QApplication([])
    demo = SettingsPanel(path, demo=True)
    demo.hybrid_stt.setChecked(True)
    assert not demo.cloud_key.isEnabled() and not demo.cloud_fetch_button.isEnabled()
    demo.deleteLater()

    def fail(secret: str, target: str) -> None:
        raise OSError("yok")

    monkeypatch.setattr(section_module, "write_secret", fail)
    panel = _hybrid_panel(path)
    panel.cloud_key.setText(SECRET)
    panel._save_cloud_key()
    assert "ARTEMIS_BULUT_API_KEY" in panel.cloud_stt_hint.text()
    assert SECRET not in panel.cloud_stt_hint.text() and panel.cloud_key.text() == ""
    panel.deleteLater()


def test_cloud_models_fetch_fills_combo_and_keeps_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _vault: dict[str, str]
) -> None:
    from PySide6.QtWidgets import QApplication

    calls: list[tuple[str, str | None]] = []

    def fake_probe(url: str, key: str | None) -> tuple[str, list[str]]:
        calls.append((url, key))
        return "Bağlantı tamam, 2 model bulundu.", ["whisper-1", "whisper-x"]

    monkeypatch.setattr(section_module, "probe_models", fake_probe)
    path = config(tmp_path, cloud=True)
    _vault["Artemis/bulut"] = "kayitli-anahtar"
    panel = _hybrid_panel(path)
    assert panel.cloud_fetch_button.isEnabled()
    panel.cloud_model.setEditText("whisper-x")
    panel._fetch_cloud_models()
    assert panel._cloud_fetch is not None
    panel._cloud_fetch.wait(3000)
    QApplication.processEvents()
    assert calls == [("https://api.example.com/v1", "kayitli-anahtar")]
    assert [panel.cloud_model.itemText(i) for i in range(panel.cloud_model.count())] == [
        "whisper-1", "whisper-x"
    ]
    assert panel.cloud_model.currentText() == "whisper-x"
    assert "2 model" in panel.cloud_stt_hint.text()
    assert not panel._dirty
    # yazılmış ama saklanmamış anahtar kayıtlıdan önce gelir
    panel.cloud_key.setText(SECRET)
    panel._fetch_cloud_models()
    panel._cloud_fetch.wait(3000)
    QApplication.processEvents()
    assert calls[-1] == ("https://api.example.com/v1", SECRET)
    assert SECRET not in panel.cloud_stt_hint.text()
    # listede olmayan, elle yazılan model korunur ve kaydedilir
    panel.cloud_model.setEditText("ozel-model")
    save_choices(path, None, values=panel.values())
    assert load_settings(path).voice.stt_cloud_model == "ozel-model"
    panel.deleteLater()


def test_cloud_models_result_for_old_server_is_dropped(tmp_path: Path) -> None:
    panel = _hybrid_panel(config(tmp_path, cloud=True))
    other = panel.cloud_server.findData("bulut2")
    assert other > 0
    panel._cloud_fetch_for = "bulut"  # istek bulut için başladı
    panel.cloud_server.setCurrentIndex(other)  # kullanıcı sunucuyu değiştirdi
    panel.cloud_model.setEditText("benim-modelim")
    panel._cloud_models_done("Bağlantı tamam, 1 model bulundu.", ["eski-sunucu-modeli"])
    assert [panel.cloud_model.itemText(i) for i in range(panel.cloud_model.count())] == []
    assert panel.cloud_model.currentText() == "benim-modelim"
    panel.deleteLater()


def test_finish_inventory_waits_for_running_model_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    def slow_probe(url: str, key: str | None) -> tuple[str, list[str]]:
        time.sleep(0.4)
        return "Bağlantı tamam, 0 model bulundu.", []

    monkeypatch.setattr(section_module, "probe_models", slow_probe)
    panel = _hybrid_panel(config(tmp_path, cloud=True))
    panel._fetch_cloud_models()
    assert panel._cloud_fetch is not None and panel._cloud_fetch.isRunning()
    panel.finish_inventory()
    assert not panel._cloud_fetch.isRunning()
    panel.deleteLater()


def test_cloud_models_fetch_disabled_for_xai_and_ignored_without_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        section_module, "probe_models", lambda url, key: (calls.append(url) or "", [])
    )
    panel = _hybrid_panel(config(tmp_path, cloud=True))
    panel.cloud_protocol.setCurrentIndex(panel.cloud_protocol.findData("xai"))
    assert not panel.cloud_fetch_button.isEnabled() and not panel.cloud_model.isEnabled()
    assert panel.cloud_key.isEnabled()  # xAI de anahtar ister
    panel.cloud_server.setCurrentIndex(0)
    panel._fetch_cloud_models()
    assert panel._cloud_fetch is None and calls == []
    panel.deleteLater()


def test_file_search_section_uses_the_shared_index(tmp_path: Path) -> None:
    """Panel ve runtime aynı FileIndex'i kullanır: "Dizini sil" çalışanı durdurabilir."""
    from artemis.safety.audit import AuditLog
    from artemis.tools.file_index import FileIndex

    path = config(tmp_path)
    panel = _panel(path)
    settings = load_settings(path)
    assert panel.file_search._index is FileIndex.shared(settings, AuditLog(settings.audit_path))
    panel.deleteLater()


# ---- Gruplu bölüm listesi, kayıt durumu, boş durumlar, düz Türkçe metin ----


def test_group_headers_are_not_selectable_rows(tmp_path: Path, qt_app) -> None:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    panel = _panel(config(tmp_path))
    panel.resize(900, 700)
    panel.show()
    qt_app.processEvents()
    assert panel.sections.count() == 14  # başlıklar satır değil: satır = sayfa
    panel.show_section(1)
    first_of_group = panel.sections.item(3)  # Rutinler, "Otomasyon" başlığının altında
    rect = panel.sections.visualItemRect(first_of_group)
    viewport = panel.sections.viewport()
    click = Qt.MouseButton.LeftButton
    QTest.mouseClick(viewport, click, pos=QPoint(20, rect.top() + 4))
    assert panel.sections.currentRow() == 1  # başlık alanı seçim yapmaz
    QTest.mouseClick(viewport, click, pos=QPoint(20, rect.bottom() - 4))
    assert panel.sections.currentRow() == 3
    panel.show_section(2)
    panel.sections.setFocus()
    QTest.keyClick(panel.sections, Qt.Key.Key_Down)  # Görünüm -> Rutinler, başlık atlanır
    assert panel.section_names()[panel.sections.currentRow()] == "Rutinler"
    panel.deleteLater()


def test_section_list_stays_put_while_the_page_scrolls(tmp_path: Path, qt_app) -> None:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    panel = _panel(config(tmp_path))
    panel.resize(900, 520)
    panel.show()
    qt_app.processEvents()
    scroll = panel.sections.scroll_area
    assert not scroll.isAncestorOf(panel.sections)  # liste kaydırma alanının dışında
    before = panel.sections.mapToGlobal(QPoint(0, 0)).y()
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    qt_app.processEvents()
    assert scroll.verticalScrollBar().maximum() > 0  # uzun sayfa: kaydırma gerçekten oldu
    assert scroll.verticalScrollBar().value() > 0
    assert panel.sections.mapToGlobal(QPoint(0, 0)).y() == before
    # Seçim hâlâ çalışır ve yeni sayfa en üstten açılır.
    rect = panel.sections.visualItemRect(panel.sections.item(2))
    QTest.mouseClick(panel.sections.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(20, rect.center().y()))
    assert panel.sections.currentRow() == 2
    assert panel.stack.currentIndex() == 2
    assert scroll.verticalScrollBar().value() == 0
    panel.deleteLater()


def test_dirty_state_follows_edits_and_refresh(tmp_path: Path, qt_app) -> None:
    panel = _panel(config(tmp_path))
    assert not panel._dirty
    panel.proactive_enabled.setChecked(True)
    assert panel._dirty
    panel.refresh()
    assert not panel._dirty
    panel.deleteLater()


def test_scheduled_jobs_checkbox_is_bound_and_saved(tmp_path: Path, qt_app) -> None:
    path = config(tmp_path)
    panel = _panel(path)
    assert panel.section_names()[3] == "Rutinler"
    assert panel.proactive_enabled.text() == "Zamanlı işler"
    assert panel.values()[("proactive", "enabled")] is False
    panel.proactive_enabled.setChecked(True)
    save_choices(path, None, values=panel.values())
    assert load_settings(path).proactive.enabled is True
    panel.refresh()
    assert panel.proactive_enabled.isChecked()
    panel.deleteLater()


def test_feature_toggles_name_their_action_and_text_has_no_config_keys(
    tmp_path: Path, qt_app
) -> None:
    from PySide6.QtWidgets import QCheckBox, QLabel

    panel = _panel(config(tmp_path))
    labels = {
        "routines_enabled": "Rutinleri kullan", "triggers_enabled": "Tetikleyicileri kullan",
        "file_search_enabled": "Dosya aramasını kullan", "skills_enabled": "Becerileri kullan",
        "research_enabled": "Araştırma işlerini kullan",
        "daily_summary_enabled": "Gün özetini kullan",
        "calendar_enabled": "Takvimi kullan", "mail_enabled": "Postayı kullan",
        "dictation_enabled": "Dikteyi kullan",
        "selection_enabled": "Seçili metin eylemlerini kullan",
    }
    for attribute, text in labels.items():
        assert getattr(panel, attribute).text() == text
    banned = ("proactive.", "triggers.items", "allow_cloud", "state_dir", "config.yaml", "`",
              "max_searches", "skills.folder", "Uygula ile kaydedilir", "hemen kaydedilir")
    texts = [w.text() for w in panel.findChildren(QLabel)] + [
        w.text() for w in panel.findChildren(QCheckBox)
    ]
    for text in texts:
        assert not any(key in text for key in banned), text
    panel.deleteLater()


def test_cloud_options_follow_their_parent_and_keep_stored_value(tmp_path: Path, qt_app) -> None:
    from PySide6.QtWidgets import QLabel

    path = config(tmp_path)
    save_choices(path, None, values={
        ("calendar", "enabled"): False, ("calendar", "allow_cloud"): True,
        ("mail", "enabled"): True, ("mail", "allow_cloud"): False,
    })
    panel = _panel(path)
    assert not panel.calendar_cloud.isEnabled() and panel.calendar_cloud.isChecked()
    assert panel.mail_cloud.isEnabled() and not panel.mail_cloud.isChecked()
    panel.calendar_enabled.setChecked(True)
    assert panel.calendar_cloud.isEnabled()
    panel.mail_enabled.setChecked(False)
    assert not panel.mail_cloud.isEnabled() and not panel.mail_cloud.isChecked()
    panel.calendar_enabled.setChecked(False)
    assert not panel.calendar_cloud.isEnabled() and panel.calendar_cloud.isChecked()
    # Kayıt pasif seçeneğin değerini korur: ana kutu kapalıyken de yazılan değer aynı kalır.
    save_choices(path, None, values=panel.values())
    saved = load_settings(path)
    assert saved.calendar.allow_cloud is True and saved.calendar.enabled is False
    assert saved.mail.allow_cloud is False and saved.mail.enabled is False
    titles = {w.text() for w in panel.findChildren(QLabel) if w.objectName() == "sectionTitle"}
    assert {"Takvim", "Posta"} <= titles
    assert panel.mail_port.accessibleName() == "IMAP portu"
    assert "Kapı" not in {w.text() for w in panel.findChildren(QLabel)}
    panel.deleteLater()


def test_level_label_fits_full_scale(tmp_path: Path, qt_app) -> None:
    panel = _panel(config(tmp_path))
    needed = panel.level_value.fontMetrics().horizontalAdvance("%100")
    assert panel.level_value.minimumWidth() >= needed
    panel.deleteLater()


def test_list_empty_states_and_disabled_messages(tmp_path: Path, qt_app) -> None:
    path = config(tmp_path)
    panel = _panel(path)
    assert panel.routines.routine_area.currentWidget() is panel.routines.routine_area.empty
    assert panel.routines.routine_area.empty.title_label.text() == "Rutinler kapalı"
    assert panel.triggers.trigger_area.empty.title_label.text() == "Tetikleyiciler kapalı"
    assert panel.skills.valid_area.empty.title_label.text() == "Beceriler kapalı"
    # Klasör listesi özellikten bağımsız: boşken yönlendirir, dolunca listeyi gösterir.
    roots = panel.file_search.roots_area
    assert roots.currentWidget() is roots.empty
    assert roots.empty.title_label.text() == "Henüz klasör eklenmedi"
    assert roots.empty.body_label.text() == "Klasör ekle ile başla."
    panel.file_search_roots.addItem(str(tmp_path))
    assert roots.currentWidget() is roots.listing
    panel.file_search_roots.clear()
    assert roots.currentWidget() is roots.empty
    save_choices(path, None, values={
        ("routines", "enabled"): True, ("triggers", "enabled"): True, ("skills", "enabled"): True,
    })
    panel.refresh()
    assert panel.routines.routine_area.empty.title_label.text() == "Henüz rutin yok"
    assert "rutin kaydet" in panel.routines.routine_area.empty.body_label.text()
    triggers = panel.triggers.trigger_area
    assert triggers.empty.title_label.text() == "Tanımlı tetikleyici yok"
    assert triggers.empty.action_button is not None
    assert triggers.empty.action_button.text() == "Tetikleyici ekle"
    assert not hasattr(panel.triggers, "open_button")  # dış editör düğmesi yok (UX-09)
    assert panel.triggers.add_button.isHidden()  # boşken tek birincil eylem: boş durumdaki düğme
    assert not triggers.empty.action_button.isHidden()
    assert panel.skills.valid_area.empty.title_label.text() == "Henüz beceri yok"
    assert panel.skills.valid_area.empty.action_button.text() == "Yeni beceri…"  # type: ignore[union-attr]
    assert panel.skills.invalid_area.empty.title_label.text() == "Geçersiz dosya yok"
    panel.deleteLater()


# ---- Arama, kirli nokta, son bölüm ----


def _visible(panel) -> list[str]:  # type: ignore[no-untyped-def]
    return [panel.sections.item(r).text() for r in range(panel.sections.count())
            if not panel.sections.item(r).isHidden()]


def test_search_filters_by_title_and_page_text_turkish_insensitive(tmp_path: Path, qt_app) -> None:
    panel = _panel(config(tmp_path))
    panel.search.setText("RUTİN")  # başlık; İ/i farkı yok
    assert _visible(panel) == ["Rutinler"]
    assert panel.sections.currentRow() == panel.section_names().index("Rutinler")  # tek eşleşme
    panel.search.setText("bütün kurulu uygulamalara")  # sayfadaki onay kutusu metni
    assert _visible(panel) == ["Uygulamalar"]
    panel.search.setText("UYANDIRMA ADI")
    assert "Ses" in _visible(panel)
    panel.search.setText("zzzyok")
    assert _visible(panel) == [] and not panel.no_results.isHidden()
    panel.search.clear()
    assert len(_visible(panel)) == panel.sections.count() and panel.no_results.isHidden()
    assert panel.section_groups() == panel.section_groups()  # bozulmadı
    panel.deleteLater()


def test_search_hides_empty_group_headers_and_enter_selects(tmp_path: Path, qt_app) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from artemis.channels.desktop.settings_panel import _GROUP_ROLE

    panel = _panel(config(tmp_path))
    names = panel.section_names()
    panel.search.setText("ra")  # birden çok eşleşme
    shown = _visible(panel)
    headers = [r for r in range(panel.sections.count())
               if panel.sections.item(r).data(_GROUP_ROLE)]
    # her görünür grubun tam bir başlığı var, gizli grubun yok
    assert all(not panel.sections.item(r).isHidden() for r in headers)
    assert len(headers) == len({g for g, _ in (panel._row_groups[names.index(n)] for n in shown)})
    panel.search.setText("Rutinler")
    panel.show_section(0)
    QTest.keyClick(panel.search, Qt.Key.Key_Return)
    assert panel.sections.currentRow() == names.index("Rutinler")
    panel.deleteLater()


def test_search_escape_clears(tmp_path: Path, qt_app) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    panel = _panel(config(tmp_path))
    panel.show()
    panel.search.setFocus()
    panel.search.setText("zzz")
    QTest.keyClick(panel.search, Qt.Key.Key_Escape)
    assert panel.search.text() == "" and len(_visible(panel)) == panel.sections.count()
    panel.deleteLater()


def test_dirty_dot_marks_edited_section_and_clears(tmp_path: Path, qt_app) -> None:
    from PySide6.QtCore import Qt

    from artemis.channels.desktop.settings_panel import _DIRTY_ROLE

    panel = _panel(config(tmp_path))
    panel.show_section(0)
    panel.barge_in_box.setChecked(not panel.barge_in_box.isChecked())
    item = panel.sections.item(0)
    assert item.data(_DIRTY_ROLE)
    assert "kaydedilmemiş değişiklik var" in item.data(Qt.ItemDataRole.AccessibleTextRole)
    assert not panel.sections.item(1).data(_DIRTY_ROLE)
    panel.refresh()
    assert not item.data(_DIRTY_ROLE)
    assert "kaydedilmemiş" not in item.data(Qt.ItemDataRole.AccessibleTextRole)
    panel.deleteLater()


def test_last_section_is_remembered_and_falls_back(tmp_path: Path, qt_app) -> None:
    from PySide6.QtCore import QSettings

    from artemis.channels.desktop.settings_panel import SettingsPanel

    state = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    first = SettingsPanel(config(tmp_path), state=state)
    first.sections.setCurrentRow(2)  # kullanıcı seçimi
    name = first.section_names()[2]
    first.show_section(1)  # kodla gezinme kaydı değiştirmez
    assert state.value("settings/section") == name
    second = SettingsPanel(config(tmp_path), state=state)
    assert second.sections.currentRow() == 2 and second.stack.currentIndex() == 2
    state.setValue("settings/section", "Artık yok")
    third = SettingsPanel(config(tmp_path), state=state)
    assert third.sections.currentRow() == 0
    for panel in (first, second, third):
        panel.deleteLater()


# --- Otomatik kayıt, başlık tekrarı, "Değişiklikleri geri al" -------------------------------


def _wait(predicate, timeout_ms: int = 3000) -> bool:  # type: ignore[no-untyped-def]
    from PySide6.QtTest import QTest

    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(20)
        waited += 20
    return bool(predicate())


def _wire(panel, path: Path, writes: list[bool]) -> None:  # type: ignore[no-untyped-def]
    """Pencerenin kayıt işleyicisinin sadeleşmiş hâli (kısayol denetimi app testindedir)."""
    from artemis.channels.desktop import settings_panel as module

    def handler(device) -> None:  # type: ignore[no-untyped-def]
        try:
            written = module.save_choices(
                path, device, values=panel.values(), applications=panel.applications,
                code_agent=panel.code_agent_values(), stt_model=panel.stt_model_path(),
            )
        except (OSError, ValueError, KeyError, TypeError):
            panel.save_failed("Ayarlar kaydedilemedi; mevcut ayarlar korunuyor.")
            return
        writes.append(written)
        panel.mark_saved(written)

    panel.apply_requested.connect(handler)


def test_same_is_type_strict_and_order_independent() -> None:
    assert not _same(True, 1) and not _same(1, True)
    assert not _same(1, 1.0) and _same(1.5, 1.5)
    assert _same({"a": 1, "b": {"c": [1, 2]}}, {"b": {"c": [1, 2]}, "a": 1})
    assert not _same({"a": 1}, {"a": 1, "b": 2})
    assert not _same([1, 2], [2, 1])
    assert not _same({"a": [True]}, {"a": [1]})
    assert not _same(None, {})


def test_pages_have_no_title_duplicating_their_name_and_search_still_works(
    tmp_path: Path, qt_app
) -> None:
    from PySide6.QtWidgets import QLabel

    panel = _panel(config(tmp_path))
    for row, name in enumerate(panel.section_names()):
        page = panel.stack.widget(row)
        titles = {
            w.text().casefold()
            for w in page.findChildren(QLabel) if w.objectName() == "sectionTitle"
        }
        assert name.casefold() not in titles, name
    panel.search.setText("telegram")
    shown = [
        n for r, n in enumerate(panel.section_names()) if not panel.sections.item(r).isHidden()
    ]
    assert "Telegram" in shown and "Ses" not in shown
    panel.search.setText("")
    assert all(not panel.sections.item(r).isHidden() for r in range(panel.sections.count()))
    panel.deleteLater()


def test_edits_do_not_write_until_explicit_save(tmp_path: Path, qt_app) -> None:
    from PySide6.QtTest import QTest

    path = config(tmp_path)
    panel = _panel(path)
    writes: list[bool] = []
    _wire(panel, path, writes)
    baseline = path.read_bytes()
    for value in ("Ali", "Veli", "Zeynep"):
        panel.wake_name.setText(value)
        panel._mark_dirty()
    QTest.qWait(100)
    assert writes == [] and panel.has_unsaved
    assert path.read_bytes() == baseline
    panel._save_now()
    assert writes == [True] and not panel._dirty and not panel.has_unsaved
    assert load_settings(path).voice.wake_word.casefold() == "zeynep"
    panel.deleteLater()


def test_revert_button_hidden_then_visible_and_a_b_a_hides_it(tmp_path: Path, qt_app) -> None:
    path = config(tmp_path)
    panel = _panel(path)
    writes: list[bool] = []
    _wire(panel, path, writes)
    assert panel.revert_button.isHidden()
    panel.barge_in_box.setChecked(not panel.barge_in_box.isChecked())
    assert not panel.revert_button.isHidden()
    panel.barge_in_box.setChecked(not panel.barge_in_box.isChecked())  # A -> B -> A
    panel.flush()
    assert not panel._dirty and panel.revert_button.isHidden()
    panel.deleteLater()


def test_revert_restores_every_kind_of_value_byte_for_byte(tmp_path: Path, qt_app) -> None:
    path = config(tmp_path, cloud=True)
    panel = _panel(path)
    writes: list[bool] = []
    _wire(panel, path, writes)
    baseline = path.read_bytes()

    def snapshot() -> tuple[object, ...]:
        return (
            panel.wake_name.text(), panel.mail_port.text(), panel.barge_in_box.isChecked(),
            panel.file_search_roots.count(), dict(panel.applications),
        )

    old = snapshot()
    panel.wake_name.setText("Veli")  # string
    panel.mail_port.setText("587")  # int
    panel.barge_in_box.setChecked(not panel.barge_in_box.isChecked())  # bool
    panel.file_search_roots.addItem(str((tmp_path / "belgeler").resolve()))  # list
    panel.applications["notepad"] = "notepad.exe"  # iç içe sözlük
    panel._refresh_applications()
    panel._mark_dirty()
    panel.flush()
    assert writes == [True] and path.read_bytes() != baseline
    # Bölümün doğrudan yazdığı değişiklik de geri alınır.
    providers = panel.providers
    providers.add_connection.setCurrentIndex(providers.add_connection.findData("bulut"))
    providers.model_name.setEditText("m-2")
    providers.make_primary.setChecked(False)
    providers._add_model()
    assert "m-2" in load_settings(path).models
    assert _wait(lambda: not panel.revert_button.isHidden())
    panel.revert_button.click()
    assert path.read_bytes() == baseline
    assert snapshot() == old
    assert "m-2" not in load_settings(path).models
    assert panel.revert_button.isHidden() and not panel._dirty
    assert panel.notice.text() == "Değişiklikler geri alındı"
    panel.deleteLater()


def test_revert_clears_unsaved_edits_without_writing_them(tmp_path: Path, qt_app) -> None:
    path = config(tmp_path)
    panel = _panel(path)
    writes: list[bool] = []
    _wire(panel, path, writes)
    baseline = path.read_bytes()
    original_wake = panel.wake_name.text()
    panel.wake_name.setText("Veli")
    panel._mark_dirty()
    assert panel.has_unsaved and not panel.revert_button.isHidden()
    panel.revert_button.click()
    assert writes == [] and path.read_bytes() == baseline
    assert panel.wake_name.text() == original_wake
    assert not panel.has_unsaved and panel.revert_button.isHidden()
    panel.deleteLater()


def test_save_failure_keeps_dirty_shows_persistent_error_and_retry_works(
    tmp_path: Path, qt_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    from artemis.channels.desktop import settings_panel as module

    path = config(tmp_path)
    panel = _panel(path)
    writes: list[bool] = []
    _wire(panel, path, writes)
    real = module.save_choices

    def broken(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("disk dolu")

    monkeypatch.setattr(module, "save_choices", broken)
    panel.barge_in_box.setChecked(not panel.barge_in_box.isChecked())
    panel.flush()
    assert panel._dirty and not panel.retry_button.isHidden()
    assert "kaydedilemedi" in panel.notice.text()
    assert not panel.notice._timer.isActive()  # hata kalıcıdır
    monkeypatch.setattr(module, "save_choices", real)
    panel.retry_button.click()
    assert writes == [True] and not panel._dirty and panel.retry_button.isHidden()
    assert panel.notice._timer.isActive()  # başarı bildirimi geçicidir
    panel.deleteLater()


def test_revert_write_failure_shows_error_and_keeps_button(
    tmp_path: Path, qt_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config(tmp_path)
    panel = _panel(path)
    writes: list[bool] = []
    _wire(panel, path, writes)
    panel.barge_in_box.setChecked(not panel.barge_in_box.isChecked())
    panel.flush()
    changed = path.read_bytes()

    def boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("kilitli")

    monkeypatch.setattr(os, "replace", boom)
    panel.revert_button.click()
    assert "geri alınamadı" in panel.notice.text()
    assert not panel.notice._timer.isActive()
    assert not panel.revert_button.isHidden() and path.read_bytes() == changed
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".settings-")]
    panel.deleteLater()


def test_notice_is_transient_but_errors_persist(tmp_path: Path, qt_app) -> None:
    panel = _panel(config(tmp_path))
    panel.notice._timer.setInterval(30)  # 6 sn yerine: davranış aynı, test hızlı
    panel.notice.setText("Kaydedildi")
    assert _wait(lambda: panel.notice.text() == "")
    panel.notice.show_error("Kaydedilemedi")
    assert not panel.notice._timer.isActive() and panel.notice.text() == "Kaydedilemedi"
    panel.deleteLater()


def test_direct_atomic_write_by_another_section_shows_revert(tmp_path: Path, qt_app) -> None:
    """Başka bölüm config'i os.replace ile yazar: izleyici bunu görür ve dosya izini yeniler."""
    path = config(tmp_path)
    panel = _panel(path)
    assert panel.revert_button.isHidden()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("skills", {})["folder"] = str(tmp_path / "beceri")
    _write_validated(path, raw)
    assert _wait(lambda: not panel.revert_button.isHidden())
    assert str(path) in panel._watcher.files()  # atomik değiştirmeden sonra yeniden eklendi
    panel.deleteLater()


def test_save_button_beside_revert_writes_now_and_requests_apply(tmp_path: Path, qt_app) -> None:
    path = config(tmp_path)
    panel = _panel(path)
    writes: list[bool] = []
    _wire(panel, path, writes)
    applied: list[bool] = []
    panel.save_requested.connect(lambda: applied.append(True))
    assert panel.save_button.isHidden()
    panel.barge_in_box.setChecked(not panel.barge_in_box.isChecked())
    assert not panel.save_button.isHidden() and panel.has_unsaved
    assert writes == []
    panel.save_button.click()
    assert writes == [True] and not panel._dirty and applied == [True]
    panel.deleteLater()


def test_finish_inventory_stops_and_detaches_running_stt_download(
    tmp_path: Path, qt_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from PySide6.QtCore import QThread

    from artemis.channels.desktop import settings_panel as module

    class _CooperativeStt(QThread):
        def __init__(self, parent) -> None:  # type: ignore[no-untyped-def]
            super().__init__(parent)
            self.entered = threading.Event()

        def run(self) -> None:
            self.entered.set()
            while not self.isInterruptionRequested():
                threading.Event().wait(0.01)

    class _StuckStt(QThread):
        def __init__(self, parent) -> None:  # type: ignore[no-untyped-def]
            super().__init__(parent)
            self.entered = threading.Event()
            self.release = threading.Event()

        def run(self) -> None:
            self.entered.set()
            self.release.wait(5)

    panel = _panel(config(tmp_path))
    cooperative = _CooperativeStt(panel)
    cooperative.finished.connect(panel._stt_download_finished)
    panel.stt_download_thread = cooperative  # type: ignore[assignment]
    cooperative.start()
    assert cooperative.entered.wait(3)
    panel.finish_inventory()
    assert not cooperative.isRunning()

    monkeypatch.setattr(module, "_SHUTDOWN_WAIT_MS", 50)
    stuck = _StuckStt(panel)
    stuck.finished.connect(panel._stt_download_finished)
    panel.stt_download_thread = stuck  # type: ignore[assignment]
    stuck.start()
    assert stuck.entered.wait(3)
    panel.finish_inventory()
    assert stuck.parent() is None
    assert stuck in module._DETACHED
    panel.deleteLater()
    qt_app.processEvents()
    stuck.release.set()
    assert stuck.wait(3000)
    qt_app.processEvents()
    assert stuck not in module._DETACHED


def test_refresh_devices_handles_none_default_device(
    tmp_path: Path, qt_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    fake_sd = types.SimpleNamespace(
        PortAudioError=RuntimeError,
        default=types.SimpleNamespace(device=(None, None)),
        query_devices=lambda: [
            {"name": "Mikrofon (Test)", "hostapi": 0, "max_input_channels": 1},
        ],
        query_hostapis=lambda: [{"name": "MME"}],
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    panel = _panel(config(tmp_path))
    assert panel.device_combo.isEnabled()
    items = [panel.device_combo.itemText(i) for i in range(panel.device_combo.count())]
    assert "Sistem varsayılanı" in items
    assert any("Test" in item for item in items)
    assert not any("None" in item for item in items)
    assert panel.current_device.text() == "Seçilmedi"

    fake_sd.query_devices = lambda: []
    panel.refresh()
    assert panel.device_combo.itemText(0) == "Mikrofon bulunamadı"
    assert panel.current_device.text() == "Seçilmedi"
    panel.deleteLater()
    qt_app.processEvents()


def test_mail_port_binding_ignores_non_decimal_unicode_digits(qt_app) -> None:
    from PySide6.QtWidgets import QLineEdit

    from artemis.channels.desktop.settings_io import PANEL_BINDINGS

    binding = next(item for item in PANEL_BINDINGS if item.path == ("mail", "port"))
    edit = QLineEdit("²")
    try:
        assert binding.read(edit) == 993
    finally:
        edit.deleteLater()


def test_stt_download_done_marks_current_page_dirty(tmp_path: Path, qt_app) -> None:
    from artemis.channels.desktop.settings_panel import _DIRTY_ROLE

    panel = _panel(config(tmp_path))
    assert not panel._dirty
    row = panel.stack.currentIndex()
    panel._stt_download_done(tmp_path / "whisper-small")
    assert panel._dirty and panel.has_unsaved
    assert row in panel._dirty_rows
    assert panel.sections.item(row).data(_DIRTY_ROLE)
    panel.deleteLater()
    qt_app.processEvents()


def test_dictation_max_seconds_widget_persists_and_marks_dirty(tmp_path: Path, qt_app) -> None:
    path = config(tmp_path)
    panel = _panel(path)
    try:
        assert panel.dictation_max_seconds.value() == 120
        assert not panel._dirty
        panel.dictation_max_seconds.setValue(5)
        assert panel._dirty and panel.has_unsaved
        assert panel.values()[("dictation", "max_seconds")] == 5.0
        assert save_choices(path, None, values=panel.values())
        assert load_settings(path).dictation.max_seconds == 5.0
        panel.refresh()
        assert panel.dictation_max_seconds.value() == 5
    finally:
        panel.deleteLater()
        qt_app.processEvents()
