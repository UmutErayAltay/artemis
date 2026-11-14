"""ADR 0071: bitiş sessizliği sürerken nihai STT'yi önceden başlatır (spekülatif çözüm).

Sonuç yalnız bölüt kapanınca ve bölüt = anlık görüntü + sessizlik ise nihai transkript olur;
konuşma sürerse, onay/iptal/kapanışta atılır. Kendi başına hiçbir şey yayımlamaz.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _Transcriber(Protocol):
    async def transcribe(self, pcm: bytes) -> str: ...


@dataclass
class _Spec:
    task: asyncio.Task[str]
    pcm: bytes
    uid: int
    epoch: int


def _quiet(task: asyncio.Task[str]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.debug("Spekülatif STT başarısız: %s", type(task.exception()).__name__)


class SpeculativeStt:
    def __init__(self, recognizer: _Transcriber, after_frames: int) -> None:
        self._recognizer = recognizer
        self._after = after_frames
        self._current: _Spec | None = None  # yakalama sürerken (en çok bir)
        self._ready: dict[int, _Spec] = {}  # bölüt kuyruğa girdi, nihai çözümü bekliyor

    def feed(self, seg: Any, utterance: Any, uid: int | None, epoch: int) -> None:
        """Denetleyici döngüsü: segmenter.push sonrası her karede çağrılır."""
        if utterance is not None:
            self.seal(uid, utterance.pcm, epoch)
            return
        silence = seg.trailing_silence_frames
        self.step(silence, seg.voiced_snapshot() if silence else b"", uid, epoch, bool(seg._active))

    def step(
        self, silence: int, voiced: bytes, uid: int | None, epoch: int, active: bool
    ) -> None:
        """Her karede çağrılır: konuşma sürdü/bölüt bitti → at; yeterli sessizlik → başlat."""
        if self._current is not None and (silence == 0 or not active):
            self._drop(self._current)
            self._current = None
        if (
            self._current is None
            and active
            and uid is not None
            and voiced
            and silence >= self._after
        ):
            task = asyncio.create_task(self._recognizer.transcribe(voiced))
            task.add_done_callback(_quiet)
            self._current = _Spec(task, voiced, uid, epoch)

    def seal(self, uid: int | None, pcm: bytes, epoch: int) -> None:
        """Bölüt kapandı: anlık görüntü bölütün sesli önekiyse adayı çözüm kuyruğuna taşı."""
        spec, self._current = self._current, None
        if spec is None:
            return
        if spec.uid == uid and spec.epoch == epoch and pcm.startswith(spec.pcm):
            self._ready[spec.uid] = spec
        else:
            self._drop(spec)

    def forget(self, uid: int) -> None:
        spec = self._ready.pop(uid, None)
        if spec is not None:
            self._drop(spec)

    def cancel_all(self) -> None:
        if self._current is not None:
            self._drop(self._current)
            self._current = None
        for spec in self._ready.values():
            self._drop(spec)
        self._ready.clear()

    @staticmethod
    def _drop(spec: _Spec) -> None:
        spec.task.cancel()

    async def result(self, uid: int, epoch: int) -> str | None:
        """Kabul edilebilir spekülatif metin ya da None (normal çözüme düş)."""
        spec = self._ready.pop(uid, None)
        if spec is None:
            return None
        if spec.epoch != epoch:
            self._drop(spec)
            return None
        await asyncio.wait({spec.task})  # dış iptal burada yayılır; görev iptali/hatası None döner
        if spec.task.cancelled() or spec.task.exception() is not None:
            return None
        return spec.task.result()
