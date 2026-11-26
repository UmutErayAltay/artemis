"""Ses denetleyicisinin canlı yazı, uyandırma ön-kapısı ve ara STT adımları (core.py'den ayrıldı).

Kare başına çalışan bu adımlar `VoiceController` durumunu (tamponlar, görevler, epoch) kullanır;
karışım sınıfı olarak ayrıldı, davranış ve durum denetleyicide kalır.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from artemis.channels.voice.contracts import (
        AudioFrame,
        AudioSegment,
        AudioSegmenter,
        LiveTranscriber,
        SpeechRecognizer,
        WakeSpotterLike,
    )
    from artemis.config.settings import VoiceConfig

logger = logging.getLogger(__name__)

# ~100 ms'lik STT PCM'i birikince bir iş gönderilir; metin güncellemesi en çok ~150 ms'de bir.
LIVE_BATCH_MS = 100
LIVE_EMIT_INTERVAL = 0.15


class LiveStepsMixin:
    """`VoiceController`'ın canlı yazı/uyandırma/ara STT adımları; durum denetleyicide durur."""

    if TYPE_CHECKING:
        _live: LiveTranscriber | None
        _live_buffer: bytearray
        _live_task: asyncio.Task[str] | None
        _live_text: str
        _live_next_emit: float
        _live_stale: bool
        _wake_spotter: WakeSpotterLike | None
        _wake_buffer: bytearray
        _wake_task: asyncio.Task[bool] | None
        _wake_heard_at: float
        _capture_epoch: int
        _partial_task: asyncio.Task[str] | None
        _decode_busy: bool
        _segments: asyncio.Queue[tuple[AudioSegment, int, int, float]]
        segmenter: AudioSegmenter
        config: VoiceConfig
        recognizer: SpeechRecognizer
        clock: Callable[[], float]
        on_state: Callable[[str], None]
        on_live_text: Callable[[str], None]

    def _live_step(self, frame: AudioFrame, was_active: bool) -> None:
        """Konuşma süren PCM'i toplar; ~100 ms'de bir, tek uçuşta işle thread'e gönderir."""
        if self._live is None:
            return
        if not self.segmenter.active or self.segmenter.discarding:
            if was_active:
                self._live_reset()
            return
        if was_active:
            self._live_buffer += frame.raw_pcm or frame.pcm
        else:  # Konuşma başı: ön tampon + bu kare zaten segmentte.
            self._live_buffer[:] = self.segmenter.snapshot()
        batch = 2 * self.config.sample_rate * LIVE_BATCH_MS // 1000
        if self._live_task is None and len(self._live_buffer) >= batch:
            chunk = bytes(self._live_buffer)
            self._live_buffer.clear()
            self._live_task = asyncio.create_task(asyncio.to_thread(self._live.feed, chunk))
            self._live_task.add_done_callback(self._live_finished)

    def _wake_step(self, frame: AudioFrame) -> None:
        """Her kareyi (konuşma başlamadan da) ~100 ms'de bir, tek uçuşta KWS'ye besler."""
        if self._wake_spotter is None:
            return
        self._wake_buffer += frame.raw_pcm or frame.pcm
        batch = 2 * self.config.sample_rate * LIVE_BATCH_MS // 1000
        if self._wake_task is None and len(self._wake_buffer) >= batch:
            chunk = bytes(self._wake_buffer)
            self._wake_buffer.clear()
            task = asyncio.create_task(asyncio.to_thread(self._wake_spotter.feed, chunk))
            self._wake_task = task
            task.add_done_callback(
                partial(self._wake_finished, timestamp=frame.timestamp, epoch=self._capture_epoch)
            )

    def _wake_finished(self, task: asyncio.Task[bool], *, timestamp: float, epoch: int) -> None:
        if self._wake_task is task:
            self._wake_task = None
        if task.cancelled():
            return
        try:
            hit = task.result()
        except Exception as error:  # noqa: BLE001 - İş parçacığı görev sınırı: türü ne olursa olsun ön-kapı kapatılır, sessizce etkin kalmaz.
            if self._wake_spotter is not None:
                logger.warning(
                    "Uyandırma ön-kapısı durdu, transkript uyandırmasına dönüldü (%s).",
                    type(error).__name__,
                )
                # Anahtar sözcük geçici dosyası close() ile silinir; yalnız None'lamak sızdırırdı.
                with contextlib.suppress(OSError):
                    self._wake_spotter.close()
                self._wake_spotter = None
                self._wake_buffer.clear()
                self.on_state("wake:unavailable:runtime")
            return
        # Onay/iptal sonrası (epoch değişti) gelen eski parti uyandırma kanıtı sayılmaz.
        if hit and epoch == self._capture_epoch:
            self._wake_heard_at = timestamp

    def _live_reset(self) -> None:
        self._live_buffer.clear()
        self._live_text = ""
        if self._live_task is not None:
            # Thread sürerken reset yarışır; iş bitince _live_finished sıfırlar.
            self._live_stale = True
        elif self._live is not None:
            self._live.reset()

    def _live_finished(self, task: asyncio.Task[str]) -> None:
        if self._live_task is task:
            self._live_task = None
        if task.cancelled():
            return
        live = self._live
        try:
            text = task.result()
        except Exception as error:  # noqa: BLE001 - İş parçacığı görev sınırı: türü ne olursa olsun ön-kapı kapatılır, sessizce etkin kalmaz.
            if live is not None:
                logger.warning(
                    "Canlı yazı durdu, bu oturumda kapatıldı (%s).", type(error).__name__
                )
                with contextlib.suppress(OSError):
                    live.close()
                self._live = None
                self._live_buffer.clear()
                self.on_state("live:unavailable:runtime")
            return
        if live is None:
            return
        if self._live_stale:
            self._live_stale = False
            live.reset()
            return
        now = self.clock()
        if text and text != self._live_text and now >= self._live_next_emit:
            self._live_text = text
            self._live_next_emit = now + LIVE_EMIT_INTERVAL
            self.on_live_text(text)

    def _start_partial_decode(self) -> None:
        method = getattr(self.recognizer, "transcribe_partial", None)
        if (
            method is None
            or self._partial_task is not None
            or self._decode_busy
            or not self._segments.empty()
        ):
            return
        pcm = self.segmenter.snapshot()
        if pcm:
            # Bu çıktı hiçbir zaman wake word veya komut yürütmesinde kullanılmaz.
            self._partial_task = asyncio.create_task(method(pcm))
            self._partial_task.add_done_callback(self._partial_finished)

    def _partial_finished(self, task: asyncio.Task[str]) -> None:
        if self._partial_task is task:
            self._partial_task = None
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.debug("Ara STT penceresi başarısız (%s).", type(error).__name__)

    async def _settle_partial(self) -> None:
        task, self._partial_task = self._partial_task, None
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, OSError, RuntimeError, ValueError):
                logger.debug("Ara STT penceresi kullanılamadı.")
