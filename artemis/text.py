"""Türkçe metin katlama; tek kaynak."""

from __future__ import annotations

_SEARCH = str.maketrans("İIı", "iii")


def turkish_fold(text: str) -> str:
    """Türkçe büyük/küçük harf eşlemesiyle casefold uygular.

    `casefold()` tek başına "I" harfini "i"ye çevirir ve "İ"yi "i̇" (noktalı birleşik
    karakter) yapar; Türkçede ise "I"→"ı", "İ"→"i" olmalıdır. Önce bu iki harf elle
    eşlenir, yoksa "ISPARTA" ile "ısparta" hiç eşleşmez.
    """
    return text.replace("I", "ı").replace("İ", "i").casefold()


def search_fold(text: str) -> str:
    """Arama için: büyük/küçük harf ve i/ı/İ/I ayrımı yok sayılır ("IŞIK" ~ "ışık" ~ "isik")."""
    return text.translate(_SEARCH).casefold()
