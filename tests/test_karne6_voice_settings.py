"""Karne 6 V1/V3: ayar paneli GPU sorgusu ve kurulu model aramasının arayüz iş parçacığı yükü."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from artemis.channels.desktop import voice_settings_section as vss
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.voice import stt_catalog
from artemis.config.settings import Settings


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> SettingsPanel:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(Settings().model_dump(mode="json")), encoding="utf-8")

    def fake_installed(name: str) -> Path | None:
        calls.append(name)
        return None

    monkeypatch.setattr(stt_catalog, "installed_path", fake_installed)
    monkeypatch.setattr(
        stt_catalog,
        "gpu_memory_mb",
        lambda: (_ for _ in ()).throw(AssertionError("GUI iş parçacığında çağrılmamalı")),
    )
    return SettingsPanel(config_path)


def test_panel_opens_without_gpu_query_and_updates_recommendation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp: QApplication
) -> None:
    panel = _panel(tmp_path, monkeypatch, [])
    try:
        assert panel._gpu_mb is None
        assert panel._recommended_stt == "small"
        vss.gpu_probe_done(panel, 8000)
        assert panel._recommended_stt == "large-v3"
        assert "önerilen" in panel.stt_combo.itemText(panel.stt_combo.findData("large-v3"))
    finally:
        panel.finish_inventory()


def test_gpu_probe_thread_emits_value(
    monkeypatch: pytest.MonkeyPatch, qapp: QApplication
) -> None:
    monkeypatch.setattr(stt_catalog, "gpu_memory_mb", lambda: 4096)
    probe = vss.GpuProbe()
    got: list[object] = []
    probe.done.connect(got.append)
    probe.run()  # iş parçacığı başlatmadan gövdeyi doğrudan dener
    assert got == [4096]


def test_installed_lookup_runs_once_per_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp: QApplication
) -> None:
    calls: list[str] = []
    panel = _panel(tmp_path, monkeypatch, calls)
    try:
        calls.clear()
        panel._populate_stt_choices("small")
        per_refresh = len(calls)
        assert per_refresh == len(stt_catalog.CATALOG)
        panel._update_stt_download_button()
        panel.stt_model_path()
        assert len(calls) == per_refresh
    finally:
        panel.finish_inventory()
