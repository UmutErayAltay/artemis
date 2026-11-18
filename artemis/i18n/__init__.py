"""Basit yerelleştirme katmanı: kaynak metin anahtardır, eksik çeviri Türkçe kalır.

Dil açılışta bir kez ayarlanır; değişiklik Artemis yeniden başlatılınca geçerli olur.
"""

from __future__ import annotations

import functools
import json
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


def t(text: str) -> str:
    """Türkçe kaynak metni etkin dile çevirir; eksik çeviri Türkçe kalır."""
    if _language != "en":
        return text
    return _load_en_catalog().get(text, text)
