"""Uygulama içi rutin düzenleyici ve metin görüntüleyici (ADR 0058 Ek karar).

Gerçek makine yok: tmp_path, offscreen Qt, sahte araç kaydı. Dış editör açılırsa test kırılır.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, Field

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
)

from artemis.channels.desktop import routines_section
from artemis.channels.desktop.routine_dialog import (
    RoutineDialog,
    RoutineFileDialog,
    validate_raw,
)
from artemis.channels.desktop.routines_section import RoutinesSection
from artemis.channels.desktop.text_viewer import TextViewerDialog
from artemis.proactive.routines import Routine, RoutineStep, RoutineStore
from artemis.tools.base import BaseTool, Risk, ToolContext, ToolResult
from artemis.tools.registry import ToolRegistry


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


class MixedArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=50, description="Bir metin.")
    count: int = Field(default=3, ge=1, le=9)
    loud: bool = False
    mode: Literal["a", "b"] = "a"
    items: list[str] = Field(default_factory=list)


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Mixed(BaseTool):
    name = "mixed_tool"
    description = "Karışık alanlı sahte araç."
    args_model = MixedArgs
    risk: ClassVar[Risk] = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(success=True, status="ok", message="tamam")


class Plain(Mixed):
    name = "plain_tool"
    description = "Argümansız sahte araç."
    args_model = NoArgs


class Hidden(Mixed):
    name = "routine_save"  # rutin araçları listede çıkmamalı
    args_model = NoArgs


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for cls in (Mixed, Plain, Hidden):
        registry.register(cls)
    return registry


def make_config(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "state"
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({
            "workspace": str(tmp_path), "state_dir": str(state), "routines": {"enabled": True}
        }),
        encoding="utf-8",
    )
    return config, state / "routines.json"


def step(tool: str = "plain_tool", **arguments: Any) -> RoutineStep:
    return RoutineStep(tool=tool, arguments=arguments)


def fill_mixed(editor: Any, **values: Any) -> None:
    """Üretilen formun denetimlerini ada göre doldurur."""
    for field in editor._fields:
        widget = field._widget
        if field.name not in values:
            continue
        value = values[field.name]
        if isinstance(widget, QLineEdit):
            widget.setText(value)
        elif isinstance(widget, QSpinBox):
            widget.setValue(value)
        elif isinstance(widget, QCheckBox):
            widget.setChecked(value)
        elif isinstance(widget, QComboBox):
            widget.setCurrentText(value)
        elif isinstance(widget, QPlainTextEdit):
            widget.setPlainText(value)


def test_tool_list_excludes_routine_tools(qt_app: QApplication) -> None:
    dialog = RoutineDialog(None, [], make_registry())
    assert dialog._tools == ["mixed_tool", "plain_tool"]
    assert dialog.save_button.objectName() == "primary"
    assert not dialog.save_button.isEnabled()  # ad boş
    dialog.deleteLater()


def test_generated_form_gives_type_correct_values(qt_app: QApplication) -> None:
    dialog = RoutineDialog(None, [], make_registry())
    dialog.name.setText("deneme")
    editor = dialog.editors[0]
    editor.tool.setCurrentText("mixed_tool")
    kinds = {f.name: f._kind for f in editor._fields}
    assert kinds == {
        "text": "string", "count": "int", "loud": "bool", "mode": "enum", "items": "json"
    }
    fill_mixed(editor, text="merhaba", count=5, loud=True, mode="b", items='["x", "y"]')
    assert dialog.save_button.isEnabled(), dialog.error.text()
    dialog.submit()
    result = dialog.result_routine
    assert result is not None
    assert result.steps[0].arguments == {
        "text": "merhaba", "count": 5, "loud": True, "mode": "b", "items": ["x", "y"]
    }
    raw = editor.step().arguments
    assert raw["loud"] is True and raw["count"] == 5 and isinstance(raw["count"], int)
    assert raw["mode"] == "b"
    dialog.deleteLater()


def test_bad_json_and_bad_step_disable_save_and_write_nothing(
    qt_app: QApplication, tmp_path: Path
) -> None:
    dialog = RoutineDialog(None, [], make_registry())
    dialog.name.setText("deneme")
    editor = dialog.editors[0]
    editor.tool.setCurrentText("mixed_tool")
    fill_mixed(editor, text="ok", items="[bozuk")
    assert not dialog.save_button.isEnabled() and "JSON" in dialog.error.text()
    dialog.submit()
    assert dialog.result_routine is None
    fill_mixed(editor, items="", count=9)
    assert dialog.save_button.isEnabled()
    editor.up_button.click()  # tek adım: taşınamaz, bozulmaz
    editor.remove_button.click()
    assert not dialog.save_button.isEnabled() and "En az bir adım" in dialog.error.text()
    dialog.deleteLater()


def test_unknown_and_routine_tool_steps_are_rejected(qt_app: QApplication) -> None:
    for tool in ("routine_save", "yok_boyle_arac"):
        existing = Routine(name="eski", steps=[step(tool)])
        dialog = RoutineDialog(existing, [], make_registry())
        assert not dialog.save_button.isEnabled()
        assert dialog.error.text().startswith("Adım 1")
        dialog.submit()
        assert dialog.result_routine is None
        dialog.deleteLater()


def test_name_validation_and_uniqueness(qt_app: QApplication) -> None:
    other = Routine(name="İşe başla", steps=[step()])
    dialog = RoutineDialog(None, [other], make_registry())
    dialog.name.setText("işe başla")
    assert not dialog.save_button.isEnabled() and "kullanılıyor" in dialog.error.text()
    dialog.name.setText("kötü/ad")
    assert not dialog.save_button.isEnabled() and "Ad" in dialog.error.text()
    dialog.name.setText("yeni ad")
    assert dialog.save_button.isEnabled()
    dialog.deleteLater()


def test_reorder_steps_and_help_tips(qt_app: QApplication) -> None:
    from artemis.channels.desktop.widgets import HelpTip

    existing = Routine(name="sıra", steps=[step("plain_tool"), step("mixed_tool", text="z")])
    dialog = RoutineDialog(existing, [], make_registry())
    first, second = dialog.editors
    second.up_button.click()
    assert dialog.editors == [second, first]
    assert [e.tool.currentText() for e in dialog.editors] == ["mixed_tool", "plain_tool"]
    dialog.submit()
    assert dialog.result_routine is not None
    assert [s.tool for s in dialog.result_routine.steps] == ["mixed_tool", "plain_tool"]
    assert len(dialog.findChildren(HelpTip)) >= 6  # ad, adımlar, araç, her argüman
    assert all(tip.toolTip() for tip in dialog.findChildren(HelpTip))
    dialog.deleteLater()


def test_section_add_edit_delete_write_expected_file(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, path = make_config(tmp_path)
    section = RoutinesSection(config, registry=make_registry())
    store = RoutineStore(path)

    def run_add(self: RoutineDialog) -> int:
        self.name.setText("sabah")
        self.editors[0].tool.setCurrentText("plain_tool")
        self._add_step()
        self.editors[1].tool.setCurrentText("mixed_tool")
        fill_mixed(self.editors[1], text="selam")
        self.submit()
        return int(self.result())

    monkeypatch.setattr(RoutineDialog, "exec", run_add)
    section.add_button.click()
    assert [r.name for r in store.load()] == ["sabah"]
    assert [s.tool for s in store.load()[0].steps] == ["plain_tool", "mixed_tool"]
    assert section.routine_list.count() == 1

    # zamanı olan rutin düzenlenince zaman korunur
    section.routine_list.setCurrentRow(0)
    section.time_edit.setText("08:30")
    section.save_time_button.click()

    def run_edit(self: RoutineDialog) -> int:
        self.name.setText("akşam")
        self.editors[0].down_button.click()
        self.submit()
        return int(self.result())

    monkeypatch.setattr(RoutineDialog, "exec", run_edit)
    section.routine_list.setCurrentRow(0)
    section.edit_button.click()
    saved = store.load()
    assert [r.name for r in saved] == ["akşam"]
    assert [s.tool for s in saved[0].steps] == ["mixed_tool", "plain_tool"]
    assert saved[0].schedule is not None and saved[0].schedule.at == "08:30"
    assert "tetikleyici" in section.notice.text()

    # silme: varsayılan Vazgeç → dokunmaz; Evet → siler
    box, yes = section.confirm_box("x")
    assert box.defaultButton().text() == "Vazgeç" and box.escapeButton() is box.defaultButton()
    box.deleteLater()
    section.routine_list.setCurrentRow(0)
    monkeypatch.setattr(section, "_confirm", lambda text: False)
    section.delete_button.click()
    assert len(store.load()) == 1
    monkeypatch.setattr(section, "_confirm", lambda text: True)
    section.delete_button.click()
    assert store.load() == [] and json.loads(path.read_text(encoding="utf-8")) == {"routines": []}
    section.deleteLater()


def test_invalid_dialog_writes_nothing(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, path = make_config(tmp_path)
    section = RoutinesSection(config, registry=make_registry())

    def run_bad(self: RoutineDialog) -> int:
        self.name.setText("kötü")
        self.editors[0].tool.setCurrentText("mixed_tool")
        fill_mixed(self.editors[0], text="a", items="{")
        self.submit()
        return int(self.result())

    monkeypatch.setattr(RoutineDialog, "exec", run_bad)
    section.add_button.click()
    assert not path.exists()
    section.deleteLater()


def test_corrupt_file_offers_raw_editor_and_never_resets(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, path = make_config(tmp_path)
    path.parent.mkdir()
    path.write_text("{bozuk", encoding="utf-8")
    section = RoutinesSection(config, registry=make_registry())
    assert "bozuk" in section.notice.text()
    assert section.fix_button.isVisibleTo(section) and section.fix_button.isEnabled()
    assert not section.add_button.isEnabled()  # bozukken yazmak dosyayı ezerdi
    assert path.read_text(encoding="utf-8") == "{bozuk"

    dialog = RoutineFileDialog(RoutineStore(path), make_registry())
    assert dialog.editor.toPlainText() == "{bozuk"
    dialog.editor.setPlainText("{hala bozuk")
    dialog.submit()
    assert not dialog.saved and dialog.error.text()
    dialog.editor.setPlainText(json.dumps({"routines": [{"name": "x", "steps": [
        {"tool": "routine_delete", "arguments": {}}]}]}))
    dialog.submit()
    assert not dialog.saved and "rutin içinden" in dialog.error.text()
    assert path.read_text(encoding="utf-8") == "{bozuk"  # hiçbiri yazılmadı

    good = {"routines": [{"name": "düzgün", "steps": [{"tool": "plain_tool"}]}]}
    dialog.editor.setPlainText(json.dumps(good))
    dialog.submit()
    assert dialog.saved
    assert [r.name for r in RoutineStore(path).load()] == ["düzgün"]
    assert not list(path.parent.glob(".routines-*"))  # atomik yazımın artığı yok
    dialog.deleteLater()

    path.write_text("{bozuk", encoding="utf-8")
    section.refresh()

    def run_fix(self: RoutineFileDialog) -> int:
        self.editor.setPlainText(json.dumps(good))
        self.submit()
        return int(self.result())

    monkeypatch.setattr(RoutineFileDialog, "exec", run_fix)
    section.fix_button.click()
    assert section.routine_list.count() == 1 and section.add_button.isEnabled()
    assert not section.fix_button.isVisibleTo(section)
    section.deleteLater()


def test_validate_raw_messages_carry_no_content(qt_app: QApplication) -> None:
    routines, problem = validate_raw('{"routines": [{"name": "SIR", "steps": 5}]}', make_registry())
    assert routines is None and "SIR" not in problem and "routines.0.steps" in problem
    routines, problem = validate_raw('{"routines": []}', make_registry())
    assert routines == [] and problem == ""


def test_section_never_opens_external_editor(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(routines_section.__file__).read_text(encoding="utf-8")
    assert "QDesktopServices" not in source and "open_button" not in source

    def boom(url: QUrl) -> bool:
        raise AssertionError("dış uygulama açılmamalı")

    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(boom))
    config, _ = make_config(tmp_path)
    section = RoutinesSection(config, registry=make_registry())
    assert not hasattr(section, "open_button")
    section.deleteLater()


def test_text_viewer_is_read_only_and_links_do_not_open(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[object] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url) or True)
    )
    text = "# Rapor\n\n[bağlantı](https://example.com/x) ve ![](file:///C:/gizli.png)\n"
    viewer = TextViewerDialog("Rapor", text)
    browser = viewer.browser
    assert browser.isReadOnly()
    assert not browser.openLinks() and not browser.openExternalLinks()
    assert "Rapor" in browser.toPlainText()
    anchors: list[QUrl] = []
    browser.anchorClicked.connect(anchors.append)
    browser.setSource(QUrl("https://example.com/x"))  # açık linkler kapalıyken gezinmez
    assert opened == [] and anchors == []
    assert browser.loadResource(2, QUrl("file:///C:/gizli.png")) is None
    viewer.copy_button.click()
    clipboard = QGuiApplication.clipboard()
    assert clipboard is not None and clipboard.text() == text
    plain = TextViewerDialog("Düz", "<b>ham</b>", markdown=False)
    assert plain.browser.toPlainText() == "<b>ham</b>"
    viewer.deleteLater()
    plain.deleteLater()
