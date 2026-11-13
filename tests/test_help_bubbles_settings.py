"""Ayarlar sayfalarındaki "?" baloncukları: alan etiketlerinin yanında var, metinler uygun."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QLayout, QWidget

from artemis.channels.desktop.code_agent_config import AgentDialog
from artemis.channels.desktop.pet_section import PetSection
from artemis.channels.desktop.routines_section import RoutinesSection
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.desktop.skills_section import SkillsSection
from artemis.channels.desktop.trigger_dialog import TriggerDialog
from artemis.channels.desktop.widgets import HelpTip
from tests.test_settings_panel import config

MAX_HELP = 260
# Sayfa adı -> en az bu kadar baloncuk (sayfadaki etiketli alan + onay kutusu + düğme grubu sayısı).
MIN_PER_PAGE = {
    "Ses": 26, "Model": 23, "Görünüm": 1, "Rutinler": 2, "Tetikleyiciler": 3, "Gün özeti": 2,
    "Araştırma": 1, "Telegram": 6, "Takvim ve posta": 12, "GitHub": 6, "Beceriler": 1,
    "Dosya araması": 3, "Uygulamalar": 3,
}


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _tips(root: QWidget) -> list[HelpTip]:
    return root.findChildren(HelpTip)


def _row_has_tip(widget: QWidget) -> bool:
    """Widget'ın içinde durduğu en yakın düzen satırında bir HelpTip var mı."""
    parent = widget.parentWidget()
    layout = parent.layout() if parent is not None else None

    def walk(lay: QLayout) -> bool | None:
        for index in range(lay.count()):
            item = lay.itemAt(index)
            if item.widget() is widget:
                return any(
                    isinstance(lay.itemAt(i).widget(), HelpTip) for i in range(lay.count())
                )
            child = item.layout()
            if child is not None:
                found = walk(child)
                if found is not None:
                    return found
        return None

    return bool(layout is not None and walk(layout))


def _skipped(widget: QWidget) -> bool:
    """Başka bölümlerin (rutin, beceri, evcil) etiketleri bu dosyanın kapsamı dışındadır."""
    node: QWidget | None = widget
    while node is not None:
        if isinstance(node, (RoutinesSection, SkillsSection, PetSection)):
            return True
        node = node.parentWidget()
    return False


def _assert_texts(root: QWidget) -> None:
    for tip in _tips(root):
        text = tip.toolTip()
        assert text.strip(), "boş yardım metni"
        assert len(text) <= MAX_HELP, (len(text), text)
        assert tip.accessibleDescription() == text


def test_settings_panel_fields_have_help_bubbles(tmp_path: Path, qt_app: QApplication) -> None:
    panel = SettingsPanel(config(tmp_path))
    _assert_texts(panel)
    missing = [
        label.text()
        for label in panel.findChildren(QLabel)
        if label.objectName() == "fieldLabel" and not _skipped(label) and not _row_has_tip(label)
    ]
    # Salt okunur etiketler (Kullanılan mikrofon, Son duyulan, Ses girişi) bilerek baloncuksuzdur.
    assert set(missing) <= {"Kullanılan mikrofon", "Son duyulan", "Ses girişi"}, missing
    for name, minimum in MIN_PER_PAGE.items():
        row = panel.section_names().index(name)
        page = panel.stack.widget(row)
        assert page is not None
        assert len(_tips(page)) >= minimum, (name, len(_tips(page)))


def test_checkboxes_and_buttons_are_covered(tmp_path: Path, qt_app: QApplication) -> None:
    panel = SettingsPanel(config(tmp_path))
    for box in (
        panel.hybrid_stt, panel.continuous_mode, panel.dictation_enabled, panel.selection_enabled,
        panel.barge_in_box, panel.followup_box, panel.live_transcript, panel.noise_suppression,
        panel.echo_cancellation, panel.noise_stt, panel.advanced_devices, panel.proactive_enabled,
        panel.routines_enabled, panel.triggers_enabled, panel.daily_summary_enabled,
        panel.research_enabled, panel.telegram_voice, panel.telegram_photo, panel.telegram_files,
        panel.calendar_enabled, panel.calendar_cloud, panel.mail_enabled, panel.mail_cloud,
        panel.github_auto, panel.skills_enabled, panel.file_search_enabled,
        panel.all_applications, panel.computer_auto,
        panel.providers.parallel_test, panel.providers.paid, panel.providers.make_primary,
        panel.providers.needs_key, panel.telegram.channel_check,
    ):
        assert _row_has_tip(box), box.text()
        assert len(_tips(panel.file_search)) >= 2


def test_dialog_fields_have_help_bubbles(tmp_path: Path, qt_app: QApplication) -> None:
    trigger = TriggerDialog(None, [], [], "Rutinler kapalı.")
    agent = AgentDialog(("claude",))
    for dialog, minimum in ((trigger, 5), (agent, 6)):
        _assert_texts(dialog)
        assert len(_tips(dialog)) >= minimum
        for label in dialog.findChildren(QLabel):
            if label.objectName() == "fieldLabel":
                assert _row_has_tip(label), label.text()


def test_apply_layout_tip_follows_hidden_button(tmp_path: Path, qt_app: QApplication) -> None:
    panel = SettingsPanel(config(tmp_path))
    providers = panel.providers
    providers._show_button(providers.apply_layout_button, False)
    tip = providers._button_tips[providers.apply_layout_button]
    assert tip.isHidden()
    providers._show_button(providers.apply_layout_button, True)
    assert not tip.isHidden()
