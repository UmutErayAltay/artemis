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
    assert sec.hero_select_button.isHidden()
    pick(sec, "mine")
    assert not sec.hero_select_button.isHidden() and sec.hero_select_button.isEnabled()
    assert not sec.delete_button.isHidden()
    sec.select_current()
    assert sec.hero_name.text() == "Mine" and sec.hero_select_button.isHidden()
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


def test_creator_has_no_nested_scroll_area(tmp_path: Path, qapp: QApplication) -> None:  # noqa: F811
    sec = build(tmp_path, qapp)
    creator = sec.creator
    assert creator is not None
    # Sayfada tek kaydırma bölgesi (app.py pet_scroll); atölye ve galeri kaydırıcı taşımaz.
    assert not sec.findChildren(QScrollArea)
    sec.deleteLater()


def test_no_hard_coded_hex_in_pet_modules() -> None:
    base = Path(section_mod.__file__).parent
    for name in ("pet_section.py", "pet_creator_panel.py"):
        code = (base / name).read_text(encoding="utf-8")
        assert not re.search(r"[\"']#[0-9a-fA-F]{3,8}\b", code), name


def _with_model(sec: PetSection) -> None:
    creator = sec.creator
    assert creator is not None
    creator._models = {"y": ModelConfig(provider="ollama", model="m",
                                        input_per_million=Decimal(0),
                                        output_per_million=Decimal(0))}
    creator._fill_models()


def _show_workshop(sec: PetSection, qapp: QApplication, width: int = 1000) -> None:  # noqa: F811
    sec.resize(width, 1400)  # kaydırma alanı içindeki serbest yükseklik
    sec.show()
    sec.show_workshop()
    for _ in range(4):
        qapp.processEvents()


def test_no_model_workshop_is_compact_and_centered(
    tmp_path: Path, qapp: QApplication  # noqa: F811
) -> None:
    sec = build(tmp_path, qapp)
    creator = sec.creator
    assert creator is not None
    _show_workshop(sec, qapp)
    assert creator.empty_state.isVisibleTo(creator) and creator.width() <= 520
    assert abs((creator.x() + creator.width() / 2) - sec.width() / 2) <= 2
    assert creator.height() < sec.height() - 150  # sayfa boyuna gerilmez
    assert not sec.back_button.isHidden()
    sec.close()
    sec.deleteLater()


def test_model_workshop_sizes_to_content(tmp_path: Path, qapp: QApplication) -> None:  # noqa: F811
    sec = build(tmp_path, qapp)
    creator = sec.creator
    assert creator is not None
    _with_model(sec)
    for width in (1000, 700):  # dar: sütunlar alt alta
        _show_workshop(sec, qapp, width)
        assert abs(creator.height() - creator.sizeHint().height()) <= 32
        # Gövdenin altında yalnız kart iç boşluğu kalır (ölü alan yok).
        assert creator.height() - creator.workshop.geometry().bottom() <= 32
        assert creator.width() >= width - 8
    sec.close()
    sec.deleteLater()


def test_preview_silhouette_and_chat_hint(tmp_path: Path, qapp: QApplication) -> None:  # noqa: F811
    sec = build(tmp_path, qapp)
    creator = sec.creator
    assert creator is not None
    _with_model(sec)
    _show_workshop(sec, qapp)
    assert creator.silhouette.isVisibleTo(creator) and creator.preview_hint.isVisibleTo(creator)
    assert "tilki" in creator.chat_hint.text() and creator.chat_hint.isVisibleTo(creator)
    assert creator.chat_log.minimumHeight() >= 140
    from PySide6.QtGui import QPixmap
    pixmap = QPixmap(8, 8)
    pixmap.fill(Qt.GlobalColor.red)
    creator.preview.setPixmap(pixmap)
    creator._sync_placeholder()
    assert not creator.silhouette.isVisibleTo(creator)
    creator.chat_log.appendPlainText("Sen: merhaba")
    assert not creator.chat_hint.isVisibleTo(creator)
    creator.chat_log.clear()
    assert creator.chat_hint.isVisibleTo(creator)
    sec.close()
    sec.deleteLater()


def test_gallery_keyboard_focus_ring(tmp_path: Path, qapp: QApplication) -> None:  # noqa: F811
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QFocusEvent

    from artemis.channels.desktop.pet_section import _PetCard
    from artemis.channels.desktop.themes import stylesheet

    sec = build(tmp_path, qapp)
    pick(sec, "mine")
    card = sec.listing.itemWidget(sec.listing.currentItem())
    other = sec.listing.itemWidget(sec.listing.item(0))
    assert isinstance(card, _PetCard) and isinstance(other, _PetCard)
    assert card.property("focused") == "false"
    QApplication.sendEvent(
        sec.listing, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason)
    )
    assert card.property("focused") == "true" and other.property("focused") == "false"
    QApplication.sendEvent(
        sec.listing, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason)
    )
    assert card.property("focused") == "false"
    for theme in ("light", "dark", "light-mixed", "dark-mixed"):
        css = stylesheet(theme)
        assert 'QFrame#galleryCard[focused="true"]' in css
    sec.deleteLater()
