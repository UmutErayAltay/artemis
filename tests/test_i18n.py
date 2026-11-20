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


def _call_context_arg(node: ast.Call) -> str | None:
    if (
        len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[1].value
    for kw in node.keywords:
        if (
            kw.arg == "context"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return None


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
                ctx = _call_context_arg(node)
                found = literal in entries or (
                    ctx is not None and f"{ctx}::{literal}" in entries
                )
                if not found:
                    rel = py_file.relative_to(artemis_root.parent)
                    missing.append(f"{rel}:{node.lineno}: {literal!r} (context={ctx!r})")
    assert not missing, "Sözlükte eksik t(...) anahtarları:\n" + "\n".join(missing)


def test_catalog_placeholders_match() -> None:
    mismatched: list[str] = []
    for key, value in catalog().items():
        if _placeholders(key) != _placeholders(value):
            mismatched.append(f"{key!r} -> {value!r}")
    assert not mismatched, "Yer tutucuları uyuşmayan girdiler:\n" + "\n".join(mismatched)


def test_catalog_has_no_conflicting_duplicates() -> None:
    import json

    en_dir = Path(i18n.__file__).parent / "en"
    seen: dict[str, tuple[str, str]] = {}
    conflicts: list[str] = []
    for path in sorted(en_dir.glob("*.json")):
        # json.loads aynı dosyadaki tekrar anahtarı sessizce yutar; çiftleri koruyup
        # dosya içi tekrarları da yakalamak için object_pairs_hook kullanılır.
        pairs = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=list)
        in_file = [key for key, _ in pairs]
        dupes = {key for key in in_file if in_file.count(key) > 1}
        conflicts.extend(f"{path.name}: {key!r} dosya içinde tekrar" for key in sorted(dupes))
        for key, value in pairs:
            if key in seen and seen[key][1] != value:
                prev_file, prev_val = seen[key]
                conflicts.append(
                    f"{key!r}: {prev_file}={prev_val!r} != {path.name}={value!r}"
                )
            else:
                seen[key] = (path.name, value)
    assert not conflicts, "Sözlük dosyalarında çakışan anahtarlar:\n" + "\n".join(conflicts)


def test_conflicting_duplicate_key_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    en_dir = tmp_path / "en"
    en_dir.mkdir()
    (en_dir / "a.json").write_text('{"Durum": "Status"}', encoding="utf-8")
    (en_dir / "b.json").write_text('{"Durum": "State"}', encoding="utf-8")
    monkeypatch.setattr(i18n, "__file__", str(tmp_path / "__init__.py"))
    i18n._load_en_catalog.cache_clear()
    i18n._reverse_en_catalog.cache_clear()
    i18n._template_patterns.cache_clear()
    try:
        with pytest.raises(ValueError, match="Çakışan çeviri anahtarı"):
            catalog()
    finally:
        monkeypatch.undo()
        i18n._load_en_catalog.cache_clear()
        i18n._reverse_en_catalog.cache_clear()
        i18n._template_patterns.cache_clear()


def test_context_lookup_and_fallback() -> None:
    from artemis.channels.desktop.themes import THEME_LABELS
    from artemis.channels.desktop.tool_titles import arg_label

    set_language("tr")
    assert t("Açık", context="theme") == "Açık"
    assert t("Açık", context="field") == "Açık"
    assert t("Koyu", context="theme") == "Koyu"
    assert arg_label("enabled") == "Açık"

    set_language("en")
    assert t("Açık", context="theme") == "Light"
    assert t("Açık", context="field") == "Enabled"
    assert t(THEME_LABELS["light"], context="theme") == "Light"
    assert t(THEME_LABELS["dark"], context="theme") == "Dark"
    assert arg_label("enabled") == "Enabled"
    assert arg_label("state") == "State"
    assert arg_label("spec") == "Spec"
    assert arg_label("name") == "Name"
    assert t("Durum") == "Status"
    assert t("Taslak") == "Draft"
    assert t("Ses", context="provider") == "Speech"
    assert t("Ses") == "Voice"
    assert t("Şablon", context="provider") == "Preset"
    assert t("Şablon") == "Template"
    assert t("Zaman", context="routine") == "Schedule"
    assert t("Zaman") == "Time"
    assert t("İptal", context="status") == "Cancelled"
    assert t("İptal") == "Cancel"
    assert t("Bekleme", context="action") == "Wait"
    assert t("Bekleme") == "Idle"
    assert t("bilinmeyen metin", context="theme") == "bilinmeyen metin"


def test_retarget_with_context() -> None:
    assert retarget("Açık", "en", context="theme") == "Light"
    assert retarget("Light", "tr", context="theme") == "Açık"
    assert retarget("Açık", "en", context="field") == "Enabled"
    assert retarget("Enabled", "tr", context="field") == "Açık"
    assert retarget("Koyu", "en", context="theme") == "Dark"
    assert retarget("Dark", "tr", context="theme") == "Koyu"


def test_every_plugin_tool_metadata_is_in_catalog() -> None:
    """Kayıtlı her eklenti aracının label, done_label ve description alanları sözlükte olmalı."""
    from artemis.tools.registry import discover_tools

    entries = catalog()
    registry = discover_tools()
    definitions = registry.definitions()
    assert definitions

    missing: list[str] = []
    mismatched: list[str] = []
    for definition in definitions:
        tool = registry.get(definition.name)
        assert tool is not None
        for field_name in ("label", "done_label", "description"):
            value = getattr(tool, field_name, "")
            if not isinstance(value, str) or not value.strip():
                missing.append(f"{definition.name}.{field_name}: boş")
                continue
            if value not in entries:
                missing.append(f"{definition.name}.{field_name}: {value!r}")
                continue
            if _placeholders(value) != _placeholders(entries[value]):
                mismatched.append(
                    f"{definition.name}.{field_name}: {value!r} -> {entries[value]!r}"
                )

    assert not missing, "Sözlükte eksik eklenti aracı metinleri:\n" + "\n".join(missing)
    assert not mismatched, "Eklenti aracı metinlerinde yer tutucu uyuşmazlığı:\n" + "\n".join(
        mismatched
    )
