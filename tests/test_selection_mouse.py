"""Fareyle metin seçimi algılayıcı ve ayar entegrasyonu testleri (ADR 0062)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from artemis.channels.desktop.selection_mouse import (
    MouseSnapshot,
    SelectionCandidate,
    SelectionMouseWatcher,
)
from artemis.config.settings import SelectionActionsConfig


def _harness(
    *,
    probe_result: tuple[str | None, bool, bool] = ("Merhaba dünya", False, True),
    own_ui: bool = False,
) -> tuple[
    SelectionMouseWatcher,
    list[SelectionCandidate],
    list[int],
    dict[str, tuple[str | None, bool, bool] | bool],
]:
    candidates: list[SelectionCandidate] = []
    dismisses: list[int] = []
    state: dict[str, Any] = {"probe": probe_result, "own_ui": own_ui}
    watcher = SelectionMouseWatcher(
        on_candidate=candidates.append,
        on_dismiss=lambda: dismisses.append(1),
        is_own_widget_at=lambda _x, _y: bool(state["own_ui"]),
        own_pid=999,
        probe_selection=lambda: state["probe"],
    )
    return watcher, candidates, dismisses, state


def test_drag_selection_emits_candidate_with_uia_text() -> None:
    watcher, candidates, dismisses, _ = _harness()
    watcher.step(MouseSnapshot(True, 100, 100, hwnd=10, pid=200, exe_path="notepad.exe"), 1.0)
    assert dismisses == [1]
    watcher.step(MouseSnapshot(False, 220, 100, hwnd=10, pid=200, exe_path="notepad.exe"), 1.3)
    assert candidates == [SelectionCandidate(220, 100, 10, pre_text="Merhaba dünya")]


def test_double_click_selection_triggers_single_click_does_not() -> None:
    watcher, candidates, _dismisses, _ = _harness()
    watcher.step(MouseSnapshot(True, 150, 200, hwnd=10, pid=200, exe_path="notepad.exe"), 1.0)
    watcher.step(MouseSnapshot(False, 151, 200, hwnd=10, pid=200, exe_path="notepad.exe"), 1.05)
    assert candidates == []

    watcher.step(MouseSnapshot(True, 151, 201, hwnd=10, pid=200, exe_path="notepad.exe"), 1.25)
    watcher.step(MouseSnapshot(False, 151, 201, hwnd=10, pid=200, exe_path="notepad.exe"), 1.30)
    assert candidates == [SelectionCandidate(151, 201, 10, pre_text="Merhaba dünya")]


def test_security_exclusions_ignore_own_blocked_password_and_nontext() -> None:
    watcher, candidates, _dismisses, state = _harness()

    # 1) Artemis'in kendi penceresi (own_pid=999) yok sayılır.
    watcher.step(MouseSnapshot(True, 10, 10, hwnd=10, pid=999, exe_path="python.exe"), 1.0)
    watcher.step(MouseSnapshot(False, 80, 10, hwnd=10, pid=999, exe_path="python.exe"), 1.2)
    assert candidates == []

    # 2) Engelli süreçler (terminal / parola yöneticisi) yok sayılır.
    for blocked in (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "KeePass.exe"):
        watcher.step(MouseSnapshot(True, 10, 10, hwnd=20, pid=300, exe_path=blocked), 2.0)
        watcher.step(MouseSnapshot(False, 90, 10, hwnd=20, pid=300, exe_path=blocked), 2.2)
        assert candidates == []

    # 3) Parola alanı yok sayılır.
    state["probe"] = ("gizli-parola", True, True)
    watcher.step(MouseSnapshot(True, 10, 10, hwnd=30, pid=400, exe_path="chrome.exe"), 3.0)
    watcher.step(MouseSnapshot(False, 90, 10, hwnd=30, pid=400, exe_path="chrome.exe"), 3.2)
    assert candidates == []

    # 4) Metin dışı UIA denetimi + boş metin yok sayılır.
    state["probe"] = (None, False, False)
    watcher.step(MouseSnapshot(True, 10, 10, hwnd=30, pid=400, exe_path="chrome.exe"), 4.0)
    watcher.step(MouseSnapshot(False, 90, 10, hwnd=30, pid=400, exe_path="chrome.exe"), 4.2)
    assert candidates == []

    # 5) Metin benzeri UIA denetimi (TextPattern boş) `pre_text=None` adayı üretir.
    state["probe"] = (None, False, True)
    watcher.step(MouseSnapshot(True, 10, 10, hwnd=30, pid=400, exe_path="chrome.exe"), 5.0)
    watcher.step(MouseSnapshot(False, 90, 10, hwnd=30, pid=400, exe_path="chrome.exe"), 5.2)
    assert candidates == [SelectionCandidate(90, 10, 30, pre_text=None)]


def test_clicking_own_badge_does_not_dismiss() -> None:
    watcher, candidates, dismisses, state = _harness(own_ui=True)
    watcher.step(MouseSnapshot(True, 300, 200, hwnd=10, pid=200, exe_path="notepad.exe"), 1.0)
    watcher.step(MouseSnapshot(False, 300, 200, hwnd=10, pid=200, exe_path="notepad.exe"), 1.1)
    assert dismisses == []
    assert candidates == []

    state["own_ui"] = False
    watcher.step(MouseSnapshot(True, 50, 50, hwnd=10, pid=200, exe_path="notepad.exe"), 2.0)
    assert dismisses == [1]


def test_mouse_popup_setting_defaults_and_settings_panel_sync(tmp_path: Path) -> None:
    assert SelectionActionsConfig().mouse_popup is True

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from artemis.channels.desktop.settings_io import save_choices
    from artemis.channels.desktop.settings_panel import SettingsPanel
    from artemis.config.settings import load_settings
    from tests.test_settings_panel import config

    app = QApplication.instance() or QApplication([])
    cfg = config(tmp_path)
    panel = SettingsPanel(cfg)
    try:
        assert panel.selection_mouse_popup.isChecked() is True
        assert not panel.selection_mouse_popup.isEnabled()
        panel.selection_enabled.setChecked(True)
        assert panel.selection_mouse_popup.isEnabled()
        panel.selection_mouse_popup.setChecked(False)
        assert save_choices(cfg, None, values=panel.values()) is True
        loaded = load_settings(cfg).selection_actions
        assert loaded.enabled is True
        assert loaded.mouse_popup is False
        panel.refresh()
        assert panel.selection_mouse_popup.isEnabled()
        assert panel.selection_mouse_popup.isChecked() is False
    finally:
        panel.finish_inventory()
        panel.deleteLater()
        app.processEvents()
