"""Uyandırma sözcüğü eşleştirme; VoiceController durumu taşımaz."""

from __future__ import annotations

import re

_FOLD = str.maketrans("ışçğöü", "iscgou")


def _fold(word: str) -> str:
    """Türkçe küçük harf + aksan katlama; sondaki z de s sayılır.

    Whisper "Artemis"i "Artemıs/Artemiş" ve en sık "Artemiz" yazıyor (sondaki s'nin
    yumuşaması); bunlar yazım farkı değil, aynı söz.
    """
    folded = word.replace("I", "ı").replace("İ", "i").lower().translate(_FOLD)
    return folded[:-1] + "s" if folded.endswith("z") else folded


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein; ``limit`` aşılınca erken döner (yalnız eşik karşılaştırması gerekir)."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_a != char_b),
                )
            )
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _substitutions(a: str, b: str, limit: int) -> int:
    """Aynı uzunluktaki iki sözcükte farklı harf sayısı; uzunluk farklıysa ``limit + 1``."""
    if len(a) != len(b):
        return limit + 1
    return sum(char_a != char_b for char_a, char_b in zip(a, b, strict=True))


_GHOST_PHRASES = {
    "abone olmayı unutmayın", "izlediğiniz için teşekkürler",
    "izlediğiniz için teşekkür ederim", "izlediğiniz için teşekkür ederiz",
    "bu videoyu izlediğiniz için teşekkür ederim",
    "bu videoyu izlediğiniz için teşekkür ederiz", "altyazı mk",
}


def is_ghost_transcript(text: str) -> bool:
    """Whisper'ın sessizlik/kısa seste uydurduğu altyazı kalıbı mı.

    Tanıyıcı yalnız tam metne bakar; canlıda "Artemis. Abone olmayı unutmayın." geçti ve
    kuyruk komut sanıldı (2026-10-26). Bu yüzden VoiceController uyandırma sonrası kuyruğa da
    uygular.
    """
    normalized = text.replace("I", "ı").replace("İ", "i").lower()
    normalized = " ".join("".join(
        char for char in normalized if char.isalnum() or char.isspace()
    ).split())
    return normalized in _GHOST_PHRASES


def find_wake_word(transcript: str, wake_word: str) -> tuple[int, int] | None:
    """Uyandırma sözcüğünün transcript'teki (başlangıç, bitiş) konumu; yoksa None.

    Tek sözcüklü uyandırmada yakın yazımlar da kabul edilir: uçtan uca denemede Whisper
    "Artemis"i çoğu kez "Artemiz", "Arteniz", "Ar temiz" yazdı ve komutlar kayboldu.
    Katlamadan sonra 5+ harfli sözcükte en çok bir harf **değişimi** kabul edilir (daha kısada
    tam eşleşme); boşlukla bölünmüş iki parça birleşik de denenir. İki harf farkı fazla
    gevşekti: "artırmış" da "artemis" sayılıyordu. Harf düşmesi/eklenmesi de kabul edilmez:
    "artmış" (fiyatlar artmış) gündelik sözcüktür ve uyandırıyordu (ADR 0045 ölçümü);
    Whisper'ın gerçekte yazdığı biçimlerin hepsi aynı uzunlukta değişimdir. Bu yalnız dinleme
    kapısıdır, onay değildir: her araç yine SafetyGate'ten geçer. Çok sözcüklü uyandırma
    ifadesi tam eşleşme ister.
    """
    wake = _fold(wake_word)
    if not wake.isalnum():
        match = re.search(
            rf"(?<!\w){re.escape(wake_word)}(?!\w)",
            transcript,
            flags=re.IGNORECASE | re.UNICODE,
        )
        return match.span() if match else None
    limit = 1 if len(wake) >= 5 else 0
    tokens = list(re.finditer(r"\w+", transcript))
    for index, token in enumerate(tokens):
        word = _fold(token.group())
        if _substitutions(word, wake, limit) <= limit:
            return token.span()
        if index + 1 < len(tokens) and transcript[token.end() : tokens[index + 1].start()] == " ":
            joined = word + _fold(tokens[index + 1].group())
            if len(word) >= 2 and _substitutions(joined, wake, limit) <= limit:
                return token.start(), tokens[index + 1].end()
    return None
