"""Pet üretici (ADR 0048 madde 8, Paket 3a): SVG temizleme, animatör, sohbet arayüzü; ağ yok."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from artemis.brain.errors import BudgetExceeded, ProviderError, RateLimitedError
from artemis.brain.models import Completion
from artemis.channels.desktop import pet_animator as anim
from artemis.channels.desktop import pet_creator as pc
from artemis.channels.desktop import pet_creator_panel as panel_mod
from artemis.channels.desktop import pet_sprite as ps
from artemis.channels.desktop.pet_section import PetSection
from artemis.config.settings import ModelConfig, Settings

GOOD = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 208">'
    '<defs><linearGradient id="g"><stop offset="0" stop-color="#fa0"/>'
    '<stop offset="1" stop-color="#f60"/></linearGradient></defs>'
    '<ellipse cx="96" cy="120" rx="60" ry="70" fill="url(#g)"/>'
    '<g id="eyes"><circle cx="76" cy="100" r="8" fill="#000"/>'
    '<circle cx="116" cy="100" r="8" fill="#000"/></g></svg>'
)


@pytest.fixture
def qapp() -> QApplication:
    current = QApplication.instance()
    return current if isinstance(current, QApplication) else QApplication([])


def wrap(inner: str, attrs: str = "") -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}>{inner}</svg>'


# ------------------------------------------------------------- temizleyici


def test_sanitize_keeps_good_svg() -> None:
    out = pc.sanitize_svg(GOOD)
    assert "linearGradient" in out and 'fill="url(#g)"' in out and 'id="eyes"' in out


def test_sanitize_strips_dangerous_things() -> None:
    dirty = wrap(
        "<script>alert(1)</script><foreignObject><div/></foreignObject>"
        '<image href="http://x/y.png"/><use href="#a"/><animate attributeName="x"/>'
        '<a href="http://evil"><circle r="1"/></a><filter id="f"/><style>*{}</style>'
        '<circle cx="5" cy="5" r="3" onload="evil()" onclick="x()" style="fill:red" '
        'xlink:href="http://x" fill="#fff" class="c"/>'
        '<rect width="4" height="4" fill="url(http://evil/x.svg#a)" stroke="url(#ok)"/>',
        'xmlns:xlink="http://www.w3.org/1999/xlink" onload="x()"',
    )
    out = pc.sanitize_svg(dirty)
    for bad in ("script", "foreignObject", "image", "<use", "animate", "<a", "filter", "style",
                "onload", "onclick", "href", "evil", "class"):
        assert bad not in out, bad
    assert 'fill="#fff"' in out and 'stroke="url(#ok)"' in out


@pytest.mark.parametrize(
    "text",
    [
        '<!DOCTYPE svg [<!ENTITY a "b">]>' + GOOD,
        '<!ENTITY x "y">' + GOOD,
        '<?xml-stylesheet href="a.css"?>' + GOOD,
        pytest.param("x" * (pc.MAX_SVG_BYTES + 1), id="huge"),
        "<svg",
        '<html xmlns="http://www.w3.org/2000/svg"><g/></html>',
        wrap(""),
    ],
)
def test_sanitize_rejects(text: str) -> None:
    with pytest.raises(pc.SvgError):
        pc.sanitize_svg(text)


def test_sanitize_accepts_xml_declaration() -> None:
    assert "ellipse" in pc.sanitize_svg('<?xml version="1.0"?>\n' + GOOD)


def test_extract_svg_from_fenced_reply() -> None:
    reply = f"Tamam!\n```svg\n{GOOD}\n```\nBitti."
    assert pc.extract_svg(reply) == GOOD
    assert pc.extract_svg("svg yok") is None


def test_slugify() -> None:
    assert pc.slugify("Şirin Çömlek!") == "sirin-comlek"
    assert pc.slugify("???") == "pet"
    assert ps.ID_RE.fullmatch(pc.slugify("a" * 200))


def test_without_eyes() -> None:
    assert 'id="eyes"' not in pc.without_eyes(pc.sanitize_svg(GOOD))


# --------------------------------------------------------------- animatör


def cell_alpha(sheet: QImage, row: int, col: int) -> bool:
    cell = sheet.copy(col * 192, row * 208, 192, 208)
    return any(bytes(cell.constBits())[3::4])


def test_sheet_layout_and_blink(qapp: QApplication) -> None:
    base = anim.render_svg(pc.sanitize_svg(GOOD))
    blink = anim.render_svg(pc.without_eyes(pc.sanitize_svg(GOOD)))
    sheet = anim.build_sheet(base, blink)
    assert (sheet.width(), sheet.height()) == (1536, 1872)
    for row, count in enumerate(ps.FRAMES):
        for col in range(8):
            assert cell_alpha(sheet, row, col) == (col < count), (row, col)

    def raw(col: int) -> bytes:
        return bytes(sheet.copy(col * 192, 0, 192, 208).constBits())

    # Göz kırpma karesi (idle 4) göz kapalı varyantla çizilir; komşusundan farklıdır
    assert raw(anim.BLINK_FRAME) != raw(3)
    # Belirleyici: aynı girdi aynı sayfa
    assert bytes(anim.build_sheet(base, blink).constBits()) == bytes(sheet.constBits())
    assert anim.build_sheet(base).width() == 1536  # varyant yoksa da çalışır


def test_render_empty_svg_is_error(qapp: QApplication) -> None:
    with pytest.raises(pc.SvgError):
        anim.render_svg(pc.sanitize_svg(wrap("<g/>", 'viewBox="0 0 192 208"')))


def test_install_roundtrip_and_overwrite(qapp: QApplication, tmp_path: Path) -> None:
    sheet = anim.build_sheet(anim.render_svg(pc.sanitize_svg(GOOD)))
    info = anim.install(sheet, "mini", "Mini", "d", tmp_path / "pets")
    assert info.id == "mini" and ps.load_sheet(info).width() == 1536
    with pytest.raises(ps.PetExistsError):
        anim.install(sheet, "mini", "Mini", "d", tmp_path / "pets")
    anim.install(sheet, "mini", "Yeni", "d", tmp_path / "pets", overwrite=True)
    assert ps.read_pet(tmp_path / "pets" / "mini", "artemis").display_name == "Yeni"


def test_animator_accepts_any_base_size(qapp: QApplication) -> None:
    big = QImage(500, 300, QImage.Format.Format_ARGB32)
    big.fill(0xFFFF0000)
    assert anim.build_sheet(big).height() == 1872


# ------------------------------------------------------------------ arayüz


class FakeRouter:
    reply: Any = None
    calls: list[tuple[str, list[Any]]] = []

    def __init__(self, settings: Settings, audit: Any) -> None:
        self.alias = settings.primary_model
        assert settings.fallback_model is None

    async def complete(self, messages: Any, tools: Any, *, timeout: float) -> Completion:
        FakeRouter.calls.append((self.alias, messages))
        if isinstance(FakeRouter.reply, Exception):
            raise FakeRouter.reply
        return FakeRouter.reply  # type: ignore[no-any-return]


def make_settings(tmp_path: Path) -> Settings:
    free = ModelConfig(provider="ollama", model="m", input_per_million=Decimal(0),
                       output_per_million=Decimal(0))
    paid = ModelConfig(provider="openrouter", model="p", input_per_million=Decimal("1"),
                       output_per_million=Decimal("2"))
    return Settings(workspace=tmp_path, state_dir=tmp_path / "s", audit_path=tmp_path / "a.jsonl",
                    models={"bedava": free, "ucretli": paid}, primary_model="bedava")


@pytest.fixture
def section(qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            request: pytest.FixtureRequest) -> PetSection:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(panel_mod, "load_settings", lambda path: settings)
    monkeypatch.setattr(panel_mod, "ProviderRouter", FakeRouter)
    FakeRouter.calls = []
    FakeRouter.reply = Completion(content=f"İşte:\n```svg\n{GOOD}\n```")
    sec = PetSection(tmp_path / "pets", None, config_path=tmp_path / "c.yaml")
    request.addfinalizer(sec.deleteLater)
    request.addfinalizer(lambda: sec.creator and sec.creator.shutdown())
    return sec


def wait_reply(creator: panel_mod.CreatorPanel, qapp: QApplication) -> None:
    for _ in range(500):
        qapp.processEvents()
        if creator._call is None:
            return
        creator._call.wait(20)
    raise AssertionError("yanıt gelmedi")


def test_ui_flow_send_preview_save(section: PetSection, qapp: QApplication,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    creator = section.creator
    assert creator is not None
    assert "Resim modeli" in creator.method.itemText(1)
    chosen: list[str] = []
    section.skin_selected.connect(chosen.append)
    creator.name_edit.setText("Şirin Maskot")
    creator.input.setText("turuncu bir kedi")
    creator.send()
    assert not creator.send_button.isEnabled()  # meşgul
    wait_reply(creator, qapp)
    assert creator._sheet is not None and not creator.preview.pixmap().isNull()
    assert creator.save_button.isEnabled()
    assert FakeRouter.calls[0][0] == "bedava"
    assert "turuncu bir kedi" in FakeRouter.calls[0][1][1].content
    # düzeltme: önceki SVG isteğe eklenir
    creator.input.setText("daha mavi yap")
    creator.send()
    wait_reply(creator, qapp)
    second = FakeRouter.calls[1][1][1].content
    assert "<svg" in second and "daha mavi" in second
    creator.save()
    assert chosen == ["sirin-maskot"]
    labels = [section.listing.item(i).text() for i in range(section.listing.count())]
    assert any("Şirin Maskot · Artemis" in t for t in labels)
    # var olan id: onay reddedilirse ezilmez
    monkeypatch.setattr(creator, "_confirm", lambda text: False)
    creator.save()
    assert "zaten kurulu" in creator.status.text() and chosen == ["sirin-maskot"]


@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (BudgetExceeded("limit"), "bütçe"),
        (RateLimitedError("429"), "kota"),
        (ProviderError("kötü"), "kötü"),
    ],
)
def test_router_errors_surface(section: PetSection, qapp: QApplication, error: Exception,
                               needle: str) -> None:
    creator = section.creator
    assert creator is not None
    FakeRouter.reply = error
    creator.input.setText("kedi")
    creator.send()
    wait_reply(creator, qapp)
    assert needle in creator.chat_log.toPlainText() and creator._sheet is None
    assert creator.send_button.isEnabled()


def test_bad_replies_are_reported(section: PetSection, qapp: QApplication) -> None:
    creator = section.creator
    assert creator is not None
    for content, needle in (("sadece yazı", "SVG yok"), (wrap("<script/>"), "kullanılamadı")):
        FakeRouter.reply = Completion(content=content)
        creator.input.setText("x")
        creator.send()
        wait_reply(creator, qapp)
        assert needle in creator.chat_log.toPlainText()
    assert creator._sheet is None


def test_paid_warning_and_demo(section: PetSection, tmp_path: Path) -> None:
    creator = section.creator
    assert creator is not None
    assert creator.warning.text() == ""
    creator.model_combo.setCurrentIndex(creator.model_combo.findData("ucretli"))
    assert "ücretli" in creator.warning.text()
    demo = PetSection(tmp_path / "p2", None, demo=True, config_path=tmp_path / "c.yaml")
    assert demo.creator is not None and not demo.creator.send_button.isEnabled()
    demo.creator.shutdown()
    demo.deleteLater()


def test_new_pet_clears_svg_context_but_keeps_installed_pet(
    section: PetSection, qapp: QApplication,
) -> None:
    creator = section.creator
    assert creator is not None
    creator.name_edit.setText("İlk pet")
    creator.input.setText("turuncu kedi")
    creator.send()
    assert not creator.reset_button.isEnabled()
    creator.reset_conversation()  # Direct calls must also respect an in-flight request.
    assert "turuncu kedi" in creator.chat_log.toPlainText()
    wait_reply(creator, qapp)
    creator.save()
    files = {p: p.read_bytes() for p in creator._pets_dir.rglob("*") if p.is_file()}
    assert files
    alias = creator.model_combo.currentData()
    creator.state_combo.setCurrentIndex(3)
    creator.input.setText("henüz gönderilmedi")
    creator.reset_button.click()
    assert creator.chat_log.toPlainText() == creator.input.text() == creator.name_edit.text() == ""
    assert creator._sheet is None and creator._svg == ""
    assert not creator._timer.isActive() and not creator._frames and creator._frame == 0
    assert creator.preview.text() == "Önizleme yok"
    assert not creator.save_button.isEnabled() and creator.reset_button.isEnabled()
    assert creator.status.text() == "" and creator.busy.text() == ""
    assert creator.state_combo.currentIndex() == 0
    assert creator.model_combo.currentData() == alias
    assert all(p.read_bytes() == content for p, content in files.items())
    creator.input.setText("yeşil köpek")
    creator.send()
    wait_reply(creator, qapp)
    assert FakeRouter.calls[-1][1][1].content == "yeşil köpek"
