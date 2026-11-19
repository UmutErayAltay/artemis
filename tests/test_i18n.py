"""Yerelleştirme altyapısı, sözlük kapsamı ve yer tutucu tutarlılığı testleri."""

from __future__ import annotations

import ast
import string
from collections.abc import Iterator
from pathlib import Path

import pytest

import artemis.i18n as i18n
from artemis.i18n import catalog, current_language, retarget, set_language, t


@pytest.fixture(autouse=True)
def _restore_language() -> Iterator[None]:
    """Her testten sonra dili varsayılan Türkçeye döndürür."""
    set_language("tr")
    try:
        yield
    finally:
        set_language("tr")


def _placeholders(text: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(text)
        if field_name is not None
    }


def test_language_switch_and_fallback() -> None:
    assert current_language() == "tr"
    assert t("Dil") == "Dil"

    set_language("en")
    assert current_language() == "en"
    assert t("Dil") == "Language"
    assert t("bilinmeyen metin") == "bilinmeyen metin"

    set_language("xx")
    assert current_language() == "tr"
    assert t("Dil") == "Dil"


def test_catalog_loads_valid_non_empty_entries() -> None:
    entries = catalog()
    assert entries
    for key, value in entries.items():
        assert isinstance(key, str) and key.strip()
        assert isinstance(value, str) and value.strip()


def test_retarget_directions_and_unknown() -> None:
    assert retarget("Dil", "en") == "Language"
    assert retarget("Language", "tr") == "Dil"
    assert retarget("Dil", "tr") == "Dil"
    assert retarget("Language", "en") == "Language"
    assert retarget("bilinmeyen metin", "en") == "bilinmeyen metin"
    assert retarget("bilinmeyen metin", "tr") == "bilinmeyen metin"
    assert retarget(retarget("Arayüz dili", "en"), "tr") == "Arayüz dili"


def test_retarget_with_placeholders() -> None:
    entries = catalog()
    chosen_key: str | None = None
    chosen_names: set[str] = set()
    for key, val in entries.items():
        if "{" not in key:
            continue
        tr_names = _placeholders(key)
        en_names = _placeholders(val)
        if tr_names and tr_names == en_names and all(n.isidentifier() for n in tr_names):
            chosen_key = key
            chosen_names = tr_names
            break
    assert chosen_key is not None
    sample_values: dict[str, object] = {name: 42 for name in chosen_names}
    tr_formatted = chosen_key.format(**sample_values)
    en_formatted = entries[chosen_key].format(**sample_values)

    assert retarget(tr_formatted, "en") == en_formatted
    assert retarget(en_formatted, "tr") == tr_formatted
    assert retarget(tr_formatted, "tr") == tr_formatted
    assert retarget(en_formatted, "en") == en_formatted


def test_invalid_catalog_json_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    en_dir = tmp_path / "en"
    en_dir.mkdir()
    (en_dir / "broken.json").write_text("{bozuk", encoding="utf-8")
    monkeypatch.setattr(i18n, "__file__", str(tmp_path / "__init__.py"))
    i18n._load_en_catalog.cache_clear()
    i18n._reverse_en_catalog.cache_clear()
    i18n._template_patterns.cache_clear()
    try:
        with pytest.raises(ValueError):
            catalog()
    finally:
        monkeypatch.undo()
        i18n._load_en_catalog.cache_clear()
        i18n._reverse_en_catalog.cache_clear()
        i18n._template_patterns.cache_clear()


def test_every_t_call_literal_is_in_catalog() -> None:
    entries = catalog()
    artemis_root = Path(__file__).resolve().parent.parent / "artemis"
    missing: list[str] = []
    for py_file in sorted(artemis_root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "t"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                literal = node.args[0].value
                if literal not in entries:
                    rel = py_file.relative_to(artemis_root.parent)
                    missing.append(f"{rel}:{node.lineno}: {literal!r}")
    assert not missing, "Sözlükte eksik t(...) anahtarları:\n" + "\n".join(missing)


def test_catalog_placeholders_match() -> None:
    mismatched: list[str] = []
    for key, value in catalog().items():
        if _placeholders(key) != _placeholders(value):
            mismatched.append(f"{key!r} -> {value!r}")
    assert not mismatched, "Yer tutucuları uyuşmayan girdiler:\n" + "\n".join(mismatched)
