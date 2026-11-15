"""Beceri penceresi ve bölüm düzenlemesi (ADR 0060 Ek karar 2): gerçek dosya, offscreen."""

import os
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from artemis.channels.desktop.skill_dialog import SkillDialog, read_for_edit, slugify
from artemis.channels.desktop.skills_section import SkillsSection
from artemis.config.settings import Settings, load_settings
from artemis.skills import (
    MAX_BODY,
    SkillWriteError,
    compose,
    create_skill,
    load_skills,
    replace_skill,
    skills_dir,
    templates,
)

YES, NO = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No


@pytest.fixture(autouse=True)
def no_external_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Beceri dosyası dış düzenleyicide açılmaz: openUrl çağrılırsa test kırılır."""
    def boom(*_a: object, **_k: object) -> bool:
        raise AssertionError("QDesktopServices.openUrl çağrılmamalı")
    monkeypatch.setattr(QDesktopServices, "openUrl", boom)


@pytest.fixture
def env(tmp_path: Path):
    QApplication.instance() or QApplication([])
    vault = tmp_path / "vault"
    vault.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "workspace": str(tmp_path / "ws"), "state_dir": str(vault / "state"),
        "audit_path": str(tmp_path / "audit.jsonl"),
        "memory": {"vault_path": str(vault)},
        "skills": {"enabled": True, "folder": str(tmp_path / "beceriler")},
    }), encoding="utf-8")
    settings = load_settings(config)
    return settings, config, skills_dir(settings)


def fill(dialog: SkillDialog, name: str, description: str, body: str) -> None:
    dialog.name.setText(name)
    dialog.description.setText(description)
    dialog.body.setPlainText(body)


def test_slugify_turkish() -> None:
    assert slugify("Haftalık Özet: İş Çıktısı!") == "haftalik-ozet-is-ciktisi"
    assert slugify("???") == ""


def test_new_valid_writes_exact_content_and_lists(env) -> None:
    settings, config, folder = env
    dialog = SkillDialog(settings)
    assert not dialog.save_button.isEnabled()
    fill(dialog, "rapor", "Haftalık rapor: ne zaman?", "1. Oku\n2. Yaz")
    assert dialog.save_button.isEnabled() and dialog.save_button.objectName() == "primary"
    dialog.submit()
    expected = compose("rapor", "Haftalık rapor: ne zaman?", "1. Oku\n2. Yaz")
    assert (folder / "rapor.md").read_bytes() == expected.encode("utf-8")
    assert [s.name for s in load_skills(settings).skills] == ["rapor"]
    section = SkillsSection(config)
    assert section.valid_list.count() == 1


def test_existing_name_blocks_and_never_writes(env) -> None:
    settings, _config, folder = env
    folder.mkdir()
    (folder / "var.md").write_text("kullanici", encoding="utf-8")
    dialog = SkillDialog(settings)
    fill(dialog, "var", "x", "y")
    assert not dialog.save_button.isEnabled() and "zaten var" in dialog.name_hint.text()
    dialog.submit()
    assert (folder / "var.md").read_text(encoding="utf-8") == "kullanici"
    with pytest.raises(SkillWriteError):  # yarışta bile open("x") ezmez
        create_skill(settings, "var", compose("var", "x", "y"))
    assert (folder / "var.md").read_text(encoding="utf-8") == "kullanici"


def test_invalid_slug_and_long_body_disable_save(env) -> None:
    settings, _c, _f = env
    dialog = SkillDialog(settings)
    fill(dialog, "Kötü Ad", "x", "y")
    assert not dialog.save_button.isEnabled()
    assert dialog.use_slug.isEnabled() and not dialog.use_slug.isHidden()
    dialog.use_slug.click()
    assert dialog.name.text() == "kotu-ad" and dialog.save_button.isEnabled()
    dialog.body.setPlainText("a" * (MAX_BODY + 1))
    assert not dialog.save_button.isEnabled() and "en çok" in dialog.status.text()
    assert dialog.body_count.objectName() == "warnNote"
    dialog.body.setPlainText("a" * 10)
    assert dialog.body_count.objectName() == "meta" and dialog.save_button.isEnabled()
    dialog.description.setText("d" * 201)
    assert not dialog.save_button.isEnabled()


def test_template_fills_and_asks_before_replacing(env, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, _c, _f = env
    template_name, _text = templates()[0]
    dialog = SkillDialog(settings)
    dialog.template.setCurrentIndex(1)
    assert dialog.name.text() == template_name and dialog.body.toPlainText().strip()
    assert dialog.save_button.isEnabled()
    dialog.body.setPlainText("benim metnim")
    asked: list[object] = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a) or NO)
    dialog.template.setCurrentIndex(2)
    assert asked and dialog.body.toPlainText() == "benim metnim"
    assert dialog.template.currentIndex() == 1  # Hayır: seçim geri alındı
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: YES)
    dialog.template.setCurrentIndex(2)
    assert dialog.body.toPlainText() != "benim metnim"


def test_preview_renders_markdown_without_opening_links(env) -> None:
    settings, _c, _f = env
    dialog = SkillDialog(settings)
    dialog.body.setPlainText("# Başlık\n\n[x](https://example.com)")
    dialog.preview_toggle.setChecked(True)
    assert "Başlık" in dialog.preview.toPlainText()
    assert not dialog.preview.openLinks() and not dialog.preview.openExternalLinks()


def _make(settings: Settings, folder: Path) -> bytes:
    create_skill(settings, "duz", compose("duz", "açıklama", "gövde"))
    return (folder / "duz.md").read_bytes()


def test_edit_saves_atomically_and_name_readonly(env) -> None:
    settings, _c, folder = env
    original = _make(settings, folder)
    dialog = SkillDialog(settings, "duz", original)
    assert dialog.name.isReadOnly() and dialog.body.toPlainText() == "gövde"
    dialog.body.setPlainText("yeni gövde")
    dialog.submit()
    assert load_skills(settings).get("duz").body == "yeni gövde"  # type: ignore[union-attr]
    assert [p.name for p in folder.iterdir()] == ["duz.md"]  # geçici dosya kalmadı


def test_edit_detects_external_change_and_keeps_it(env) -> None:
    settings, _c, folder = env
    original = _make(settings, folder)
    dialog = SkillDialog(settings, "duz", original)
    dialog.body.setPlainText("benim düzenlemem")
    external = compose("duz", "açıklama", "dışarıda değişti").encode("utf-8")
    (folder / "duz.md").write_bytes(external)
    dialog.submit()
    assert (folder / "duz.md").read_bytes() == external
    assert "değişti" in dialog.status.text() and dialog.result() == 0
    assert dialog.body.toPlainText() == "benim düzenlemem"  # yazdıkların kaybolmaz


def test_edit_refuses_symlink(env) -> None:
    settings, _c, folder = env
    folder.mkdir()
    target = folder.parent / "hedef.md"
    target.write_bytes(compose("bag", "a", "b").encode("utf-8"))
    try:
        (folder / "bag.md").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Bu makinede sembolik bağlantı oluşturma izni yok")
    with pytest.raises(SkillWriteError):
        replace_skill(settings, "bag", compose("bag", "a", "yeni"), target.read_bytes())
    with pytest.raises(SkillWriteError):
        read_for_edit(settings, "bag")
    assert target.read_bytes() == compose("bag", "a", "b").encode("utf-8")


def test_fix_invalid_file_loads_raw_with_error_then_saves(env) -> None:
    settings, config, folder = env
    folder.mkdir()
    (folder / "bozuk.md").write_text("başlık yok\ngövde", encoding="utf-8")
    section = SkillsSection(config)
    assert section.invalid_list.count() == 1
    section.invalid_list.setCurrentRow(0)
    assert section.fix_button.isEnabled()
    original = read_for_edit(settings, "bozuk")
    dialog = SkillDialog(settings, "bozuk", original)
    assert dialog.raw.toPlainText() == "başlık yok\ngövde"
    assert "başlıkla" in dialog.status.text() and not dialog.save_button.isEnabled()
    dialog.raw.setPlainText(compose("bozuk", "artık geçerli", "gövde"))
    assert dialog.save_button.isEnabled()
    dialog.submit()
    assert [s.name for s in load_skills(settings).skills] == ["bozuk"]


def test_delete_asks_default_no_then_removes(env, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, config, folder = env
    _make(settings, folder)
    section = SkillsSection(config)
    section.valid_list.setCurrentRow(0)
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: seen.append(a) or NO)
    section.delete_button.click()
    assert (folder / "duz.md").exists() and seen[0][-1] == NO  # varsayılan düğme Hayır
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: YES)
    section.valid_list.setCurrentRow(0)
    section.delete_button.click()
    assert not (folder / "duz.md").exists() and section.valid_list.count() == 0


def test_section_opens_dialog_for_new_edit_and_never_external(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, config, folder = env
    _make(settings, folder)
    opened: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(
        SkillDialog, "exec",
        lambda self: opened.append((self.windowTitle(), self.name.isReadOnly())) or 0,
    )
    section = SkillsSection(config)
    section.new_button.click()
    section.valid_list.setCurrentRow(0)
    section.edit_button.click()
    assert opened == [("Yeni beceri", False), ("Beceriyi düzenle", True)]
    assert section.open_button.text() == "Klasörü göster"


def test_delete_skill_rejects_path_like_names(env) -> None:
    """Silme yalnız klasördeki tek parça `.md` adını kabul eder; yol parçası reddedilir."""
    from artemis.skills import SkillWriteError, delete_skill

    settings, _config, _folder = env
    for bad in ("../x.md", "a/b.md", "a\b.md", "C:x.md", "..", ""):
        with pytest.raises(SkillWriteError):
            delete_skill(settings, bad)


def test_delete_skill_removes_dangling_link_only(env, tmp_path: Path) -> None:
    from artemis.skills import delete_skill

    settings, _config, folder = env
    folder.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "hedef.md"
    outside.write_text("x", encoding="utf-8")
    link = folder / "bag.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Bu makinede sembolik bağlantı oluşturma izni yok")
    outside.unlink()  # kırık bağlantı
    delete_skill(settings, "bag.md")
    assert not link.is_symlink()
