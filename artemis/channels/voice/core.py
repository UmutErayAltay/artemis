"""Ses kanalı orkestrasyonu; ses adaptörleri araç veya onay kararı vermez."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing, suppress
from time import monotonic

from artemis.brain.agent_loop import RunResult
from artemis.channels.runner import CommandRunner
from artemis.channels.voice.audio_routing import (
    MAX_DIRECT_AUDIO_BYTES as MAX_DIRECT_AUDIO_BYTES,
)
from artemis.channels.voice.audio_routing import (
    accepts_kwarg,
    call_run_command,
    close_voice_resources,
    continuous_toggle,
    emit_private_tts_blocked,
    encode_direct_audio_wav,
    frame_input_level,
    is_remote_synthesizer,
    is_spoken_echo,
    play_speech_stream,
    ptt_command,
    speech_words,
    strip_spoken_prefix,
    synth_text_chunks,
    transcribe_cloud_command,
)
from artemis.channels.voice.audio_routing import (
    pcm_to_wav as pcm_to_wav,
)

# Sözleşmeler contracts.py'de; eski içe aktarma yolları için buradan yeniden dışa aktarılır.
from artemis.channels.voice.contracts import (
    AudioFrame as AudioFrame,
)
from artemis.channels.voice.contracts import (
    AudioSegment as AudioSegment,
)
from artemis.channels.voice.contracts import (
    AudioSegmenter as AudioSegmenter,
)
from artemis.channels.voice.contracts import (
    AudioSink as AudioSink,
)
from artemis.channels.voice.contracts import (
    LiveTranscriber as LiveTranscriber,
)
from artemis.channels.voice.contracts import (
    Microphone as Microphone,
)
from artemis.channels.voice.contracts import (
    PCMChunk as PCMChunk,
)
from artemis.channels.voice.contracts import (
    SpeechRecognizer as SpeechRecognizer,
)
from artemis.channels.voice.contracts import (
    SpeechSynthesizer as SpeechSynthesizer,
)
from artemis.channels.voice.contracts import (
    TurnDetector as TurnDetector,
)
from artemis.channels.voice.contracts import (
    VoiceActivityDetector as VoiceActivityDetector,
)
from artemis.channels.voice.contracts import (
    WakeSpotterLike as WakeSpotterLike,
)
from artemis.channels.voice.live_steps import (
    LIVE_BATCH_MS as LIVE_BATCH_MS,
)
from artemis.channels.voice.live_steps import (
    LIVE_EMIT_INTERVAL as LIVE_EMIT_INTERVAL,
)
from artemis.channels.voice.live_steps import (
    LiveStepsMixin,
)
from artemis.channels.voice.multimodal import maybe_capture_voice_screen
from artemis.channels.voice.speculative import SpeculativeStt
from artemis.channels.voice.streaming import SpeechStream
from artemis.channels.voice.telemetry import (
    CommandClass,
    Outcome,
    Stage,
    VoiceTimingEvent,
)
from artemis.channels.voice.wake import find_wake_word as find_wake_word
from artemis.channels.voice.wake import is_ghost_transcript as is_ghost_transcript
from artemis.config.settings import VoiceConfig
from artemis.config.work_profile import is_local_only_profile, should_mute_tts
from artemis.i18n import t
from artemis.safety.audit import AuditLog

logger = logging.getLogger(__name__)


# Kapı kararı en çok bu kadar uçuştaki beslemeyi bekler; eşleşme sözün başından bu kadar geç
# de olsa sayılır (uyandırma sözü bölütü başlatır).
WAKE_GATE_WAIT = 0.3
WAKE_HIT_SLACK = 0.5



class VoiceController(LiveStepsMixin):
    """Wake word, CommandRunner ve TTS arasındaki tek voice-channel sınırı."""

    def __init__(
        self,
        runner: CommandRunner,
        config: VoiceConfig,
        microphone: Microphone,
        vad: VoiceActivityDetector,
        recognizer: SpeechRecognizer,
        synthesizer: SpeechSynthesizer,
        sink: AudioSink,
        *,
        clock: Callable[[], float] = monotonic,
        on_first_pcm: Callable[[], None] | None = None,
        on_timing: Callable[[str, float], None] | None = None,
        on_event: Callable[[VoiceTimingEvent], None] | None = None,
        run_command: Callable[..., Awaitable[RunResult]] | None = None,
        stream_responses: bool = False,
        on_state: Callable[[str], None] | None = None,
        on_input_level: Callable[[int], None] | None = None,
        on_transcript: Callable[[str], None] | None = None,
        cloud_recognizer: SpeechRecognizer | None = None,
        live_transcriber: LiveTranscriber | None = None,
        on_live_text: Callable[[str], None] | None = None,
        turn_detector: TurnDetector | None = None,
        wake_spotter: WakeSpotterLike | None = None,
        should_use_cloud: Callable[[], bool] | None = None,
        local_synthesizer: SpeechSynthesizer | None = None,
        audit: AuditLog | None = None,
        muted: bool = False,
        profile_getter: Callable[[], str] | None = None,
        capture_screen: Callable[[], bytes | None] | None = None,
    ) -> None:
        self.runner = runner
        # ADR 0069: eşleşme tek başına uyandırmaz/çalıştırmaz; yalnız eşleşmesiz bölütler STT'ye
        # gitmeden elenir. Nihai uyandırma kararı hâlâ transkriptteki `find_wake_word`'dedir.
        self._wake_spotter = wake_spotter
        self._wake_buffer = bytearray()
        self._wake_task: asyncio.Task[bool] | None = None
        self._wake_heard_at = -math.inf
        # ADR 0047: canlı metin YALNIZ on_live_text'e gider; komut/uyandırma/yankı/söz kesme
        # hiçbir yerde okumaz.
        self._live = live_transcriber
        self.on_live_text = on_live_text or (lambda _text: None)
        self._live_buffer = bytearray()
        self._live_task: asyncio.Task[str] | None = None
        self._live_text = ""
        self._live_next_emit = 0.0
        self._live_stale = False
        # ADR 0079: yalnız komut olacak söz için; uyandırma/yankı kararları hep yerel metinle.
        self.cloud_recognizer = cloud_recognizer
        self._should_use_cloud = should_use_cloud
        self.config = config
        self.microphone = microphone
        self.segmenter = AudioSegmenter(config, vad, turn_detector)
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self._local_synthesizer = local_synthesizer
        self._audit = audit
        self.sink = sink
        self.clock = clock
        self.on_first_pcm = on_first_pcm or (lambda: None)
        self.on_timing = on_timing or (lambda _stage, _time: None)
        self.on_event = on_event
        self._utterance_sequence = 0
        self._generation_sequence = 0
        self._capturing_id: int | None = None
        # Controller ömrünün ilk görev turu soğuk sayılır (model/önbellek ısınmamış olabilir).
        self._task_turns = 0
        # Hoparlöre yazım sürerken True; söz kesme stop olay çiftini yalnız o zaman üretir.
        self._output_active = False
        # Masaüstü köprüsü komutu kendi istek modelinden (request_id, tek aktif istek)
        # geçirir; varsayılan runner.run CLI/bench davranışını değiştirmez.
        self.run_command = run_command if run_command is not None else runner.run
        self._run_accepts_audio = accepts_kwarg(self.run_command, "audio_wav")
        self._run_accepts_image = accepts_kwarg(self.run_command, "image_png")
        # ADR 0043: True ise run_command ``stream=`` kabul etmelidir (runner.run ve masaüstü
        # köprüsü eder); sağlayıcı akmıyorsa yanıt yine tamamı gelince söylenir.
        self.stream_responses = stream_responses
        # on_state yalnız durum bildirir; arbitrary callback'in hatasını burada
        # yakalamak kör except olurdu, bu yüzden çağıran fırlatmamalıdır.
        self.on_state = on_state or (lambda _state: None)
        self.on_input_level = on_input_level or (lambda _level: None)
        self.on_transcript = on_transcript or (lambda _text: None)
        self._next_level_update = self.clock()
        self._awaiting_followup = False
        self._followup_until = 0.0
        self._active: asyncio.Task[None] | None = None
        self._barge_cancelled = False
        self._closed = False
        self._stop = asyncio.Event()
        self._partial_task: asyncio.Task[str] | None = None
        self._partial_frames = 0
        # İki bekleyen segment + tek çalışan çözüm; ham ses diske yazılmaz.
        self._segments: asyncio.Queue[tuple[AudioSegment, int, int, float]] = asyncio.Queue(2)
        self._decoder: asyncio.Task[None] | None = None
        self._decode_busy = False
        self._capture_epoch = 0
        self._spec = SpeculativeStt.build(config, recognizer, self._wake_gate_blocks)
        self._seen_drops = 0
        self._playback: asyncio.Task[None] | None = None
        self._playback_cancelled: asyncio.Event | None = None
        self._spoken_text = ""
        self._spoken_until = 0.0
        self._output_requires_wake = False
        # Uyandırma karşılıkları metne göre bir kez sentezlenir, sonra aynı PCM çalınır.
        self._ack_cache: dict[str, list[PCMChunk]] = {}
        # Çalışma anı kipi; config yalnız başlangıç değeridir (ADR 0024).
        self.continuous = config.continuous_mode
        self._ptt_active = False
        self._ptt_uids: set[int] = set()
        self._muted = muted
        self._profile_getter = profile_getter
        self._capture_screen = capture_screen

    async def run(self) -> None:
        """Mikrofon yalnız bu açıkça çağrılan döngü süresince tüketilir."""
        self._decoder = asyncio.create_task(self._decode_segments())
        try:
            frames = self.microphone.frames().__aiter__()
            self._emit_state("listening")
            if self.continuous:
                self._emit_state("mode:on")
            while not self._closed:

                async def next_audio_frame() -> AudioFrame:
                    return await anext(frames)

                next_frame: asyncio.Task[AudioFrame] = asyncio.create_task(next_audio_frame())
                stop_wait = asyncio.create_task(self._stop.wait())
                done, pending = await asyncio.wait(
                    {next_frame, stop_wait, self._decoder}, return_when=asyncio.FIRST_COMPLETED
                )
                pending.discard(self._decoder)
                for task in pending:
                    task.cancel()
                for task in pending:
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await task
                if self._decoder in done:
                    self._decoder.result()
                if stop_wait in done and self._stop.is_set():
                    break
                try:
                    frame = next_frame.result()
                except StopAsyncIteration:
                    if self._active is not None and not self._active.done():
                        self.runner.cancel_current()
                        self._active.cancel()
                        self._barge_cancelled = True
                    break
                drops = int(getattr(self.microphone, "dropped_frames", 0))
                if drops > self._seen_drops:
                    self.on_transcript(
                        t("Mikrofon yoğun: {dropped} ses karesi kayboldu.").format(
                            dropped=drops - self._seen_drops
                        )
                    )
                    self._seen_drops = drops
                    self.on_timing("audio_dropped_frames", float(drops))
                    self._event(0, "capture_health", reason=f"dropped_frames={drops}")
                if self.clock() >= self._next_level_update:
                    self.on_input_level(frame_input_level(frame.pcm))
                    self._next_level_update = self.clock() + 0.2
                if self._awaiting_followup and self.clock() > self._followup_until:
                    self._awaiting_followup = False
                    self._emit_state("listening")
                was_active = self.segmenter.active
                utterance = self.segmenter.push(frame.pcm, frame.timestamp, frame.raw_pcm)
                if not was_active and self.segmenter.active:
                    self._capturing_id = self._new_utterance(frame.timestamp)
                if self._capturing_id is not None and self.segmenter.discarding:
                    self._event(
                        self._capturing_id, "terminal", outcome="rejected", reason="max_utterance"
                    )
                    self._capturing_id = None
                # VAD gürültüyü/yankıyı da konuşma sanabilir. Oynatma yalnız nihai
                # transcript kabul edilince _handle_utterance içinde kesilir (ADR 0040).
                self._live_step(frame, was_active)
                self._wake_step(frame)
                if self._spec is not None:
                    self._spec.feed(
                        self.segmenter, utterance, self._capturing_id, self._capture_epoch
                    )
                if self.segmenter.active and self.config.stt_partial_enabled:
                    self._partial_frames += 1
                    if self._partial_frames >= max(1, 1000 // self.config.frame_ms):
                        self._partial_frames = 0
                        self._start_partial_decode()
                if utterance is not None:
                    await self._queue_segment(utterance)
            if not self._stop.is_set():
                await self._drain_segments()
                await self._settle_active()
        finally:
            await self.close()

    async def _drain_segments(self) -> None:
        # EOF ile aynı anda decoder hata verirse join sonsuza dek beklememeli.
        joined = asyncio.create_task(self._segments.join())
        try:
            if self._decoder is not None:
                done, _pending = await asyncio.wait(
                    {joined, self._decoder},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if self._decoder in done:
                    self._decoder.result()
            await joined
        finally:
            if not joined.done():
                joined.cancel()
                with suppress(asyncio.CancelledError):
                    await joined

    def _wake_gate_blocks(self, speech_start: float) -> bool:
        """ADR 0069: eldeki kanıtla bölüt STT'ye gidemez mi (düşürme + spekülasyon)."""
        if (
            self._wake_spotter is None
            or self.continuous
            or self._awaiting_followup
            or self.clock() <= self._followup_until
            or (self.config.barge_in_without_wake and self._output_active)
        ):
            return False
        return self._wake_heard_at < speech_start - WAKE_HIT_SLACK

    async def _wake_gate_drops(self, segment: AudioSegment) -> bool:
        """ADR 0069: KWS açıkken uyandırma işareti olmayan bölüt STT'ye gitmez."""
        if not self._wake_gate_blocks(segment.speech_start):
            return False
        if (task := self._wake_task) is not None:
            # Besleme en çok bir toplu iş geride kalır; sonucu kısa bekle (iptal etmeden).
            await asyncio.wait({task}, timeout=WAKE_GATE_WAIT)
        return self._wake_gate_blocks(segment.speech_start)

    def _submit_segment(self, segment: AudioSegment, uid: int) -> bool:
        self._event(uid, "stt_queued")
        try:
            self._segments.put_nowait((segment, uid, self._capture_epoch, self.clock()))
        except asyncio.QueueFull:
            self._ptt_uids.discard(uid)
            self.on_transcript(t("Ses tanıma yoğun; son söz alınamadı. Lütfen tekrar söyle."))
            self._event(uid, "terminal", outcome="rejected", reason="stt_queue_full")
            self._forget_spec(uid)
            return False
        self.on_timing("stt_queue_depth", float(self._segments.qsize()))
        return True

    async def _queue_segment(self, segment: AudioSegment, *, ptt: bool = False) -> None:
        uid = self._capturing_id
        self._capturing_id = None
        if uid is None:
            uid = self._new_utterance(segment.speech_start)
        self.on_timing("speech_start", segment.speech_start)
        self.on_timing("speech_end", segment.speech_end)
        self._event(uid, "speech_end", timestamp=segment.speech_end)
        self._event(uid, "endpoint")
        if not ptt and await self._wake_gate_drops(segment):
            self._event(uid, "terminal", outcome="rejected", reason="no_wake_kws")
            self._forget_spec(uid)
            return
        if ptt:
            self._ptt_uids.add(uid)
        self._submit_segment(segment, uid)

    def ptt_press(self) -> None:
        """Bas-konuş başlangıcı: etkin TTS varsa keser ve segmenteri bas-konuş kipine alır."""
        if self._closed or self._stop.is_set() or self._ptt_active:
            return
        self._ptt_active = True
        if self._active is not None and not self._active.done():
            self._barge_cancelled = True
            self.runner.cancel_current()
            self._active.cancel()
        self.segmenter.begin_ptt(self.clock())
        if self._capturing_id is None:
            self._capturing_id = self._new_utterance(self.clock())
        self._emit_state("listening")

    def ptt_release(self) -> AudioSegment | None:
        """Bas-konuş bitişi: biriken sesi beklemeden tek komut olarak sıraya alır."""
        if not self._ptt_active or self._closed:
            return None
        self._ptt_active = False
        segment = self.segmenter.flush_active(self.clock())
        if segment is None or not segment.pcm:
            self._capturing_id = None
            self._live_reset()
            return None
        self._live_reset()
        uid = self._capturing_id
        self._capturing_id = None
        if uid is None:
            uid = self._new_utterance(segment.speech_start)
        self.on_timing("speech_start", segment.speech_start)
        self.on_timing("speech_end", segment.speech_end)
        self._event(uid, "speech_end", timestamp=segment.speech_end)
        self._event(uid, "endpoint")
        self._ptt_uids.add(uid)
        if not self._submit_segment(segment, uid):
            return None
        return segment

    async def drain_and_stop(self) -> None:
        """Bekleyen bas-konuş bölütü ve yanıtı bitince denetleyiciyi temizce durdurur."""
        await self._drain_segments()
        await self._settle_active()
        if not self._ptt_active and not self._closed:
            self.request_stop()

    async def _decode_segments(self) -> None:
        while True:
            segment, uid, epoch, queued_at = await self._segments.get()
            self._decode_busy = True
            try:
                self.on_timing("stt_queue_wait_seconds", self.clock() - queued_at)
                await self._handle_utterance(segment, uid=uid, epoch=epoch)
            finally:
                self._decode_busy = False
                self._segments.task_done()

    def _forget_spec(self, uid: int) -> None:
        if self._spec is not None:
            self._spec.forget(uid)

    def _invalidate_segments(self) -> None:
        self._capture_epoch += 1
        if self._spec is not None:
            self._spec.cancel_all()
        # Onay sırasındaki/eski ses uyandırma kanıtı olarak taşınmaz (CLAUDE.md ses kuralı).
        self._wake_heard_at = -math.inf
        self._wake_buffer.clear()
        self._ptt_uids.clear()
        while not self._segments.empty():
            _segment, uid, _epoch, _at = self._segments.get_nowait()
            self._segments.task_done()
            self._event(uid, "terminal", outcome="cancelled", reason="capture_invalidated")

    def _new_utterance(self, timestamp: float) -> int:
        self._utterance_sequence += 1
        self._event(self._utterance_sequence, "speech_start", timestamp=timestamp)
        return self._utterance_sequence

    def _event(
        self,
        utterance_id: int,
        stage: Stage,
        *,
        generation_id: int | None = None,
        timestamp: float | None = None,
        outcome: Outcome | None = None,
        reason: str | None = None,
        ack: bool = False,
        command_class: CommandClass | None = None,
        warm: bool | None = None,
    ) -> None:
        if self.on_event is not None:
            self.on_event(
                VoiceTimingEvent(
                    utterance_id,
                    generation_id,
                    stage,
                    self.clock() if timestamp is None else timestamp,
                    "ack" if ack else "task",
                    outcome,
                    reason,
                    command_class,
                    warm,
                )
            )

    def _discard_capture(self, reason: str) -> None:
        if self._capturing_id is not None:
            self._event(self._capturing_id, "terminal", outcome="cancelled", reason=reason)
            self._capturing_id = None

    def request_stop(self) -> None:
        self._stop.set()

    def _emit_state(self, state: str) -> None:
        """Yalnız listening/thinking/speaking/off; ekran durumu buradan okur."""
        self.on_state(state)

    async def _handle_utterance(
        self,
        segment: AudioSegment,
        *,
        uid: int | None = None,
        epoch: int | None = None,
        ptt: bool = False,
    ) -> None:
        # Kimlik, async yanıtın ömrünce korunur; reddedilen sonraki söz bunu değiştirmez.
        if uid is None:
            uid = self._capturing_id
            self._capturing_id = None
        if uid is None:
            uid = self._new_utterance(segment.speech_start)
        is_ptt = ptt or uid in self._ptt_uids
        self._ptt_uids.discard(uid)
        if epoch is None:
            epoch = self._capture_epoch
            self.on_timing("speech_start", segment.speech_start)
            self.on_timing("speech_end", segment.speech_end)
            self._event(uid, "speech_end", timestamp=segment.speech_end)
            self._event(uid, "endpoint")
        try:
            if self._partial_task is not None:
                self._partial_task.cancel()
            await self._settle_partial()
            self._event(uid, "stt_start")
            stt_started = self.clock()
            early = await self._spec.result(uid, epoch) if self._spec is not None else None
            if self._spec is not None:
                self.on_timing("stt_speculative_hit", 0.0 if early is None else 1.0)
            if early is None:
                early = await self.recognizer.transcribe(segment.pcm)
            transcript = early.strip()
        except asyncio.CancelledError:
            self._event(uid, "terminal", outcome="cancelled", reason="stt_cancelled")
            raise
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("Ses tanıma başarısız (%s).", type(error).__name__)
            self._event(uid, "terminal", outcome="error", reason="stt_error")
            return
        self.on_timing("stt_duration_seconds", self.clock() - stt_started)
        if self._closed or self._stop.is_set() or epoch != self._capture_epoch:
            self._event(uid, "terminal", outcome="cancelled", reason="capture_invalidated")
            return
        self._event(uid, "stt_final")
        if not transcript:
            self.on_transcript(t("Net konuşma algılanmadı; bu ses komut olarak kullanılmadı."))
            self._event(uid, "terminal", outcome="rejected", reason="empty_transcript")
            return
        if self._is_spoken_echo(transcript, segment.speech_start):
            self.on_transcript(t("Asistan sesinin yankısı yok sayıldı."))
            self._event(uid, "terminal", outcome="rejected", reason="echo")
            return
        transcript = self._strip_spoken_prefix(transcript, segment.speech_start)
        if (
            not is_ptt
            and self.config.require_wake_during_output
            and self._output_requires_wake
            and self._spoken_text
            and segment.speech_start <= self._spoken_until
            and find_wake_word(transcript, self.config.wake_word) is None
        ):
            # ADR 0042: hoparlöre yazım sürerken doğrulanmış söz susturur ama komut olmaz;
            # kısa patlama (öksürük, kapı) STT'den geçse bile barge_in_ms altında kalır.
            if (
                self.config.barge_in_without_wake
                and self._output_active
                and (segment.speech_end - segment.speech_start) * 1000 >= self.config.barge_in_ms
            ):
                self.on_transcript(t("Söz kesildi; dinliyorum."))
                await self._interrupt_output(uid)
                # Kullanıcı bilerek susturdu; takip sözü yalnız followup_listening açıkken
                # wake istemeden kabul edilir (varsayılan kapalı, Umut 2026-11-20 kararı).
                self._output_requires_wake = False
                if self.config.followup_listening:
                    self._followup_until = self.clock() + self.config.followup_seconds
                    self._awaiting_followup = True
                    self._emit_state("awaiting_command")
                else:
                    self._emit_state("listening")
                self._event(uid, "terminal", outcome="interrupted", reason="barge_in")
                return
            self.on_transcript(t("Ses yok sayıldı; söz kesmek için Artemis diyerek başla."))
            self._event(uid, "terminal", outcome="rejected", reason="output_requires_wake")
            return
        self.on_transcript(transcript)
        self.on_timing("stt_final", self.clock())
        toggle = None if is_ptt else self._continuous_toggle(transcript)
        if is_ptt:
            command: str | None = self._ptt_command(transcript)
        else:
            command = None if toggle is not None else self._wake_command(transcript)
        if toggle is None and command is None:
            self._event(uid, "terminal", outcome="rejected", reason="no_wake")
            return
        audio_wav: bytes | None = None
        if (
            command
            and self.config.direct_audio_input
            and not self._is_private_profile()
            and segment.pcm
        ):
            # Opt-in doğrudan ses modu: ham ses yalnız beyin modeline gider, ikinci bulut STT
            # yüklemesi yapılmaz; çok uzun kayıtlar boyut sınırında güvenli biçimde metne düşer.
            audio_wav = encode_direct_audio_wav(segment.pcm, self.config.sample_rate)
        elif (
            command
            and not self.config.direct_audio_input
            and not self._is_private_profile()
            and self.cloud_recognizer is not None
            and (self._should_use_cloud is None or self._should_use_cloud())
        ):
            # Karar yerel metinle bir kez verildi (_wake_command takip durumunu değiştirir);
            # burada yalnız komut metni bulut sürümüyle değişir.
            command = await self._cloud_command(uid, segment, command)
        await self._interrupt_output(uid)
        if self._closed or self._stop.is_set() or epoch != self._capture_epoch:
            self._event(uid, "terminal", outcome="cancelled", reason="capture_invalidated")
            return
        self._generation_sequence += 1
        gid = self._generation_sequence
        if toggle is not None:
            self.set_continuous(toggle)
            text = t("Kesintisiz mod açık.") if toggle else t("Kesintisiz mod kapalı.")
            self._active = asyncio.create_task(self._acknowledge_wake(text, uid=uid, gid=gid))
        elif not command:
            self._active = asyncio.create_task(self._acknowledge_wake(uid=uid, gid=gid))
        else:
            self._barge_cancelled = False
            capture = self._capture_screen
            image_png: bytes | None = None
            if (
                audio_wav is not None and capture is not None and not self._is_private_profile()
                and self.config.multimodal_screen_context and self.config.direct_audio_input
            ):
                # Ekran yakalama GDI/BitBlt içerdiğinden asyncio olay döngüsünü bloklamaz.
                image_png = await asyncio.to_thread(
                    maybe_capture_voice_screen, self.config,
                    is_private=False, has_audio=True, capture_screen=capture,
                )
            coro = self._run_and_speak(
                command, uid=uid, gid=gid, audio_wav=audio_wav, image_png=image_png
            )
            self._active = asyncio.create_task(coro)

    async def _cloud_command(self, uid: int, segment: AudioSegment, local_command: str) -> str:
        """Komut metnini bulut tanıyıcıdan alır."""
        assert self.cloud_recognizer is not None
        return await transcribe_cloud_command(
            self.cloud_recognizer,
            segment.pcm,
            local_command,
            self.config.wake_word,
            on_cancelled=lambda: self._event(
                uid, "terminal", outcome="cancelled", reason="stt_cancelled"
            ),
            on_fallback=lambda: self._event(
                uid, "stt_cloud_fallback", reason="stt_cloud_fallback"
            ),
        )

    async def _interrupt_output(self, uid: int) -> None:
        """Etkin turu ve oynatmayı durdurur; temizlik bitmeden yeni tur başlamaz.

        Stop çifti kesen sözün kimliğinde tutulur (generation'sız): böylece hem stop
        isteğinden hem kullanıcı sesinin başından oynatmanın durmasına süre ölçülür.
        """
        interrupting = self._output_active
        if self._active is not None and not self._active.done():
            if interrupting:
                self._event(uid, "stop_requested")
            self._barge_cancelled = True
            self.runner.cancel_current()
            self._active.cancel()
        try:
            await self._settle_active()
        except asyncio.CancelledError:
            self._event(uid, "terminal", outcome="cancelled", reason="waiting_previous")
            raise
        if interrupting:
            # Sink'e yazım burada bitmiştir; aygıt tamponundaki son blok ölçülmez.
            self._event(uid, "playback_stopped")

    @staticmethod
    def _speech_words(text: str) -> list[str]:
        return speech_words(text)

    def set_continuous(self, enabled: bool) -> None:
        """Kesintisiz kipi çalışırken değiştirir; arayüze bildirir."""
        self.continuous = enabled
        self._emit_state("mode:on" if enabled else "mode:off")

    def set_muted(self, muted: bool) -> None:
        """Konuşma çıkışını (toplantı profili vb.) sessize alır ve aktif oynatmayı keser."""
        self._muted = bool(muted)
        if self._muted and self._playback_cancelled is not None:
            self._playback_cancelled.set()
        if self._muted and self._playback is not None and not self._playback.done():
            self._playback.cancel()

    def _is_muted(self) -> bool:
        return self._muted or (
            self._profile_getter is not None and should_mute_tts(self._profile_getter())
        )

    def _is_private_profile(self) -> bool:
        return self._profile_getter is not None and is_local_only_profile(self._profile_getter())

    def _continuous_toggle(self, transcript: str) -> bool | None:
        """Yalnız (uyandırma sözcüğü + ) kip cümlesinden ibaret söz; LLM'e gitmez."""
        return continuous_toggle(
            transcript,
            self.config.wake_word,
            awaiting_followup=self._awaiting_followup,
            followup_active=self.clock() <= self._followup_until,
        )

    def _is_spoken_echo(self, text: str, speech_start: float) -> bool:
        return is_spoken_echo(text, speech_start, self._spoken_text, self._spoken_until)

    def _strip_spoken_prefix(self, text: str, speech_start: float) -> str:
        """Söz kendi sesimizle üst üste bindiyse baştaki o sözcükleri atar."""
        return strip_spoken_prefix(text, speech_start, self._spoken_text, self._spoken_until)

    def _mark_spoken(self, text: str, *, requires_wake: bool = True) -> None:
        self._spoken_text = text
        self._spoken_until = math.inf
        self._output_requires_wake = requires_wake

    async def _acknowledge_wake(
        self,
        text: str | None = None,
        *,
        uid: int = 0,
        gid: int | None = None,
    ) -> None:
        """Tek başına uyandırmada (ve kip değişiminde) LLM çağırmadan kısa karşılık verir.

        Mikrofon durdurulmaz: kullanıcı "Artemis" deyip duraksamadan sürdürünce komutun başı
        "Dinliyorum" sırasında kayboluyordu. Kendi sesimiz yankı filtresiyle (_is_spoken_echo)
        elenir, başa yapışırsa _strip_spoken_prefix ayıklar.
        """
        text = t(self.config.wake_ack_text if text is None else text)
        cancelled = asyncio.Event()
        self._emit_state("awaiting_command")
        played = False
        outcome: Outcome = "no_audio"

        def first_pcm() -> None:
            nonlocal played
            if not played:
                self._event(uid, "first_pcm", generation_id=gid, ack=True)
            played = True
            self._mark_spoken(text, requires_wake=False)

        try:
            muted = self._is_muted()
            self._output_active = not muted
            if not muted:
                chunks = self._timed_chunks(
                    text, cancelled, uid, gid, ack=True, private=self._is_private_profile()
                )
                async with aclosing(chunks) as stream:
                    await self.sink.play(stream, cancelled, first_pcm)
            outcome = "ack" if played else "no_audio"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except (OSError, RuntimeError, ValueError) as error:
            outcome = "error"
            logger.warning("Uyandırma sesi üretilemedi (%s).", type(error).__name__)
        finally:
            self._output_active = False
            self._event(uid, "terminal", generation_id=gid, outcome=outcome, ack=True)
            cancelled.set()
            self._spoken_until = self.clock() + 1.2
            if not self._closed:
                self._followup_until = self.clock() + self.config.followup_seconds
                self._awaiting_followup = True
                self._emit_state("awaiting_command")

    def _clear_followup(self) -> None:
        # Takip tüketildiğinde run() döngüsünün meşgulken "listening" yaymaması için
        # bitiş zamanı ile bayrak aynı noktada sıfırlanır.
        self._followup_until = 0.0
        self._awaiting_followup = False

    def _ptt_command(self, transcript: str) -> str:
        """Bas-konuş bölütü için uyandırma adını isteğe bağlı ayıklar; continuous'u değiştirmez."""
        return ptt_command(transcript, self.config.wake_word)

    def _wake_command(self, transcript: str) -> str | None:
        """None: wake word yok/expired; boş dize: wake word tek başına."""
        span = find_wake_word(transcript, self.config.wake_word)
        if span is not None:
            tail = transcript[span[1] :].lstrip(" \t,.;:!?-")
            # Uydurma altyazı kuyruğu ("Artemis. Abone olmayı unutmayın.") tek başına uyandırmadır.
            if not tail.strip(" \t,.;:!?-") or is_ghost_transcript(tail):
                self._followup_until = self.clock() + self.config.followup_seconds
                return ""
            self._clear_followup()
            return tail
        if self.clock() <= self._followup_until:
            self._clear_followup()
            return transcript
        self._clear_followup()
        # Kesintisiz kipte wake word'süz nihai söz komuttur (çıktı sırasında
        # _handle_utterance zaten wake word ister; yankı da öncesinde elenir).
        return transcript if self.continuous else None

    @staticmethod
    def _is_private_result(result: RunResult) -> bool:
        return bool(result.record is not None and result.record.answer_private)

    def _is_remote_synthesizer(self) -> bool:
        return is_remote_synthesizer(self.synthesizer, self.config.tts_backend)

    def _emit_private_tts_blocked(self) -> None:
        emit_private_tts_blocked(self._audit, self.synthesizer, self.config)

    def _synth_stream(
        self, text: str, cancelled: asyncio.Event, *, private: bool = False,
        record_block: bool = True,
    ) -> AsyncGenerator[PCMChunk]:
        """Gizli yanıtları bulut/Edge TTS'ten kesen tek ses kapısı."""
        return synth_text_chunks(
            text,
            cancelled,
            self.synthesizer,
            self._local_synthesizer,
            self.config,
            private=private,
            record_block=record_block,
            audit=self._audit,
            on_transcript=self.on_transcript,
        )

    async def _timed_chunks(
        self,
        text: str,
        cancelled: asyncio.Event,
        uid: int,
        gid: int | None,
        *,
        ack: bool = False,
        private: bool = False,
    ) -> AsyncGenerator[PCMChunk]:
        first = True
        cached = self._ack_cache.get(text) if ack and not private else None
        if cached is not None:
            for chunk in cached:
                if first and chunk.pcm:
                    first = False
                    self._event(uid, "tts_first_chunk", generation_id=gid, ack=ack)
                yield chunk
            return
        recorded: list[PCMChunk] = []
        # Adaptörün süreç/ağ finally bloğu, sink erken dönse de kapanabilmeli.
        chunks = self._synth_stream(text, cancelled, private=private)
        try:
            async for chunk in chunks:
                if first and chunk.pcm:
                    first = False
                    self._event(uid, "tts_first_chunk", generation_id=gid, ack=ack)
                recorded.append(chunk)
                yield chunk
            # Yalnız sonuna dek üretilen ses saklanır; kesilen akış yarım kalıp önbelleğe girmez.
            if ack and not private and recorded and not cancelled.is_set():
                self._ack_cache[text] = recorded
        finally:
            closer = getattr(chunks, "aclose", None)
            if closer is not None:
                await closer()

    async def _play_response(
        self, text: str, cancelled: asyncio.Event, uid: int, gid: int | None,
        first_pcm: Callable[[], None], *, private: bool = False,
    ) -> None:
        stream = self._timed_chunks(text, cancelled, uid, gid, private=private)
        async with aclosing(stream) as chunks:
            await self.sink.play(chunks, cancelled, first_pcm)

    def _record_result(self, uid: int, gid: int | None, result: RunResult) -> None:
        self.on_timing("model_complete", self.clock())
        # Aşamalar AgentLoop'un monotonik saatindendir; sahte saatli testte sıra
        # bozulursa rapor örneği geçersiz sayar, sessizce düzeltmez.
        for phase in result.phases:
            self._event(uid, phase.stage, generation_id=gid, timestamp=phase.timestamp)
        command_class: CommandClass | None = None
        if result.turn_id:
            command_class = "tool" if result.tool_calls else "chat"
        self._event(uid, "model_complete", generation_id=gid, command_class=command_class)

    async def _play_stream(
        self,
        speech: SpeechStream,
        cancelled: asyncio.Event,
        uid: int,
        gid: int | None,
        first_pcm: Callable[[], None],
        *,
        privacy_ready: asyncio.Event | None = None,
        is_private: Callable[[], bool] | None = None,
    ) -> None:
        """Cümleleri sırayla sentezler; hepsi tek sink akışında çalınır (cihaz bir kez açılır)."""

        def update_spoken() -> None:
            if self._output_active:
                self._spoken_text = " ".join(speech.spoken)

        await play_speech_stream(
            self.sink,
            speech,
            cancelled,
            first_pcm,
            synth_stream=self._synth_stream,
            on_first_chunk=lambda: self._event(uid, "tts_first_chunk", generation_id=gid),
            on_sentence=update_spoken,
            should_stop_private=lambda priv: (
                priv and self._is_remote_synthesizer() and self._local_synthesizer is None
            ),
            privacy_ready=privacy_ready,
            is_private=is_private,
        )

    def _call_run_command(
        self,
        command: str,
        *,
        stream: SpeechStream | None = None,
        audio_wav: bytes | None = None,
        image_png: bytes | None = None,
    ) -> Awaitable[RunResult]:
        return call_run_command(
            self.run_command,
            command,
            stream=stream,
            audio_wav=audio_wav,
            accepts_audio=self._run_accepts_audio,
            image_png=image_png,
            accepts_image=self._run_accepts_image,
        )

    async def _run_and_speak_streamed(
        self,
        command: str,
        uid: int,
        gid: int | None,
        *,
        audio_wav: bytes | None = None,
        image_png: bytes | None = None,
    ) -> None:
        """ADR 0043: model yazarken cümleler söylenir; tek ``generation`` olayı hepsini durdurur."""
        generation = asyncio.Event()
        speech = SpeechStream()
        played = False
        outcome: Outcome = "no_audio"
        playback: asyncio.Task[None] | None = None

        def first_pcm() -> None:
            nonlocal played
            if not played:
                # Hoparlöre ilk yazım: söz kesme (ADR 0042) ve yankı süzgeci ancak şimdi devrede.
                self._mark_spoken(" ".join(speech.spoken))
                self._output_active = True
                self._emit_state("speaking")
                self.on_timing("first_pcm", self.clock())
                self._event(uid, "first_pcm", generation_id=gid)
                self.on_first_pcm()
            played = True

        try:
            self._emit_state("thinking")
            self._task_turns += 1
            self._event(uid, "command_start", generation_id=gid, warm=self._task_turns > 1)
            privacy_ready = asyncio.Event()
            turn_private = False
            if not self._is_remote_synthesizer():
                privacy_ready.set()
            muted = self._is_muted()
            if not muted:
                self._playback_cancelled = generation
                playback = asyncio.create_task(
                    self._play_stream(
                        speech,
                        generation,
                        uid,
                        gid,
                        first_pcm,
                        privacy_ready=privacy_ready,
                        is_private=lambda: turn_private,
                    )
                )
                self._playback = playback
            result = await self._call_run_command(
                command,
                stream=None if muted else speech,
                audio_wav=audio_wav,
                image_png=image_png,
            )
            self._record_result(uid, gid, result)
            turn_private = self._is_private_result(result) or self._is_private_profile()
            speech.finish(result.text)
            privacy_ready.set()
            if playback is not None:
                try:
                    await playback
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if not generation.is_set() or (current is not None and current.cancelling()):
                        raise
            if played and self.config.followup_listening:
                self._followup_until = self.clock() + self.config.followup_seconds
            outcome = "completed" if played else "no_audio"
        except asyncio.CancelledError:
            outcome = "cancelled"
            generation.set()
            raise
        except (OSError, RuntimeError, ValueError) as error:
            outcome = "error"
            logger.warning("Ses yanıtı başarısız (%s).", type(error).__name__)
        finally:
            # Kuyrukta kalan cümleler yeni tura geçmez: akış kapanır, oynatma beklenir.
            speech.close()
            if playback is not None and not playback.done():
                if outcome != "completed":
                    generation.set()
                    playback.cancel()
                with suppress(asyncio.CancelledError):
                    await playback
            self._output_active = False
            self._playback = None
            self._playback_cancelled = None
            if played:
                self._spoken_until = self.clock() + 1.2
            self._event(uid, "terminal", generation_id=gid, outcome=outcome)
            if not self._closed:
                self._emit_state("listening")

    async def _run_and_speak(
        self,
        command: str,
        *,
        uid: int = 0,
        gid: int | None = None,
        audio_wav: bytes | None = None,
        image_png: bytes | None = None,
    ) -> None:
        if self.stream_responses:
            await self._run_and_speak_streamed(
                command, uid, gid, audio_wav=audio_wav, image_png=image_png
            )
            return
        generation = asyncio.Event()
        played = False
        outcome: Outcome = "no_audio"
        try:
            self._emit_state("thinking")
            self._task_turns += 1
            self._event(uid, "command_start", generation_id=gid, warm=self._task_turns > 1)
            result = await self._call_run_command(
                command, audio_wav=audio_wav, image_png=image_png
            )
            self._record_result(uid, gid, result)
            private = self._is_private_result(result) or self._is_private_profile()
            if result.text.strip() and not generation.is_set() and not self._is_muted():

                def first_pcm() -> None:
                    nonlocal played
                    self._mark_spoken(result.text)
                    if not played:
                        self.on_timing("first_pcm", self.clock())
                        self._event(uid, "first_pcm", generation_id=gid)
                        self.on_first_pcm()
                    played = True

                blocked_silent = (
                    private and self._is_remote_synthesizer() and self._local_synthesizer is None
                )
                if not blocked_silent:
                    self._emit_state("speaking")
                self._playback_cancelled = generation
                self._output_active = not blocked_silent
                self._playback = asyncio.create_task(
                    self._play_response(
                        result.text, generation, uid, gid, first_pcm, private=private
                    )
                )
                try:
                    await self._playback
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if not generation.is_set() or (current is not None and current.cancelling()):
                        raise
                finally:
                    self._output_active = False
                    self._playback = None
                    self._playback_cancelled = None
                    self._spoken_until = self.clock() + 1.2
                if self.config.followup_listening:
                    self._followup_until = self.clock() + self.config.followup_seconds
            outcome = "completed" if played else "no_audio"
        except asyncio.CancelledError:
            outcome = "cancelled"
            generation.set()
            raise
        except (OSError, RuntimeError, ValueError) as error:
            outcome = "error"
            logger.warning("Ses yanıtı başarısız (%s).", type(error).__name__)
        finally:
            self._event(uid, "terminal", generation_id=gid, outcome=outcome)
            if not self._closed:
                self._emit_state("listening")

    async def _settle_active(self) -> None:
        active = self._active
        if active is None:
            return
        if not active.done() and self._barge_cancelled:
            active.cancel()
        with suppress(asyncio.CancelledError):
            await active
        self._active = None
        self._barge_cancelled = False

    def _reset_for_approval(self, *, pause_mic: bool = False) -> None:
        self._invalidate_segments()
        (self.microphone.pause if pause_mic else self.microphone.discard_pending)()
        self._discard_capture("approval")
        self.segmenter.reset()
        self._live_reset()

    def discard_during_approval(self) -> None:
        """Senkron terminal onayı dönüşünde eski ses kuyruğunu ve segmenti sıfırlar."""
        self._reset_for_approval()

    def pause_for_approval(self) -> None:
        self._ptt_active = False
        self._reset_for_approval(pause_mic=True)

    def resume_after_approval(self) -> None:
        self._reset_for_approval()
        self.microphone.resume()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._invalidate_segments()
        if self._decoder is not None:
            self._decoder.cancel()
            with suppress(asyncio.CancelledError):
                await self._decoder
        self._discard_capture("capture_stopped")
        if self._active is not None and not self._active.done():
            self.runner.cancel_current()
            self._active.cancel()
        await self._settle_active()
        await self._settle_partial()
        if self._live_task is not None:
            with suppress(asyncio.CancelledError, OSError, RuntimeError, ValueError):
                await self._live_task
        if self._live is not None:
            self._live.close()
            self._live = None
        if self._wake_task is not None:
            with suppress(asyncio.CancelledError, OSError, RuntimeError, ValueError):
                await self._wake_task
        if self._wake_spotter is not None:
            self._wake_spotter.close()
            self._wake_spotter = None
        closer = getattr(self.recognizer, "close", None)
        if callable(closer):
            closer()  # oyun modu izleyici iş parçacığı; düz tanıyıcıda yok
        await close_voice_resources(
            self.microphone, self.sink, self.synthesizer, self._local_synthesizer
        )
        # Mikrofon gerçekten kapandıktan sonra bildirilir; ekran "açık" demeye devam etmez.
        self._emit_state("off")
