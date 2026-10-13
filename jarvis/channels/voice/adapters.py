"""İsteğe bağlı ses kitaplıklarının lazy adaptörleri."""

from __future__ import annotations

import asyncio
import importlib
import logging
import queue
import subprocess
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from time import monotonic
from typing import Any

from jarvis.channels.voice.core import AudioFrame, PCMChunk, SpeechSynthesizer
from jarvis.config.settings import VoiceConfig
from jarvis.safety.audit import AuditLog
from jarvis.tools.platform import child_environment

logger = logging.getLogger(__name__)


def _edge_exception_types(edge_module: Any) -> tuple[type[Exception], ...]:
    exceptions = getattr(edge_module, "exceptions", None)
    if exceptions is None:
        try:
            exceptions = importlib.import_module("edge_tts.exceptions")
        except ImportError:
            exceptions = None
    classes: list[type[Exception]] = []
    for name in (
        "EdgeTTSException",
        "NoAudioReceived",
        "UnexpectedResponse",
        "UnknownResponse",
        "WebSocketError",
        "SkewAdjustmentError",
    ):
        error_type = getattr(exceptions, name, None)
        if isinstance(error_type, type) and issubclass(error_type, Exception):
            classes.append(error_type)
    try:
        aiohttp = importlib.import_module("aiohttp")
    except ImportError:
        pass
    else:
        client_error = getattr(aiohttp, "ClientError", None)
        if isinstance(client_error, type) and issubclass(client_error, Exception):
            classes.append(client_error)
    return tuple(classes)


def _audio_exception_types(module: Any) -> tuple[type[Exception], ...]:
    error_type = getattr(module, "PortAudioError", None)
    if isinstance(error_type, type) and issubclass(error_type, Exception):
        return (error_type,)
    return ()


class SoundDeviceMicrophone:
    """sounddevice callback'i bounded queue'ya 16 kHz mono PCM16 taşır."""

    def __init__(self, config: VoiceConfig, device: int | str | None = None) -> None:
        self.config = config
        self.device = device
        self._queue: queue.Queue[AudioFrame | None] = queue.Queue(config.queue_frames)
        self._stream: Any = None
        self._paused = False
        self._discard_before = 0.0
        self._queue_lock = threading.Lock()
        self._audio_errors: tuple[type[Exception], ...] = ()
        self.dropped_frames = 0

    def _offer(self, pcm: bytes) -> None:
        with self._queue_lock:
            if self._paused:
                return
            if self._queue.full():
                with suppress(queue.Empty):
                    self._queue.get_nowait()
                self.dropped_frames += 1
            with suppress(queue.Full):
                self._queue.put_nowait(AudioFrame(pcm, monotonic()))

    def _callback(self, data: bytes, _frames: int, _time_info: Any, status: Any) -> None:
        if status:
            logger.warning("Mikrofon akış durumu: %s", status)
        self._offer(bytes(data))

    async def frames(self) -> AsyncIterator[AudioFrame]:
        try:
            import sounddevice  # type: ignore[import-not-found]  # İsteğe bağlı voice grubu.
        except ImportError as error:
            raise RuntimeError("Ses için `pip install .[voice]` kurulumu gerekli.") from error
        self._audio_errors = _audio_exception_types(sounddevice)
        frame_samples = self.config.sample_rate * self.config.frame_ms // 1000
        try:
            self._stream = sounddevice.RawInputStream(
                samplerate=self.config.sample_rate,
                blocksize=frame_samples,
                channels=1,
                dtype="int16",
                device=self.device,
                callback=self._callback,
            )
            self._stream.start()
        except self._audio_errors as error:
            raise RuntimeError("Mikrofon aygıtı açılamadı.") from error
        while True:
            frame = await asyncio.to_thread(self._queue.get)
            if frame is None:
                return
            if frame.timestamp < self._discard_before:
                continue
            yield frame

    def discard_pending(self) -> None:
        with self._queue_lock:
            self._discard_before = monotonic()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def pause(self) -> None:
        with self._queue_lock:
            self._paused = True
            self._discard_before = monotonic()
        self.discard_pending()

    def resume(self) -> None:
        self.discard_pending()
        with self._queue_lock:
            self._discard_before = monotonic()
            self._paused = False

    async def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            cleanup_errors = self._audio_errors + (OSError, RuntimeError)
            try:
                await asyncio.to_thread(stream.stop)
            except cleanup_errors as error:
                logger.warning("Mikrofon akışı durmadı (%s).", type(error).__name__)
            try:
                await asyncio.to_thread(stream.close)
            except cleanup_errors as error:
                logger.warning("Mikrofon akışı kapatılamadı (%s).", type(error).__name__)
        self.discard_pending()
        with suppress(queue.Full):
            self._queue.put_nowait(None)


class WebRTCVAD:
    def __init__(self, config: VoiceConfig) -> None:
        try:
            import webrtcvad  # type: ignore[import-not-found]  # İsteğe bağlı voice grubu.
        except ImportError as error:
            raise RuntimeError(
                "WebRTC VAD bağımlılığı yüklü değil; `pip install .[voice]`."
            ) from error
        self._vad = webrtcvad.Vad(config.vad_aggressiveness)
        self._sample_rate = config.sample_rate
        self._frame_ms = config.frame_ms

    def is_speech(self, pcm: bytes) -> bool:
        return bool(self._vad.is_speech(pcm, self._sample_rate))


class FasterWhisperRecognizer:
    """Model yalnız yerel dosyalardan açılır; inference thread'i tekilleştirilir."""

    def __init__(self, config: VoiceConfig) -> None:
        if not config.stt_model:
            raise ValueError(
                "voice.stt_model için önceden indirilmiş faster-whisper modeli gerekli."
            )
        self.config = config
        self._model: Any = None
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    def _load(self) -> Any:
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    try:
                        whisper_module = importlib.import_module("faster_whisper")
                    except ImportError as error:
                        raise RuntimeError(
                            "faster-whisper kurulu değil; `pip install .[voice]`."
                        ) from error
                    self._model = whisper_module.WhisperModel(
                        str(self.config.stt_model),
                        device=self.config.stt_device,
                        compute_type=self.config.stt_compute_type,
                        local_files_only=True,
                    )
        return self._model

    def prepare(self) -> None:
        """Ses aygıtı açılmadan önce yerel modeli doğrular; indirme yapmaz."""
        self._load()

    async def transcribe(self, pcm: bytes) -> str:
        return await self._decode(pcm)

    async def transcribe_partial(self, pcm: bytes) -> str:
        """Artan geçici pencereyi çözer; VoiceController sonucu asla dispatch etmez."""
        return await self._decode(pcm)

    async def _decode(self, pcm: bytes) -> str:
        import numpy

        samples = numpy.frombuffer(pcm, dtype=numpy.int16).astype(numpy.float32) / 32768.0

        def infer() -> str:
            # asyncio iptali yerel inference thread'ini durduramaz.
            with self._inference_lock:
                segments, _info = self._load().transcribe(
                    samples,
                    language="tr",
                    beam_size=1,
                    vad_filter=False,
                    condition_on_previous_text=False,
                )
                return " ".join(segment.text.strip() for segment in segments).strip()

        return await asyncio.to_thread(infer)


class SoundDeviceSink:
    """PCM'i küçük bloklarla yazıp ilk gerçek sink yazımında işaret üretir."""

    def __init__(self) -> None:
        self._streams: set[Any] = set()
        self._audio_errors: tuple[type[Exception], ...] = ()

    async def play(
        self,
        chunks: AsyncIterator[PCMChunk],
        cancelled: asyncio.Event,
        on_first_pcm: Callable[[], None],
    ) -> None:
        try:
            import sounddevice
        except ImportError as error:
            raise RuntimeError("Ses için `pip install .[voice]` kurulumu gerekli.") from error
        audio_errors = _audio_exception_types(sounddevice)
        cleanup_errors = audio_errors + (OSError, RuntimeError)
        self._audio_errors = audio_errors
        stream: Any = None
        notified = False
        failed = False
        try:
            async for chunk in chunks:
                if cancelled.is_set():
                    return
                if not chunk.pcm:
                    continue
                if (
                    chunk.sample_rate <= 0
                    or chunk.channels <= 0
                    or chunk.sample_width not in {1, 2, 4}
                ):
                    raise ValueError("PCM akış biçimi desteklenmiyor.")
                if stream is None:
                    stream = sounddevice.RawOutputStream(
                        samplerate=chunk.sample_rate,
                        channels=chunk.channels,
                        dtype={1: "int8", 2: "int16", 4: "int32"}.get(chunk.sample_width, "int16"),
                    )
                    stream.start()
                    self._streams.add(stream)
                sample_frame = chunk.channels * chunk.sample_width
                block_bytes = max(sample_frame, chunk.sample_rate * sample_frame // 50)
                block_bytes -= block_bytes % sample_frame
                if len(chunk.pcm) % sample_frame:
                    raise ValueError("PCM bloğu eksik örnek içeriyor.")
                for offset in range(0, len(chunk.pcm), block_bytes):
                    if cancelled.is_set():
                        return
                    block = chunk.pcm[offset : offset + block_bytes]
                    write_task = asyncio.create_task(asyncio.to_thread(stream.write, block))
                    try:
                        await asyncio.shield(write_task)
                    except asyncio.CancelledError:
                        # Yerel blocking write 20 ms ile sınırlıdır; stream'i ancak
                        # iptal et, sonra yazıcı thread'in çıktığını doğrula.
                        cancelled.set()
                        try:
                            await asyncio.to_thread(stream.abort)
                        except cleanup_errors as error:
                            logger.warning("Ses çıkışı iptal edilemedi (%s).", type(error).__name__)
                        await asyncio.shield(write_task)
                        raise
                    if not notified and block:
                        notified = True
                        on_first_pcm()
        except audio_errors as error:
            failed = True
            raise RuntimeError("Ses çıkış aygıtı başarısız oldu.") from error
        except (OSError, RuntimeError, ValueError):
            failed = True
            raise
        finally:
            close_chunks = getattr(chunks, "aclose", None)
            if callable(close_chunks):
                try:
                    await close_chunks()
                except cleanup_errors as error:
                    logger.warning("TTS akışı kapatılamadı (%s).", type(error).__name__)
            if stream is not None:
                self._streams.discard(stream)
                try:
                    if cancelled.is_set() or failed:
                        await asyncio.to_thread(stream.abort)
                    else:
                        await asyncio.to_thread(stream.stop)
                except cleanup_errors as error:
                    logger.warning("Ses çıkışı kapatılamadı (%s).", type(error).__name__)
                try:
                    await asyncio.to_thread(stream.close)
                except cleanup_errors as error:
                    logger.warning("Ses çıkışı kapatılamadı (%s).", type(error).__name__)

    async def close(self) -> None:
        streams, self._streams = tuple(self._streams), set()
        cleanup_errors = self._audio_errors + (OSError, RuntimeError)
        for stream in streams:
            try:
                await asyncio.to_thread(stream.abort)
            except cleanup_errors as error:
                logger.warning("Ses çıkışı iptal edilemedi (%s).", type(error).__name__)
            try:
                await asyncio.to_thread(stream.close)
            except cleanup_errors as error:
                logger.warning("Ses çıkışı kapatılamadı (%s).", type(error).__name__)


class PiperSynthesizer:
    """Piper modeli kullanıcı dosyasından yükler ve cümle PCM blokları üretir."""

    def __init__(self, config: VoiceConfig) -> None:
        if not config.piper_model:
            raise ValueError("voice.piper_model için önceden hazırlanmış Piper modeli gerekli.")
        self.config = config
        self._voice: Any = None
        self._inference_lock = threading.Lock()

    def _load(self) -> Any:
        if self._voice is None:
            try:
                piper_module = importlib.import_module("piper")
            except ImportError as error:
                raise RuntimeError("Piper TTS kurulu değil; `pip install .[voice]`.") from error
            config_path = self.config.piper_config
            self._voice = piper_module.PiperVoice.load(
                str(self.config.piper_model),
                config_path=config_path,
                use_cuda=False,
            )
        return self._voice

    def prepare(self) -> None:
        """Piper modelini ses aygıtı açılmadan doğrular."""
        self._load()

    async def stream(self, text: str, cancelled: asyncio.Event) -> AsyncIterator[PCMChunk]:
        chunks: queue.Queue[PCMChunk | None] = queue.Queue(maxsize=4)
        failure: list[BaseException] = []

        def produce() -> None:
            with self._inference_lock:
                try:
                    for audio in self._load().synthesize(text):
                        if cancelled.is_set():
                            break
                        pcm = audio.audio_int16_bytes
                        item = PCMChunk(
                            pcm,
                            audio.sample_rate,
                            audio.sample_channels,
                            audio.sample_width,
                        )
                        while not cancelled.is_set():
                            try:
                                chunks.put(item, timeout=0.1)
                                break
                            except queue.Full:
                                continue
                except (OSError, RuntimeError, ValueError) as error:
                    failure.append(error)
                finally:
                    with suppress(queue.Full):
                        chunks.put_nowait(None)

        worker = asyncio.create_task(asyncio.to_thread(produce))
        try:
            while True:
                try:
                    item = await asyncio.to_thread(chunks.get, True, 0.1)
                except queue.Empty:
                    if worker.done():
                        break
                    if cancelled.is_set():
                        break
                    continue
                if item is None:
                    break
                if not cancelled.is_set():
                    yield item
            await asyncio.shield(worker)
            if failure:
                raise RuntimeError("Piper sesi üretilemedi.") from failure[0]
        finally:
            if not worker.done():
                # Generator kapanınca üreticiye iptal aktar; yerel native inference
                # bittiği ve thread lock bırakıldığı doğrulanana kadar bekle.
                cancelled.set()
                await asyncio.shield(worker)

    async def close(self) -> None:
        return None


class EdgeTTSynthesizer:
    """Edge MP3 akışını sabit ffmpeg argv ile PCM'e çevirir."""

    def __init__(self, config: VoiceConfig, audit: AuditLog) -> None:
        if not config.allow_cloud_tts_text:
            raise ValueError("Edge TTS için allow_cloud_tts_text açık olmalı.")
        if not config.edge_voice:
            raise ValueError("voice.edge_voice açıkça ayarlanmalı.")
        self.config = config
        self.audit = audit
        self._process: asyncio.subprocess.Process | None = None

    async def stream(self, text: str, cancelled: asyncio.Event) -> AsyncIterator[PCMChunk]:
        try:
            edge_tts = importlib.import_module("edge_tts")
        except ImportError as error:
            raise RuntimeError("Edge TTS kurulu değil; `pip install .[voice]`.") from error
        edge_errors = _edge_exception_types(edge_tts)
        # Gizlilik denetimi metni göndermeden önce yazılır; ham metin kaydedilmez.
        self.audit.emit(
            "cloud_tts_transfer",
            service="edge_tts",
            content_kind="assistant_response_text",
            allowed=True,
        )
        try:
            communicate = edge_tts.Communicate(text, self.config.edge_voice)
        except edge_errors as error:
            raise RuntimeError("Edge TTS yanıt vermedi.") from error
        process = await self._start_process()
        self._process = process
        if process.stdin is None or process.stdout is None:
            try:
                await self._stop(process)
            finally:
                self._process = None
            raise RuntimeError("ffmpeg pipe'ları açılamadı.")

        async def feed() -> None:
            assert process.stdin is not None
            try:
                async for packet in communicate.stream():
                    if cancelled.is_set():
                        break
                    if packet.get("type") == "audio":
                        process.stdin.write(packet["data"])
                        await process.stdin.drain()
            except edge_errors as error:
                raise RuntimeError("Edge TTS akışı başarısız oldu.") from error
            finally:
                try:
                    process.stdin.close()
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    logger.debug("ffmpeg stdin kapanırken pipe zaten kapanmıştı.")
                except (OSError, RuntimeError) as error:
                    logger.warning("ffmpeg girdisi kapatılamadı (%s).", type(error).__name__)

        feeder = asyncio.create_task(feed())
        try:
            while not cancelled.is_set():
                pcm = await process.stdout.read(4096)
                if not pcm:
                    break
                yield PCMChunk(pcm, 24000, 1, 2)
            await feeder
            if await process.wait() != 0 and not cancelled.is_set():
                raise RuntimeError("ffmpeg MP3 çözümleyemedi.")
        finally:
            if not feeder.done():
                feeder.cancel()
            try:
                await feeder
            except asyncio.CancelledError:
                pass
            except (OSError, RuntimeError) as error:
                logger.warning("Edge TTS akışı temizlenemedi (%s).", type(error).__name__)
            await self._stop(process)
            self._process = None

    async def _start_process(self) -> asyncio.subprocess.Process:
        async def create() -> asyncio.subprocess.Process:
            return await asyncio.create_subprocess_exec(
                self.config.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "mp3",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-ar",
                "24000",
                "-ac",
                "1",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=child_environment(),
            )

        start_task = asyncio.create_task(create())
        try:
            return await asyncio.shield(start_task)
        except asyncio.CancelledError:
            try:
                process = await asyncio.shield(start_task)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                raise
            await self._stop(process)
            raise

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 0.5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                try:
                    await asyncio.wait_for(process.wait(), 2.0)
                except TimeoutError as error:
                    raise RuntimeError("ffmpeg süreci zamanında kapanmadı.") from error

    async def close(self) -> None:
        if self._process is not None:
            await self._stop(self._process)
            self._process = None


class FallbackSynthesizer:
    """Edge ilk PCM'den önce başarısızsa Piper'a düşer; yarım cümleyi yinelemez."""

    def __init__(self, primary: SpeechSynthesizer, local: SpeechSynthesizer) -> None:
        self.primary = primary
        self.local = local

    async def stream(self, text: str, cancelled: asyncio.Event) -> AsyncIterator[PCMChunk]:
        first = True
        try:
            async for chunk in self.primary.stream(text, cancelled):
                if cancelled.is_set():
                    return
                first = False
                yield chunk
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("Birincil TTS başarısız (%s).", type(error).__name__)
            if not first or cancelled.is_set():
                raise
            async for chunk in self.local.stream(text, cancelled):
                if cancelled.is_set():
                    return
                yield chunk

    async def close(self) -> None:
        await self.primary.close()
        await self.local.close()
