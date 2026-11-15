"""Bulut TTS duygu etiketleri: etkinleştirme denetimi ve metinden temizleme."""

from __future__ import annotations

import re
from collections.abc import Iterable

from artemis.config.settings import VoiceConfig


def emotion_tags_active(voice: VoiceConfig) -> tuple[str, ...]:
    """Yalnız bulut TTS seçili ve etiket desteği açıkken yapılandırılan etiketleri döner."""
    if voice.tts_backend == "cloud" and voice.tts_cloud_emotion_tags:
        return voice.tts_cloud_emotion_tag_list
    return ()


def strip_emotion_tags(text: str, tags: Iterable[str]) -> str:
    """Yalnız verilen duygu etiketlerini (büyük/küçük harf duyarsız) metinden kaldırır.

    Diğer köşeli parantezlere (`[1]`, `[link]`) dokunmaz; kaldırma sonrası oluşan fazla
    boşlukları ve noktalama öncesi boşlukları temizler.
    """
    tag_list = tuple(tag for tag in tags if tag)
    if not tag_list:
        return text
    pattern = re.compile("|".join(re.escape(tag) for tag in tag_list), re.IGNORECASE)
    if pattern.search(text) is None:
        return text
    cleaned = pattern.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([.!?,;:…])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    return cleaned.strip()
