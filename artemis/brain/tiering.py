"""Model kademesi: turu ana modelle mi, zor işler modeliyle mi çalıştıracağını seçer.

Seçim tur başında, kurallarla yapılır (ek model çağrısı yok) ve tur boyunca değişmez: tur
ortasında model değiştirmek önbelleği bozar ve tutarsız yanıt üretir. Kurallar model testinin
(bench/model_testi.py, 2026-11-07) gösterdiği farka göre seçildi: Haiku temel işlerde Sonnet
kadar iyi, çok adımlı işlerde (eksik klasörü açıp sürdürme, birden çok eylem) geride.
Güvenlik kapısı modelden bağımsızdır; burası yalnız hangi modelin cevap yazacağını seçer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from artemis.brain.models import Message
from artemis.config.settings import Settings

# Kullanıcının açıkça daha dikkatli düşünme istemesi.
_DEEP = re.compile(
    r"\b(derin(lemesine)? düşün|iyice düşün|dikkatli(ce)? düşün|detaylı (analiz|incele)|"
    r"adım adım (düşün|planla))",
    re.IGNORECASE,
)
# Eylem fiilleri; iki farklı eylem aynı istekte geçerse iş çok adımlıdır.
# ponytail: kök eşleşmesi kaba (ör. "ara" "arasında"yı da tutar); yanlış pozitif yalnız turu
# daha güçlü modele gönderir. Ölçüm kötüleşirse sözcük sınırları sıkılaştırılır.
_ACTIONS = re.compile(
    r"\b(taşı|oluştur|yaz|sil|kopyala|topla|ayır|düzenle|ekle|bul|değiştir|"
    r"yeniden adlandır|adlandır|özetle|karşılaştır|planla|kaydet|güncelle|doğrula)",
    re.IGNORECASE,
)
# Kod ajanı taslağı: proje/görev/kapsam netleştirmesi planlama işidir.
_CODE_JOB = re.compile(r"\b(iş yaptır|kod ajan|taslak hazırla)", re.IGNORECASE)

REASONS = {
    "deep": "derin düşünme istendi",
    "multi_step": "çok adımlı istek",
    "code_job": "kod işi taslağı",
}


def _in_code_draft(past: Sequence[Message]) -> bool:
    """Son turda kod ajanı aracı çağrıldıysa taslak konuşması sürüyordur."""
    for message in reversed(past):
        if message.role == "user":
            return False
        if any(call.name.startswith("code_agent_") for call in message.tool_calls):
            return True
    return False


def choose_model(
    text: str, past: Sequence[Message], settings: Settings
) -> tuple[str | None, str]:
    """(model anahtarı, neden kodu); zor işler modeli yoksa ya da gerekmiyorsa (None, "")."""
    hard = settings.hard_model
    if not hard or hard == settings.primary_model or hard not in settings.models:
        return None, ""
    if _DEEP.search(text):
        return hard, "deep"
    if _CODE_JOB.search(text) or _in_code_draft(past):
        return hard, "code_job"
    if len({match.group(1).casefold() for match in _ACTIONS.finditer(text)}) >= 2:
        return hard, "multi_step"
    return None, ""
