"""Voice settings tanıma dili seçimi ve Piper İngilizce ipucu testleri."""

import os
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from artemis.brain import providers
from artemis.channels.desktop import provider_section as section_module
from artemis.channels.desktop import settings_panel as panel_module
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.config.settings import load_settings
from artemis.i18n import set_language


@pytest.fixture(autouse=True)
def _vault(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(providers, "read_secret", lambda target: store.get(target))
    monkeypatch.setattr(section_module, "write_secret", lambda s, t: store.__setitem__(t, s))
    for name in ("BULUT", "BULUT2", "XAI"):
        monkeypatch.delenv(f"ARTEMIS_{name}_API_KEY", raising=False)
    return store


@pytest.fixture
def make_panel(tmp_path: Path):
    app = QApplication.instance() or QApplication([])
    panels: list[SettingsPanel] = []

    def _create(stt_language: str = "tr") -> tuple[SettingsPanel, Path]:
        config_path = tmp_path / f"config_{len(panels)}.yaml"
        config_path.write_text(yaml.safe_dump({
            "workspace": "desktop", "state_dir": "state", "audit_path": "audit.jsonl",
            "primary_model": "first",
            "openrouter_base_url": "http://127.0.0.1:8317/v1",
            "applications": {"brave": "brave.exe"}, "close_without_confirmation": ["brave"],
            "models": {"first": {"provider": "openrouter", "model": "first-model",
                                 "input_per_million": "0", "output_per_million": "0"}},
            "voice": {"stt_device": "cpu", "stt_model": "models/whisper",
                      "stt_language": stt_language},
        }), encoding="utf-8")
        panel = SettingsPanel(config_path)

        def handler(device) -> None:
            written = panel_module.save_choices(
                config_path, device, values=panel.values(), applications=panel.applications,
                code_agent=panel.code_agent_values(), stt_model=panel.stt_model_path(),
            )
            panel.mark_saved(written)

        panel.apply_requested.connect(handler)
        panels.append(panel)
        return panel, config_path

    yield _create

    for panel in panels:
        panel.deleteLater()
    app.processEvents()


def test_stt_language_combo_shows_config_value(make_panel) -> None:
    panel, _ = make_panel(stt_language="tr_en")
    assert panel.stt_language_combo.currentData() == "tr_en"


def test_stt_language_change_marks_dirty_and_saves_on_flush(make_panel) -> None:
    panel, path = make_panel(stt_language="tr")
    assert not panel._dirty
    index = panel.stt_language_combo.findData("en")
    assert index >= 0
    panel.stt_language_combo.setCurrentIndex(index)
    assert panel._dirty is True
    panel.flush()
    settings = load_settings(path)
    assert settings.voice.stt_language == "en"


def test_piper_hint_hidden_in_tr_visible_in_en_with_piper(make_panel) -> None:
    try:
        set_language("tr")
        panel_tr, _ = make_panel()
        piper_idx = panel_tr.tts_combo.findData("piper")
        assert piper_idx >= 0
        panel_tr.tts_combo.setCurrentIndex(piper_idx)
        assert panel_tr.tts_piper_hint.isHidden() is True

        set_language("en")
        panel_en, _ = make_panel()
        edge_idx = panel_en.tts_combo.findData("edge")
        piper_idx_en = panel_en.tts_combo.findData("piper")
        panel_en.tts_combo.setCurrentIndex(edge_idx)
        panel_en.tts_combo.setCurrentIndex(piper_idx_en)
        assert panel_en.tts_piper_hint.isHidden() is False
    finally:
        set_language("tr")
