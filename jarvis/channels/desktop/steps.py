"""Denetim olaylarını kullanıcıya gösterilecek adım satırlarına çevirir (ADR 0014).

Bu modül Qt'ye bağımlı değildir: kurallar Linux'ta test edilebilir ve arayüz yalnız
hazır satırları çizer. Ham argüman ve sonuç verisi buraya girmez; ayrıntı kutusu o
alanları denetim olayından ayrı okur. Böylece bir argümanda geçen sır, görünür
satıra yanlışlıkla sızamaz.

Satır türleri ayrıdır çünkü "gönderildi" ile "tamamlandı" aynı şey değildir:
✓ yalnız sonuç doğrulanmış (status "ok" ve success True) olduğunda basılır. Bu
kural, başarı işaretinin koşulsuz üretilmesini engeller (kural 5).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

StepKind = Literal["ok", "sent", "fail", "denied", "info"]


@dataclass(frozen=True)
class StepLine:
    text: str
    kind: StepKind
    # True ise satır yalnız kullanıcı "Ayrıntılar"ı açtığında görünür; ana akışı
    # model ve güvenlik iç adımlarıyla doldurmamak için.
    internal: bool


# Tur sonu nedenleri kullanıcı dilinde açıklanır; kaynaktaki ham kodlar gösterilmez.
# Bilinmeyen veya eksik neden, sessizce "tamam" yerine açık bir uyarıya düşer.
_TURN_END_TEXT: dict[str, str] = {
    "cancelled": "İşlem iptal edildi",
    "timeout": "Süre sınırı doldu",
    "budget": "Bütçe sınırı",
    "max_steps": "Adım sınırı doldu",
    "cleanup_pending": "Önceki işlem temizleniyor",
    "denied_repeat": "Reddedilen işlem tekrar istendi; durduruldu",
    "repeated_call": "Aynı işlem tekrar istendi; durduruldu",
    "local_limit": "Yerel model sınırı",
    "provider_error": "Model yanıt veremedi",
}
_TURN_END_UNKNOWN = "Tur bitti (bilinmeyen neden)"

# İç olayların Türkçe karşılıkları. Burada olmayan olay adları, alt çizgisi boşluğa
# çevrilmiş ham adıyla iç satır olarak gösterilir; hiçbir olay sessizce düşmez.
_INTERNAL_TEXT: dict[str, str] = {
    "turn_start": "İstek alındı",
    "model_response": "Model yanıtı alındı",
    "privacy_decision": "Gizlilik kontrolü",
    "context_reset": "Yeni sohbet başladı",
}


def step_line(
    event: str,
    fields: Mapping[str, Any],
    labels: Mapping[str, tuple[str, str]],
) -> StepLine | None:
    """Bir denetim olayını adım satırına çevirir; gösterilecek bir şey yoksa None.

    ``labels`` araç adından ``(label, done_label)`` çiftine gider. Boş değer "bildirilmedi"
    anlamındadır; o durumda ham araç adı ve "... tamamlandı" metni kullanılır.
    """
    if not event:
        return None
    raw_tool = fields.get("tool")
    tool = raw_tool if isinstance(raw_tool, str) else ""
    label, done_label = labels.get(tool, ("", ""))
    display = label or tool or "Araç"
    done = done_label or f"{display} tamamlandı"

    if event == "tool_result":
        return _tool_result_line(fields, display, done)
    if event == "tool_interrupted":
        # Alt işlem kesildiğinde tamamlanıp tamamlanmadığı bilinmez; "başarısız" da
        # "yapılmadı" da denmez.
        return StepLine(f"{display}: kesildi, sonucu doğrulanamadı", "fail", False)
    if event == "turn_end":
        return _turn_end_line(fields)
    if event == "approval":
        approved = fields.get("approved")
        verdict = "verildi" if approved is True else "verilmedi"
        return StepLine(f"Onay {verdict}: {display}", "info", True)
    if event == "tool_requested":
        return StepLine(f"{display} istendi", "info", True)
    if event == "tool_dispatch":
        return StepLine(f"{display} başlatıldı", "info", True)
    text = _INTERNAL_TEXT.get(event, event.replace("_", " "))
    return StepLine(text, "info", True)


def _tool_result_line(fields: Mapping[str, Any], display: str, done: str) -> StepLine:
    result = fields.get("result")
    if not isinstance(result, Mapping):
        return StepLine(f"{display} başarısız ✗", "fail", False)
    status = result.get("status")
    # Yalnız iki koşul birlikte sağlanırsa ✓: doğrulanmış sonuç. Aksi halde, ör.
    # status "ok" ama success False gibi tutarsız bir kayıt, başarı diye sunulmaz.
    if status == "ok" and result.get("success") is True:
        return StepLine(f"{done} ✓", "ok", False)
    if status == "sent":
        # Komut gönderildi ama sonucu doğrulanamadı; tamamlandı denmez.
        return StepLine(f"{display}: komut gönderildi, sonucu doğrulanamadı", "sent", False)
    if status == "denied":
        return StepLine(f"{display}: onay verilmedi", "denied", False)
    return StepLine(f"{display} başarısız ✗", "fail", False)


def _turn_end_line(fields: Mapping[str, Any]) -> StepLine:
    reason = fields.get("stop_reason")
    if reason == "completed":
        return StepLine("Yanıt hazır", "info", True)
    text = _TURN_END_UNKNOWN
    if isinstance(reason, str):
        text = _TURN_END_TEXT.get(reason, _TURN_END_UNKNOWN)
    return StepLine(text, "fail", False)
