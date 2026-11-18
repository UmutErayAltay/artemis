"""Ses denetleyicisini ortak runtime'dan kurar; masaüstü köprüsü bunu işçide çağırır."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Protocol

from artemis.brain.agent_loop import RunResult
from artemis.brain.emotion import emotion_tags_active
from artemis.channels.runtime import Runtime
from artemis.channels.voice.adapters import (
    EdgeTTSynthesizer,
    EmaSynthesizer,
    FallbackSynthesizer,
    FasterWhisperRecognizer,
    OpenAICompatibleTranscriber,
    PiperSynthesizer,
    SoundDeviceMicrophone,
    SoundDeviceSink,
    WebRTCVAD,
)
from artemis.channels.voice.cloud_tts import CloudSpeechSynthesizer
from artemis.channels.voice.core import (
    AudioSink,
    Microphone,
    SpeechRecognizer,
    SpeechSynthesizer,
    TurnDetector,
    VoiceActivityDetector,
    VoiceController,
)
from artemis.channels.voice.denoise import (
    MODEL_SR,
    NOISE_LEVEL_ATTEN_DB,
    DeepFilterNetDenoiser,
    DenoiseDependencyError,
    DenoisedMicrophone,
)
from artemis.channels.voice.echo import (
    EchoCancelledMicrophone,
    EchoCanceller,
    EchoReference,
)
from artemis.channels.voice.game_mode import (
    GameModeRecognizer,
    SwappableRecognizer,
    fullscreen_app_active,
    resolve_light_model,
    resolve_model_label,
)
from artemis.channels.voice.kws import WakeSpotter
from artemis.channels.voice.live_text import SherpaLiveTranscriber
from artemis.channels.voice.silero import SileroVAD
from artemis.channels.voice.smart_turn import SmartTurnDetector
from artemis.channels.voice.telemetry import VoiceTimingEvent
from artemis.config.model_paths import DEFAULT_NOISE_MODEL_DIR
from artemis.config.settings import Settings, VoiceConfig
from artemis.tools.app_resolver import AliasStoreError, alias_store

logger = logging.getLogger(__name__)


def alias_hotword_config(settings: Settings) -> VoiceConfig:
    """``stt_alias_hotwords`` açıksa öğretilmiş takma adların hedef adlarını ipucuna ekler.

    Yalnız kullanıcının düzelterek öğrettiği uygulamalar eklenir, katalog değil: geniş
    hotword listesi doğruluğu ve uyandırmayı bozabilir (ADR 0044). Ekleme ayarın sınırlarını
    (100 sözcük, 2048 karakter) aşmaz; sığmayan ad atlanır. Takma ad dosyası okunamazsa ses
    yine açılır, ipucu eklenmez.
    """
    voice = settings.voice
    if not voice.stt_alias_hotwords or not settings.persist_memory:
        return voice
    try:
        aliases = alias_store(settings.state_dir).load()
    except AliasStoreError as error:
        logger.warning("Takma ad ipuçları eklenemedi: %s", error)
        return voice
    words = list(voice.stt_hotwords)
    for name in sorted(set(aliases.values())):
        if name not in settings.applications or name in words:
            continue
        if len(words) >= 100 or sum(map(len, words)) + len(name) > 2048:
            break
        words.append(name)
    return VoiceConfig.model_validate({**voice.model_dump(), "stt_hotwords": tuple(words)})


def _denoise_reason(error: Exception) -> str:
    """``kind:metin``; kind `deps` (paket eksik) ve `model` (dosya sorunu) farklı çözüm ister."""
    if isinstance(error, DenoiseDependencyError | ImportError):
        return "deps:onnxruntime veya numpy kurulu değil"
    if isinstance(error, OSError):
        return "model:model dosyaları bulunamadı veya okunamadı"
    if isinstance(error, ValueError):
        return "model:model dosyası sha256 doğrulamasından geçmedi"
    return "model:model yüklenemedi (dosya bozuk olabilir)"


def build_vad(voice: VoiceConfig, on_state: StateCallback) -> VoiceActivityDetector:
    """Silero kurulamazsa ses hattı WebRTC ile sürer; düşüş ``vad:webrtc`` ile görünür olur."""
    if voice.vad_backend != "webrtc":
        try:
            return SileroVAD(threshold=voice.vad_threshold)
        except (OSError, ValueError, RuntimeError, ImportError) as error:
            logger.warning("Silero VAD açılamadı, WebRTC VAD ile sürülüyor: %s", error)
            on_state("vad:webrtc")
    return WebRTCVAD(voice)


def build_turn_detector(voice: VoiceConfig, on_state: StateCallback) -> TurnDetector | None:
    """Smart Turn kurulamazsa ses hattı onsuz sürer; düşüş ``turn:off`` ile görünür olur."""
    if not voice.turn_detection:
        return None
    try:
        return SmartTurnDetector(threshold=voice.turn_threshold)
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        logger.warning("Smart Turn açılamadı, yalnız sessizlikle sürülüyor: %s", error)
        on_state("turn:off")
        return None


def _live_reason(error: Exception) -> str:
    """``denoise`` ile aynı ``kind:metin`` kalıbı; deps ve model farklı çözüm ister."""
    if isinstance(error, ImportError) or (
        isinstance(error, RuntimeError) and "pip install" in str(error)
    ):
        return "deps:sherpa-onnx veya huggingface-hub kurulu değil"
    return "model:canlı yazı modeli indirilemedi veya yüklenemedi"


async def build_wake_spotter(voice: VoiceConfig, on_state: StateCallback) -> WakeSpotter | None:
    """KWS ön-kapısı (ADR 0069); kurulamazsa transkript uyandırmasıyla sürer, düşüş görünür olur."""
    if voice.wake_backend != "kws":
        return None
    if not (voice.live_transcript_model and voice.live_transcript_revision):
        on_state("wake:unavailable:model:live_transcript_model/revision boş")
        return None
    spotter = WakeSpotter(
        voice.live_transcript_model, voice.live_transcript_revision, voice.wake_word,
        threshold=voice.wake_kws_threshold,
    )
    try:
        await _prepare(spotter)
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        logger.warning("Uyandırma ön-kapısı açılamadı, transkriptle sürülüyor: %s", error)
        on_state("wake:unavailable:" + _live_reason(error))
        return None
    return spotter


RunCommand = Callable[[str], Awaitable[RunResult]]
StateCallback = Callable[[str], None]


def _cloud_stt_ready(runtime: Runtime) -> bool:
    settings = runtime.settings
    endpoint_id = settings.voice.stt_cloud_endpoint
    return bool(
        endpoint_id
        and endpoint_id in settings.endpoints
        and (
            settings.voice.stt_cloud_protocol == "xai"
            or bool(settings.voice.stt_cloud_model.strip())
        )
    )


def _setup_game_mode(
    runtime: Runtime,
    whisper: FasterWhisperRecognizer,
    local_voice: VoiceConfig,
    cloud_recognizer: OpenAICompatibleTranscriber | None,
    on_state: StateCallback,
    cleanup: AsyncExitStack,
) -> tuple[SpeechRecognizer, OpenAICompatibleTranscriber | None, Callable[[], bool] | None]:
    """Oyun modu açıkken tam ekran algılandığında ana modeli hafif veya bulut hedefe geçirir."""
    settings = runtime.settings
    if not settings.voice.game_mode:
        return whisper, cloud_recognizer, None
    primary_label = resolve_model_label(settings.voice.stt_model, "ana yerel model")
    light_path = resolve_light_model(settings.voice)
    if settings.voice.game_mode_target == "cloud":
        if not _cloud_stt_ready(runtime):
            on_state("game:cloud_unavailable")
            return whisper, cloud_recognizer, None
        if light_path is None:
            # Uyandırma yerelde verilmeli: hafif model yokken bulut hedefi her sesi buluta
            # yollardı ("Artemis"ten öncesi dahil). Büyük model GPU'da kalır.
            on_state("game:unavailable")
            return whisper, cloud_recognizer, None
        endpoint_id = settings.voice.stt_cloud_endpoint
        active_cloud = cloud_recognizer or OpenAICompatibleTranscriber(
            local_voice, endpoint_id, settings.endpoints[endpoint_id], runtime.audit,
        )
        cloud_name = settings.voice.stt_cloud_model.strip() or endpoint_id
        should_use_cloud: Callable[[], bool] | None = None
        wake_cfg = VoiceConfig.model_validate({
            **local_voice.model_dump(),
            "stt_model": light_path,
            "stt_device": "cpu",
            "stt_compute_type": "int8",
            "stt_hotwords": (local_voice.wake_word,),
        })
        light_rec: SwappableRecognizer = FasterWhisperRecognizer(wake_cfg)
        wake_name = resolve_model_label(light_path, "small")
        light_label = f"{wake_name} (CPU) + Bulut ({cloud_name})"
        game = GameModeRecognizer(
            whisper, light_rec, fullscreen_app_active, on_state,
            primary_label=primary_label, light_label=light_label,
        )
        if settings.voice.stt_mode != "hybrid":
            should_use_cloud = lambda: game.in_game  # noqa: E731
        game.start()
        cleanup.callback(game.close)
        return game, active_cloud, should_use_cloud
    if light_path is None:
        # Hafif model yoksa büyük model GPU'da kalır; durum görünür olur.
        on_state("game:unavailable")
        return whisper, cloud_recognizer, None
    light = FasterWhisperRecognizer(local_voice.model_copy(update={"stt_model": light_path}))
    game = GameModeRecognizer(
        whisper, light, fullscreen_app_active, on_state,
        primary_label=primary_label, light_label=resolve_model_label(light_path, "small"),
    )
    game.start()
    # Kurulum yarıda kalırsa izleyici iş parçacığı da durur.
    cleanup.callback(game.close)
    return game, cloud_recognizer, None


async def build_voice_controller(
    runtime: Runtime, run_command: RunCommand, on_state: StateCallback,
    *, on_timing: Callable[[str, float], None] | None = None,
    on_event: Callable[[VoiceTimingEvent], None] | None = None,
    microphone: Microphone | None = None,
    sink: AudioSink | None = None,
) -> VoiceController:
    """STT/TTS modellerini hazırlar; eksik model OSError/RuntimeError/ValueError olarak çıkar.

    ``microphone``/``sink`` yalnız ölçüm içindir (bench WAV mikrofonu, sessiz sink): verilirse
    ses aygıtı açılmaz; yankı/gürültü sarmalayıcıları ve STT/TTS/model hattı aynen kurulur.

    Hazırlık engelleyici dosya/model yüklemesi olduğu için ``to_thread`` ile yapılır:
    işçi döngüsü (onay iptali, kapanış) hazırlık sürerken tıkanmaz. Onay çağrısı
    burada bağlanmaz; onay yalnız ``create_runtime``'a verilen ekran onayıdır.
    """
    settings = runtime.settings
    denoiser: DeepFilterNetDenoiser | None = None
    if settings.voice.noise_suppression:
        # Gürültü engelleme isteğe bağlı bir iyileştirmedir: kurulamazsa ses hattı ham
        # mikrofonla sürer, ama durum görünür olur (sessizce çalışıyormuş gibi davranılmaz).
        try:
            model_dir = settings.voice.noise_model_dir or DEFAULT_NOISE_MODEL_DIR
            denoiser = await asyncio.to_thread(
                DeepFilterNetDenoiser,
                model_dir,
                atten_lim_db=NOISE_LEVEL_ATTEN_DB[settings.voice.noise_suppression_level],
                frame_samples=settings.voice.sample_rate * settings.voice.frame_ms // 1000,
            )
        except (OSError, ValueError, RuntimeError, ImportError) as error:
            logger.warning("Gürültü engelleme açılamadı, ham mikrofonla sürülüyor: %s", error)
            on_state("denoise:unavailable:" + _denoise_reason(error))
    # Gürültü engelleme açıksa mikrofon yerel 48 kHz yakalanır (modelin doğal hızı, >8 kHz bilgi
    # korunur); açılamazsa adaptör uyarıyla 16 kHz'e döner. Aşağı akış hep 16 kHz görür.
    async with AsyncExitStack() as cleanup:
        if microphone is None:
            microphone = SoundDeviceMicrophone(
                settings.voice,
                device=settings.voice.input_device,
                capture_rate=MODEL_SR if denoiser is not None else None,
            )
        cleanup.push_async_callback(_close_optional, microphone)
        echo_reference: EchoReference | None = None
        if settings.voice.echo_cancellation:
            reference = EchoReference(
                MODEL_SR if denoiser is not None else settings.voice.sample_rate
            )
            echo_reference = reference

            def make_canceller(rate: int) -> EchoCanceller:
                reference.retarget(rate)
                return EchoCanceller(rate, reference, delay_ms=settings.voice.echo_delay_ms)

            echo_off_reported = False

            def report_echo_state(state: str) -> None:
                # Mikrofon sarmalayıcısı ve hoparlör beslemesi aynı arızayı ayrı ayrı görebilir
                # (ör. livekit yok); kullanıcıya tek bildirim yeter.
                nonlocal echo_off_reported
                if state == "echo:off":
                    if echo_off_reported:
                        return
                    echo_off_reported = True
                on_state(state)

            microphone = EchoCancelledMicrophone(microphone, make_canceller, report_echo_state)
        if denoiser is not None:
            microphone = DenoisedMicrophone(
                microphone, denoiser, on_state, raw_for_stt=not settings.voice.noise_suppression_stt
            )
        if sink is None and echo_reference is None:
            sink = SoundDeviceSink()
        elif sink is None:
            sink = SoundDeviceSink(
                echo_reference=echo_reference,
                on_echo_failure=lambda: report_echo_state("echo:off"),
            )
        cleanup.push_async_callback(_close_optional, sink)
        cloud_recognizer: OpenAICompatibleTranscriber | None = None
        if settings.voice.stt_mode == "hybrid":
            endpoint_id = settings.voice.stt_cloud_endpoint
            cloud_recognizer = OpenAICompatibleTranscriber(
                alias_hotword_config(settings), endpoint_id,
                settings.endpoints[endpoint_id], runtime.audit,
            )
        local_voice = alias_hotword_config(settings)
        if cloud_recognizer is not None:
            # Hybrid'de yerel tanıyıcı yalnız uyandırmaya karar verir; tam hotword listesi
            # "Artemis tarayıcı" gibi kuyruk halüsinasyonu üretiyordu (ADR 0079 eki).
            local_voice = VoiceConfig.model_validate(
                {**local_voice.model_dump(), "stt_hotwords": (local_voice.wake_word,)}
            )
        whisper = FasterWhisperRecognizer(local_voice)
        await _prepare(whisper)
        recognizer, cloud_recognizer, should_use_cloud = _setup_game_mode(
            runtime, whisper, local_voice, cloud_recognizer, on_state, cleanup,
        )
        synthesizer: SpeechSynthesizer
        if settings.voice.tts_backend == "edge":
            edge_tts = EdgeTTSynthesizer(settings.voice, runtime.audit)
            cleanup.push_async_callback(_close_optional, edge_tts)
            # Yedek yalnız Piper modeli varken kurulur: `tts_local_fallback` varsayılan açıktır ve
            # model yokken Piper kurucusu çalışabilecek Edge sesini de kapatırdı.
            if settings.voice.tts_local_fallback and settings.voice.piper_model:
                local_tts = PiperSynthesizer(settings.voice)
                await _prepare(local_tts)
                cleanup.push_async_callback(_close_optional, local_tts)
                synthesizer = FallbackSynthesizer(edge_tts, local_tts)
            else:
                synthesizer = edge_tts
        elif settings.voice.tts_backend == "ema":
            primary_tts = EmaSynthesizer(settings.voice)
            await _prepare(primary_tts)
            cleanup.push_async_callback(_close_optional, primary_tts)
            if settings.voice.tts_local_fallback and settings.voice.piper_model:
                local_tts = PiperSynthesizer(settings.voice)
                await _prepare(local_tts)
                cleanup.push_async_callback(_close_optional, local_tts)
                synthesizer = FallbackSynthesizer(primary_tts, local_tts)
            else:
                synthesizer = primary_tts
        elif settings.voice.tts_backend == "cloud":
            endpoint_id = settings.voice.tts_cloud_endpoint
            cloud_tts = CloudSpeechSynthesizer(
                settings.voice,
                endpoint_id,
                settings.endpoints[endpoint_id],
                runtime.audit,
            )
            cleanup.push_async_callback(_close_optional, cloud_tts)
            # Ücretsiz model süresi dolduğunda veya kota sınırında yerel Piper'a düşülür.
            if settings.voice.tts_local_fallback and settings.voice.piper_model:
                local_tts = PiperSynthesizer(settings.voice)
                await _prepare(local_tts)
                cleanup.push_async_callback(_close_optional, local_tts)
                synthesizer = FallbackSynthesizer(
                    cloud_tts, local_tts, strip_tags=emotion_tags_active(settings.voice)
                )
            else:
                synthesizer = cloud_tts
        else:
            local_tts = PiperSynthesizer(settings.voice)
            await _prepare(local_tts)
            cleanup.push_async_callback(_close_optional, local_tts)
            synthesizer = local_tts
        live: SherpaLiveTranscriber | None = None
        if settings.voice.live_transcript:
            # Canlı yazı yalnız gösterimdir (ADR 0047): kurulamazsa ses hattı onsuz sürer.
            voice = settings.voice
            if not (voice.live_transcript_model and voice.live_transcript_revision):
                on_state("live:unavailable:model:live_transcript_model/revision boş")
            else:
                candidate = SherpaLiveTranscriber(
                    voice.live_transcript_model, voice.live_transcript_revision
                )
                try:
                    await _prepare(candidate)
                    live = candidate
                except (OSError, ValueError, RuntimeError, ImportError) as error:
                    logger.warning("Canlı yazı açılamadı, onsuz sürülüyor: %s", error)
                    on_state("live:unavailable:" + _live_reason(error))
        wake_spotter = await build_wake_spotter(settings.voice, on_state)
        if wake_spotter is not None:
            cleanup.callback(wake_spotter.close)
        controller = VoiceController(
            runtime.runner,
            settings.voice,
            microphone,
            build_vad(settings.voice, on_state),
            recognizer,
            synthesizer,
            sink,
            run_command=run_command,
            stream_responses=settings.voice.streaming,
            on_state=on_state,
            on_timing=on_timing,
            on_event=on_event,
            on_input_level=lambda level: on_state(f"level:{level}"),
            on_transcript=lambda text: on_state("heard:" + text[:200]),
            cloud_recognizer=cloud_recognizer,
            live_transcriber=live,
            on_live_text=lambda text: on_state("live:" + text[:200]),
            turn_detector=build_turn_detector(settings.voice, on_state),
            wake_spotter=wake_spotter,
            should_use_cloud=should_use_cloud,
        )
        cleanup.pop_all()
        return controller


async def build_dictation_recognizer(
    runtime: Runtime, content_kind: str = "dictation_audio"
) -> SpeechRecognizer:
    """Dikte (ADR 0057) ses yoluyla AYNI gizlilik ayarını izler: `stt_mode: hybrid` seçilmişse
    ses bulut ucuna gider (`cloud_stt_transfer` denetimiyle), değilse yerel Whisper kullanılır.
    Yeni bir izin ya da atlama yolu yoktur. `content_kind` yalnız denetim etiketidir (Telegram
    sesi `telegram_audio`, ADR 0064). Hazırlık engelleyicidir, `to_thread` ile yapılır."""
    settings = runtime.settings
    voice = alias_hotword_config(settings)
    if settings.voice.stt_mode == "hybrid":
        endpoint_id = settings.voice.stt_cloud_endpoint
        return OpenAICompatibleTranscriber(
            voice, endpoint_id, settings.endpoints[endpoint_id], runtime.audit,
            content_kind=content_kind,
        )
    recognizer = FasterWhisperRecognizer(voice)
    await _prepare(recognizer)
    return recognizer


async def _close_optional(resource: object) -> None:
    closer = getattr(resource, "close", None)
    if closer is not None:
        try:
            # WakeSpotter/SherpaLiveTranscriber.close eşzamanlıdır; ötekiler eşzamansız.
            if inspect.isawaitable(result := closer()):
                await result
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("Ses kaynağı kapatılamadı (%s).", type(error).__name__)


class PreparedResource(Protocol):
    def prepare(self) -> None: ...


async def _prepare(resource: PreparedResource) -> None:
    """to_thread iptali yüklemeyi durdurmaz; bitince kapat, yarışarak dispose etme."""
    task = asyncio.create_task(asyncio.to_thread(resource.prepare))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        def finish_and_close(done: asyncio.Task[None]) -> None:
            if done.cancelled():
                # Döngü kapanıyorsa çalışan thread'e yarışarak dokunulmaz.
                return
            error = done.exception()
            if error is not None:
                logger.debug("İptal edilen hazırlık başarısız (%s).", type(error).__name__)
            asyncio.create_task(_close_optional(resource))

        task.add_done_callback(finish_and_close)
        raise
    except (OSError, RuntimeError, ValueError, ImportError):
        await _close_optional(resource)
        raise
