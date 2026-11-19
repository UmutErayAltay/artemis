"""Basit yerelleştirme katmanı: kaynak metin anahtardır, eksik çeviri Türkçe kalır.

Dil açılışta ayarlanır ve Ayarlar'dan değiştirildiğinde arayüzde canlı olarak güncellenir.
Bağlama bağlı çeviriler sözlükte ``"<bağlam>::<Türkçe metin>"`` anahtarıyla saklanır;
arama önce bağlamlı anahtara, yoksa düz anahtara bakar.
"""

from __future__ import annotations

import functools
import json
import re
import string
from pathlib import Path

_language: str = "tr"
_SUPPORTED = frozenset({"tr", "en"})
_CONTEXT_SEP = "::"


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
            if key in merged and merged[key] != value:
                raise ValueError(
                    f"Çakışan çeviri anahtarı ({path.name}): "
                    f"{key!r} -> {merged[key]!r} != {value!r}"
                )
            merged[key] = value
    return merged


def catalog() -> dict[str, str]:
    """Birleştirilmiş İngilizce çeviri sözlüğünü döndürür."""
    return dict(_load_en_catalog())


@functools.cache
def _reverse_en_catalog() -> dict[str, str]:
    entries = _load_en_catalog()
    reverse: dict[str, str] = {}
    # Düz anahtarlar önce eklenir; bağlamsız ters arama genel anlamı tercih eder.
    for key, en_text in entries.items():
        if _CONTEXT_SEP not in key:
            reverse.setdefault(en_text, key)
    for key, en_text in entries.items():
        if _CONTEXT_SEP in key:
            _, tr_text = key.split(_CONTEXT_SEP, 1)
            reverse.setdefault(en_text, tr_text)
    return reverse


_reverse_context_cache: tuple[dict[str, str], dict[tuple[str, str], str]] | None = None


def _reverse_context_catalog() -> dict[tuple[str, str], str]:
    """(bağlam, İngilizce) -> Türkçe eşlemesi; ana katalog yenilenince kendini tazeler."""
    global _reverse_context_cache
    entries = _load_en_catalog()
    if _reverse_context_cache is not None and _reverse_context_cache[0] is entries:
        return _reverse_context_cache[1]
    reverse: dict[tuple[str, str], str] = {}
    for key, en_text in entries.items():
        if _CONTEXT_SEP in key:
            ctx, tr_text = key.split(_CONTEXT_SEP, 1)
            reverse.setdefault((ctx, en_text), tr_text)
    _reverse_context_cache = (entries, reverse)
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


_TemplateEntry = tuple[str | None, re.Pattern[str], re.Pattern[str], str, str, str]


@functools.cache
def _template_patterns() -> list[_TemplateEntry]:
    patterns: list[_TemplateEntry] = []
    for key, val in _load_en_catalog().items():
        if _CONTEXT_SEP in key:
            ctx: str | None
            raw_key: str
            ctx, raw_key = key.split(_CONTEXT_SEP, 1)
        else:
            ctx, raw_key = None, key
        if "{" not in raw_key:
            continue
        tr_comp = _compile_template(raw_key)
        en_comp = _compile_template(val)
        if tr_comp is None or en_comp is None:
            continue
        tr_re, tr_clean, tr_names = tr_comp
        en_re, en_clean, en_names = en_comp
        if tr_names != en_names:
            continue
        patterns.append((ctx, tr_re, en_re, raw_key, tr_clean, en_clean))
    return patterns


def _ordered_templates(context: str | None) -> list[_TemplateEntry]:
    all_patterns = _template_patterns()
    if context:
        return [p for p in all_patterns if p[0] == context] + [
            p for p in all_patterns if p[0] is None
        ]
    return [p for p in all_patterns if p[0] is None] + [
        p for p in all_patterns if p[0] is not None
    ]


def _resolve_templated_source(text: str, context: str | None = None) -> str | None:
    for _ctx, tr_re, en_re, _raw_key, tr_clean, _en_clean in _ordered_templates(context):
        if tr_re.fullmatch(text) is not None:
            return text
        match = en_re.fullmatch(text)
        if match is not None:
            try:
                return tr_clean.format(**match.groupdict())
            except (KeyError, ValueError, IndexError):
                continue
    return None


def _translate_templated_source(
    source: str, language: str, context: str | None = None
) -> str:
    if language != "en":
        return source
    for _ctx, tr_re, _en_re, _raw_key, _tr_clean, en_clean in _ordered_templates(context):
        match = tr_re.fullmatch(source)
        if match is None:
            continue
        try:
            return en_clean.format(**match.groupdict())
        except (KeyError, ValueError, IndexError):
            continue
    return source


def resolve_source(text: str, context: str | None = None) -> str | None:
    """Görünen bir arayüz metninin Türkçe kaynağını çözer; katalogda yoksa None döndürür."""
    entries = _load_en_catalog()
    if context and f"{context}{_CONTEXT_SEP}{text}" in entries:
        return text
    if _CONTEXT_SEP not in text and text in entries:
        return text
    if context:
        found_ctx = _reverse_context_catalog().get((context, text))
        if found_ctx is not None:
            return found_ctx
    found = _reverse_en_catalog().get(text)
    if found is not None:
        return found
    return _resolve_templated_source(text, context=context)


def translate_source(source: str, language: str, context: str | None = None) -> str:
    """Bilinen Türkçe kaynak metni hedef dile çevirir; Türkçe için kaynağı aynen korur."""
    if language != "en":
        return source
    entries = _load_en_catalog()
    if context:
        ctx_key = f"{context}{_CONTEXT_SEP}{source}"
        if ctx_key in entries:
            return entries[ctx_key]
    if _CONTEXT_SEP not in source and source in entries:
        return entries[source]
    return _translate_templated_source(source, language, context=context)


def retarget(text: str, language: str, context: str | None = None) -> str:
    """Görünen bir arayüz metnini kaynağını bularak hedef dile çevirir."""
    source = resolve_source(text, context=context)
    if source is None:
        return text
    return translate_source(source, language, context=context)


def t(text: str, context: str | None = None) -> str:
    """Türkçe kaynak metni etkin dile çevirir; eksik çeviri Türkçe kalır.

    Bağlam verilirse önce ``"<bağlam>::<metin>"`` anahtarı, yoksa düz anahtar aranır.
    """
    if _language != "en":
        return text
    entries = _load_en_catalog()
    if context:
        ctx_key = f"{context}{_CONTEXT_SEP}{text}"
        if ctx_key in entries:
            return entries[ctx_key]
    if _CONTEXT_SEP not in text:
        return entries.get(text, text)
    return text
