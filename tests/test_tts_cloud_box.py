"""Ayarlar > Ses > Konuşma bulut TTS kutusu ve model/ses katalog ayrıştırıcıları."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from artemis.brain import providers
from artemis.channels.desktop import provider_section as section_module
from artemis.channels.desktop.settings_panel import SettingsPanel, save_choices
from artemis.channels.desktop.tts_cloud_box import (
    GEMINI_TTS_VOICES,
    parse_gemini_tts_models,
    parse_openai_tts_models,
)
from artemis.config.settings import load_settings
from tests.test_settings_panel import config


@pytest.fixture(autouse=True)
def _vault(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(providers, "read_secret", lambda target: store.get(target))
    monkeypatch.setattr(section_module, "write_secret", lambda s, t: store.__setitem__(t, s))
    return store


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def test_tts_cloud_box_visibility_follows_tts_combo(tmp_path: Path, qt_app: QApplication) -> None:
    panel = SettingsPanel(config(tmp_path, cloud=True))
    panel.show()
    panel.tts_combo.setCurrentIndex(panel.tts_combo.findData("piper"))
    assert not panel.tts_cloud_box.isVisible()

    panel.tts_combo.setCurrentIndex(panel.tts_combo.findData("cloud"))
    assert panel.tts_cloud_box.isVisible()
    panel.tts_cloud_server.setCurrentIndex(panel.tts_cloud_server.findData("bulut"))
    assert "kayıtlı değil" in panel.tts_cloud_hint.text()
    assert "Ücretli modele izin ver" in panel.tts_cloud_hint.text()
    assert "Metin gönderme izni kapalıyken" in panel.tts_cloud_hint.text()
    panel.deleteLater()
    qt_app.processEvents()


def test_tts_cloud_save_roundtrip(tmp_path: Path, qt_app: QApplication) -> None:
    path = config(tmp_path, cloud=True)
    panel = SettingsPanel(path)
    panel.tts_combo.setCurrentIndex(panel.tts_combo.findData("cloud"))
    panel.tts_cloud_server.setCurrentIndex(panel.tts_cloud_server.findData("bulut"))
    panel.tts_cloud_protocol.setCurrentIndex(panel.tts_cloud_protocol.findData("gemini"))
    panel.tts_cloud_model.setEditText("gemini-2.5-flash-preview-tts")
    panel.tts_cloud_voice.setEditText("Kore")
    panel.tts_cloud_consent.setChecked(True)
    panel.tts_cloud_paid.setChecked(True)
    assert not panel.tts_cloud_emotion_tags.isEnabled()
    panel.tts_cloud_emotion.setChecked(True)
    assert panel.tts_cloud_emotion_tags.isEnabled()
    panel.tts_cloud_emotion_tags.setText("[laugh] [whisper]")

    save_choices(path, None, values=panel.values())
    voice = load_settings(path).voice
    assert voice.tts_backend == "cloud"
    assert voice.tts_cloud_endpoint == "bulut"
    assert voice.tts_cloud_protocol == "gemini"
    assert voice.tts_cloud_model == "gemini-2.5-flash-preview-tts"
    assert voice.tts_cloud_voice == "Kore"
    assert voice.allow_cloud_tts_text is True
    assert voice.tts_cloud_allow_paid is True
    assert voice.tts_cloud_emotion_tags is True
    assert voice.tts_cloud_emotion_tag_list == ("[laugh]", "[whisper]")

    reloaded = SettingsPanel(path)
    assert reloaded.tts_combo.currentData() == "cloud"
    assert reloaded.tts_cloud_server.currentData() == "bulut"
    assert reloaded.tts_cloud_protocol.currentData() == "gemini"
    assert reloaded.tts_cloud_model.currentText() == "gemini-2.5-flash-preview-tts"
    assert reloaded.tts_cloud_voice.currentText() == "Kore"
    assert reloaded.tts_cloud_consent.isChecked() is True
    assert reloaded.tts_cloud_paid.isChecked() is True
    assert reloaded.tts_cloud_emotion.isChecked() is True
    assert reloaded.tts_cloud_emotion_tags.text() == "[laugh] [whisper]"
    assert reloaded.tts_cloud_emotion_tags.isEnabled() is True

    panel.deleteLater()
    reloaded.deleteLater()
    qt_app.processEvents()


def test_parse_openai_tts_models_sorts_free_first_and_extracts_voices() -> None:
    payload = {
        "data": [
            {"id": "openai/tts-1", "supported_voices": ["alloy", "nova"]},
            {"id": "heygen/heygen-voice-1-turbo:free", "supported_voices": ["tr-emel"]},
            {"id": "alpha/paid-tts"},
            {"id": ""},
        ]
    }
    parsed = parse_openai_tts_models(payload)
    assert parsed == [
        ("heygen/heygen-voice-1-turbo:free", ["tr-emel"]),
        ("alpha/paid-tts", []),
        ("openai/tts-1", ["alloy", "nova"]),
    ]
    assert parse_openai_tts_models({"unexpected": []}) == []


def test_parse_gemini_tts_models_filters_tts_and_strips_prefix() -> None:
    payload = {
        "models": [
            {"name": "models/gemini-2.5-flash"},
            {"name": "models/gemini-2.5-flash-preview-tts"},
            {"name": "models/gemini-2.5-pro-preview-tts"},
        ]
    }
    parsed = parse_gemini_tts_models(payload)
    assert [model_id for model_id, _ in parsed] == [
        "gemini-2.5-flash-preview-tts",
        "gemini-2.5-pro-preview-tts",
    ]
    assert all(voices == list(GEMINI_TTS_VOICES) for _, voices in parsed)
    assert "Kore" in GEMINI_TTS_VOICES and len(GEMINI_TTS_VOICES) == 30


def test_tts_cloud_box_close_detaches_running_fetch_thread(
    tmp_path: Path, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from artemis.channels.desktop import tts_cloud_box as mod
    from artemis.channels.desktop.tts_cloud_box import TtsCloudBox

    entered = threading.Event()
    release = threading.Event()

    def slow_fetch(
        _base_url: str, _protocol: str, _key: str | None
    ) -> tuple[str, list[tuple[str, list[str]]]]:
        entered.set()
        release.wait(5)
        return "Bağlantı tamam, 0 ses modeli bulundu.", []

    monkeypatch.setattr(mod, "fetch_tts_models", slow_fetch)
    monkeypatch.setattr(mod, "_SHUTDOWN_WAIT_MS", 50)

    panel = SettingsPanel(config(tmp_path, cloud=True))
    panel.tts_combo.setCurrentIndex(panel.tts_combo.findData("cloud"))
    panel.tts_cloud_server.setCurrentIndex(panel.tts_cloud_server.findData("bulut"))
    box = panel.tts_cloud_box
    assert isinstance(box, TtsCloudBox)
    panel.tts_cloud_fetch_button.click()
    assert entered.wait(3)

    thread = box._fetch
    assert thread is not None and thread.isRunning() and thread.parent() is box

    box.close()
    assert thread.parent() is None
    assert thread in mod._DETACHED
    panel.deleteLater()
    qt_app.processEvents()

    release.set()
    assert thread.wait(3000)
    qt_app.processEvents()
    assert thread not in mod._DETACHED
