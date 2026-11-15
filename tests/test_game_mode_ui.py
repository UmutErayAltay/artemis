"""Ses oyun modu masaüstü arayüzü ve ayarlar paneli testleri (ADR 0072)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
import yaml

os.environ["QT_QPA_PLATFORM"] = "offscreen"
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from artemis.channels.desktop.bridge import DesktopBridge, State
from artemis.channels.desktop.settings_io import save_choices
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.desktop.window_events import handle_state
from artemis.config.settings import Settings, load_settings


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_bridge_maps_game_state_to_voice_game_event(tmp_path):
    bridge = DesktopBridge(tmp_path / "unused.yaml", lambda *_a, **_k: None)
    try:
        bridge._on_voice_state("game:on")
        bridge._on_voice_state("game:off")
        bridge._on_voice_state("game:error")
        bridge._on_voice_state("game:unavailable")
        events = [(e.state, e.message) for e in bridge.poll_events() if isinstance(e, State)]
        assert ("voice_game", "on") in events
        assert ("voice_game", "off") in events
        assert ("voice_game", "error") in events
        assert ("voice_game", "unavailable") in events
    finally:
        bridge.shutdown()


@pytest.mark.parametrize(
    ("value", "expected_fragment"),
    [
        ("on", "Oyun modu: tam ekran uygulama algılandı"),
        ("off", "Oyun modu bitti"),
        ("error", "Oyun modu model geçişi başarısız oldu"),
        ("unavailable", "Oyun modu açık ama hafif model"),
    ],
)
def test_window_events_game_mode_messages(value, expected_fragment):
    window = MagicMock()
    window._active_request = None
    handle_state(window, State("voice_game", value))
    assert window._append_chat.call_count == 1
    speaker, text = window._append_chat.call_args[0]
    assert speaker == "Artemis"
    assert expected_fragment in text


def test_window_events_game_mode_unknown_ignored():
    window = MagicMock()
    window._active_request = None
    handle_state(window, State("voice_game", "unknown_mode"))
    assert window._append_chat.call_count == 0


def test_game_mode_settings_round_trip(tmp_path, qapp):
    config_path = tmp_path / "config.yaml"
    initial_settings = Settings()
    config_path.write_text(
        yaml.safe_dump(initial_settings.model_dump(mode="json")), encoding="utf-8"
    )

    panel = SettingsPanel(config_path)
    try:
        assert hasattr(panel, "game_mode")
        assert panel.game_mode.isChecked() is False
        assert "ekran kartı belleğinden bırakılır" in panel.game_mode.toolTip()

        panel.game_mode.setChecked(True)
        saved = save_choices(config_path, None, values=panel.values())
        assert saved is True

        reloaded = load_settings(config_path)
        assert reloaded.voice.game_mode is True

        panel.refresh()
        assert panel.game_mode.isChecked() is True
    finally:
        panel.finish_inventory()
