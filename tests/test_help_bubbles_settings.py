"""Ayarlar sayfalarındaki "?" baloncukları: yalnız önemli olanlar kalır (maliyet, gizlilik,
güvenlik, şaşırtıcı zamanlama); sayfa başına üst sınır var, kalması gerekenler yerinde."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QWidget

from artemis.channels.desktop.code_agent_config import AgentDialog
from artemis.channels.desktop.settings_panel import SettingsPanel
from artemis.channels.desktop.skill_dialog import SkillDialog
from artemis.channels.desktop.trigger_dialog import TriggerDialog
from artemis.channels.desktop.widgets import HelpTip
from tests.test_settings_panel import config
from tests.test_skill_dialog import env  # noqa: F401 - fixture

MAX_HELP = 260
# Sayfa adı -> en çok bu kadar baloncuk.
MAX_PER_PAGE = {
    "Ses": 6,
    "Dikte ve Kısayollar": 5,
    "Model ve Bağlantılar": 6,
    "Görünüm": 5,
    "Otomasyon Kuralları": 5,
    "Bilgi ve Beceriler": 5,
    "Bağlantılar": 5,
    "Uygulamalar ve Güvenlik": 5,
    "Hakkında ve Güncelleme": 5,
}
# Sayfa adı -> metinlerinde bulunması gereken ayırt edici sözcükler (her biri ayrı bir baloncukta).
MUST_KEEP = {
    "Ses": ["VRAM", "float16", "gizlilik bedeli", "Edge", "Karma tanıma açıksa"],
    "Model ve Bağlantılar": ["Beyin:", "tahmini tutarı", "aynı anda", "kimlik kasasına"],
    "Otomasyon Kuralları": ["Telegram'a gitmez", "onay sorulur"],
    "Bilgi ve Beceriler": ["bulut modele", "dizinlenmez"],
    "Bağlantılar": ["kimlik kasasına", "fotoğraf"],
    "Uygulamalar ve Güvenlik": ["Onaysız kapatma", "sormadan"],
}


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _tips(root: QWidget) -> list[HelpTip]:
    return root.findChildren(HelpTip)


def _assert_texts(root: QWidget) -> None:
    for tip in _tips(root):
        text = tip.toolTip()
        assert text.strip(), "boş yardım metni"
        assert len(text) <= MAX_HELP, (len(text), text)
        assert tip.accessibleDescription() == text


def test_settings_pages_stay_within_caps_and_keep_key_bubbles(
    tmp_path: Path, qt_app: QApplication
) -> None:
    panel = SettingsPanel(config(tmp_path))
    _assert_texts(panel)
    for name, cap in MAX_PER_PAGE.items():
        page = panel.stack.widget(panel.section_names().index(name))
        assert page is not None
        found = _tips(page)
        assert len(found) <= cap, (name, len(found))
        texts = [tip.toolTip() for tip in found]
        for word in MUST_KEEP.get(name, []):
            assert any(word in text for text in texts), (name, word)


def test_dialogs_stay_within_caps(tmp_path: Path, qt_app: QApplication, env) -> None:  # noqa: F811
    settings, _config, _folder = env
    skill = SkillDialog(settings)
    trigger = TriggerDialog(None, [], [], "Rutinler kapalı.")
    for dialog in (trigger, AgentDialog(("claude",)), skill):
        _assert_texts(dialog)
        assert 1 <= len(_tips(dialog)) <= 3
    assert any("bulut modele" in tip.toolTip() for tip in _tips(skill))


def test_apply_layout_tip_follows_hidden_button(tmp_path: Path, qt_app: QApplication) -> None:
    panel = SettingsPanel(config(tmp_path))
    providers = panel.providers
    providers._show_button(providers.apply_layout_button, False)
    tip = providers._button_tips[providers.apply_layout_button]
    assert tip.isHidden()
    providers._show_button(providers.apply_layout_button, True)
    assert not tip.isHidden()
