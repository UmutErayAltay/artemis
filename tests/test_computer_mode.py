"""Bilgisayar kullanımı kipi: köprü (Qt'siz, sahte döngü) ve üst çubuk düğmesi (offscreen Qt)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from artemis.channels.desktop.bridge import DesktopBridge
from artemis.config.settings import Settings


class FakeLoop:
    """call_soon_threadsafe'i hemen çalıştırır; `closed` ise gerçek döngü gibi RuntimeError atar."""

    def __init__(self, closed: bool = False) -> None:
        self.closed = closed

    def call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
        if self.closed:
            raise RuntimeError("Event loop is closed")
        callback(*args)


def _bridge(tmp_path: Path, loop: FakeLoop | None, with_runtime: bool = True) -> DesktopBridge:
    bridge = object.__new__(DesktopBridge)  # worker iş parçacığı başlatılmaz
    bridge._loop = loop  # type: ignore[assignment]
    context = SimpleNamespace(settings=Settings(workspace=tmp_path))
    bridge._runtime = SimpleNamespace(context=context) if with_runtime else None  # type: ignore[assignment]
    return bridge


def test_bridge_set_computer_mode_replaces_runtime_settings(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, FakeLoop())
    runtime: Any = bridge._runtime
    before = runtime.context.settings

    assert bridge.set_computer_mode("auto") is True
    assert runtime.context.settings.computer_use.mode == "auto"
    assert before.computer_use.mode == "accept"  # eski ayar nesnesi değişmez (kopya)

    assert bridge.set_computer_mode("accept") is True
    assert runtime.context.settings.computer_use.mode == "accept"


def test_bridge_set_computer_mode_rejects_bad_mode_and_missing_runtime(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, FakeLoop())
    assert bridge.set_computer_mode("yolo") is False
    assert bridge._runtime.context.settings.computer_use.mode == "accept"  # type: ignore[union-attr]

    assert _bridge(tmp_path, None).set_computer_mode("auto") is False
    assert _bridge(tmp_path, FakeLoop(), with_runtime=False).set_computer_mode("auto") is False
    assert _bridge(tmp_path, FakeLoop(closed=True)).set_computer_mode("auto") is False


# --- Üst çubuk düğmesi -------------------------------------------------------------------


@pytest.fixture
def window(tmp_path: Path) -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from tests.test_desktop_gui import _close_cleanly, _window

    current = QApplication.instance()
    app = current if isinstance(current, QApplication) else QApplication([])
    app.setQuitOnLastWindowClosed(False)
    window = _window(app, tmp_path)
    real_bridge = window.bridge
    window._demo = False  # kayıt yolunu denemek için; köprü ve kayıt aşağıda sahte
    calls: list[str] = []
    window.bridge = SimpleNamespace(set_computer_mode=lambda mode: calls.append(mode) or True)
    window.calls = calls
    yield window
    window._demo = True
    window.bridge = real_bridge
    _close_cleanly(app, window)


def test_toggle_writes_config_first_then_applies_runtime(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import artemis.channels.desktop.app as app_module

    order: list[str] = []
    monkeypatch.setattr(app_module, "save_computer_mode", lambda path, mode: order.append("save"))
    window.bridge = SimpleNamespace(
        set_computer_mode=lambda mode: order.append("runtime") or True
    )

    window.auto_button.setChecked(True)  # üst çubuk düğmesi sinyali tetikler

    assert order == ["save", "runtime"]
    assert window.settings_panel.computer_auto.isChecked() is True
    assert window.auto_button.text() == "Oto onay: açık"


def test_failed_save_leaves_runtime_untouched_and_restores_both_controls(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import artemis.channels.desktop.app as app_module

    def broken(path: Path, mode: str) -> None:
        raise OSError("disk dolu")

    monkeypatch.setattr(app_module, "save_computer_mode", broken)

    window.auto_button.setChecked(True)

    assert window.calls == []  # config yazılamadı: runtime kipi değişmedi
    assert window.auto_button.isChecked() is False
    assert window.settings_panel.computer_auto.isChecked() is False
    assert window.auto_button.text() == "Oto onay: kapalı"


def test_settings_checkbox_keeps_header_button_in_sync(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import artemis.channels.desktop.app as app_module

    monkeypatch.setattr(app_module, "save_computer_mode", lambda path, mode: None)

    window.settings_panel.computer_auto.setChecked(True)  # ayar kutusundan

    assert window.auto_button.isChecked() is True
    assert window.calls == ["auto"]
