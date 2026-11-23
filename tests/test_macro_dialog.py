"""Makro kaydedici penceresi, Rutinler düğmesi ve ApprovalDialog önizleme testleri (ADR 0087)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from artemis.channels.desktop.approval_dialog import ApprovalDialog
from artemis.channels.desktop.bridge import Approval
from artemis.channels.desktop.macro_dialog import MacroDialog
from artemis.channels.desktop.routines_section import RoutinesSection
from artemis.channels.desktop.widgets import HelpTip
from artemis.computer.windows import UiElement
from artemis.config.settings import RoutinesConfig, Settings
from artemis.i18n import set_language
from artemis.proactive.macro_recorder import FocusedControlInfo, ObserverSnapshot
from artemis.proactive.routines import Routine, RoutineStep, RoutineStore
from artemis.tools.plugins.routines import RoutineSaveArguments
from tests.test_macro_recorder import FakeBackend, _registry


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _reset_lang() -> None:
    set_language("tr")


def _make_config(tmp_path: Path, *, enabled: bool = True) -> tuple[Path, Path]:
    state = tmp_path / "state"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "workspace": str(tmp_path),
                "state_dir": str(state),
                "routines": {"enabled": enabled},
                "applications": {"Not Defteri": "notepad.exe"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return cfg, state / "routines.json"


def test_macro_dialog_observation_privacy_masking_and_submit(
    qt_app: QApplication, tmp_path: Path
) -> None:
    secret_pw = "SuperGizliParola!42"
    backend = FakeBackend(
        [
            ObserverSnapshot(
                exe_path=r"C:\Windows\System32\cmd.exe",
                pid=10,
                left_down=True,
            ),
            ObserverSnapshot(
                exe_path=r"C:\Windows\System32\notepad.exe",
                pid=20,
                hwnd=5,
                cursor_pos=(40, 40),
                left_down=True,
                focused=FocusedControlInfo(kind="Edit", is_password=True),
            ),
            ObserverSnapshot(
                exe_path=r"C:\Windows\System32\notepad.exe",
                pid=20,
                hwnd=5,
                cursor_pos=(80, 20),
                left_down=False,
            ),
            ObserverSnapshot(
                exe_path=r"C:\Windows\System32\notepad.exe",
                pid=20,
                hwnd=5,
                cursor_pos=(80, 20),
                left_down=True,
                focused=FocusedControlInfo(name="Kaydet", kind="Button"),
                elements=(
                    UiElement(
                        id=1,
                        kind="Button",
                        name="Kaydet",
                        rect=(70, 10, 40, 25),
                        interactive=True,
                        focused=True,
                    ),
                ),
            ),
        ]
    )
    existing = [Routine(name="Mevcut", steps=[RoutineStep(tool="open_url", arguments={"url": "https://a.example"})])]
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        routines=RoutinesConfig(enabled=True),
        applications={"Not Defteri": "notepad.exe"},
    )
    dialog = MacroDialog(existing, _registry(), settings, backend=backend)
    try:
        tips = dialog.findChildren(HelpTip)
        assert 1 <= len(tips) <= 3
        for tip in tips:
            assert len(tip.toolTip()) <= 260
            assert tip.accessibleDescription() == tip.toolTip()

        assert not dialog.save_button.isEnabled()
        dialog.start_recording()
        for _ in range(4):
            dialog.poll_once()
        dialog.stop_recording()

        assert not dialog.privacy_badge.isHidden()
        badge_text = dialog.privacy_badge.text()
        assert "1 hassas alan gizlendi" in badge_text
        assert "1 engelli pencere atlandı" in badge_text

        dialog.nl_input.setPlainText(f"parola: {secret_pw}\nhttps://example.com aç")
        dialog.name.setText("mevcut")
        assert not dialog.save_button.isEnabled() and "kullanılıyor" in dialog.error_label.text()

        dialog.name.setText("sabah-makro")
        assert dialog.save_button.isEnabled()
        assert "2 hassas alan gizlendi" in dialog.privacy_badge.text()
        dialog.submit()

        assert dialog.result() == int(QDialog.DialogCode.Accepted)
        assert isinstance(dialog.result_args, RoutineSaveArguments)
        assert dialog.result_args.name == "sabah-makro"
        assert [s.tool for s in dialog.result_args.steps] == [
            "open_application",
            "computer_use",
            "open_url",
        ]
        # Sır hiçbir adım veya UI metninde kalmaz.
        assert secret_pw not in dialog.result_args.model_dump_json()
        for row in range(dialog.steps_list.count()):
            assert secret_pw not in dialog.steps_list.item(row).text()

        # Küçük ekrana sığar ve düğme sırası (Vazgeç solda, Kaydet sağda) korunur.
        dialog.show()
        dialog.fit_to_available(QRect(0, 0, 1366, 480))
        qt_app.processEvents()
        assert dialog.height() <= 480
        assert dialog.rect().contains(dialog.cancel_button.geometry())
        assert dialog.rect().contains(dialog.ok_button.geometry())
        assert dialog.cancel_button.x() < dialog.ok_button.x()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_routines_section_macro_button_emits_routine_save_without_direct_write(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, routines_path = _make_config(tmp_path, enabled=True)
    section = RoutinesSection(cfg, registry=_registry())
    emitted: list[tuple[str, Any]] = []
    section.tool_requested.connect(lambda tool, args: emitted.append((tool, args)))

    def fake_exec(self: MacroDialog) -> int:
        self.name.setText("yeni-makro")
        self.nl_input.setPlainText("https://example.com aç")
        self.submit()
        return int(self.result())

    monkeypatch.setattr(MacroDialog, "exec", fake_exec)
    try:
        assert section.macro_button.isEnabled()
        section.macro_button.click()
        assert len(emitted) == 1
        tool_name, args = emitted[0]
        assert tool_name == "routine_save"
        assert isinstance(args, RoutineSaveArguments)
        assert args.name == "yeni-makro"
        assert [s.tool for s in args.steps] == ["open_url"]
        # Doğrudan routines.json dosyasına yazmaz: SafetyGate onayına gider.
        assert not routines_path.exists()
        assert RoutineStore(routines_path).load() == []

        demo_section = RoutinesSection(cfg, demo=True, registry=_registry())
        assert not demo_section.macro_button.isEnabled()
        demo_section.deleteLater()
    finally:
        section.deleteLater()


@pytest.mark.parametrize("lang", ["tr", "en"])
def test_approval_dialog_shows_routine_save_preview_without_secrets(
    qt_app: QApplication, lang: str
) -> None:
    set_language(lang)
    parent = QWidget()
    item = Approval(
        "req-1",
        "turn-1",
        "app-1",
        "routine_save",
        {
            "name": "sabah-makro",
            "steps": [
                {"tool": "open_application", "arguments": {"name": "Not Defteri"}},
                {"tool": "open_url", "arguments": {"url": "https://example.com"}},
            ],
        },
        "Bu işlem onay gerektirir. Rutin kaydeder.",
        None,
        "Rutin kaydet",
    )
    dialog = ApprovalDialog(item, lambda *_: True, parent)
    try:
        assert dialog.routine_preview_box is not None
        assert dialog.routine_preview_text is not None
        assert dialog.routine_preview_text.isReadOnly()
        preview = dialog.routine_preview_text.toPlainText()
        assert "sabah-makro" in preview
        assert "1." in preview and "open_application" in preview
        assert "2." in preview and "open_url" in preview
        assert dialog.no_button.isDefault()
        assert not dialog.yes_button.isDefault()
    finally:
        dialog._choose(False)
        dialog.deleteLater()
        parent.deleteLater()
        set_language("tr")
