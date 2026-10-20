"""Yankı iptali için referans tamponu, AEC işleyicisi ve mikrofon sarmalayıcısı."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any, Protocol, cast

from jarvis.channels.voice.core import AudioFrame, Microphone

logger = logging.getLogger(__name__)

_ECHO_ERRORS: tuple[type[Exception], ...] = (RuntimeError, ValueError, OSError)
# Saat ancak bu kadar kopunca yeniden hizalanır. Geri çağrı ve yazma saatleri birkaç on ms
# titrer; AEC3 kesintisiz akış ister ve böyle küçük, sabit kaymaları kendi gecikme
# kestiricisiyle izler. Her karede saate atlamak süzgeci bozuyordu (uçtan uca ~1 dB).
_RESYNC_MS = 100


def _load_livekit_rtc() -> Any:
    """LiveKit RTC modülünü yalnızca kullanılırken yükler."""
    try:
        from livekit import rtc
    except ImportError as error:
        raise RuntimeError(
            "Yankı işleme için livekit yüklü değil; "
            '`pip install -e ".[voice]"` komutunu çalıştırın.'
        ) from error
    return rtc


class Resampler(Protocol):
    """Mono int16 örnekleri başka bir örnekleme hızına çevirir."""

    def process(self, pcm: bytes) -> bytes:
        ...


class LivekitResampler:
    """LiveKit örnekleme çeviricisini streaming biçimde sarar."""

    def __init__(self, input_rate: int, output_rate: int) -> None:
        if input_rate <= 0 or output_rate <= 0:
            raise ValueError("Örnekleme hızları pozitif olmalıdır.")

        # LiveKit yalnızca gerçekten çevirici gerektiğinde yüklenir.
        rtc = _load_livekit_rtc()

        self._rtc: Any = rtc
        self._input_rate = input_rate
        self._resampler: Any = rtc.AudioResampler(
            input_rate,
            output_rate,
            num_channels=1,
            quality=rtc.AudioResamplerQuality.MEDIUM,
        )

    def process(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise ValueError("PCM verisi 16 bitlik tam örneklerden oluşmalıdır.")

        frame = self._rtc.AudioFrame(
            pcm,
            self._input_rate,
            1,
            len(pcm) // 2,
        )
        frames = self._resampler.push(frame)
        return b"".join(bytes(item.data) for item in frames)


class EchoReference:
    """Konuşmacı çıkışını hedef hızda mono int16 olarak, çalındığı zamanla birlikte saklar.

    Tampon bir zaman çizelgesidir: ilk örneğin saati ``_start`` tutulur. Mikrofon karesi kendi
    yakalama saatiyle (``pull(..., at=...)``) aynı anın referansını ister. Sıra ile eşlemek
    yetmiyordu: STT döngüyü tutarken mikrofon kuyruğundan kare düşünce referans akmaya devam
    ediyor, iki akış saniyelerce kayıyor ve uçtan uca denemede yankı azaltma ~0,4 dB'e iniyordu.
    """

    def __init__(
        self,
        target_rate: int,
        *,
        max_ms: int = 2000,
        resampler_factory: Callable[[int, int], Resampler] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if target_rate <= 0:
            raise ValueError("Hedef örnekleme hızı pozitif olmalıdır.")
        if max_ms < 0:
            raise ValueError("Azami tampon süresi negatif olamaz.")

        def default_factory(input_rate: int, output_rate: int) -> Resampler:
            return LivekitResampler(input_rate, output_rate)

        self._target_rate = target_rate
        self._max_ms = max_ms
        self._max_samples = target_rate * max_ms // 1000
        self._resampler_factory: Callable[[int, int], Resampler] = (
            resampler_factory if resampler_factory is not None else default_factory
        )
        self._buffer = bytearray()
        # Tampondaki ilk örneğin çalınma saati; boş tamponda anlamsızdır.
        self._start = 0.0
        self._clock = clock
        self._resamplers: dict[int, Resampler] = {}
        self._lock = threading.Lock()

    def feed(self, pcm: bytes, sample_rate: int, channels: int, sample_width: int) -> None:
        """Yeni konuşma örneğini ekler ve eski örnekleri sınırın dışında bırakır."""
        if sample_rate <= 0:
            raise ValueError("Örnekleme hızı pozitif olmalıdır.")
        if channels <= 0:
            raise ValueError("Kanal sayısı pozitif olmalıdır.")
        if sample_width not in (1, 2, 4):
            raise ValueError("Örnek genişliği yalnızca 1, 2 veya 4 bayt olabilir.")
        if len(pcm) % (sample_width * channels):
            raise ValueError("PCM verisi kanal sayısıyla uyumlu tam örnekler içermelidir.")

        try:
            mono = self._to_mono(pcm, channels, sample_width)

            with self._lock:
                if sample_rate == self._target_rate:
                    converted = mono
                else:
                    resampler = self._resamplers.get(sample_rate)
                    if resampler is None:
                        resampler = self._resampler_factory(sample_rate, self._target_rate)
                        self._resamplers[sample_rate] = resampler

                    converted = resampler.process(mono)
                    if len(converted) % 2:
                        raise ValueError("Örnekleyici 16 bitlik tam örnekler döndürmelidir.")

                now = self._clock()
                if not self._buffer:
                    self._start = now
                else:
                    # Çalma arasında boşluk olduysa (cevaplar arası) zaman çizelgesi
                    # sessizlikle doldurulur; ardışık bloklar ise bitişik kalır.
                    end = self._start + len(self._buffer) // 2 / self._target_rate
                    gap = int((now - end) * self._target_rate)
                    if gap > self._target_rate * _RESYNC_MS // 1000:
                        self._buffer.extend(bytes(min(gap, self._max_samples) * 2))
                self._buffer.extend(converted)
                max_bytes = self._max_samples * 2
                overflow = len(self._buffer) - max_bytes
                if overflow > 0:
                    overflow -= overflow % 2
                    del self._buffer[:overflow]
                    self._start += overflow // 2 / self._target_rate
        except ImportError as error:
            raise RuntimeError(
                "Yankı referansı dönüştürülemedi; numpy veya livekit yüklü değil; "
                '`pip install -e ".[voice]"` komutunu çalıştırın.'
            ) from error

    def retarget(self, target_rate: int) -> None:
        """Hedef hızı değiştirir; eski örnekleri ve çevirici önbelleğini temizler."""
        if target_rate <= 0:
            raise ValueError("Hedef örnekleme hızı pozitif olmalıdır.")

        with self._lock:
            if target_rate == self._target_rate:
                return
            self._target_rate = target_rate
            self._max_samples = target_rate * self._max_ms // 1000
            self._buffer.clear()
            self._resamplers.clear()

    @staticmethod
    def _to_mono(pcm: bytes, channels: int, sample_width: int) -> bytes:
        """Farklı genişlikleri int16'ya çevirir ve kanalları ortalar."""
        # NumPy içe aktarma sırasında değil, ses işlenirken yüklenir.
        import numpy as np

        if sample_width == 1:
            # int8 örnekleri int16 tam genlik ölçeğine 256 ile taşınır.
            samples = np.frombuffer(pcm, dtype="<i1").astype(np.int32) << 8
        elif sample_width == 2:
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
        else:
            samples = np.frombuffer(pcm, dtype="<i4").astype(np.int64) >> 16

        if samples.size == 0:
            return b""

        mono = samples.reshape((-1, channels)).mean(axis=1)
        mono = np.clip(mono, -32768, 32767)
        return mono.astype("<i2").tobytes()

    def pull(self, samples: int, at: float | None = None) -> bytes:
        """İstenen örnekleri tüketir; eksik kısmı sessizlikle doldurur.

        ``at`` verilirse o andan başlayan örnekler döner (daha eskiler atılır, henüz
        çalınmamış kısım sessizliktir); verilmezse tampon sırayla tüketilir.
        """
        if samples < 0:
            raise ValueError("Örnek sayısı negatif olamaz.")

        with self._lock:
            lead = 0
            if at is not None and self._buffer:
                offset = round((at - self._start) * self._target_rate)
                if offset > 0:
                    drop = min(offset, len(self._buffer) // 2)
                    del self._buffer[: drop * 2]
                    self._start += drop / self._target_rate
                    if not self._buffer:
                        # Hepsi geçmişte kaldı; sıradaki besleme yeni çizelge başlatır.
                        lead = samples
                else:
                    # İstenen an tampondan önce: başı sessizlik, sonra tampon.
                    lead = min(samples, -offset)
            elif at is not None:
                lead = samples
            available = min(samples - lead, len(self._buffer) // 2)
            result = bytes(lead * 2) + bytes(self._buffer[: available * 2])
            del self._buffer[: available * 2]
            self._start += available / self._target_rate
            available += lead

        if available < samples:
            result += bytes((samples - available) * 2)
        return result

    def clear(self) -> None:
        """Sessiz örnekleri ve çeviricinin durumunu birlikte siler."""
        with self._lock:
            self._buffer.clear()
            self._resamplers.clear()


class Apm(Protocol):
    """EchoCanceller'ın ihtiyaç duyduğu AEC işlemci arayüzü."""

    def process_stream(self, data: Any) -> None:
        ...

    def process_reverse_stream(self, data: Any) -> None:
        ...

    def set_stream_delay_ms(self, delay_ms: int) -> None:
        ...


class EchoCanceller:
    """Yankı referansını 10 ms'lik AEC karelerinde işler."""

    def __init__(
        self,
        rate: int,
        reference: EchoReference,
        *,
        delay_ms: int = 50,
        apm_factory: Callable[[], Apm] | None = None,
        frame_factory: Callable[[bytes, int, int], Any] | None = None,
    ) -> None:
        if rate not in (16000, 48000):
            raise ValueError("Yankı iptali yalnızca 16000 veya 48000 Hz hızı destekler.")
        if delay_ms < 0:
            raise ValueError("Gecikme negatif olamaz.")

        self._rate = rate
        self._reference = reference
        self._delay_ms = delay_ms
        self._samples_per_frame = rate // 100
        self._frame_bytes = self._samples_per_frame * 2
        self._apm_factory: Callable[[], Apm]
        self._frame_factory: Callable[[bytes, int, int], Any]

        livekit_rtc: Any = None
        if apm_factory is None or frame_factory is None:
            livekit_rtc = _load_livekit_rtc()

        if apm_factory is None:

            def default_apm_factory() -> Apm:
                return cast(
                    Apm,
                    livekit_rtc.AudioProcessingModule(
                        echo_cancellation=True,
                        high_pass_filter=True,
                    ),
                )

            self._apm_factory = default_apm_factory
        else:
            self._apm_factory = apm_factory

        if frame_factory is None:

            def default_frame_factory(data: bytes, rate: int, samples: int) -> Any:
                return livekit_rtc.AudioFrame(data, rate, 1, samples)

            self._frame_factory = default_frame_factory
        else:
            self._frame_factory = frame_factory
        self._lock = threading.Lock()
        self._next_time: float | None = None

        self._apm = self._apm_factory()

    def process(self, pcm: bytes, start_time: float | None = None) -> bytes:
        """Mikrofon örneğini yankı iptalinden geçirir.

        ``start_time`` karenin ilk örneğinin yakalanma saatidir; verilirse referans o ana
        göre hizalanır (bkz. ``EchoReference``).
        """
        if len(pcm) % self._frame_bytes:
            raise ValueError("PCM verisi 10 ms'lik karelere bölünmelidir.")

        with self._lock:
            if start_time is not None:
                expected = self._next_time
                if expected is not None and abs(start_time - expected) * 1000 < _RESYNC_MS:
                    start_time = expected
                self._next_time = start_time + len(pcm) // 2 / self._rate
            output = bytearray()
            for offset in range(0, len(pcm), self._frame_bytes):
                microphone_frame = pcm[offset : offset + self._frame_bytes]
                at = (
                    None
                    if start_time is None
                    else start_time + offset // 2 / self._rate
                )
                reference = self._reference.pull(self._samples_per_frame, at)

                reverse_frame = self._frame_factory(
                    reference,
                    self._rate,
                    self._samples_per_frame,
                )
                self._apm.process_reverse_stream(reverse_frame)

                # Gecikme ters akıştan sonra, normal akıştan önce verilmelidir.
                self._apm.set_stream_delay_ms(self._delay_ms)
                stream_frame = self._frame_factory(
                    microphone_frame,
                    self._rate,
                    self._samples_per_frame,
                )
                self._apm.process_stream(stream_frame)
                output.extend(bytes(stream_frame.data))

        return bytes(output)

    def reset(self) -> None:
        """Yalnızca referansı temizler; uyarlanabilir süzgecin durumunu korur."""
        with self._lock:
            # APM her sıfırlamada yeniden kurulursa uyarlanabilir süzgeç yakınsamasını
            # kaybediyor; ölçümde yankı azaltma 19,5 dB'den 6,9 dB'ye düşüyor ve
            # yaklaşık 0,5 s boyunca düşük kalıyordu.
            self._reference.clear()
            self._next_time = None


class FrameEchoCanceller(Protocol):
    """Mikrofon sarmalayıcısının kare iptalcisi arayüzü."""

    def process(self, pcm: bytes, start_time: float | None = None) -> bytes:
        ...

    def reset(self) -> None:
        ...


class EchoCancelledMicrophone:
    """Mikrofon protokolünü yankı iptali ekleyerek uygular."""

    def __init__(
        self,
        inner: Microphone,
        canceller_factory: Callable[[int], FrameEchoCanceller],
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        self._inner = inner
        self._canceller_factory = canceller_factory
        self._canceller: FrameEchoCanceller | None = None
        self._canceller_rate: int | None = None
        self._on_state = on_state if on_state is not None else _ignore_state
        self._passthrough = False
        self._state_lock = threading.Lock()

    def _canceller_for(self, rate: int) -> FrameEchoCanceller:
        """İlk karede kurar; örnekleme hızı değişirse yenisini oluşturur."""
        with self._state_lock:
            canceller = self._canceller
            if canceller is None or self._canceller_rate != rate:
                canceller = self._canceller_factory(rate)
                self._canceller = canceller
                self._canceller_rate = rate
            return canceller

    @property
    def passthrough(self) -> bool:
        with self._state_lock:
            return self._passthrough

    def _disable(self, error: Exception) -> None:
        with self._state_lock:
            if self._passthrough:
                return
            self._passthrough = True

        logger.warning(
            "Yankı iptali çalışma anında başarısız oldu (%s); bu oturumda kapatıldı, "
            "ses işlenmeden iletiliyor.",
            type(error).__name__,
        )
        self._on_state("echo:off")

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Her iç kareyi iptal eder; hata olursa oturum boyunca değiştirmez."""
        async for frame in self._inner.frames():
            if self.passthrough:
                yield frame
                continue

            try:
                canceller = self._canceller_for(frame.sample_rate)
                # Zaman damgası geri çağrı anıdır (karenin sonu); başlangıca çekilir.
                duration = len(frame.pcm) // 2 / frame.sample_rate
                pcm = canceller.process(frame.pcm, frame.timestamp - duration)
            except _ECHO_ERRORS as error:
                self._disable(error)
                yield frame
                continue

            yield replace(frame, pcm=pcm, raw_pcm=None)

    def discard_pending(self) -> None:
        self._inner.discard_pending()
        with self._state_lock:
            canceller = self._canceller
        if canceller is None:
            return
        try:
            canceller.reset()
        except _ECHO_ERRORS:
            # Sıfırlama hatası mevcut ses akışını bozmamalıdır.
            return

    def pause(self) -> None:
        self._inner.pause()

    def resume(self) -> None:
        self._inner.resume()

    async def close(self) -> None:
        await self._inner.close()


def _ignore_state(_state: str) -> None:
    return None
