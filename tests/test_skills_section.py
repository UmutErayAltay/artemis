"""Ayarlar > Beceriler: klasörü aç mesajı ve Klasör seç (ADR 0060); gerçek diyalog açılmaz."""

import os
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QFileDialog

from artemis.channels.desktop import skills_section as module
from artemis.channels.desktop.skills_section import SkillsSection
from artemis.config.settings import load_settings
from artemis.skills import skills_dir


def make_config(tmp_path: Path, vault: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "workspace": str(tmp_path / "ws"), "state_dir": str(vault / "state"),
        "audit_path": str(tmp_path / "audit.jsonl"),
        "memory": {"vault_path": str(vault)}, "skills": {"enabled": True},
    }), encoding="utf-8")
    return path


@pytest.fixture
def section(tmp_path: Path):
    QApplication.instance() or QApplication([])
    vault = tmp_path / "vault"
    vault.mkdir()
    widget = SkillsSection(make_config(tmp_path, vault))
    yield widget
    widget.deleteLater()


def pick(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))


def test_default_folder_is_shown_and_outside_vault(section: SkillsSection) -> None:
    assert section.folder_label.text() == str(skills_dir(section._settings))  # type: ignore[arg-type]
    assert section.folder_label.text().startswith(os.environ["APPDATA"])


def test_open_folder_opens_default_and_shows_specific_reason(
    section: SkillsSection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[object] = []
    monkeypatch.setattr(module.QDesktopServices, "openUrl", lambda url: opened.append(url))
    section._open_folder()
    assert opened and not section.notice.text()
    # Elle yazılmış, vault içindeki klasör: genel mesaj değil, neden gösterilir.
    nested = tmp_path / "vault" / "beceri"
    nested.mkdir()
    raw = yaml.safe_load(section._config_path.read_text(encoding="utf-8"))
    raw["skills"]["folder"] = str(nested)
    section._config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    section._settings = load_settings(section._config_path)
    opened.clear()
    section._open_folder()
    reason = section.notice.text()
    assert not opened and "vault" in reason and "Klasör seç" in reason
    assert "skills.folder" not in reason and "config" not in reason


def test_choose_folder_rejects_nested_and_saves_good(
    section: SkillsSection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = section._config_path
    pick(monkeypatch, tmp_path / "vault" / "x")
    section._choose_folder()
    assert load_settings(config).skills.folder is None and "vault" in section.notice.text()
    good = tmp_path / "benim-beceriler"
    good.mkdir()
    pick(monkeypatch, good)
    section._choose_folder()
    saved = load_settings(config)
    assert saved.skills.folder == good and saved.skills.enabled
    assert section.folder_label.text() == str(good) and not section.notice.text()


def test_cancelled_dialog_changes_nothing(
    section: SkillsSection, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = section._config_path.read_text(encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    section._choose_folder()
    assert section._config_path.read_text(encoding="utf-8") == before


def test_demo_disables_choose(tmp_path: Path) -> None:
    QApplication.instance() or QApplication([])
    widget = SkillsSection(make_config(tmp_path, tmp_path / "v"), demo=True)
    assert not widget.choose_button.isEnabled()
    widget.deleteLater()


def test_new_skill_refused_in_unsafe_location_and_demo(
    section: SkillsSection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[int] = []
    monkeypatch.setattr(module.SkillDialog, "exec", lambda self: called.append(1) or 0)
    raw = yaml.safe_load(section._config_path.read_text(encoding="utf-8"))
    raw["skills"]["folder"] = str(tmp_path / "vault" / "b")
    section._config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    section._settings = load_settings(section._config_path)
    section._new_skill()
    assert not called and "vault" in section.notice.text()
    demo = SkillsSection(make_config(tmp_path, tmp_path / "v"), demo=True)
    assert not demo.new_button.isEnabled()
    demo.deleteLater()


def test_try_emits_name_of_selected_valid_skill(section: SkillsSection) -> None:
    from artemis.skills import ensure_example

    ensure_example(section._settings)  # type: ignore[arg-type]
    section.refresh()
    got: list[str] = []
    section.try_requested.connect(got.append)
    assert not section.try_button.isEnabled()
    section.valid_list.setCurrentRow(0)
    assert section.try_button.isEnabled()
    section.try_button.click()
    section.valid_list.itemDoubleClicked.emit(section.valid_list.item(0))
    assert got == [got[0]] * 2 and got[0]


def test_legacy_unsafe_folder_notice(tmp_path: Path) -> None:
    QApplication.instance() or QApplication([])
    vault = tmp_path / "vault"
    legacy = vault / "state" / "beceriler"
    legacy.mkdir(parents=True)
    (legacy / "eski.md").write_text("x", encoding="utf-8")
    widget = SkillsSection(make_config(tmp_path, vault))
    assert str(legacy) in widget.notice.text() and "taşı" in widget.notice.text()
    widget.deleteLater()
