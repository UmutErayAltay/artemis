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

_ESC_UNAVAILABLE = "Esc kısayolu kullanılamıyor; yazımı durdurmak için başka pencereye geçin."
_VOICE_ON = "Ses açıkken dikte kullanılamaz; önce sesi kapatın."
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
    ) -> None:
        self._max_seconds = max_seconds
        self._audit = audit
        self._open_microphone = open_microphone
        self._get_recognizer = get_recognizer
        self._backend = backend
        self._emit = emit  # (tür, metin): listening | working | idle | message | text
        self._voice_active = voice_active
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._clock = clock
        self._recognizer: SpeechRecognizer | None = None
        self._phase = "idle"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def busy(self) -> bool:
        return self._phase != "idle"

    def toggle(self) -> None:
        """Kısayol basışı (işçi döngüsünde): boşta başlatır, kayıttaysa durdurup yazdırır."""
        if self._phase == "recording":
            self._stop.set()
            return
        if self._phase != "idle":
            self._emit("message", _BUSY)
            return
        # Önce kendi bayrağımızı kaldırıp sonra sese bakarız: köprü de önce kendi bayrağını
        # kaldırıp bize bakar, böylece ses ve dikte aynı anda mikrofon açamaz.
        self._phase = "recording"
        if self._voice_active():
            self._phase = "idle"
            self._emit("message", _VOICE_ON)
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
        """Odaktaki pencerenin kimliği; Artemis'in kendisi ya da okunamayan pencere hedef değil."""
        try:
            if self._backend.foreground_pid() == self._own_pid:
                return None
            return self._backend.foreground_window()
        except OSError:
            return None

    async def _session(self) -> None:
        started = self._clock()
        target = self._target()
        self._emit("listening", "")
        outcome, chars, typed = "error", 0, 0
        message = "Dikte çalışmadı; ses ve model ayarlarını kontrol edin."
        text = ""
        prepare = asyncio.ensure_future(self._recognizer_ready())
        prepare.add_done_callback(lambda done: done.cancelled() or done.exception())
        try:
            pcm = await self._record()
            self._phase = "working"
            self._emit("working", "")
            recognizer = await prepare
            text = clean_transcript(await recognizer.transcribe(pcm)) if pcm else ""
            chars = len(text)
            if not text:
                outcome, message = "empty", "Dikte: konuşma anlaşılamadı."
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
        """(sonuç, yazılan karakter, ileti). Hedef yazım başlarken ve her karakterde denetlenir."""
        if target is None or self._target() != target:
            return (
                "no_target", 0,
                "Dikte yazılmadı: hedef pencere yok ya da değişti. Metin panelde.",
            )
        escaped = threading.Event()
        hotkey = self._backend.escape_hotkey(escaped.set)
        if not hotkey.start():
            self._emit("message", _ESC_UNAVAILABLE)
        reasons: list[str] = []

        def should_continue() -> bool:
            if escaped.is_set() or self._closing:
                reasons.append("esc")
            elif self._target() != target:
                reasons.append("target")
            return not reasons

        try:
            typed = await asyncio.to_thread(self._backend.type_text, text, should_continue)
        except OSError:
            # Kaç karakterin gittiği bilinmez; başarı denmez, gidenin geri alınmadığı söylenir.
            return (
                "error", 0,
                "Dikte yazımı hata verdi; bir kısmı yazılmış olabilir, geri alınmadı.",
            )
        finally:
            await asyncio.to_thread(hotkey.stop)
        if typed >= len(text):
            return "typed", typed, f"Dikte: {typed} karakter gönderildi."
        if typed > 0:
            return (
                "partial", typed,
                f"Dikte yarım kaldı: {typed}/{len(text)} karakter yazıldı, geri alınmadı. "
                "Tam metin panelde.",
            )
        if reasons == ["esc"]:
            return "cancelled", 0, "Dikte durduruldu (Esc); hiçbir şey yazılmadı. Metin panelde."
        return "no_target", 0, "Dikte yazılmadı: hedef pencere değişti. Metin panelde."
