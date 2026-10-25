"""Akan model metnini seslendirilecek cümlelere çevirir (ADR 0043).

Kurallar: araç çağrısı başlamış bir adımın metni bundan sonra seslendirilmez; geçmiş
zamanlı başarı iddiası taşıyan cümle (ve ardından gelenler, sıra korunsun diye) adım
bitene dek bekletilir ve adım araç çağrısıyla biterse atılır: eylem henüz yapılmadı.
Seslendirilen metin geri alınamaz; bu yüzden şüpheli olan beklemeye alınır.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

# Cümle sonu: noktalama + boşluk (3.5 gibi sayıları bölmez) ya da satır sonu.
_SENTENCE_END = re.compile(r"[.!?…:;]+[\"'”’)\]]*\s+|\n+")
# Uzun ve noktalamasız ilk cümle ilk sesi geciktirmesin: bu uzunlukta virgülde bölünür.
_SOFT_LIMIT = 160
_SOFT_BREAK = re.compile(r",\s+")
# Türkçe geçmiş zaman: 1. tekil/çoğul (kapattım, yaptık) ve edilgen/oldu (silindi,
# gönderildi, tamamlandı, oldu); ayrıca sık İngilizce karşılıklar.
_CLAIM_WORD = re.compile(r"(?:[dt][ıiuü][mk]|[lnt][dt][ıiuü])$")
_CLAIM_EN = re.compile(
    r"\b(?:done|completed|finished|closed|opened|deleted|sent|saved|turned (?:on|off))\b"
)
_WORD = re.compile(r"\w+")
# Türkçede yüklem yan cümlenin sonundadır: yalnız son sözcüğe bakmak "anlatım", "yardım",
# "adım" gibi -dım/-tım ile biten isimleri iddia sanmayı önler.
_CLAUSE = re.compile(r"[,;:]|\s+(?:ve|ama|sonra|ardından)\s+")


def _lower(text: str) -> str:
    return text.replace("I", "ı").replace("İ", "i").lower()


def claims_success(sentence: str) -> bool:
    """Seslendirilmeden önce eylem sonucunu bekletecek kadar şüpheli mi?"""
    lowered = _lower(sentence)
    if _CLAIM_EN.search(lowered):
        return True
    for clause in _CLAUSE.split(lowered):
        words = _WORD.findall(clause)
        if words and len(words[-1]) >= 4 and _CLAIM_WORD.search(words[-1]):
            return True
    return False


class SpeechStream:
    """``ResponseStream``: AgentLoop'tan senkron parça alır, TTS'ye cümle verir."""

    def __init__(self, max_chars: int = 4000) -> None:
        self.max_chars = max_chars
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._buffer = ""
        self._held: list[str] = []
        self._step_text: list[str] = []
        self._step_tool = False
        self._finished_step_text: str | None = None
        self._queued_chars = 0
        self._closed = False
        self.truncated = False
        self.spoken: list[str] = []

    # --- ResponseStream ---
    def step_start(self) -> None:
        self._buffer = ""
        self._held = []
        self._step_text = []
        self._step_tool = False

    def text(self, delta: str) -> None:
        self._step_text.append(delta)
        if self._closed or self._step_tool:
            return
        self._buffer += delta
        for sentence in self._take_sentences():
            self._offer(sentence)

    def tool_call(self) -> None:
        # Adımın kalanı ve bekletilen iddialar atılır; araç sonucu henüz yok.
        self._step_tool = True
        self._buffer = ""
        self._held = []

    def step_end(self, has_tool_calls: bool) -> None:
        if has_tool_calls:
            self.tool_call()
        elif not self._step_tool and not self._closed:
            if self._buffer.strip():
                self._offer(self._buffer)
            self._buffer = ""
            held, self._held = self._held, []
            for sentence in held:
                self._emit(sentence)
        self._finished_step_text = None if has_tool_calls else "".join(self._step_text)

    # --- tur sonu ---
    def finish(self, final_text: str) -> None:
        """Akmayan kısmı ekler: akış hiç yoksa ya da tur başka bir mesajla bittiyse tümü.

        Örn. zaman aşımı, iptal veya adım sınırı mesajı AgentLoop'tan gelir; son adımın
        akan metniyle başlamaz ve olduğu gibi söylenir.
        """
        if not self._closed:
            # Boşluk farkı (sağlayıcının blokları satır sonuyla birleştirmesi) akan yanıtı
            # ikinci kez söyletmesin: karşılaştırma boşluk normalize edilerek yapılır.
            streamed = " ".join((self._finished_step_text or "").split())
            final = " ".join(final_text.split())
            if not streamed:
                remainder = final
            elif final.startswith(streamed):
                remainder = final[len(streamed):]
            else:
                remainder = final
            if remainder.strip():
                self._emit(remainder)
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(None)

    @property
    def streamed(self) -> bool:
        return self._finished_step_text is not None and bool(self._finished_step_text.strip())

    async def sentences(self) -> AsyncIterator[str]:
        while (sentence := await self._queue.get()) is not None:
            yield sentence

    # --- iç ---
    def _take_sentences(self) -> list[str]:
        result: list[str] = []
        while True:
            match = _SENTENCE_END.search(self._buffer)
            if match is None:
                break
            result.append(self._buffer[:match.end()])
            self._buffer = self._buffer[match.end():]
        if len(self._buffer) >= _SOFT_LIMIT:
            breaks = list(_SOFT_BREAK.finditer(self._buffer))
            if breaks:
                cut = breaks[-1].end()
                result.append(self._buffer[:cut])
                self._buffer = self._buffer[cut:]
        return result

    def _offer(self, sentence: str) -> None:
        if not sentence.strip():
            return
        if self._held or claims_success(sentence):
            self._held.append(sentence)
        else:
            self._emit(sentence)

    def _emit(self, sentence: str) -> None:
        text = sentence.strip()
        if not text or self._closed:
            return
        if self._queued_chars + len(text) > self.max_chars:
            self.truncated = True
            return
        self._queued_chars += len(text)
        self.spoken.append(text)
        self._queue.put_nowait(text)
