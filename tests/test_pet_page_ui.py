"""Petler sayfası (hero, kart galerisi, bağlamsal eylemler, atölye boş durumu)."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QScrollArea

from artemis.channels.desktop import pet_section as section_mod
from artemis.channels.desktop.pet_section import PetSection
from artemis.config.settings import ModelConfig
from tests.test_pet_sprite import make_pet, qapp  # noqa: F401


def build(tmp_path: Path, qapp: QApplication, **kw: object) -> PetSection:  # noqa: F811
    codex = tmp_path / "codex"
    make_pet(codex, "theirs")
    sec = PetSection(tmp_path / "pets", codex, config_path=tmp_path / "c.yaml", **kw)  # type: ignore[arg-type]
    assert sec.import_path(make_pet(tmp_path / "src", "mine")) is not None
    return sec


def pick(sec: PetSection, pet_id: str) -> None:
    for i in range(sec.listing.count()):
        if sec.listing.item(i).data(Qt.ItemDataRole.UserRole) == pet_id:
            sec.listing.setCurrentRow(i)


def test_hero_follows_selection_and_actions_are_contextual(
    tmp_path: Path, qapp: QApplication  # noqa: F811
) -> None:
    sec = build(tmp_path, qapp)
    assert sec.hero_name.text() == "Küre"
    assert sec.select_button.isHidden() and sec.hero_select_button.isHidden()
    pick(sec, "mine")
    assert not sec.select_button.isHidden() and sec.select_button.isEnabled()
    assert not sec.delete_button.isHidden()
    sec.select_current()
    assert sec.hero_name.text() == "Mine" and sec.select_button.isHidden()
    pick(sec, "theirs")
    assert sec.delete_button.isHidden()  # Codex pet'i silinemez
    sec.deleteLater()


def test_gallery_items_have_accessible_names(tmp_path: Path, qapp: QApplication) -> None:  # noqa: F811
    sec = build(tmp_path, qapp)
    pick(sec, "mine")
    sec.select_current()
    names = {
        sec.listing.item(i).data(Qt.ItemDataRole.AccessibleTextRole)
        for i in range(sec.listing.count())
    }
    assert {"Mine, seçili", "Küre", "Theirs"} <= names
    card = sec.listing.itemWidget(sec.listing.item(1))
    assert card is not None and card.accessibleName()
    sec.deleteLater()


def test_empty_production_state_and_form(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    sec = build(tmp_path, qapp)
    creator = sec.creator
    assert creator is not None
    assert not creator.empty_state.isHidden() and creator.workshop.isHidden()
    assert creator.stage_bar.isHidden() and creator.model_button.isHidden()
    sec.model_settings_requested.connect(lambda: None)
    sec.sync_model_hook()
    assert not creator.model_button.isHidden()  # pencere bağladıysa düğme açılır
    creator._models = {"y": ModelConfig(provider="ollama", model="m",
                                        input_per_million=Decimal(0),
                                        output_per_million=Decimal(0))}
    creator._fill_models()
    assert creator.empty_state.isHidden() and not creator.workshop.isHidden()
    assert not creator.stage_bar.isHidden()
    sec.deleteLater()


def test_stage_bar_is_outside_scrolling_body(tmp_path: Path, qapp: QApplication) -> None:  # noqa: F811
    sec = build(tmp_path, qapp)
    creator = sec.creator
    assert creator is not None
    body = creator.workshop
    assert isinstance(body, QScrollArea)
    parent = creator.stage_bar.parent()
    while parent is not None and parent is not creator:
        assert parent is not body
        parent = parent.parent()
    sec.deleteLater()


def test_no_hard_coded_hex_in_pet_modules() -> None:
    base = Path(section_mod.__file__).parent
    for name in ("pet_section.py", "pet_creator_panel.py"):
        code = (base / name).read_text(encoding="utf-8")
        assert not re.search(r"[\"']#[0-9a-fA-F]{3,8}\b", code), name
