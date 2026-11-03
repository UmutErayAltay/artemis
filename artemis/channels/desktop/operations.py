"""Mesaj başına işlem kartları (ADR 0014): denetim olaylarını gruplar ve özetler.

Qt'siz tutulur; arayüz yalnız hazır kartları çizer. Girdi, bridge'in zaten
`audit.redact`'tan geçmiş olaylarıdır; buradaki ek kural, yazılan `text` gibi içerik
alanlarının değerini ASLA göstermemek, yalnız uzunluğunu vermektir. Böylece ayrıntı
kutusu da yeni bir temizlenmemiş yol açmaz.

Durum rozeti dürüstlüktür: `status == "sent"` olan bir araç varsa tur "Tamam" değil
"Doğrulanamadı" okunur (kural 5).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

CARDS_KEPT = 20
EVENTS_PER_CARD = 200
_TITLE_LIMIT = 60
_VALUE_LIMIT = 80
_MESSAGE_LIMIT = 160

# Değeri değil yalnız uzunluğu gösterilen alanlar: yazılan metin parola olabilir.
_LENGTH_ONLY = frozenset({"text", "content", "value", "body", "message", "password", "secret"})
# Özette önce gelen, adı yazılmadan değeri gösterilen alanlar.
_PRIORITY = ("goal", "app", "app_name", "application", "name", "url", "query", "q", "title")
_PATH_KEYS = frozenset({"path", "file", "filename", "file_path"})
_ACTION_TEXT = {
    "click": "Tıklama",
    "type": "Yazma",
    "key": "Tuş",
    "scroll": "Kaydırma",
    "wait": "Bekleme",
    "done": "Bitiş",
    "ask_user": "Soru",
}

# Rozet metni -> tema renk anahtarı (themes.py token adları).
CHIP_TONES = {
    "Çalışıyor": "accent",
    "Tamam": "success",
    "Hata": "error",
    "İptal": "muted",
    "Doğrulanamadı": "warning",
}
# İşlem durumu -> tema renk anahtarı.
STATE_TONES = {
    "running": "muted", "ok": "success", "sent": "warning",
    "fail": "error", "denied": "error", "interrupted": "error",
}


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _arg_text(key: str, value: Any, limit: int = _VALUE_LIMIT) -> str | None:
    """Tek argümanın görünür değeri; içerik alanı yalnız uzunluk olarak döner."""
    if value is None or value == "" or isinstance(value, (dict, list, tuple)):
        return None
    if key in _LENGTH_ONLY:
        return f"{len(str(value))} karakter"
    text = str(value)
    if key == "url":
        parts = urlsplit(text)
        text = parts.hostname or text
    elif key in _PATH_KEYS:
        text = text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or text
    return truncate(text, limit)


def summarize_arguments(arguments: Any) -> str:
    """İnsan okur özet: öncelikli alanlar değerle, diğerleri `ad: değer` ile (en çok 3)."""
    if not isinstance(arguments, Mapping):
        return ""
    ordered = [k for k in _PRIORITY if k in arguments]
    ordered += [k for k in arguments if k not in _PRIORITY]
    parts: list[str] = []
    for key in ordered:
        shown = _arg_text(str(key), arguments[key])
        if shown is None:
            continue
        bare = key in _PRIORITY or key in _PATH_KEYS
        parts.append(shown if bare else f"{key}: {shown}")
        if len(parts) == 3:
            break
    return " · ".join(parts)


def _duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0:
        return ""
    return f"{round(seconds * 1000)} ms" if seconds < 1 else f"{seconds:.1f} sn".replace(".", ",")


@dataclass
class SubRow:
    text: str
    # "info" ya da doğrulama için "ok"/"sent".
    kind: str = "info"


@dataclass
class ToolOp:
    tool: str
    label: str
    summary: str = ""
    state: str = "running"  # running ok sent fail denied interrupted
    state_text: str = "çalışıyor…"
    message: str = ""
    duration: str = ""
    approval: str = ""
    subrows: list[SubRow] = field(default_factory=list)


@dataclass
class Card:
    request_id: str
    title: str
    started: str
    ended: bool = False
    stop_reason: str = ""
    failed: bool = False
    ops: list[ToolOp] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    # Arayüz tercihi kartla yaşar; kart yeniden çizilince kapanmasın.
    expanded: bool = False

    @property
    def chip(self) -> str:
        if not self.ended and not self.failed:
            return "Çalışıyor"
        if self.stop_reason == "cancelled":
            return "İptal"
        if self.failed or self.stop_reason not in ("completed", ""):
            return "Hata"
        states = {op.state for op in self.ops}
        if "sent" in states:
            return "Doğrulanamadı"
        if "fail" in states:
            return "Hata"
        return "Tamam"


class OperationLog:
    def __init__(self, clock: Callable[[], str] | None = None) -> None:
        self._clock = clock or (lambda: time.strftime("%H:%M"))
        self.cards: dict[str, Card] = {}
        self.dropped = 0

    def begin(self, request_id: str, text: str) -> Card:
        card = self.cards.get(request_id)
        if card is None:
            card = Card(request_id, "", self._clock())
            self.cards[request_id] = card
            while len(self.cards) > CARDS_KEPT:
                del self.cards[next(iter(self.cards))]
        if text:
            card.title = truncate(text, _TITLE_LIMIT)
        return card

    def fail(self, request_id: str) -> Card:
        """Tur olayı gelmeden biten istek (bridge Failed): rozet Hata olur."""
        card = self.begin(request_id, "")
        card.failed = True
        return card

    def clear(self) -> None:
        """Yeni sohbet: kartlar sıfırlanır. Denetim günlüğü başka yerde durur, buna dokunulmaz."""
        self.cards.clear()
        self.dropped = 0

    def newest_first(self) -> list[Card]:
        return list(reversed(self.cards.values()))

    def feed(
        self,
        request_id: str | None,
        event: str,
        fields: Mapping[str, Any],
        labels: Mapping[str, tuple[str, str]],
    ) -> Card | None:
        """Bir denetim olayını ilgili karta işler; kartı döndürür, kapsam dışıysa None."""
        if request_id is None:
            return None
        card = self.begin(request_id, "")
        if not card.title:
            card.title = "İstek"
        if len(card.details) < EVENTS_PER_CARD:
            card.details.append(_detail_line(event, fields))
        raw = fields.get("tool")
        tool = raw if isinstance(raw, str) else ""
        if event == "turn_end":
            card.ended = True
            reason = fields.get("stop_reason")
            card.stop_reason = reason if isinstance(reason, str) else "unknown"
            for op in card.ops:
                if op.state == "running":
                    # Sonuçsuz biten tur "çalışıyor" kalamaz; tamamlandığı da iddia edilmez.
                    op.state, op.state_text = "interrupted", "sonucu doğrulanamadı"
        elif event == "tool_requested":
            summary = summarize_arguments(fields.get("arguments"))
            card.ops.append(ToolOp(tool, _label(tool, labels), summary))
        elif event in ("tool_dispatch", "approval", "approval_skipped"):
            self._on_tool_step(card, event, tool, fields, labels)
        elif event == "tool_result":
            self._on_result(card, tool, fields, labels)
        elif event == "tool_interrupted":
            op = self._open_op(card, tool, labels)
            op.state, op.state_text = "interrupted", "kesildi, sonucu doğrulanamadı"
        elif event == "computer_step":
            self._on_computer_step(card, fields)
        elif event == "computer_verify":
            self._on_computer_verify(card, fields)
        return card

    # -- yardımcılar ---------------------------------------------------------------

    @staticmethod
    def _open_op(card: Card, tool: str, labels: Mapping[str, tuple[str, str]]) -> ToolOp:
        """Aynı araçtan sonucu gelmemiş ilk işlem; yoksa (ör. gözetimsiz ret) yenisi."""
        for op in card.ops:
            if op.tool == tool and op.state == "running":
                return op
        op = ToolOp(tool, _label(tool, labels))
        card.ops.append(op)
        return op

    def _on_tool_step(
        self, card: Card, event: str, tool: str, fields: Mapping[str, Any],
        labels: Mapping[str, tuple[str, str]],
    ) -> None:
        op = self._open_op(card, tool, labels)
        if not op.summary:
            op.summary = summarize_arguments(fields.get("arguments"))
        if event == "approval_skipped":
            op.approval = "Otomatik onay"
        elif event == "approval":
            if fields.get("unattended") is True:
                op.approval = "Gözetimsiz tur: otomatik reddedildi"
            else:
                granted = fields.get("approved") is True
                op.approval = "Onay verildi" if granted else "Onay reddedildi"

    def _on_result(
        self, card: Card, tool: str, fields: Mapping[str, Any],
        labels: Mapping[str, tuple[str, str]],
    ) -> None:
        op = self._open_op(card, tool, labels)
        op.duration = _duration(fields.get("elapsed_seconds"))
        result = fields.get("result")
        raw_message = result.get("message") if isinstance(result, Mapping) else None
        op.message = truncate(raw_message, _MESSAGE_LIMIT) if isinstance(raw_message, str) else ""
        status = result.get("status") if isinstance(result, Mapping) else None
        # ✓ yalnız status "ok" ve success True (steps.py sözleşmesi).
        if isinstance(result, Mapping) and status == "ok" and result.get("success") is True:
            op.state, op.state_text = "ok", "✓ tamamlandı"
        elif status == "sent":
            op.state, op.state_text = "sent", "komut gönderildi, doğrulanamadı"
        elif status == "denied":
            op.state, op.state_text = "denied", "reddedildi"
        else:
            op.state, op.state_text = "fail", "başarısız ✗"

    @staticmethod
    def _computer_op(card: Card) -> ToolOp | None:
        for op in reversed(card.ops):
            if op.tool == "computer_use":
                return op
        return None

    def _on_computer_step(self, card: Card, fields: Mapping[str, Any]) -> None:
        op = self._computer_op(card)
        if op is None:
            return
        action = fields.get("action")
        name = _ACTION_TEXT.get(str(action), str(action or "Adım"))
        head = f"Adım {fields.get('step', '?')} · {name}"
        if action == "type" and isinstance(fields.get("text_length"), int):
            head += f" ({fields['text_length']} karakter)"
        reason = fields.get("reason")
        text = head
        if isinstance(reason, str) and reason:
            text += f" — {truncate(reason, _VALUE_LIMIT)}"
        if fields.get("sensitive") is True:
            text += " · hassas"
        if fields.get("auto_approved") is True:
            text += " · otomatik onay"
        op.subrows.append(SubRow(text))
        # Sonuç ayrı satır: arayüz "info" türünü soluk çizer. `observed` modelin ekrandan
        # türettiği serbest metindir; yazılan `text` hiçbir yoldan buraya gelmez.
        observed = fields.get("observed")
        if isinstance(observed, str) and observed:
            op.subrows.append(SubRow(f"↳ sonuç: {truncate(observed, 120)}"))

    def _on_computer_verify(self, card: Card, fields: Mapping[str, Any]) -> None:
        op = self._computer_op(card)
        if op is None:
            return
        evidence = fields.get("evidence")
        tail = ""
        if isinstance(evidence, str) and evidence:
            tail = " " + truncate(evidence, _VALUE_LIMIT)
        if fields.get("verified") is True:
            op.subrows.append(SubRow("Ekranda doğrulandı:" + tail, "ok"))
        else:
            op.subrows.append(SubRow("Ekranda doğrulanamadı:" + tail, "sent"))


def _label(tool: str, labels: Mapping[str, tuple[str, str]]) -> str:
    return labels.get(tool, ("", ""))[0] or tool or "Araç"


def _detail_line(event: str, fields: Mapping[str, Any]) -> str:
    """Ayrıntı satırı: olay, araç, kısaltılmış argüman anahtarları, durum, mesaj."""
    parts = [event]
    if isinstance(fields.get("tool"), str):
        parts.append(f"araç={fields['tool']}")
    arguments = fields.get("arguments")
    if isinstance(arguments, Mapping):
        shown = []
        for key, value in arguments.items():
            text = _arg_text(str(key), value, 40)
            shown.append(f"{key}={text}" if text is not None else str(key))
        parts.append("argümanlar: " + ", ".join(shown))
    result = fields.get("result")
    if isinstance(result, Mapping):
        if "status" in result:
            parts.append(f"durum={result['status']}")
        if isinstance(result.get("message"), str):
            parts.append(f"mesaj={truncate(result['message'], _MESSAGE_LIMIT)}")
    for key in ("stop_reason", "approved", "step", "action", "verified"):
        if key in fields:
            parts.append(f"{key}={fields[key]}")
    if event == "computer_step" and isinstance(fields.get("text_length"), int):
        parts.append(f"text_length={fields['text_length']}")
    return " · ".join(parts)

