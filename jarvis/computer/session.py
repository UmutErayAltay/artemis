"""Bilgisayar kullanımı oturum döngüsü (ADR 0039, dalga 5).

Döngü: her monitörün görüntüsünü al -> görsel modelden tek JSON eylem iste -> kodla denetle ->
(hassasaksa sor; `mode: auto` ise sorma) -> uygula. Sınırların hepsi burada KODLA zorlanır;
modelin beyanına güvenilmez. Görüntü yalnız bellekte kalır, ne diske ne denetime yazılır;
yazılan metnin de yalnız uzunluğu loglanır.
Arka uç, onay ve saat enjekte edilir, bu yüzden testler gerçek fareye/klavyeye dokunmaz.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal, Protocol

from jarvis.brain.errors import ProviderError
from jarvis.brain.models import Message
from jarvis.brain.providers import Provider
from jarvis.computer.actions import (
    SYSTEM_PROMPT,
    Action,
    is_sensitive,
    key_block_reason,
    parse_action,
    parse_keys,
    process_block_reason,
    scale_point,
)
from jarvis.config.settings import ComputerUseConfig

Reason = Literal[
    "done", "ask_user", "limit", "timeout", "rejected", "stopped", "blocked", "invalid", "error"
]

_RETRY_PROMPT = "Yalnız tek bir geçerli JSON eylem döndür."
_HISTORY_LINES = 10
# Engellenen adım oturumu hemen bitirmez, modele söylenir (ör. panelden komut verilince
# Jarvis penceresi görüntüde durur); bu kadar engelden sonra oturum biter.
_MAX_BLOCKS = 3
# Girdisi noktadaki pencereye düşen eylemler; geri kalan her girdi (type, key) ön plana gider.
_POINTER_ACTIONS = frozenset({"click", "scroll"})
# Deadline'a yaklaşırken son modelin ve ana döngünün yanıt verebilmesi için bırakılan pay.
_DEADLINE_MARGIN = 2.0


class ComputerBackend(Protocol):
    """`jarvis.computer.windows` modülünün kullanılan yüzeyi; testler sahtesini verir."""

    def monitors(self) -> list[tuple[int, int, int, int]]: ...
    def capture(self, left: int, top: int, width: int, height: int) -> tuple[bytes, int, int]: ...
    def foreground_process(self) -> tuple[str, int]: ...
    def process_at(self, x: int, y: int) -> tuple[str, int]: ...
    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None: ...
    def scroll(self, x: int, y: int, notches: int) -> None: ...
    def type_text(self, text: str) -> None: ...
    def press_keys(self, keys: tuple[str, ...]) -> None: ...


class _Audit(Protocol):
    def emit(self, event: str, **fields: Any) -> None: ...


@dataclass(frozen=True)
class SessionResult:
    reason: Reason
    summary: str
    steps: int


@dataclass(frozen=True)
class _Screen:
    """Bir monitör: sanal masaüstü köşesi, gerçek boyut, küçültülmüş görüntü boyutu ve PNG."""

    origin: tuple[int, int]
    size: tuple[int, int]
    shot: tuple[int, int]
    png: bytes


class ComputerSession:
    def __init__(
        self,
        *,
        provider: Provider,
        backend: ComputerBackend,
        approve: Callable[[str, dict[str, Any]], bool],
        audit: _Audit,
        config: ComputerUseConfig,
        own_pid: int,
        stop: threading.Event,
        on_step: Callable[[str], None],
        provider_timeout: float,
        mode: Callable[[], str] | None = None,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._provider = provider
        self._backend = backend
        self._approve = approve
        self._audit = audit
        self._config = config
        # Kip her hassas adımda canlı okunur: panelden oto onay kapatılınca süren oturum da
        # sormaya başlamalı; başlangıçtaki config kopyası bunu göremezdi.
        self._mode = mode or (lambda: config.mode)
        self._own_pid = own_pid
        self._stop = stop
        self._on_step = on_step
        self._provider_timeout = provider_timeout
        self._clock = clock
        self._sleep = sleep
        self._steps = 0

    async def run(self, goal: str, turn_id: str, deadline: float | None) -> SessionResult:
        limit = self._clock() + self._config.max_minutes * 60
        if deadline is not None:
            limit = min(limit, deadline - _DEADLINE_MARGIN)
        try:
            result = await self._loop(goal, turn_id, limit)
        except asyncio.CancelledError:
            self._end(turn_id, "stopped")
            raise
        self._end(turn_id, result.reason)
        return result

    def _end(self, turn_id: str, reason: str) -> None:
        self._audit.emit("computer_session_end", turn_id=turn_id, reason=reason, steps=self._steps)

    def _result(self, reason: Reason, summary: str) -> SessionResult:
        return SessionResult(reason, summary, self._steps)

    async def _loop(self, goal: str, turn_id: str, limit: float) -> SessionResult:
        history: list[str] = []
        blocks = 0
        while True:
            if self._stop.is_set():
                return self._result("stopped", "Kullanıcı oturumu durdurdu.")
            if self._steps >= self._config.max_steps:
                return self._result("limit", f"{self._config.max_steps} adım sınırına ulaşıldı.")
            remaining = limit - self._clock()
            if remaining <= 0:
                return self._result("timeout", "Oturum süresi doldu.")

            try:
                # Geç yükleme: png numpy ister; numpy yalnız ses/hafıza ekleriyle gelir ve araç
                # keşfi bu modülü içe aktardığı için üstte yüklense temel kurulum çökerdi.
                from jarvis.computer.png import screenshot_png

                # Her monitör ayrı görüntü: sanal masaüstünü tek görüntüye küçültmek
                # yazıyı okunmaz yapardı (ADR 0039 ek karar).
                screens: list[_Screen] = []
                monitor_rects = await asyncio.to_thread(self._backend.monitors)
                for left, top, width, height in monitor_rects:
                    buffer, width, height = await asyncio.to_thread(
                        self._backend.capture, left, top, width, height
                    )
                    png, shot_size = screenshot_png(
                        buffer, width, height, self._config.screenshot_width
                    )
                    screens.append(_Screen((left, top), (width, height), shot_size, png))
            except ImportError:
                return self._result(
                    "error", "Ekran görüntüsü için numpy gerekli (`pip install numpy`)."
                )
            except (OSError, RuntimeError, ValueError) as error:
                return self._result("error", f"Ekran görüntüsü alınamadı: {error}")
            if not screens:
                return self._result("error", "Ekran bulunamadı.")

            lines = "\n".join(history[-_HISTORY_LINES:]) or "-"
            # Görüntüler ayrı kullanıcı iletilerinde, etiketleriyle: sağlayıcılar ardışık
            # kullanıcı iletilerini birleştirir ve "Ekran N" eşlemesi korunur.
            listing = ", ".join(
                f"Ekran {number}: {item.shot[0]}x{item.shot[1]} piksel"
                for number, item in enumerate(screens, start=1)
            )
            messages = [
                Message(role="system", content=SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=(
                        f"Hedef: {goal}\nEkran sayısı: {len(screens)}. {listing}.\n"
                        f"Önceki adımlar:\n{lines}"
                    ),
                ),
                *(
                    Message(
                        role="user",
                        content=f"Ekran {number} ({item.shot[0]}x{item.shot[1]} piksel)",
                        image_png=item.png,
                    )
                    for number, item in enumerate(screens, start=1)
                ),
            ]
            try:
                action = await self._ask(messages, remaining)
            except ProviderError as error:
                return self._result("error", f"Model çağrısı başarısız: {error}")
            if action is None:
                return self._result("invalid", "Model iki denemede de geçerli eylem döndürmedi.")

            if action.type == "done":
                return self._result("done", action.text or "")
            if action.type == "ask_user":
                return self._result("ask_user", action.text or "")

            outcome = await self._act(action, turn_id, screens)
            if isinstance(outcome, SessionResult):
                return outcome
            if outcome is not None:
                blocks += 1
                self._audit.emit(
                    "computer_blocked", turn_id=turn_id, action=action.type, reason=outcome
                )
                if blocks >= _MAX_BLOCKS:
                    return self._result("blocked", outcome)
                history.append(
                    f"ENGELLENDİ ({action.type}): {outcome}. Bu pencereye girdi gönderilemez; "
                    "başka bir yol dene ya da ask_user kullan."
                )
                await self._sleep(self._config.step_delay_seconds)
                continue
            history.append(f"Adım {self._steps}: {action.type} - {action.reason}")

            await self._sleep(self._config.step_delay_seconds)

    async def _ask(self, messages: list[Message], remaining: float) -> Action | None:
        """Modelden eylem ister; bozuksa YENİ ekran görüntüsü almadan bir kez yeniden dener."""
        timeout = min(self._provider_timeout, remaining)
        for attempt in range(2):
            completion = await self._provider.complete(messages, [], timeout=timeout)
            try:
                return parse_action(completion.content)
            except ValueError as error:
                if attempt:
                    return None
                messages.append(Message(role="assistant", content=completion.content))
                # Neden geçersiz olduğunu söylemezsek model aynı hatayı tekrarlıyor.
                messages.append(Message(role="user", content=f"{error}\n{_RETRY_PROMPT}"))
        return None

    async def _act(
        self,
        action: Action,
        turn_id: str,
        screens: list[_Screen],
    ) -> SessionResult | str | None:
        """Eylemi denetler ve uygular. Oturum bitecekse sonucu, adım engellendiyse nedeni
        (modele geri bildirilir), adım uygulandıysa None döner."""
        point: tuple[int, int] | None = None
        window: str | None = None
        # Modelin koordinatı seçtiği ekranın görüntüsüne göredir; fare ise sanal masaüstü.
        # Yalnız işaretçi eyleminde anlamlı (type/key/wait koordinat kullanmaz); geçersiz
        # ekran oturumu bitirmez, model düzeltsin diye engel olarak geri bildirilir.
        if action.type in _POINTER_ACTIONS and action.screen > len(screens):
            return f"Ekran {action.screen} yok; {len(screens)} ekran görülüyor"
        screen = screens[min(action.screen, len(screens)) - 1]
        if action.type == "wait":
            await self._sleep(action.seconds or 0.0)  # arka uca dokunmaz
        else:
            if action.type == "key":
                key_blocked = key_block_reason(parse_keys(action.keys or ""))
                if key_blocked:
                    return key_blocked
            if action.x is not None and action.y is not None:
                try:
                    scaled = scale_point(action.x, action.y, screen.shot, screen.size)
                    point = (scaled[0] + screen.origin[0], scaled[1] + screen.origin[1])
                except ValueError as error:
                    return self._result("invalid", str(error))
            blocked, window = await self._target(action.type, point)
            if blocked:
                return blocked

        sensitive = is_sensitive(action)
        approved: bool | None = None
        auto_approved = False
        # Otomatik kip (ADR 0039 ek karar): oturum içi hassas adım sorulmaz. Kodla zorlanan
        # engeller yukarıda zaten işledi; Esc denetimi ve sınırlar iki kipte de aynı.
        if sensitive and self._mode() == "auto":
            # Denetimde "approved" kullanıcının cevabıdır: kimse sorulmadı, None kalır;
            # atlamanın kendisi auto_approved ile görünür.
            auto_approved = True
        elif sensitive:
            approved = await asyncio.to_thread(
                self._approve,
                "computer_action",
                {
                    "eylem": action.type,
                    "konum": list(point) if point else None,
                    "metin": action.text,
                    "tuslar": action.keys,
                    "gerekce": action.reason,
                    "pencere": window,
                },
            )
            if not approved:
                return self._result("rejected", "Hassas adım kullanıcı tarafından reddedildi.")
            # Onay penceresi kapanınca odak çoğu kez Jarvis paneline geçer: hedef yeniden
            # denetlenmezse onaylanan metin Jarvis'in kendi penceresine yazılabilirdi.
            blocked, _ = await self._target(action.type, point)
            if blocked:
                return blocked
        # Onay beklerken Esc'ye basılmış olabilir: sıradaki eylem çalışmamalı.
        if self._stop.is_set():
            return self._result("stopped", "Kullanıcı oturumu durdurdu.")

        try:
            await self._execute(action, point)
        except (OSError, RuntimeError) as error:
            return self._result("error", f"Eylem uygulanamadı: {error}")

        self._steps += 1
        self._on_step(f"Adım {self._steps}: {action.reason}")
        self._audit.emit(
            "computer_step",
            turn_id=turn_id,
            step=self._steps,
            action=action.type,
            x=point[0] if point else None,
            y=point[1] if point else None,
            keys=action.keys,
            # Yazılan metin parola olabilir: yalnız uzunluk loglanır.
            text_length=len(action.text or "") if action.type == "type" else None,
            reason=action.reason,
            sensitive=sensitive,
            approved=approved,
            auto_approved=auto_approved,
            screen=action.screen,
        )
        return None

    async def _target(
        self, action_type: str, point: tuple[int, int] | None
    ) -> tuple[str | None, str | None]:
        """(engel nedeni, pencere adı): girdinin gerçekten düşeceği pencere denetlenir.

        Tıklama/kaydırma noktadaki pencereye gider (ve onu öne getirir); yazma/tuş ön plana.
        Tıklamada ön plana bakmak yanlıştı: panelden komut verilince ön plan hep Jarvis'ti ve
        ilk tıklama Brave'e olsa da oturum engelleniyordu.
        """
        # Seçim eylem türüne göredir, koordinatın varlığına göre değil: doğrulayıcı type/key
        # eyleminde x/y'yi reddetmiyor; modelin verdiği zararsız bir nokta yazmanın gittiği
        # ön planı (ör. terminal) denetimden kaçırmasın.
        if action_type in _POINTER_ACTIONS:
            if point is None:
                return "İşaretçi eyleminde konum yok.", None
            exe, pid = await asyncio.to_thread(self._backend.process_at, point[0], point[1])
        else:
            exe, pid = await asyncio.to_thread(self._backend.foreground_process)
        if not exe:
            return "Hedef pencere belirlenemedi.", None
        blocked = process_block_reason(exe, pid, self._own_pid)
        if blocked:
            return blocked, None
        return None, exe.replace("\\", "/").rsplit("/", 1)[-1]

    async def _execute(self, action: Action, point: tuple[int, int] | None) -> None:
        backend = self._backend
        if action.type == "click" and point:
            await asyncio.to_thread(backend.click, point[0], point[1], action.button, action.double)
        elif action.type == "scroll" and point:
            await asyncio.to_thread(backend.scroll, point[0], point[1], action.dy or 0)
        elif action.type == "type":
            await asyncio.to_thread(backend.type_text, action.text or "")
        elif action.type == "key":
            await asyncio.to_thread(backend.press_keys, parse_keys(action.keys or ""))
