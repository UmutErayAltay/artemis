"""Kullanıcı becerileri sertleştirme testleri: junction, yarış/geri alma ve başlık ayracı."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import artemis.skills as skills_mod
from artemis.config.settings import Settings, SkillsConfig
from artemis.skills import (
    SkillWriteError,
    create_skill,
    ensure_example,
    load_skills,
    location_problem,
    parse_bytes,
    replace_skill,
    skills_dir,
)


def make_settings(
    tmp_path: Path, enabled: bool = True, folder: Path | None = None, **extra: Any
) -> Settings:
    return Settings(
        workspace=tmp_path / "ws",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "a.jsonl",
        persist_memory=False,
        skills=SkillsConfig(enabled=enabled, folder=folder),
        **extra,
    )


def write(settings: Settings, name: str, text: str | bytes) -> Path:
    folder = skills_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="")
    return path


def skill_text(
    name: str = "rapor", desc: str = "Haftalık rapor.", body: str = "Notları oku."
) -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n"


def test_folder_junction_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Bugünkü kodda klasör junction denetlenmediği için bu test mevcut kodda başarısız olur.
    folder = tmp_path / "beceriler"
    folder.mkdir(parents=True)
    settings = make_settings(tmp_path, folder=folder)
    monkeypatch.setattr(
        os.path, "isjunction", lambda p: Path(p) == folder, raising=False
    )

    problem = location_problem(settings, folder)
    assert problem is not None and "bağlantı" in problem
    assert load_skills(settings).refused == problem
    with pytest.raises(SkillWriteError):
        create_skill(settings, "ornek", skill_text("ornek"))


def test_file_junction_is_refused_on_load_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bugünkü kodda load_skills ve replace_skill dosya junction'ını reddetmez, başarısız olur.
    folder = tmp_path / "beceriler"
    settings = make_settings(tmp_path, folder=folder)
    valid = skill_text("ornek")
    target = write(settings, "ornek.md", valid)
    original_bytes = valid.encode("utf-8")
    monkeypatch.setattr(
        os.path, "isjunction", lambda p: Path(p) == target, raising=False
    )

    catalog = load_skills(settings)
    assert catalog.skills == []
    assert "bağlantı" in dict(catalog.invalid)["ornek.md"]
    with pytest.raises(SkillWriteError):
        replace_skill(settings, "ornek", skill_text("ornek", body="Güncel."), original_bytes)


def test_ensure_example_handles_race_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bugünkü kod exist_ok=True ve FileExistsError yakalaması olmadan FileExistsError fırlatır.
    folder = tmp_path / "beceriler"
    folder.mkdir(parents=True)
    existing = "---\nname: ornek-a\ndescription: eski\n---\n\nÖzgün gövde\n"
    (folder / "ornek-a.md").write_text(existing, encoding="utf-8", newline="")
    tpl_a = "---\nname: ornek-a\ndescription: test\n---\n\nGövde A\n"
    tpl_b = "---\nname: ornek-b\ndescription: test\n---\n\nGövde B\n"
    monkeypatch.setattr(
        skills_mod, "templates", lambda: [("ornek-a", tpl_a), ("ornek-b", tpl_b)]
    )

    orig_exists = Path.exists
    seen = False

    def fake_exists(self: Path) -> bool:
        nonlocal seen
        # Yalnız ilk klasör varlığı denetimini atlatarak yarış durumunu canlandır.
        if self == folder and not seen:
            seen = True
            return False
        return orig_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    settings = make_settings(tmp_path, folder=folder)
    ensure_example(settings)

    assert (folder / "ornek-a.md").read_text(encoding="utf-8") == existing
    assert (folder / "ornek-b.md").read_text(encoding="utf-8") == tpl_b


def test_ensure_example_rolls_back_on_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bugünkü kodda ikinci şablon yazımı başarısız olunca ilk dosya geri alınmaz, kalır.
    folder = tmp_path / "beceriler"
    tpl_a = "---\nname: ornek-a\ndescription: test\n---\n\nGövde A\n"
    tpl_b = "---\nname: ornek-b\ndescription: test\n---\n\nGövde B\n"
    monkeypatch.setattr(
        skills_mod, "templates", lambda: [("ornek-a", tpl_a), ("ornek-b", tpl_b)]
    )

    orig_open = Path.open

    def fake_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == "ornek-b.md":
            raise PermissionError("erişim engellendi")
        return orig_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    settings = make_settings(tmp_path, folder=folder)
    with pytest.raises(OSError):
        ensure_example(settings)

    assert not (folder / "ornek-a.md").exists()
    assert not folder.exists()


def test_parse_bytes_requires_clean_closing_delimiter() -> None:
    # Bugünkü kod "---bozuk" satırını kapanış ayracı sayıp kabul ettiği için başarısız olur.
    bad = "---\nname: a\ndescription: b\n---bozuk\n\nGövde\n".encode()
    with pytest.raises(ValueError, match="başlıkla"):
        parse_bytes(bad, "a")

    trailing_spaces = "---\nname: a\ndescription: b\n---  \n\nGövde\n".encode()
    skill = parse_bytes(trailing_spaces, "a")
    assert skill.name == "a"
    assert skill.description == "b"
    assert skill.body == "Gövde"
