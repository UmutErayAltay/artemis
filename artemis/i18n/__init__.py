"""Basit yerelleştirme katmanı: kaynak metin anahtardır, eksik çeviri Türkçe kalır.

Dil açılışta ayarlanır ve Ayarlar'dan değiştirildiğinde arayüzde canlı olarak güncellenir.
"""

from __future__ import annotations

import functools
import json
import re
import string
from pathlib import Path

_language: str = "tr"
_SUPPORTED = frozenset({"tr", "en"})


def set_language(code: str) -> None:
    """Etkin arayüz dilini ayarlar; bilinmeyen kodlar Türkçeye düşer."""
    global _language
    _language = code if code in _SUPPORTED else "tr"


def current_language() -> str:
    """Etkin arayüz dili kodunu döndürür."""
    return _language


@functools.cache
def _load_en_catalog() -> dict[str, str]:
    en_dir = Path(__file__).parent / "en"
    merged: dict[str, str] = {}
    if not en_dir.is_dir():
        return merged
    for path in sorted(en_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Çeviri dosyası JSON nesnesi olmalı: {path.name}")
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError(f"Çeviri anahtarı ve değeri metin olmalı: {path.name}")
            merged[key] = value
    return merged


def catalog() -> dict[str, str]:
    """Birleştirilmiş İngilizce çeviri sözlüğünü döndürür."""
    return dict(_load_en_catalog())


@functools.cache
def _reverse_en_catalog() -> dict[str, str]:
    reverse: dict[str, str] = {}
    for tr_text, en_text in catalog().items():
        reverse.setdefault(en_text, tr_text)
    return reverse


def _compile_template(template: str) -> tuple[re.Pattern[str], str, set[str]] | None:
    regex_parts: list[str] = ["^"]
    clean_parts: list[str] = []
    names: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return None
    for literal, field_name, _, _ in parsed:
        regex_parts.append(re.escape(literal))
        clean_parts.append(literal.replace("{", "{{").replace("}", "}}"))
        if field_name is None:
            continue
        if not field_name.isidentifier():
            return None
        if field_name in names:
            regex_parts.append(f"(?P={field_name})")
        else:
            regex_parts.append(f"(?P<{field_name}>.+?)")
            names.add(field_name)
        clean_parts.append(f"{{{field_name}}}")
    if not names:
        return None
    regex_parts.append("$")
    return (
        re.compile("".join(regex_parts), re.DOTALL),
        "".join(clean_parts),
        names,
    )


@functools.cache
def _template_patterns() -> list[tuple[re.Pattern[str], re.Pattern[str], str, str, str]]:
    patterns: list[tuple[re.Pattern[str], re.Pattern[str], str, str, str]] = []
    for key, val in _load_en_catalog().items():
        if "{" not in key:
            continue
        tr_comp = _compile_template(key)
        en_comp = _compile_template(val)
        if tr_comp is None or en_comp is None:
            continue
        tr_re, tr_clean, tr_names = tr_comp
        en_re, en_clean, en_names = en_comp
        if tr_names != en_names:
            continue
        patterns.append((tr_re, en_re, key, tr_clean, en_clean))
    return patterns


def _retarget_templated(text: str, language: str) -> str:
    for tr_re, en_re, _key, tr_clean, en_clean in _template_patterns():
        match = tr_re.fullmatch(text) or en_re.fullmatch(text)
        if match is None:
            continue
        groups = match.groupdict()
        target = en_clean if language == "en" else tr_clean
        try:
            return target.format(**groups)
        except (KeyError, ValueError, IndexError):
            continue
    return text


def retarget(text: str, language: str) -> str:
    """Görünen bir arayüz metnini kaynağını bularak hedef dile çevirir."""
    entries = _load_en_catalog()
    if text in entries:
        source = text
    else:
        found = _reverse_en_catalog().get(text)
        if found is None:
            return _retarget_templated(text, language)
        source = found
    if language == "en":
        return entries.get(source, source)
    return source


def t(text: str) -> str:
    """Türkçe kaynak metni etkin dile çevirir; eksik çeviri Türkçe kalır."""
    if _language != "en":
        return text
    return _load_en_catalog().get(text, text)
