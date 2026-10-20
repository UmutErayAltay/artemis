"""İsteğe bağlı ses kitaplıklarının lazy adaptörleri."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import queue
import subprocess
import threading
import unicodedata
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from time import monotonic
from typing import Any, Protocol

from jarvis.channels.voice.core import AudioFrame, PCMChunk, SpeechSynthesizer
from jarvis.config.settings import VoiceConfig
from jarvis.safety.audit import AuditLog
from jarvis.tools.platform import child_environment

logger = logging.getLogger(__name__)


class EchoReferenceSink(Protocol):
    """Yankı referansına yazılan ses örneğini alır."""

    def feed(
        self,
        pcm: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
    ) -> None:
        ...


_GHOST_PHRASES = {
    "abone olmayı unutmayın", "izlediğiniz için teşekkürler",
    "izlediğiniz için teşekkür ederim", "izlediğiniz için teşekkür ederiz",
    "bu videoyu izlediğiniz için teşekkür ederim",
    "bu videoyu izlediğiniz için teşekkür ederiz", "altyazı mk",
}


def is_ghost_transcript(text: str) -> bool:
    normalized = text.replace("I", "ı").replace("İ", "i").lower()
    normalized = " ".join("".join(
        char for char in normalized if char.isalnum() or char.isspace()
    ).split())
    return normalized in _GHOST_PHRASES


def is_repeating_transcript(text: str) -> bool:
    """Bütünü aynı kısa kalıbı en az üç kez dönen STT çıktısını reddeder."""
    words = "".join(char if char.isalnum() else " " for char in text.casefold()).split()
    if len(words) < 6:
        return False
    for period in range(1, min(12, len(words) // 3) + 1):
        matches = sum(word == words[index % period] for index, word in enumerate(words))
        if matches / len(words) >= 0.95:
            return True
    return False


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
    """sounddevice callback'i bounded queue'ya mono PCM16 taşır (varsayılan 16 kHz).

    ``capture_rate`` (ör. 48000) gürültü engelleme içindir: aygıt bu hızda açılır, kareler
    ``AudioFrame.sample_rate`` ile işaretlenir ve ``DenoisedMicrophone`` 16 kHz'e indirir.
    Aygıt bu hızı açamazsa tek Türkçe uyarıyla 16 kHz yakalamaya dönülür.
    """

    def __init__(
        self,
        config: VoiceConfig,
        device: int | str | None = None,
        capture_rate: int | None = None,
    ) -> None:
        self.config = config
        self.device = device
        self.capture_rate = capture_rate or config.sample_rate
        self._rate = self.capture_rate
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
                self._queue.put_nowait(AudioFrame(pcm, monotonic(), self._rate))

    def _callback(self, data: bytes, _frames: int, _time_info: Any, status: Any) -> None:
        if status:
            logger.warning("Mikrofon akış durumu: %s", status)
        self._offer(bytes(data))

    async def frames(self) -> AsyncIterator[AudioFrame]:
        try:
            sounddevice = importlib.import_module("sounddevice")
        except ImportError as error:
            raise RuntimeError("Ses için `pip install .[voice]` kurulumu gerekli.") from error
        self._audio_errors = _audio_exception_types(sounddevice)
        try:
            self._open(sounddevice, self.capture_rate)
        except self._audio_errors as error:
            if self.capture_rate == self.config.sample_rate:
                raise RuntimeError("Mikrofon aygıtı açılamadı.") from error
            logger.warning(
                "Mikrofon %d Hz açılamadı (%s); gürültü engelleme için 16 kHz yakalamaya "
                "dönüldü (>8 kHz bilgi olmadan daha düşük kalite).",
                self.capture_rate, type(error).__name__,
            )
            self._close_failed_stream()
            self.capture_rate = self.config.sample_rate
            try:
                self._open(sounddevice, self.capture_rate)
            except self._audio_errors as retry_error:
                raise RuntimeError("Mikrofon aygıtı açılamadı.") from retry_error
        while True:
            frame = await asyncio.to_thread(self._queue.get)
            if frame is None:
                return
            if frame.timestamp < self._discard_before:
                continue
            yield frame

    def _open(self, sounddevice: Any, rate: int) -> None:
        self._rate = rate
        self._stream = sounddevice.RawInputStream(
            samplerate=rate,
            blocksize=rate * self.config.frame_ms // 1000,
            channels=1,
            dtype="int16",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def _close_failed_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            for action in (stream.stop, stream.close):
                with suppress(*self._audio_errors, OSError, RuntimeError):
                    action()

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
            webrtcvad = importlib.import_module("webrtcvad")
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
        self._cuda_handles: list[Any] = []

    def _prepare_cuda_libraries(self) -> None:
        if os.name != "nt" or self.config.stt_device != "cuda":
            return
        import ctypes

        for directory in self.config.cuda_library_dirs:
            if not directory.is_dir():
                raise RuntimeError("Yapılandırılmış CUDA kütüphane dizini bulunamadı")
            handle = os.add_dll_directory(str(directory))  # type: ignore[attr-defined, unused-ignore]
            self._cuda_handles.append(handle)
        for filename in ("cublasLt64_12.dll", "cublas64_12.dll", "cudnn64_9.dll"):
            for directory in self.config.cuda_library_dirs:
                path = directory / filename
                if path.is_file():
                    self._cuda_handles.append(ctypes.CDLL(str(path)))
                    break

    def _load(self) -> Any:
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    self._prepare_cuda_libraries()
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
        return await self._decode(pcm, partial=False)

    async def transcribe_partial(self, pcm: bytes) -> str:
        """Artan geçici pencereyi çözer; VoiceController sonucu asla dispatch etmez."""
        return await self._decode(pcm, partial=True)

    async def _decode(self, pcm: bytes, *, partial: bool) -> str:
        try:
            numpy = importlib.import_module("numpy")
        except ImportError as error:
            raise RuntimeError("Ses için `pip install .[voice]` kurulumu gerekli.") from error

        samples = numpy.frombuffer(pcm, dtype=numpy.int16).astype(numpy.float32) / 32768.0

        def infer() -> str:
            # asyncio iptali yerel inference thread'ini durduramaz.
            with self._inference_lock:
                model = self._load()
                language = None if self.config.stt_language == "auto" else "tr"
                if self.config.stt_language == "tr_en":
                    # Kısa Türkçe komutta tüm diller arasında yanlış tahmin yapma.
                    # İngilizceye yalnız güçlü ses kanıtı olduğunda geçilir.
                    detected, confidence, probabilities = model.detect_language(audio=samples)
                    scores = dict(probabilities)
                    if detected == "en" and confidence >= 0.7 and (
                        scores.get("en", 0.0) >= 2 * scores.get("tr", 0.0)
                    ):
                        language = "en"
                segments, _info = model.transcribe(
                    samples,
                    language=language,
                    task="transcribe",
                    beam_size=(
                        self.config.stt_partial_beam_size
                        if partial
                        else self.config.stt_beam_size
                    ),
                    temperature=0,
                    hotwords=" ".join(self.config.stt_hotwords) or None,
                    vad_filter=self.config.stt_vad_filter,
                    vad_parameters={"min_speech_duration_ms": 100, "speech_pad_ms": 200},
                    condition_on_previous_text=False,
                )
                decoded = list(segments)
                if any(getattr(segment, "compression_ratio", 0.0) > 2.4 for segment in decoded):
                    return ""
                text = " ".join(
                    segment.text.strip() for segment in decoded
                    if getattr(segment, "avg_logprob", 0.0) >= self.config.stt_min_logprob
                    and getattr(segment, "no_speech_prob", 0.0) <= self.config.stt_max_no_speech
                ).strip()
                if self.config.stt_language == "tr_en" and any(
                    "CYRILLIC" in unicodedata.name(char, "") for char in text
                ):
                    return ""
                return "" if is_ghost_transcript(text) or is_repeating_transcript(text) else text

        return await asyncio.to_thread(infer)


class SoundDeviceSink:
    """PCM'i küçük bloklarla yazıp ilk gerçek sink yazımında işaret üretir."""

    def __init__(
        self,
        echo_reference: EchoReferenceSink | None = None,
        on_echo_failure: Callable[[], None] | None = None,
    ) -> None:
        self._streams: set[Any] = set()
        self._audio_errors: tuple[type[Exception], ...] = ()
        self._echo_reference = echo_reference
        self._on_echo_failure = on_echo_failure

    def _feed_echo_reference(
        self,
        pcm: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
    ) -> None:
        reference = self._echo_reference
        if reference is None:
            return
        try:
            reference.feed(pcm, sample_rate, channels, sample_width)
        except (ImportError, ValueError, RuntimeError, OSError) as error:
            self._echo_reference = None
            logger.warning(
                "Yankı referansı beslenemedi (%s); ses çıkışı sürüyor ve "
                "yankı referansı kapatıldı.",
                type(error).__name__,
            )
            if self._on_echo_failure is not None:
                self._on_echo_failure()

    async def play(
        self,
        chunks: AsyncIterator[PCMChunk],
        cancelled: asyncio.Event,
        on_first_pcm: Callable[[], None],
    ) -> None:
        try:
            sounddevice = importlib.import_module("sounddevice")
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
                    self._feed_echo_reference(
                        block,
                        chunk.sample_rate,
                        chunk.channels,
                        chunk.sample_width,
                    )
                    if not notified and block:
                        notified = True
                        on_first_pcm()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except audio_errors as error:
            failed = True
            raise RuntimeError("Ses çıkış aygıtı başarısız oldu.") from error
        except (OSError, RuntimeError, ValueError):
            failed = True
            raise
        finally:
            # Önce aygıtı sustur; üretici thread/uzak akış temizliği bekletebilir.
            aborted = stream is not None and (cancelled.is_set() or failed)
            if aborted:
                try:
                    await asyncio.to_thread(stream.abort)
                except cleanup_errors as error:
                    logger.warning("Ses çıkışı iptal edilemedi (%s).", type(error).__name__)
            close_chunks = getattr(chunks, "aclose", None)
            if callable(close_chunks):
                try:
                    await close_chunks()
                except cleanup_errors as error:
                    logger.warning("TTS akışı kapatılamadı (%s).", type(error).__name__)
            if stream is not None:
                self._streams.discard(stream)
                try:
                    if not aborted:
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
                    close_wait = asyncio.create_task(process.stdin.wait_closed())
                    try:
                        # Windows pipe callback'i ortak Future'ı sonradan tamamlar;
                        # feed iptali bu Future'ı iptal edip InvalidStateError üretmemeli.
                        await asyncio.shield(close_wait)
                    except asyncio.CancelledError:
                        await self._stop(process)
                        with suppress(BrokenPipeError, ConnectionResetError, OSError):
                            await close_wait
                        raise
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
        # Proactor pipe'ları process.wait() dönüşünden sonra hâlâ açık olabilir.
        # Çıkışı tüketmek transport kapanışını event loop kapanmadan tamamlar.
        try:
            await asyncio.wait_for(process.communicate(), 2.0)
        except TimeoutError as error:
            raise RuntimeError("ffmpeg pipe temizliği zamanında tamamlanmadı.") from error

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
