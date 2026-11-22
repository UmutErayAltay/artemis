"""Faz 1 testleri: 9 sayfa yönlendirme/arama/kirli izleme, kısayollar ve gizlilik ayarları."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from artemis.brain.fake import ScriptedProvider
from artemis.channels.desktop import app as app_module
from artemis.channels.desktop import bridge as bridge_module
from artemis.channels.desktop import hotkey_manager, window_restart
from artemis.channels.desktop.app import build_window
from artemis.channels.desktop.settings_io import PANEL_BINDINGS, save_choices
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.desktop.settings_widgets import _DIRTY_ROLE
from artemis.channels.runtime import create_runtime
from artemis.config.settings import load_settings
from artemis.i18n import set_language
from tests.test_desktop_gui import _close_cleanly
from tests.test_settings_panel import config


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _restore_lang() -> Any:
    set_language("tr")
    try:
        yield
    finally:
        set_language("tr")


@pytest.mark.parametrize("lang", ["tr", "en"])
def test_legacy_and_subtab_routing_in_tr_and_en(
    tmp_path: Path, qt_app: QApplication, lang: str
) -> None:
    set_language(lang)
    panel = SettingsPanel(config(tmp_path))
    try:
        assert len(panel.section_names()) == 9
        assert panel.select_section("Yanıt (Çıkış)")
        assert panel.sections.currentRow() == 0
        assert panel.voice_tabs.currentIndex() == 1

        assert panel.select_section("Dinleme (Giriş)")
        assert panel.sections.currentRow() == 0
        assert panel.voice_tabs.currentIndex() == 0

        assert panel.select_section("Model")
        assert panel.sections.currentRow() == 2

        assert panel.select_section("Model Ekle ve Bağlantılar")
        assert panel.sections.currentRow() == 2
        assert panel.providers.tabs.currentIndex() == 1

        automation_subs = (
            ("Rutinler", 0),
            ("Tetikleyiciler", 1),
            ("Gün özeti", 2),
            ("Araştırma", 3),
        )
        for key, idx in automation_subs:
            assert panel.select_section(key)
            assert panel.sections.currentRow() == 4
            assert panel.automation_tabs.currentIndex() == idx

        for key, idx in (("Beceriler", 0), ("Dosya araması", 1)):
            assert panel.select_section(key)
            assert panel.sections.currentRow() == 5
            assert panel.knowledge_tabs.currentIndex() == idx

        for key, idx in (("Telegram", 0), ("Takvim ve posta", 1), ("GitHub", 2)):
            assert panel.select_section(key)
            assert panel.sections.currentRow() == 6
            assert panel.connections_tabs.currentIndex() == idx

        assert panel.select_section("Uygulamalar")
        assert panel.sections.currentRow() == 7
        assert panel.select_section("Hakkında")
        assert panel.sections.currentRow() == 8
        assert not panel.select_section("Böyle Bir Bölüm Yok")
    finally:
        panel.deleteLater()
        qt_app.processEvents()


def test_search_indexes_subtab_titles_and_dirty_tracks_owner_row(
    tmp_path: Path, qt_app: QApplication
) -> None:
    panel = SettingsPanel(config(tmp_path))
    try:
        panel.search.setText("Yanıt (Çıkış)")
        visible = [
            panel.sections.item(r).text()
            for r in range(panel.sections.count())
            if not panel.sections.item(r).isHidden()
        ]
        assert visible == ["Ses"]
        panel.search.clear()

        panel.show_section(0)
        panel.allow_cloud_clipboard.setChecked(True)
        apps_row = panel.section_names().index("Uygulamalar ve Güvenlik")
        assert panel.sections.item(apps_row).data(_DIRTY_ROLE) is True
        assert not panel.sections.item(0).data(_DIRTY_ROLE)
    finally:
        panel.deleteLater()
        qt_app.processEvents()


def test_validate_hotkeys_syntax_and_all_pairwise_conflicts() -> None:
    assert (
        hotkey_manager.validate_hotkeys(
            listening="", dictation="ctrl+alt+d", selection="ctrl+alt+s"
        )
        is None
    )
    assert (
        hotkey_manager.validate_hotkeys(
            listening="space", dictation="ctrl+alt+d", selection="ctrl+alt+s"
        )
        is not None
    )
    assert (
        hotkey_manager.validate_hotkeys(
            listening="ctrl+badkey", dictation="ctrl+alt+d", selection="ctrl+alt+s"
        )
        is not None
    )
    assert (
        hotkey_manager.validate_hotkeys(
            listening="ctrl+space", dictation="", selection="ctrl+alt+s"
        )
        is not None
    )
    assert (
        hotkey_manager.validate_hotkeys(
            listening="ctrl+space", dictation="ctrl+alt+d", selection="a"
        )
        is not None
    )
    assert (
        hotkey_manager.validate_hotkeys(
            listening="ctrl+space", dictation="space+ctrl", selection="ctrl+alt+s"
        )
        is not None
    )
    assert (
        hotkey_manager.validate_hotkeys(
            listening="ctrl+space", dictation="ctrl+alt+d", selection="space+ctrl"
        )
        is not None
    )
    assert (
        hotkey_manager.validate_hotkeys(
            listening="ctrl+space", dictation="ctrl+alt+d", selection="alt+ctrl+d"
        )
        is not None
    )


def test_apply_settings_validates_listening_hotkey_from_panel_values(
    tmp_path: Path,
) -> None:
    path = config(tmp_path)
    failed: list[str] = []
    marked: list[bool] = []
    reloaded: list[int] = []
    values = {
        ("voice", "listening_hotkey"): "ctrl+shift+v",
        ("dictation", "hotkey"): "ctrl+shift+v",
        ("selection_actions", "hotkey"): "ctrl+alt+s",
    }
    panel = SimpleNamespace(
        values=lambda: values,
        applications={"brave": "brave.exe"},
        code_agent_values=lambda: {},
        stt_model_path=lambda: None,
        mark_saved=marked.append,
        save_failed=failed.append,
    )
    window = SimpleNamespace(
        settings_panel=panel,
        bridge=SimpleNamespace(reload_dictation_settings=lambda _cfg: True),
        _reload_hotkeys=lambda: reloaded.append(1),
        _demo=False,
        _exiting=False,
        _config_path=path,
    )

    window_restart.apply_settings(window, None)  # type: ignore[arg-type]
    assert len(failed) == 1 and marked == []

    failed.clear()
    values[("voice", "listening_hotkey")] = "ctrl+bad!"
    values[("dictation", "hotkey")] = "ctrl+alt+d"
    window_restart.apply_settings(window, None)  # type: ignore[arg-type]
    assert len(failed) == 1 and marked == []

    failed.clear()
    values[("voice", "listening_hotkey")] = ""
    window_restart.apply_settings(window, None)  # type: ignore[arg-type]
    assert failed == [] and marked == [True] and reloaded == [1]
    assert load_settings(path).voice.listening_hotkey == ""


def test_start_hotkey_stops_prior_and_never_duplicates_signal(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Any] = []

    class _FakeHotkey:
        def __init__(self, text: str, on_press: Any) -> None:
            self.text = text
            self.on_press = on_press
            self.stopped = 0
            created.append(self)

        def start(self) -> bool:
            return True

        def stop(self) -> None:
            self.stopped += 1

    def factory(config_path: Path, approve: Any, **kwargs: Any) -> Any:
        return create_runtime(
            config_path,
            approve,
            settings=load_settings(config_path),
            provider=ScriptedProvider([]),
            **kwargs,
        )

    monkeypatch.setattr(bridge_module, "create_runtime", factory)
    monkeypatch.setattr(SettingsPanel, "_refresh_devices", lambda *_: None)
    monkeypatch.setattr(app_module, "GlobalHotkey", _FakeHotkey)
    window = build_window(config(tmp_path), app=qt_app)
    try:
        clicks: list[int] = []
        monkeypatch.setattr(window, "voice_button_click", lambda: clicks.append(1))
        before = len(created)
        window._start_hotkey()
        window._start_hotkey()
        assert len(created) == before + 2
        assert created[-2].stopped == 1
        window._hotkey_pressed.emit()
        qt_app.processEvents()
        assert clicks == [1]
    finally:
        _close_cleanly(qt_app, window)


def test_privacy_encryption_and_close_without_confirmation_app_sync(
    tmp_path: Path, qt_app: QApplication
) -> None:
    path = config(tmp_path)
    exe = tmp_path / "demoapp.exe"
    exe.write_text("exe", encoding="utf-8")
    panel = SettingsPanel(path)
    try:
        assert panel.close_without_confirmation_list.count() == 1
        item = panel.close_without_confirmation_list.item(0)
        assert item.data(Qt.ItemDataRole.UserRole) == "brave"
        assert item.checkState() == Qt.CheckState.Checked

        panel.allow_cloud_file_content.setChecked(True)
        panel.allow_cloud_clipboard.setChecked(True)
        panel.memory_encrypt_at_rest.setChecked(True)

        # Uygulama eklenince yeni öğe unchecked eklenir, mevcut işaret korunur.
        panel.applications["demoapp"] = str(exe)
        panel._refresh_applications()
        assert panel.close_without_confirmation_list.count() == 2
        states = {
            panel.close_without_confirmation_list.item(i).data(Qt.ItemDataRole.UserRole): (
                panel.close_without_confirmation_list.item(i).checkState()
            )
            for i in range(panel.close_without_confirmation_list.count())
        }
        assert states == {"brave": Qt.CheckState.Checked, "demoapp": Qt.CheckState.Unchecked}

        # Tüm kurulu uygulamalar açılınca liste gizlenir/pasifleşir.
        panel.all_applications.setChecked(True)
        assert not panel.close_without_confirmation_list.isEnabled()
        panel.all_applications.setChecked(False)
        assert panel.close_without_confirmation_list.isEnabled()

        # Uygulama çıkarılınca listeden otomatik düşer.
        panel.application_list.setCurrentRow(0)  # brave
        panel._remove_application()
        assert panel.close_without_confirmation_list.count() == 1
        panel.close_without_confirmation_list.item(0).setCheckState(Qt.CheckState.Checked)

        # Kaydet ve yeniden yükle round-trip.
        save_choices(path, None, values=panel.values(), applications=panel.applications)
        saved = load_settings(path)
        assert saved.allow_cloud_file_content is True
        assert saved.allow_cloud_clipboard is True
        assert saved.memory.encrypt_at_rest is True
        assert saved.close_without_confirmation == ["demoapp"]

        # Eksik bölüm YAML'da yokken güvenle oluşturulur.
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw.pop("memory", None)
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        assert save_choices(path, None, values={("memory", "encrypt_at_rest"): False})
        assert load_settings(path).memory.encrypt_at_rest is False
        assert {b.path for b in PANEL_BINDINGS} >= {
            ("voice", "listening_hotkey"),
            ("allow_cloud_file_content",),
            ("allow_cloud_clipboard",),
            ("memory", "encrypt_at_rest"),
            ("close_without_confirmation",),
        }
    finally:
        panel.deleteLater()
        qt_app.processEvents()
