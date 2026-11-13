"""Ayarlar > Tetikleyiciler formu (ADR 0061 Ek): doğrulama, yazım, düzenle/sil, uyarı, demo."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QDialog

from artemis.channels.desktop import triggers_section
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.desktop.trigger_dialog import TriggerDialog, slug_name, validate_trigger
from artemis.channels.desktop.triggers_section import TriggersSection
from artemis.config.settings import MAX_TRIGGERS, TriggerConfig, load_settings


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def make_config(tmp_path: Path, **extra: Any) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "workspace": "desktop", "state_dir": "state", "audit_path": "audit.jsonl",
        "primary_model": "first", "applications": {"brave": "brave.exe"},
        "models": {"first": {"provider": "openrouter", "model": "first-model",
                             "input_per_million": "0", "output_per_million": "0"}},
        **extra,
    }), encoding="utf-8")
    return path


def valid(base_folder: Path, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "indirilenler", "folder": str(base_folder), "pattern": "*",
        "message": "Yeni dosya geldi: {file}", "suggest": None, "routine": None,
        "taken": [], "count": 0,
    }
    return {**base, **over}


def test_slug_name() -> None:
    assert slug_name("İndirilenler Çalışma") == "Indirilenler-Calisma"
    assert slug_name("!!!") == ""
    assert slug_name("a", ["A"]) == "a-2"


def test_validate_ok_and_errors(tmp_path: Path) -> None:
    item, errors = validate_trigger(**valid(tmp_path))
    assert item is not None and not errors
    cases = [
        ({"name": "kötü ad"}, "name"),
        ({"name": ""}, "name"),
        ({"folder": "goreli/yol"}, "folder"),
        ({"folder": str(tmp_path / "yok")}, "folder"),
        ({"folder": ""}, "folder"),
        ({"pattern": "*.pdf;*.png"}, "pattern"),
        ({"message": ""}, "message"),
        ({"suggest": "x", "routine": "y"}, "routine"),
        ({"suggest": ""}, "suggest"),
        ({"taken": ["INDIRILENLER"]}, "name"),
        ({"count": MAX_TRIGGERS}, "name"),
    ]
    for over, key in cases:
        item, errors = validate_trigger(**valid(tmp_path, **over))
        assert item is None and key in errors, over
        assert "Traceback" not in errors[key] and "validation" not in errors[key].lower()


def test_dialog_submit_and_prefill(qt_app: QApplication, tmp_path: Path) -> None:
    folder = tmp_path / "Çalışma Evrak"
    folder.mkdir()
    dialog = TriggerDialog(None, [], [], "Rutinler kapalı.")
    dialog._set_folder(str(folder))
    assert dialog.name.text() == "Calisma-Evrak"
    assert not dialog.routine_radio.isEnabled()
    dialog.suggest_radio.setChecked(True)
    dialog.kind.setCurrentIndex(1)
    dialog.submit()
    assert dialog.result_trigger == TriggerConfig(
        name="Calisma-Evrak", folder=str(folder), pattern="*.pdf",
        suggest="Şu dosyayı özetle: {path}",
    )
    bad = TriggerDialog(None, [], [], "")
    bad.submit()
    assert bad.result_trigger is None and not bad._errors["folder"].isHidden()


def read(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def accept_with(monkeypatch: pytest.MonkeyPatch, folder: Path, **fields: Any) -> None:
    def fake_exec(self: TriggerDialog) -> QDialog.DialogCode:
        if not self.folder.text():
            self._set_folder(str(folder))
        for key, value in fields.items():
            getattr(self, key).setText(value)
        self.submit()
        return QDialog.DialogCode(self.result_trigger is not None)

    monkeypatch.setattr(TriggerDialog, "exec", fake_exec)


def test_add_edit_delete_writes_yaml(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "gelen"
    folder.mkdir()
    path = make_config(tmp_path)
    section = TriggersSection(path)
    seen: list[bool] = []
    section.saved.connect(lambda: seen.append(True))
    accept_with(monkeypatch, folder)
    section.add_button.click()
    raw = read(path)
    assert raw["triggers"]["enabled"] is True  # kapalıydı: eklenince açılır
    assert raw["triggers"]["items"][0]["name"] == "gelen"
    assert raw["applications"] == {"brave": "brave.exe"}  # ilgisiz anahtar korunur
    assert "açıldı" in section.notice.text()
    assert "Uygula" in section.notice.text() and seen == [True]
    assert section.trigger_list.count() == 1
    # Düzenle: satırdaki düğme (sender + property) üzerinden.
    accept_with(monkeypatch, folder, message="Merhaba {file}")
    row = section.trigger_list.itemWidget(section.trigger_list.item(0))
    edit = row.findChildren(type(section.add_button))[0]
    assert edit.text() == "Düzenle"
    edit.click()
    assert read(path)["triggers"]["items"][0]["message"] == "Merhaba {file}"
    assert len(read(path)["triggers"]["items"]) == 1
    # Sil: onay reddedilirse kalır, kabul edilirse gider.
    monkeypatch.setattr(section, "_confirm", lambda text: False)
    delete = row.findChildren(type(section.add_button))[1]
    section._delete("gelen")
    assert len(read(path)["triggers"]["items"]) == 1
    monkeypatch.setattr(section, "_confirm", lambda text: True)
    section._delete("gelen")
    assert read(path)["triggers"]["items"] == []
    assert delete.text() == "Sil"
    assert load_settings(path).triggers.items == ()
    assert section.trigger_area.currentWidget() is section.trigger_area.empty


def test_edit_keeps_own_name_and_rejects_duplicate(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "a"
    folder.mkdir()
    path = make_config(tmp_path, triggers={"enabled": True, "items": [
        {"name": "bir", "folder": str(folder)}, {"name": "iki", "folder": str(folder)},
    ]}, proactive={"enabled": True})
    section = TriggersSection(path)
    accept_with(monkeypatch, folder, name="iki")  # başka tetikleyicinin adı
    section._edit("bir")
    assert [i["name"] for i in read(path)["triggers"]["items"]] == ["bir", "iki"]
    accept_with(monkeypatch, folder, name="uc")
    section._edit("bir")
    assert [i["name"] for i in read(path)["triggers"]["items"]] == ["uc", "iki"]


def test_max_count_blocks_add(qt_app: QApplication, tmp_path: Path) -> None:
    items = [{"name": f"t{i}", "folder": str(tmp_path)} for i in range(MAX_TRIGGERS)]
    section = TriggersSection(make_config(tmp_path, triggers={"enabled": True, "items": items}))
    assert not section.add_button.isEnabled()
    section._add()
    assert "En çok" in section.notice.text()


def test_proactive_warning_ticks_checkbox_without_writing(
    qt_app: QApplication, tmp_path: Path
) -> None:
    path = make_config(tmp_path, triggers={"enabled": True, "items": [
        {"name": "bir", "folder": str(tmp_path)}]})
    before = path.read_text(encoding="utf-8")
    panel = SettingsPanel(path)
    section = panel.triggers
    assert not section.warning_row.isHidden()
    assert not panel.proactive_enabled.isChecked() and not panel._dirty
    section.proactive_button.click()
    assert panel.proactive_enabled.isChecked() and panel._dirty
    assert path.read_text(encoding="utf-8") == before
    assert section.proactive_button.isHidden()
    panel.deleteLater()


def test_saving_enables_checkbox_so_apply_does_not_revert(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_config(tmp_path)
    panel = SettingsPanel(path)
    assert not panel.triggers_enabled.isChecked()
    accept_with(monkeypatch, tmp_path)
    panel.triggers.add_button.click()
    assert panel.triggers_enabled.isChecked()
    panel.deleteLater()


def test_demo_disables_buttons(qt_app: QApplication, tmp_path: Path) -> None:
    folder = tmp_path / "x"
    folder.mkdir()
    path = make_config(tmp_path, triggers={"enabled": True, "items": [
        {"name": "bir", "folder": str(folder)}]})
    section = TriggersSection(path, demo=True)
    assert not section.add_button.isEnabled()
    assert not section.open_button.isEnabled()
    row = section.trigger_list.itemWidget(section.trigger_list.item(0))
    assert all(not b.isEnabled() for b in row.findChildren(type(section.add_button)))
    section._edit(None)  # doğrudan çağrı bile yazmaz
    assert read(path)["triggers"]["items"][0]["name"] == "bir"
    assert triggers_section.TriggersSection is TriggersSection


def test_delete_confirmation_defaults_to_cancel(qt_app: QApplication, tmp_path: Path) -> None:
    """Enter silmeyi onaylamamalı: varsayılan ve Esc düğmesi "Vazgeç"."""
    section = TriggersSection(tmp_path / "config.yaml", demo=True)
    box, yes = section.confirm_box("Silinsin mi?")
    default = box.defaultButton()
    assert default is not yes and default.text() == "Vazgeç"
    assert box.escapeButton() is default
    assert {b.text() for b in box.buttons()} == {"Sil", "Vazgeç"}
    box.deleteLater()
    section.deleteLater()
