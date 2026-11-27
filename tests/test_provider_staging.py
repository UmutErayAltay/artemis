"""Model rol seçimlerinin (Beyin, Zor işler, Yedek, Bilgisayar, Düzen uygula) Kaydet'e dek
bekletilmesi (staging) testleri."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.config.settings import load_settings


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication([])


def _two_models_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({
            "workspace": "desktop",
            "state_dir": "state",
            "audit_path": "audit.jsonl",
            "primary_model": "first",
            "endpoints": {
                "local": {"base_url": "http://127.0.0.1:8317/v1", "api_key": False},
            },
            "models": {
                "first": {
                    "provider": "openai_compatible",
                    "endpoint": "local",
                    "model": "first-model",
                    "input_per_million": "0",
                    "output_per_million": "0",
                },
                "second": {
                    "provider": "openai_compatible",
                    "endpoint": "local",
                    "model": "second-model",
                    "input_per_million": "0",
                    "output_per_million": "0",
                },
            },
            "voice": {"stt_device": "cpu", "stt_model": "models/whisper"},
        }),
        encoding="utf-8",
    )
    return path


def test_clicking_primary_stages_without_writing_file(
    tmp_path: Path, qt_app: QApplication
) -> None:
    path = _two_models_config(tmp_path)
    before = path.read_bytes()
    panel = SettingsPanel(path)
    changed: list[int] = []
    panel.providers.changed.connect(lambda: changed.append(1))

    panel.providers.select_model("second")
    panel.providers.primary_button.click()

    assert path.read_bytes() == before
    assert load_settings(path).primary_model == "first"
    assert panel.providers.has_pending
    assert panel.has_unsaved
    assert changed == []
    assert "Beyin: second" in panel.providers.banner.text()
    assert "Kaydet ile uygulanır" in panel.providers.status.text()
    panel.deleteLater()


def test_flush_commits_pending_primary_and_clears_unsaved(
    tmp_path: Path, qt_app: QApplication
) -> None:
    path = _two_models_config(tmp_path)
    panel = SettingsPanel(path)
    changed: list[int] = []
    panel.providers.changed.connect(lambda: changed.append(1))

    panel.providers.select_model("second")
    panel.providers.primary_button.click()
    assert panel.has_unsaved

    panel.flush()

    assert not panel.providers.has_pending
    assert not panel.has_unsaved
    assert load_settings(path).primary_model == "second"
    assert changed == [1]
    panel.deleteLater()


def test_discard_changes_drops_pending_and_restores_old_primary(
    tmp_path: Path, qt_app: QApplication
) -> None:
    path = _two_models_config(tmp_path)
    before = path.read_bytes()
    panel = SettingsPanel(path)

    panel.providers.select_model("second")
    panel.providers.primary_button.click()
    assert panel.has_unsaved and panel.providers.has_pending

    panel.discard_changes()

    assert path.read_bytes() == before
    assert not panel.providers.has_pending
    assert not panel.has_unsaved
    assert "Beyin: first" in panel.providers.banner.text()
    rows = dict(panel.providers.model_rows())
    assert "Beyin" in rows["first"] and "Beyin" not in rows["second"]
    panel.deleteLater()


def test_fallback_then_primary_on_same_model_drops_fallback_after_commit(
    tmp_path: Path, qt_app: QApplication
) -> None:
    path = _two_models_config(tmp_path)
    before = path.read_bytes()
    panel = SettingsPanel(path)

    panel.providers.select_model("second")
    panel.providers.fallback_button.click()
    assert panel.providers.fallback_button.isChecked()
    assert "Yedek: second" in panel.providers.banner.text()
    assert path.read_bytes() == before

    panel.providers.primary_button.click()
    assert panel.providers.primary_button.isChecked()
    assert not panel.providers.fallback_button.isChecked()
    assert "Yedek" not in panel.providers.banner.text()
    assert path.read_bytes() == before

    panel.flush()

    saved = load_settings(path)
    assert saved.primary_model == "second"
    assert saved.fallback_model is None
    assert not panel.has_unsaved
    panel.deleteLater()


def test_light_mode_combo_stages_and_commits(tmp_path: Path, qt_app: QApplication) -> None:
    path = _two_models_config(tmp_path)
    panel = SettingsPanel(path)
    panel.providers.select_model("second")
    assert panel.providers.light_combo.currentData() is None  # Otomatik

    panel.providers.light_combo.setCurrentIndex(panel.providers.light_combo.findData(True))
    panel.providers.light_combo.activated.emit(panel.providers.light_combo.currentIndex())

    assert load_settings(path).models["second"].light_mode is None  # henüz yazılmadı
    assert panel.providers.has_pending
    assert panel.providers.light_combo.currentData() is True
    panel.flush()
    assert load_settings(path).models["second"].light_mode is True
    panel.deleteLater()
