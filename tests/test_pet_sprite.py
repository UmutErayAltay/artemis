"""Sprite pet yükleme, kayıt, içe aktarma ve ayar sekmesi (ADR 0048); ağ ve gerçek ev dizini yok."""

from __future__ import annotations

import json
import os
import stat
import zipfile
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from artemis.channels.desktop import pet_sprite as ps
from artemis.channels.desktop.pet_section import PetSection


@pytest.fixture
def qapp() -> QApplication:
    current = QApplication.instance()
    return current if isinstance(current, QApplication) else QApplication([])


def make_sheet(path: Path, size: tuple[int, int] = (1536, 1872), fmt: str = "PNG") -> None:
    """Her hücreye (satır, sütun)'a göre renk basar: R=satır*25, G=sütun*25."""
    image = QImage(size[0], size[1], QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    for row in range(9):
        for col in range(ps.FRAMES[row]):
            painter.fillRect(
                col * ps.CELL_W + 20, row * ps.CELL_H + 20, ps.CELL_W - 40, ps.CELL_H - 40,
                QColor(row * 25, col * 25, 200),
            )
    painter.end()
    assert image.save(str(path), fmt, 100)


def make_pet(
    root: Path, pet_id: str = "blob", *, size: tuple[int, int] = (1536, 1872),
    sheet_name: str = "spritesheet.png", meta: dict[str, Any] | None = None,
) -> Path:
    folder = root / pet_id
    folder.mkdir(parents=True)
    data: dict[str, Any] = {
        "id": pet_id, "displayName": pet_id.title(), "description": "test",
        "spritesheetPath": sheet_name,
    }
    data.update(meta or {})
    (folder / "pet.json").write_text(json.dumps(data), encoding="utf-8")
    make_sheet(folder / sheet_name, size, "WEBP" if sheet_name.endswith("webp") else "PNG")
    return folder


def zip_of(path: Path, files: dict[str, bytes], *, symlink: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            if name == symlink:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, data)
    return path


def pet_files(tmp_path: Path, **kwargs: Any) -> dict[str, bytes]:
    folder = make_pet(tmp_path / "src", **kwargs)
    return {p.name: p.read_bytes() for p in folder.iterdir()}


# ------------------------------------------------------------------ yükleyici


@pytest.mark.parametrize("size", [(1536, 1872), (1536, 2288)])
def test_loads_v1_and_v2(tmp_path: Path, size: tuple[int, int]) -> None:
    folder = make_pet(tmp_path, size=size)
    info = ps.read_pet(folder, "artemis")
    assert info.id == "blob" and info.display_name == "Blob"
    assert ps.load_sheet(info).width() == 1536


def test_loads_webp(tmp_path: Path) -> None:
    folder = make_pet(tmp_path, sheet_name="spritesheet.webp")
    assert ps.load_sheet(ps.read_pet(folder, "artemis")).height() == 1872


@pytest.mark.parametrize(
    "case",
    [
        {"size": (1536, 1000)},
        {"meta": {"id": "Bad_Id"}},
        {"meta": {"id": "-x"}},
        {"meta": {"displayName": ""}},
        {"meta": {"spritesheetPath": "../x.png"}},
        {"meta": {"spritesheetPath": "yok.png"}},
        {"meta": {"spritesheetPath": "spritesheet.gif"}},
    ],
)
def test_rejects_invalid_pets(tmp_path: Path, case: dict[str, Any]) -> None:
    folder = make_pet(tmp_path, **case)
    with pytest.raises(ps.PetFormatError):
        ps.read_pet(folder, "artemis")


def test_rejects_missing_fields_and_bad_json(tmp_path: Path) -> None:
    folder = make_pet(tmp_path)
    (folder / "pet.json").write_text(json.dumps({"id": "blob"}), encoding="utf-8")
    with pytest.raises(ps.PetFormatError, match="displayName"):
        ps.read_pet(folder, "artemis")
    (folder / "pet.json").write_text("{bozuk", encoding="utf-8")
    with pytest.raises(ps.PetFormatError):
        ps.read_pet(folder, "artemis")


def test_rejects_oversized_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = make_pet(tmp_path)
    monkeypatch.setattr(ps, "MAX_IMAGE_BYTES", 100)
    with pytest.raises(ps.PetFormatError, match="20 MB"):
        ps.read_pet(folder, "artemis")


# ------------------------------------------------------------- içe aktarma


def test_import_folder_and_zip(tmp_path: Path) -> None:
    pets = tmp_path / "pets"
    info = ps.import_pet(make_pet(tmp_path / "a"), pets)
    assert info.folder == pets / "blob" and info.source == "artemis"
    archive = zip_of(tmp_path / "b.zip", pet_files(tmp_path, pet_id="zed"))
    assert ps.import_pet(archive, pets).id == "zed"
    assert sorted(p.name for p in pets.iterdir()) == ["blob", "zed"]  # staging kalmaz


# "sheet:evil.png" ad ve üst klasör denetiminden geçer; yalnız safe_join (NTFS alternatif veri
# akışı ':' reddi) yakalar, bu yüzden üye başına safe_join'u sabitler. "CON.png" aygıt adıdır.
@pytest.mark.parametrize(
    "name", ["../x.png", "/abs.png", "C:\\x.png", "sub/x.png", "sheet:evil.png", "CON.png"],
)
def test_zip_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    files = pet_files(tmp_path)
    files[name] = files.pop("spritesheet.png")
    archive = zip_of(tmp_path / "z.zip", files)
    with pytest.raises(ps.PetImportError):
        ps.import_pet(archive, tmp_path / "pets")
    assert not (tmp_path / "x.png").exists()
    assert [p.name for p in (tmp_path / "pets").iterdir()] == []


def test_corrupt_zip_stream_is_pet_import_error(tmp_path: Path) -> None:
    files = pet_files(tmp_path)
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data * 4)
    raw = bytearray(path.read_bytes())
    for index in range(200, 600):  # sıkıştırılmış gövdeyi boz (yerel başlıklar sağlam kalır)
        raw[index] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(ps.PetImportError):
        ps.import_pet(path, tmp_path / "pets")
    assert [p.name for p in (tmp_path / "pets").iterdir()] == []


def test_zip_rejects_symlink_extra_file_and_directory(tmp_path: Path) -> None:
    files = pet_files(tmp_path)
    pets = tmp_path / "pets"
    with pytest.raises(ps.PetImportError, match="bağlantı"):
        ps.import_pet(zip_of(tmp_path / "s.zip", files, symlink="pet.json"), pets)
    with pytest.raises(ps.PetImportError, match="Yalnız"):
        ps.import_pet(zip_of(tmp_path / "e.zip", {**files, "readme.txt": b"x"}), pets)
    with pytest.raises(ps.PetImportError, match="klasör"):
        ps.import_pet(zip_of(tmp_path / "d.zip", {**files, "alt/": b""}), pets)
    with pytest.raises(ps.PetImportError, match="zip değil"):
        (tmp_path / "n.zip").write_bytes(b"degil")
        ps.import_pet(tmp_path / "n.zip", pets)


def test_zip_size_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = pet_files(tmp_path)
    archive = zip_of(tmp_path / "z.zip", files)
    monkeypatch.setattr(ps, "MAX_ZIP_BYTES", 1000)
    with pytest.raises(ps.PetImportError, match="25 MB"):  # bildirilen boyut
        ps.import_pet(archive, tmp_path / "pets")
    monkeypatch.setattr(ps, "MAX_ZIP_BYTES", 25 * 1024 * 1024)
    monkeypatch.setattr(ps, "MAX_IMAGE_BYTES", 500)
    with pytest.raises(ps.PetImportError, match="sınırını"):
        ps.import_pet(archive, tmp_path / "pets")


def test_import_rejects_symlink_in_folder(tmp_path: Path) -> None:
    folder = make_pet(tmp_path / "a")
    link = folder / "link.png"
    try:
        link.symlink_to(folder / "spritesheet.png")
    except (OSError, NotImplementedError):
        pytest.skip("symlink oluşturulamıyor")
    with pytest.raises(ps.PetImportError, match="bağlantı"):
        ps.import_pet(folder, tmp_path / "pets")


def test_import_wrong_dims_and_overwrite_refusal(tmp_path: Path) -> None:
    pets = tmp_path / "pets"
    with pytest.raises(ps.PetFormatError):
        ps.import_pet(make_pet(tmp_path / "bad", size=(1536, 1000)), pets)
    first = make_pet(tmp_path / "a", meta={"displayName": "Birinci"})
    ps.import_pet(first, pets)
    second = make_pet(tmp_path / "b", meta={"displayName": "Ikinci"})
    with pytest.raises(ps.PetExistsError):
        ps.import_pet(second, pets)
    assert ps.read_pet(pets / "blob", "artemis").display_name == "Birinci"
    assert ps.import_pet(second, pets, overwrite=True).display_name == "Ikinci"
    assert sorted(p.name for p in pets.iterdir()) == ["blob"]


# ------------------------------------------------------------------- kayıt


def test_registry_artemis_wins_and_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pets, codex = tmp_path / "pets", tmp_path / "codex" / "pets"
    make_pet(pets, "mine")
    make_pet(pets, "shared", meta={"displayName": "Artemis'in"})
    make_pet(codex, "theirs")
    make_pet(codex, "shared", meta={"displayName": "Codex'in"})
    make_pet(codex, "broken", size=(10, 10))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    found = {p.id: p for p in ps.list_pets(pets)}
    assert sorted(found) == ["mine", "shared", "theirs"]
    assert found["shared"].display_name == "Artemis'in"
    assert found["theirs"].source == "codex" and found["mine"].source == "artemis"
    assert ps.list_pets(tmp_path / "yok", tmp_path / "yok2") == []


def test_delete_only_artemis_folder(tmp_path: Path) -> None:
    pets = tmp_path / "pets"
    make_pet(pets, "mine")
    ps.delete_pet(pets, "mine")
    assert not (pets / "mine").exists()
    with pytest.raises(ps.PetError):
        ps.delete_pet(pets, "../x")


# ------------------------------------------------------------- durum eşlemesi


def test_state_to_row_mapping() -> None:
    row = ps.row_for
    expected = {
        "idle": ps.IDLE, "off": ps.IDLE, "listening": ps.REVIEW, "thinking": ps.RUNNING,
        "speaking": ps.WAVING, "approval": ps.WAITING, "error": ps.FAILED,
    }
    for state, want in expected.items():
        assert row(state, None, 9.0, False, True) == want, state
    assert row("idle", "happy", 0.1, False, True) == ps.JUMPING
    assert row("idle", "happy", 5.0, False, True) == ps.IDLE  # bir tur sonra idle
    assert row("approval", "happy", 0.1, False, True) == ps.WAITING
    assert row("idle", None, 0, True, True) == ps.RUN_RIGHT
    assert row("thinking", None, 0, True, False) == ps.RUN_LEFT


def test_frame_index_loop_once_and_reduced(tmp_path: Path, qapp: QApplication) -> None:
    sprite = ps.SpritePet(ps.read_pet(make_pet(tmp_path), "artemis"), 120)
    assert sprite.frame_index(ps.IDLE, 0.0, False) == 0
    assert sprite.frame_index(ps.IDLE, 7.5 / ps.FPS, False) == 1  # 6 kare döner; 7.5 float payı
    assert sprite.frame_index(ps.JUMPING, 99.0, False) == 4  # döngü yok
    assert sprite.frame_index(ps.RUN_RIGHT, 99.0, True) == 0  # hareket azaltıldı


# --------------------------------------------------------------- ayar sekmesi


def test_section_select_import_delete(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    pets, codex = tmp_path / "pets", tmp_path / "codex"
    make_pet(codex, "theirs")
    section = PetSection(pets, codex)
    request.addfinalizer(section.deleteLater)
    monkeypatch.setattr(section, "_confirm", lambda text: True)
    chosen: list[str] = []
    section.skin_selected.connect(chosen.append)

    assert section.import_path(make_pet(tmp_path / "src", "mine")) is not None
    labels = [section.listing.item(i).text() for i in range(section.listing.count())]
    assert any("Küre" in t for t in labels)
    assert any("Mine · Artemis" in t for t in labels) and any("Theirs · Codex" in t for t in labels)
    assert not section.listing.item(1).icon().isNull()

    def pick(pet_id: str) -> None:
        for i in range(section.listing.count()):
            if section.listing.item(i).data(0x0100) == pet_id:
                section.listing.setCurrentRow(i)

    pick("mine")
    section.select_current()
    assert chosen == ["mine"]
    assert "seçili" in section.listing.item(section.listing.currentRow()).text()
    assert section.delete_button.isEnabled()

    pick("theirs")
    assert not section.delete_button.isEnabled()
    section.delete_selected()  # düğme kapalı olsa da yöntem Codex'e dokunmaz
    assert (codex / "theirs").exists()

    pick("mine")
    section.select_current()
    section.delete_selected()
    assert not (pets / "mine").exists() and chosen[-1] == "orb"

    assert section.import_path(tmp_path / "yok.zip") is None
    assert "yüklenemedi" in section.status.text()
    # Var olan id: onay verilince ezilir, reddedilince değişmez
    section.import_path(make_pet(tmp_path / "s2", "mine", meta={"displayName": "Yeni"}))
    monkeypatch.setattr(section, "_confirm", lambda text: False)
    assert section.import_path(make_pet(tmp_path / "s3", "mine", meta={"displayName": "X"})) is None
    assert ps.read_pet(pets / "mine", "artemis").display_name == "Yeni"
