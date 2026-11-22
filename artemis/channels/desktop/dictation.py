"""Dikte kipi (ADR 0057): kısayola bas, konuş, tekrar bas; metin odaktaki uygulamaya yazılır.

Bu bir komut yolu DEĞİLDİR. Metin ne `CommandRunner`'a, ne modele, ne konuşma geçmişine ne de
hafızaya gider; LLM çağrısı yoktur. Uyandırma sözcüğü içerse bile komut sayılmaz, olduğu gibi
yazılır. Mikrofon yalnız iki basış arasında açıktır, ham ses saklanmaz, kısmi tanıma yazılmaz.
Qt'siz tutulur; Windows'a dokunan parçalar `DictationBackend` ile enjekte edilir (testler sahte
verir). Denetime metin değil yalnız süre ve karakter sayıları yazılır.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol

from artemis.channels.desktop.hotkey import GlobalHotkey
from artemis.channels.voice.core import Microphone, SpeechRecognizer
from artemis.i18n import t

_ESC_UNAVAILABLE = "Esc kısayolu kullanılamıyor; yazımı durdurmak için başka pencereye geçin."
_VOICE_ON = "Ses açıkken dikte kullanılamaz; önce sesi kapatın."
_SELECTION_ON = "Seçili metin eylemi sürerken dikte kullanılamaz."
_BUSY = "Dikte metni işleniyor; bitmesini bekleyin."


class _Audit(Protocol):
    def emit(self, event: str, **fields: Any) -> object: ...


class _Hotkey(Protocol):
    def start(self) -> bool: ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class DictationBackend:
    """Windows'a dokunan parçalar; platform kontrolünden sonra lazy import edilir."""

    foreground_window: Callable[[], int]  # hwnd; okunamazsa OSError
    foreground_pid: Callable[[], int]  # okunamazsa OSError
    type_text: Callable[[str, Callable[[], bool]], int]
    escape_hotkey: Callable[[Callable[[], None]], _Hotkey]


def windows_backend() -> DictationBackend:
    def window() -> int:
        from artemis.computer import windows

        return windows.foreground_window()[0]

    def pid() -> int:
        from artemis.computer import windows

        return windows.foreground_process()[1]

    def type_text(text: str, should_continue: Callable[[], bool]) -> int:
        from artemis.computer import windows

        return windows.type_text(text, should_continue)

    return DictationBackend(window, pid, type_text, lambda press: GlobalHotkey("escape", press))


def clean_transcript(text: str) -> str:
    """Satır sonu/sekme ve denetim karakterleri tek boşluğa iner: SendInput'ta Enter/Tab gibi
    davranıp hedef uygulamada istenmeyen eylem yapmasınlar."""
    printable = "".join(char if char.isprintable() else " " for char in text)
    return " ".join(printable.split())


def foreground_target(backend: DictationBackend, own_pid: int) -> int | None:
    """Odaktaki pencerenin kimliği; Artemis'in kendisi ya da okunamayan pencere hedef değil."""
    try:
        if backend.foreground_pid() == own_pid:
            return None
        return backend.foreground_window()
    except OSError:
        return None


async def type_into_target(
    backend: DictationBackend,
    text: str,
    target: int | None,
    own_pid: int,
    *,
    closing: Callable[[], bool],
    esc_unavailable: Callable[[], None],
) -> tuple[str, int]:
    """(sonuç, yazılan karakter): typed|partial|cancelled|no_target|error. Dikte ve seçili metin
    eylemi (ADR 0062) aynı yazım motorunu paylaşır: hedef pencere yazım başlarken ve her
    karakterde denetlenir, geçici Esc kısayolu yazımı durdurur."""
    if target is None or foreground_target(backend, own_pid) != target:
        return "no_target", 0
    escaped = threading.Event()
    hotkey = backend.escape_hotkey(escaped.set)
    if not hotkey.start():
        esc_unavailable()
    reasons: list[str] = []

    def should_continue() -> bool:
        if escaped.is_set() or closing():
            reasons.append("esc")
        elif foreground_target(backend, own_pid) != target:
            reasons.append("target")
        return not reasons

    try:
        typed = await asyncio.to_thread(backend.type_text, text, should_continue)
    except OSError:
        return "error", 0  # kaç karakterin gittiği bilinmez
    finally:
        await asyncio.to_thread(hotkey.stop)
    if typed >= len(text):
        return "typed", typed
    if typed > 0:
        return "partial", typed
    return ("cancelled" if reasons == ["esc"] else "no_target"), 0


class Dictation:
    """Tek oturumluk durum makinesi: idle → recording → working → idle."""

    def __init__(
        self,
        *,
        max_seconds: float,
        audit: _Audit,
        open_microphone: Callable[[], Microphone],
        get_recognizer: Callable[[], Awaitable[SpeechRecognizer]],
        backend: DictationBackend,
        emit: Callable[[str, str], None],
        voice_active: Callable[[], bool],
        own_pid: int | None = None,
        clock: Callable[[], float] = monotonic,
        other_busy: Callable[[], bool] = lambda: False,
        polish: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._max_seconds = max_seconds
        self._audit = audit
        self._open_microphone = open_microphone
        self._get_recognizer = get_recognizer
        self._backend = backend
        self._emit = emit  # (tür, metin): listening | working | idle | message | text | history
        self._voice_active = voice_active
        self._other_busy = other_busy  # seçili metin eylemi sürerken dikte başlamaz (ADR 0062)
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._clock = clock
        self._polish = polish
        self._recognizer: SpeechRecognizer | None = None
        self._phase = "idle"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._last_target: int | None = None

    @property
    def busy(self) -> bool:
        return self._phase != "idle"

    @property
    def last_target(self) -> int | None:
        return self._last_target

    def toggle(self) -> None:
        """Kısayol basışı (işçi döngüsünde): boşta başlatır, kayıttaysa durdurup yazdırır."""
        if self._phase == "recording":
            self._stop.set()
            return
        if self._phase != "idle":
            self._emit("message", t(_BUSY))
            return
        # Önce kendi bayrağımızı kaldırıp sonra sese bakarız: köprü de önce kendi bayrağını
        # kaldırıp bize bakar, böylece ses ve dikte aynı anda mikrofon açamaz.
        self._phase = "recording"
        if self._voice_active():
            self._phase = "idle"
            self._emit("message", t(_VOICE_ON))
            return
        if self._other_busy():
            self._phase = "idle"
            self._emit("message", t(_SELECTION_ON))
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._session())

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    def shutdown(self) -> None:
        self._closing = True  # yazım thread'i iptalle durmaz; kendi denetiminde kesilir
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _target(self) -> int | None:
        return foreground_target(self._backend, self._own_pid)

    async def _session(self) -> None:
        started = self._clock()
        target = self._target()
        if target is not None:
            self._last_target = target
        self._emit("listening", "")
        outcome, chars, typed = "error", 0, 0
        message = t("Dikte çalışmadı; ses ve model ayarlarını kontrol edin.")
        text = ""
        prepare = asyncio.ensure_future(self._recognizer_ready())
        prepare.add_done_callback(lambda done: done.cancelled() or done.exception())
        try:
            pcm = await self._record()
            self._phase = "working"
            self._emit("working", "")
            recognizer = await prepare
            text = clean_transcript(await recognizer.transcribe(pcm)) if pcm else ""
            if text and self._polish is not None:
                try:
                    polished = clean_transcript(await self._polish(text))
                    if polished:
                        text = polished
                except (OSError, RuntimeError, ValueError, TimeoutError):
                    pass
            chars = len(text)
            if not text:
                outcome, message = "empty", t("Dikte: konuşma anlaşılamadı.")
            else:
                outcome, typed, message = await self._type(text, target)
        except asyncio.CancelledError:
            outcome = "cancelled"  # kapanış; denetim finally'de yazılır
            raise
        except (OSError, RuntimeError, ValueError, ImportError):
            pass  # sabit ileti; istisna metni sır/yol taşıyabilir
        finally:
            prepare.cancel()
            self._phase = "idle"
            self._audit.emit(
                "dictation",
                duration_seconds=round(self._clock() - started, 1),
                chars=chars,
                typed=typed,
                outcome=outcome,
            )
            if text and outcome != "empty":
                self._emit("history", text)
            # Yazılmayan metin kaybolmasın: panelde gösterilir (pano kullanılmaz).
            if text and outcome in {"no_target", "partial", "cancelled"}:
                self._emit("text", text)
            self._emit("idle", "")
            self._emit("message", message)

    async def _recognizer_ready(self) -> SpeechRecognizer:
        if self._recognizer is None:
            self._recognizer = await self._get_recognizer()
        return self._recognizer

    async def _record(self) -> bytes:
        microphone = self._open_microphone()
        chunks: list[bytes] = []

        async def collect() -> None:
            async for frame in microphone.frames():
                chunks.append(frame.pcm)

        collector = asyncio.ensure_future(collect())
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait(
                {collector, stopper}, timeout=self._max_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if collector.done():
                collector.result()  # mikrofon açılamadıysa hata burada yükselir
        finally:
            for task in (collector, stopper):
                task.cancel()
            await asyncio.gather(collector, stopper, return_exceptions=True)
            await microphone.close()
        return b"".join(chunks)

    async def _type(self, text: str, target: int | None) -> tuple[str, int, str]:
        """(sonuç, yazılan karakter, ileti)."""
        outcome, typed = await type_into_target(
            self._backend, text, target, self._own_pid,
            closing=lambda: self._closing,
            esc_unavailable=lambda: self._emit("message", t(_ESC_UNAVAILABLE)),
        )
        if outcome == "typed":
            return outcome, typed, t("Dikte: {typed} karakter gönderildi.").format(typed=typed)
        if outcome == "partial":
            return (
                outcome, typed,
                t("Dikte yarım kaldı: {typed}/{total} karakter yazıldı, geri alınmadı. "
                  "Tam metin panelde.").format(typed=typed, total=len(text)),
            )
        if outcome == "error":
            # Kaç karakterin gittiği bilinmez; başarı denmez, gidenin geri alınmadığı söylenir.
            return (
                outcome, 0,
                t("Dikte yazımı hata verdi; bir kısmı yazılmış olabilir, geri alınmadı."),
            )
        if outcome == "cancelled":
            return outcome, 0, t("Dikte durduruldu (Esc); hiçbir şey yazılmadı. Metin panelde.")
        if target is None or self._target() != target:
            # Yazım hiç başlamadan hedef yok ya da değişmişti.
            return (
                "no_target", 0,
                t("Dikte yazılmadı: hedef pencere yok ya da değişti. Metin panelde."),
            )
        return "no_target", 0, t("Dikte yazılmadı: hedef pencere değişti. Metin panelde.")
