"""Ayarlar'dan çıkarken ve kapanışta kaydedilmemiş değişiklik onayı testleri."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from artemis.channels.desktop import window_restart


@dataclass
class _FakePanel:
    has_unsaved: bool = False
    save_succeeds: bool = True
    flushed: int = 0
    discarded: int = 0

    def flush(self) -> None:
        self.flushed += 1
        if self.save_succeeds:
            self.has_unsaved = False

    def discard_changes(self) -> None:
        self.discarded += 1
        self.has_unsaved = False


@dataclass
class _FakeWindow:
    settings_panel: _FakePanel = field(default_factory=_FakePanel)


def test_leave_settings_clean_returns_true_without_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[object] = []
    monkeypatch.setattr(window_restart, "_ask_unsaved", lambda w: asked.append(w) or "cancel")
    window = _FakeWindow(_FakePanel(has_unsaved=False))
    assert window_restart.leave_settings(window) is True  # type: ignore[arg-type]
    assert asked == []
    assert window.settings_panel.flushed == 0
    assert window.settings_panel.discarded == 0


def test_leave_settings_save_ok_flushes_and_allows_leave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(window_restart, "_ask_unsaved", lambda _w: "save")
    window = _FakeWindow(_FakePanel(has_unsaved=True, save_succeeds=True))
    assert window_restart.leave_settings(window) is True  # type: ignore[arg-type]
    assert window.settings_panel.flushed == 1
    assert not window.settings_panel.has_unsaved


def test_leave_settings_save_failure_blocks_leave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(window_restart, "_ask_unsaved", lambda _w: "save")
    window = _FakeWindow(_FakePanel(has_unsaved=True, save_succeeds=False))
    assert window_restart.leave_settings(window) is False  # type: ignore[arg-type]
    assert window.settings_panel.flushed == 1
    assert window.settings_panel.has_unsaved


def test_leave_settings_discard_restores_and_allows_leave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(window_restart, "_ask_unsaved", lambda _w: "discard")
    window = _FakeWindow(_FakePanel(has_unsaved=True))
    assert window_restart.leave_settings(window) is True  # type: ignore[arg-type]
    assert window.settings_panel.discarded == 1
    assert window.settings_panel.flushed == 0
    assert not window.settings_panel.has_unsaved


def test_leave_settings_cancel_keeps_dirty_and_blocks_leave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(window_restart, "_ask_unsaved", lambda _w: "cancel")
    window = _FakeWindow(_FakePanel(has_unsaved=True))
    assert window_restart.leave_settings(window) is False  # type: ignore[arg-type]
    assert window.settings_panel.flushed == 0
    assert window.settings_panel.discarded == 0
    assert window.settings_panel.has_unsaved


def test_discard_changes_on_real_panel_reloads_saved_config(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.config.settings import load_settings

    QApplication.instance() or QApplication([])
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({
            "workspace": "desktop", "state_dir": "state", "audit_path": "audit.jsonl",
            "primary_model": "first",
            "models": {"first": {"provider": "openrouter", "model": "first-model",
                                 "input_per_million": "0", "output_per_million": "0"}},
            "voice": {"stt_device": "cpu", "stt_model": "models/whisper"},
        }),
        encoding="utf-8",
    )
    panel = SettingsPanel(path)
    original_wake = panel.wake_name.text()
    original_barge = panel.barge_in_box.isChecked()
    panel.wake_name.setText("BaskaAd")
    panel.barge_in_box.setChecked(not original_barge)
    panel.save_failed("Kayıt hatası")
    assert panel.has_unsaved and not panel.retry_button.isHidden()

    panel.discard_changes()

    assert not panel.has_unsaved
    assert panel.wake_name.text() == original_wake
    assert panel.barge_in_box.isChecked() == original_barge
    assert panel.retry_button.isHidden()
    assert panel.notice.text() == ""
    assert load_settings(path).voice.wake_word == original_wake
    panel.deleteLater()
