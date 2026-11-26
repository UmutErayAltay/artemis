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
from time import monotonic
from typing import TYPE_CHECKING, Any

from artemis.brain.errors import ProviderError
from artemis.brain.models import Message
from artemis.brain.providers import Provider
from artemis.brain.strict_rules import STRICT_COMPUTER_RULES, destructive_unrequested
from artemis.computer.actions import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_UIA,
    UIA_ADDENDUM,
    Action,
    is_sensitive,
    key_block_reason,
    mask_screen_secrets,
    parse_action,
    parse_keys,
    parse_verdict,
    process_block_reason,
    scale_point,
)
from artemis.computer.planner import (
    _ELEMENTS_HEADER as _ELEMENTS_HEADER,
)
from artemis.computer.planner import (
    _LIST_SYNTAX as _LIST_SYNTAX,
)
from artemis.computer.planner import (
    _MEMORY_STEPS as _MEMORY_STEPS,
)
from artemis.computer.planner import (
    _PENDING as _PENDING,
)
from artemis.computer.planner import (
    _REPEAT_LIMIT as _REPEAT_LIMIT,
)
from artemis.computer.planner import (
    _REPEAT_WINDOW as _REPEAT_WINDOW,
)
from artemis.computer.planner import (
    MAX_STRATEGIST_CALLS,
    StrategyVerdict,
    build_executor_messages,
    build_verify_messages,
    consult_strategist_decision,
    describe_action,
    describe_screens_context,
    is_give_up_question,
    is_no_progress,
    parse_strategy,
    render_elements,
    render_strategy_block,
)
from artemis.computer.planner import (
    SessionMemory as SessionMemory,
)
from artemis.computer.planner import (
    StepRecord as StepRecord,
)
from artemis.computer.session_types import (
    _DEADLINE_MARGIN as _DEADLINE_MARGIN,
)
from artemis.computer.session_types import (
    _MAX_BLOCKS as _MAX_BLOCKS,
)
from artemis.computer.session_types import (
    _MIN_INTERACTIVE_UIA as _MIN_INTERACTIVE_UIA,
)
from artemis.computer.session_types import (
    _NO_COORDS_IN_UIA as _NO_COORDS_IN_UIA,
)
from artemis.computer.session_types import (
    _POINTER_ACTIONS as _POINTER_ACTIONS,
)
from artemis.computer.session_types import (
    _REFOCUS_POLL_SECONDS as _REFOCUS_POLL_SECONDS,
)
from artemis.computer.session_types import (
    _REFOCUS_SECONDS as _REFOCUS_SECONDS,
)
from artemis.computer.session_types import (
    _REPEAT_BLOCK as _REPEAT_BLOCK,
)
from artemis.computer.session_types import (
    _REPEATABLE_KEYS as _REPEATABLE_KEYS,
)
from artemis.computer.session_types import (
    _RETRY_PROMPT as _RETRY_PROMPT,
)
from artemis.computer.session_types import (
    SCREEN_LOCK as SCREEN_LOCK,
)
from artemis.computer.session_types import (
    ComputerBackend as ComputerBackend,
)
from artemis.computer.session_types import (
    Reason as Reason,
)
from artemis.computer.session_types import (
    ScreenBusyError as ScreenBusyError,
)
from artemis.computer.session_types import (
    SessionResult as SessionResult,
)
from artemis.computer.session_types import (
    _Audit as _Audit,
)
from artemis.computer.session_types import (
    _Layout as _Layout,
)
from artemis.computer.session_types import (
    _Screen as _Screen,
)
from artemis.computer.session_types import (
    _signature as _signature,
)
from artemis.computer.session_types import (
    _Stale as _Stale,
)
from artemis.config.settings import ComputerUseConfig
from artemis.tools.base import run_blocking

if TYPE_CHECKING:
    from artemis.computer.windows import UiElement

_render_elements = render_elements



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
        memory: SessionMemory | None = None,
        on_frame: Callable[[tuple[bytes, ...]], None] | None = None,
        strict: bool = False,
        planner: Provider | None = None,
    ) -> None:
        # Verilen bellek aynı mesajdaki önceki çağrıdan gelir: kayıtları "önceki çağrı" olur.
        self._memory = memory if memory is not None else SessionMemory()
        self._memory.mark_prior()
        self._provider = provider
        self._planner = planner
        self._strategist_calls = 0
        self._stalled_steps = 0
        self._vision_steps_left = 0
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
        # Son taze kare (yalnız RAM); sahibi karar verir, burada diske/denetime yazılmaz.
        self._on_frame = on_frame
        self._layout: _Layout | None = None
        self._strict = strict
        self._goal = ""
        # Kullanıcının kendi mesajı (ADR 0078); None: eski davranış, aile izni hedeften.
        self._user_text: str | None = None
        # ADR 0073: o adımın UI Automation öğeleri (id -> UiElement); her adım başında sıfırlanır.
        self._elements: dict[int, UiElement] = {}
        self._step_has_image = False
        self._last_used_vision = False
        # ADR 0070: OS düzeyi kanıt. Yazılan metin yalnız bellekte tutulur, asla loglanmaz.
        self._os_issues: dict[tuple[Any, ...], str] = {}  # imza → sorun
        self._last_typed: tuple[str, int | None] | None = None

    async def run(
        self,
        goal: str,
        turn_id: str,
        deadline: float | None,
        *,
        user_text: str | None = None,
    ) -> SessionResult:
        limit = self._clock() + self._config.max_minutes * 60
        if deadline is not None:
            limit = min(limit, deadline - _DEADLINE_MARGIN)
        try:
            result = await self._loop(goal, turn_id, limit, user_text)
        except asyncio.CancelledError:
            self._end(turn_id, "stopped")
            raise
        self._end(turn_id, result.reason)
        return result

    def _end(self, turn_id: str, reason: str) -> None:
        self._audit.emit("computer_session_end", turn_id=turn_id, reason=reason, steps=self._steps)

    def _result(self, reason: Reason, summary: str) -> SessionResult:
        return SessionResult(reason, summary, self._steps)

    async def _loop(
        self, goal: str, turn_id: str, limit: float, user_text: str | None = None
    ) -> SessionResult:
        self._goal = goal
        self._user_text = user_text
        memory = self._memory
        blocks = 0
        observation = self._config.observation
        with_uia = observation in ("uia", "both", "hybrid")
        fallback_image = False
        base_uia_prompt = (
            f"{SYSTEM_PROMPT_UIA}\n\n{STRICT_COMPUTER_RULES}" if self._strict else SYSTEM_PROMPT_UIA
        )
        base_both_prompt = (
            f"{SYSTEM_PROMPT}\n\n{UIA_ADDENDUM}\n\n{STRICT_COMPUTER_RULES}"
            if self._strict
            else f"{SYSTEM_PROMPT}\n\n{UIA_ADDENDUM}"
        )
        base_screen_prompt = (
            f"{SYSTEM_PROMPT}\n\n{STRICT_COMPUTER_RULES}" if self._strict else SYSTEM_PROMPT
        )
        while True:
            self._elements = {}
            self._last_used_vision = False
            if self._stop.is_set():
                return self._result("stopped", "Kullanıcı oturumu durdurdu.")
            if self._steps >= self._config.max_steps:
                return self._result("limit", f"{self._config.max_steps} adım sınırına ulaşıldı.")
            remaining = limit - self._clock()
            if remaining <= 0:
                return self._result("timeout", "Oturum süresi doldu.")

            step_images = observation in ("screen", "both") or (
                observation == "hybrid" and (fallback_image or self._vision_steps_left > 0)
            )
            fallback_image = False
            if self._vision_steps_left > 0:
                self._vision_steps_left -= 1
            captured = await self._capture(limit, with_images=step_images)
            if isinstance(captured, SessionResult):
                return captured
            screens = captured
            # Yakalama anındaki düzen: girdi gönderilmeden hemen önce karşılaştırılır (_act).
            layout = self._layout
            elements_block = ""
            if with_uia:
                elements = await self._read_elements(
                    layout.window[0] if layout else 0, limit=limit
                )
                if observation == "hybrid" and not step_images and (
                    not elements or self._last_used_vision
                ):
                    step_images = True
                    recaptured = await self._recapture_hybrid(
                        elements, layout.window if layout else None, limit
                    )
                    if isinstance(recaptured, SessionResult):
                        return recaptured
                    screens, elements = recaptured
                    layout = self._layout
                self._elements = {item.id: item for item in elements}
                elements_block = "\n" + _render_elements(elements)
            self._step_has_image = step_images
            system_prompt = (
                base_both_prompt
                if (observation == "both" or (observation == "hybrid" and step_images))
                else base_uia_prompt
                if observation in ("uia", "hybrid")
                else base_screen_prompt
            )

            screens_context = await self._read_window_context()
            messages = build_executor_messages(
                goal=goal,
                screens=screens,
                memory_text=memory.render(),
                screens_context=screens_context,
                elements_block=elements_block,
                system_prompt=system_prompt,
                step_images=step_images,
            )
            try:
                if self._clock() >= limit:
                    return self._result("timeout", "Oturum süresi doldu.")
                action = await self._ask(messages, limit, parse_action)
            except ProviderError as error:
                return self._result("error", f"Model çağrısı başarısız: {error}")
            if self._stop.is_set():
                return self._result("stopped", "Kullanıcı oturumu durdurdu.")
            if self._clock() >= limit:
                return self._result("timeout", "Oturum süresi doldu.")
            if action is None:
                return self._result("invalid", "Model iki denemede de geçerli eylem döndürmedi.")

            # Önceki eylemin sonucu bu yanıtın `observed` alanındadır (done/ask_user dahil).
            memory.learn(action)
            if is_no_progress(action.observed):
                self._stalled_steps += 1
            elif action.observed:
                self._stalled_steps = 0
            if action.type == "done":
                return await self._verify(goal, turn_id, action.text or "", limit)
            if action.type == "ask_user":
                raw_text = action.text or ""
                if self._planner is not None and is_give_up_question(raw_text):
                    verdict = await self._consult_strategist(
                        goal=goal, turn_id=turn_id, trigger="give_up", reason_text=raw_text,
                        limit=limit, memory=memory, elements_block=elements_block,
                    )
                    if verdict is not None and verdict.decision == "try":
                        if observation == "hybrid":
                            fallback_image = True
                        await self._sleep(self._config.step_delay_seconds)
                        continue
                    if verdict is not None and verdict.decision == "ask_user" and verdict.question:
                        return self._result("ask_user", mask_screen_secrets(verdict.question))
                if self._stop.is_set():
                    return self._result("stopped", "Kullanıcı oturumu durdurdu.")
                # Modelin sorusu ekrandan okunan sırları yankılayabilir; kullanıcıya maskeli gider.
                return self._result("ask_user", mask_screen_secrets(raw_text))

            resolved = self._element_point(action) if action.type == "click_element" else None
            signature = _signature(action, resolved if isinstance(resolved, tuple) else None)
            repeat = signature is not None and memory.is_repeat(signature)
            # Tekrar, mevcut engel sayacından geçer; hiçbir güvenlik denetimi atlanmaz/gevşetilmez.
            outcome = self._repeat_hint(action) if repeat else await self._act(
                action, turn_id, screens, layout, limit
            )
            if isinstance(outcome, SessionResult):
                return outcome
            if outcome is not None:
                blocks += 1
                self._audit.emit(
                    "computer_blocked", turn_id=turn_id, action=action.type, reason=outcome
                )
                if blocks >= _MAX_BLOCKS:
                    return self._result("blocked", outcome)
                if repeat:
                    note = outcome
                    if observation == "hybrid":
                        fallback_image = True
                elif isinstance(outcome, _Stale):
                    # Pencere yasak değil; yalnız görüntü eskidi. Model hedefi bırakmamalı.
                    note = (
                        f"ATLANDI ({action.type}): {outcome}; girdi gönderilmedi. "
                        "Yeni görüntüye bakıp adımı yeniden planla."
                    )
                else:
                    note = (
                        f"ENGELLENDİ ({action.type}): {outcome}. Bu pencereye girdi "
                        "gönderilemez; başka bir yol dene ya da ask_user kullan."
                    )
                    if observation == "hybrid":
                        fallback_image = True
                memory.add(StepRecord(note=note))
                if not isinstance(outcome, _Stale):
                    verdict = await self._consult_strategist(
                        goal=goal, turn_id=turn_id,
                        trigger="repeat" if repeat else "blocked",
                        reason_text=outcome, limit=limit, memory=memory,
                        elements_block=elements_block,
                    )
                    if verdict is not None and verdict.decision == "ask_user" and verdict.question:
                        return self._result("ask_user", mask_screen_secrets(verdict.question))
                    if (
                        verdict is not None
                        and verdict.decision == "try"
                        and observation == "hybrid"
                    ):
                        fallback_image = True
                await self._sleep(self._config.step_delay_seconds)
                continue

            if self._stalled_steps >= 2:
                verdict = await self._consult_strategist(
                    goal=goal, turn_id=turn_id, trigger="stalled",
                    reason_text=action.observed or "Son adımlarda ekran ilerlemedi.",
                    limit=limit, memory=memory, elements_block=elements_block,
                )
                if verdict is not None and verdict.decision == "ask_user" and verdict.question:
                    return self._result("ask_user", mask_screen_secrets(verdict.question))
                if verdict is not None and verdict.decision == "try" and observation == "hybrid":
                    fallback_image = True
            await self._sleep(self._config.step_delay_seconds)

    async def _read_window_context(self) -> str:
        """Görünür pencere listesini ekrana ve tam ekran durumuna göre biçimlendirir."""
        reader = getattr(self._backend, "visible_windows", None)
        layout = self._layout
        if not callable(reader) or layout is None:
            return ""
        try:
            windows = list(await asyncio.to_thread(reader))
        except (OSError, RuntimeError):
            return ""
        return describe_screens_context(layout.monitors, layout.window, windows, self._own_pid)

    async def _consult_strategist(
        self,
        *,
        goal: str,
        turn_id: str,
        trigger: str,
        reason_text: str,
        limit: float,
        memory: SessionMemory,
        elements_block: str,
    ) -> StrategyVerdict | None:
        """Tıkanmada sessizce güçlü stratejiste danışır; `try` ise belleği günceller."""
        if (
            self._planner is None
            or self._strategist_calls >= MAX_STRATEGIST_CALLS
            or self._stop.is_set()
            or self._clock() >= limit
        ):
            return None
        captured = await self._capture(limit, with_images=True, emit_frame=False)
        if isinstance(captured, SessionResult):
            return None
        self._strategist_calls += 1
        screens_context = await self._read_window_context()
        verdict = await consult_strategist_decision(
            goal=goal,
            reason_text=reason_text,
            memory=memory,
            elements_block=elements_block,
            screens=captured,
            screens_context=screens_context,
            ask_fn=lambda msgs: self._ask(msgs, limit, parse_strategy, provider=self._planner),
        )
        if self._stop.is_set() or self._clock() >= limit:
            verdict = None
        self._audit.emit(
            "computer_strategist",
            turn_id=turn_id,
            trigger=trigger,
            call=self._strategist_calls,
            step=self._steps,
            decision=verdict.decision if verdict is not None else "unavailable",
            hypothesis=verdict.hypothesis[:200] if verdict is not None else None,
        )
        if verdict is not None and verdict.decision == "try":
            memory.strategy = render_strategy_block(verdict)
            self._stalled_steps = 0
            # Stratejist tüm monitörlere bakıp çok adımlı görsel plan verdiyse hybrid kipte
            # plan boyunca görüntü açık kalsın; yürütücü 2. adımda tekrar UIA-only kaba düşmesin.
            self._vision_steps_left = max(self._vision_steps_left, len(verdict.plan) + 1)
        return verdict

    def _element_point(self, action: Action) -> tuple[int, int] | str:
        """Öğenin tıklama noktası ya da engel nedeni. Dikdörtgen UIA'dan (başka sürecin
        bildirdiği veri) gelir: ön plan penceresinin içinde ve bir monitörde olmalıdır, yoksa
        bozuk/kötü niyetli bir dikdörtgen tıklamayı pencere dışına taşırdı."""
        element = self._elements.get(action.element or 0)
        if element is None:
            return f"Öğe {action.element} güncel listede yok"
        layout = self._layout
        if layout is None:
            return "Pencere düzeni doğrulanamadı; öğeye tıklanmadı"
        ex, ey, ew, eh = element.rect
        left, top, right, bottom = layout.window[1]
        if ew <= 0 or eh <= 0 or ex < left or ey < top or ex + ew > right or ey + eh > bottom:
            return f"Öğe {action.element} ön plan penceresinin dışında; tıklanmadı"
        tx = ex + (
            round((ew - 1) * action.ratio) if action.ratio is not None else ew // 2
        )
        point = (tx, ey + eh // 2)
        if not any(
            mx <= point[0] < mx + mw and my <= point[1] < my + mh
            for mx, my, mw, mh in layout.monitors
        ):
            return f"Öğe {action.element} hiçbir ekranda değil; tıklanmadı"
        return point

    async def _destructive_target(
        self, action: Action, point: tuple[int, int] | None, turn_id: str
    ) -> str | None:
        """ADR 0078 kapısı; öğeye, koordinata ya da odaktaki öğeye giden her girdide çalışır.

        Öğe: click_element'te seçilen öğe; koordinatlı tıklamada noktayı içeren en küçük öğe
        (both kipinde UIA listesi vardır); onaylayan tuşta (enter/space) ya da odağı tetikleyen
        yazımda (satır sonu veya yalnız boşluk) odaktaki öğe. Öğe listesi boşsa (screen kipi
        veya boş UIA sonucu) odaktaki öğenin adı `focused_name` ile okunur; okunamazsa adım
        engellenmez ancak `computer_focus_unreadable` denetime yazılır."""
        element: UiElement | None = None
        typed_text = action.text or ""
        activates_focused = (
            action.type == "key"
            and bool(set(parse_keys(action.keys or "")) & {"enter", "space"})
        ) or (
            action.type == "type"
            and (
                "\r" in typed_text
                or "\n" in typed_text
                or (bool(typed_text) and typed_text.isspace())
            )
        )
        if action.type == "click_element":
            element = self._elements.get(action.element or 0)
        elif action.type in ("click", "scroll") and point is not None:
            hits = [
                item for item in self._elements.values()
                if item.rect[0] <= point[0] < item.rect[0] + item.rect[2]
                and item.rect[1] <= point[1] < item.rect[1] + item.rect[3]
            ]
            element = min(hits, key=lambda item: item.rect[2] * item.rect[3], default=None)
        elif action.type == "key" and (
            "delete" in (keys := parse_keys(action.keys or "")) or set(keys) == {"ctrl", "d"}
        ):
            # Delete/Shift+Delete ve Gezgin'in Ctrl+D'si seçili dosyayı tıklamasız siler: hedef
            # silme istemiyorsa odaktaki öğeden bağımsız engellenir (metin düzeltmede backspace).
            from artemis.computer.windows import UiElement as _Key

            element = _Key(0, "Key", "Sil", (0, 0, 0, 0), interactive=True, focused=False)
        elif activates_focused:
            if not self._elements:
                # screen kipinde veya boş UIA sonucunda öğe listesi yoktur: yine de odak
                # adı UIA'dan okunup yıkıcı öğeye basılmasını engeller (ADR 0078).
                try:
                    focus_name = await asyncio.to_thread(self._backend.focused_name, 3.0)
                except (OSError, RuntimeError):
                    focus_name = None
                if not focus_name:
                    self._audit.emit("computer_focus_unreadable", turn_id=turn_id)
                    return None
                from artemis.computer.windows import UiElement as _Focus

                element = _Focus(
                    0, "Focus", focus_name, (0, 0, 0, 0), interactive=True, focused=True
                )
            else:
                element = next((item for item in self._elements.values() if item.focused), None)
                if element is None and self._last_used_vision:
                    # OCR öğelerinde odak bilgisi yoktur (focused=False): enter/boşluk veya satır
                    # sonu yazımı farkında olmadan yıkıcı bir denetimi etkinleştirebilir.
                    return (
                        "Odaktaki öğe bilinmiyor (görsel öğe listesi); "
                        "enter/boşluk yerine hedef öğeye click_element ile tıkla."
                    )
        if element is None:
            return None
        safe_name = mask_screen_secrets(_LIST_SYNTAX.sub(" ", element.name))
        reason = destructive_unrequested(safe_name, self._goal, self._user_text)
        if reason is not None:
            self._audit.emit(
                "computer_destructive_blocked", turn_id=turn_id, element_kind=element.kind
            )
        return reason

    def _repeat_hint(self, action: Action) -> str:
        """Tekrar engeli notu; click_element için alternatif etkileşimli öğeler ve klavye önerir."""
        if action.type == "type":
            return (
                "AYNI METİN TEKRARLANMADI (type): Bu metni az önce yazdın; tekrar yazma. "
                "Aramayı/formu tetiklemek için enter bas veya listeden bir öğeye tıkla."
            )
        if action.type != "click_element":
            return _REPEAT_BLOCK
        repeated = self._elements.get(action.element or 0)
        repeated_name = repeated.name if repeated is not None else None
        alternatives: list[str] = []
        for item in self._elements.values():
            if not item.interactive or item.name == repeated_name:
                continue
            name = mask_screen_secrets(_LIST_SYNTAX.sub(" ", item.name))
            kind = " ".join(_LIST_SYNTAX.sub(" ", item.kind).split())[:40]
            alternatives.append(f'[{item.id}] {kind} "{name}"')
            if len(alternatives) >= 5:
                break
        if alternatives:
            return (
                f"{_REPEAT_BLOCK} Diğer öğeler: {', '.join(alternatives)}. "
                "Klavye dene: tab, enter, ctrl+s, alt+f."
            )
        return f"{_REPEAT_BLOCK} Klavye dene: tab, enter, ctrl+s, alt+f."

    def _element_label(self, action: Action) -> str | None:
        """Onay penceresi için öğe türü ve (maskeli) adı; kullanıcı neye tıklanacağını görür."""
        if action.type != "click_element":
            return None
        element = self._elements.get(action.element or 0)
        if element is None:
            return None
        name = mask_screen_secrets(_LIST_SYNTAX.sub(" ", element.name))
        kind = " ".join(_LIST_SYNTAX.sub(" ", element.kind).split())[:40]
        return f'{kind} "{name}"'

    async def _read_vision_elements(
        self, hwnd: int, start_id: int, *, limit: float
    ) -> list[UiElement]:
        """UIA yetersiz kaldığında Windows OCR ile görsel öğe listesi çıkarır."""
        # Süre dolduysa veya Esc basıldıysa engelleyici thread işi başlatılmaz.
        if self._stop.is_set() or self._clock() >= limit:
            return []
        reader = getattr(self._backend, "vision_elements", None)
        try:
            if reader is not None:
                return list(await asyncio.to_thread(reader, hwnd, start_id))
            from artemis.computer.vision_elements import vision_elements

            return list(await asyncio.to_thread(vision_elements, hwnd, start_id=start_id))
        except (OSError, RuntimeError, ValueError):
            return []

    async def _read_elements(self, hwnd: int, *, limit: float) -> list[UiElement]:
        """Ön plan penceresinin UIA öğelerini okur; azsa görsel OCR öğeleriyle tamamlar."""
        if hwnd <= 0:
            return []
        # Süre dolduysa veya Esc basıldıysa UIA/OCR okuma iş parçacığı başlatılmaz.
        if self._stop.is_set() or self._clock() >= limit:
            return []
        reader = getattr(self._backend, "ui_elements", None)
        elements: list[UiElement] = []
        if reader is not None:
            try:
                elements = list(await asyncio.to_thread(reader, hwnd))
            except (OSError, RuntimeError, ValueError):
                elements = []
        self._last_used_vision = False
        interactive_count = sum(1 for item in elements if item.interactive)
        if interactive_count < _MIN_INTERACTIVE_UIA:
            visual = await self._read_vision_elements(
                hwnd, start_id=len(elements) + 1, limit=limit
            )
            if visual:
                self._last_used_vision = True
                self._audit.emit(
                    "computer_vision_fallback",
                    hwnd=hwnd,
                    uia_count=len(elements),
                    vision_count=len(visual),
                )
                elements.extend(visual)
        return elements

    async def _capture(
        self, limit: float, *, with_images: bool = True, emit_frame: bool = True
    ) -> list[_Screen] | SessionResult:
        """Her monitörün taze görüntüsünü (ya da `with_images=False` iken yalnız düzenini) alır."""
        try:
            # Her monitör ayrı görüntü: sanal masaüstünü tek görüntüye küçültmek
            # yazıyı okunmaz yapardı (ADR 0039 ek karar).
            screens: list[_Screen] = []
            monitor_rects = await asyncio.to_thread(self._backend.monitors)
            self._layout = None
            try:
                window = await asyncio.to_thread(self._backend.foreground_window)
                self._layout = _Layout(list(monitor_rects), window)
            except (OSError, RuntimeError):
                # Düzen kaydedilemedi: _act bunu "doğrulanamadı" sayıp girdi göndermez.
                pass
            if with_images:
                # Geç yükleme: png numpy ister; numpy yalnız ses/hafıza ekleriyle gelir ve araç
                # keşfi bu modülü içe aktardığı için üstte yüklense temel kurulum çökerdi.
                from artemis.computer.png import screenshot_png

                for left, top, width, height in monitor_rects:
                    if self._clock() >= limit:
                        return self._result("timeout", "Oturum süresi doldu.")
                    if self._stop.is_set():
                        return self._result("stopped", "Kullanıcı oturumu durdurdu.")
                    buffer, width, height = await asyncio.to_thread(
                        self._backend.capture, left, top, width, height
                    )
                    # PNG sıkıştırma ana döngüyü bloklamasın (Esc/iptal gecikmesin).
                    png, shot_size = await asyncio.to_thread(
                        screenshot_png, buffer, width, height, self._config.screenshot_width
                    )
                    screens.append(_Screen((left, top), (width, height), shot_size, png))
            else:
                for left, top, width, height in monitor_rects:
                    screens.append(_Screen((left, top), (width, height), (width, height), b""))
        except ImportError:
            return self._result(
                "error", "Ekran görüntüsü için numpy gerekli (`pip install numpy`)."
            )
        except (OSError, RuntimeError, ValueError) as error:
            return self._result("error", f"Ekran görüntüsü alınamadı: {error}")
        if not screens:
            return self._result("error", "Ekran bulunamadı.")
        if with_images and emit_frame and self._on_frame is not None:
            # Bütün monitörler gider: model hepsini görür, kullanıcı da hepsini görmeli.
            self._on_frame(tuple(screen.png for screen in screens))
        return screens

    async def _verify(self, goal: str, turn_id: str, done_text: str, limit: float) -> SessionResult:
        """`done` sonrası taze görüntüyle (ya da `uia` kipinde öğe listesiyle) sonucu doğrular.

        Doğrulama çalışamazsa (sağlayıcı hatası, süre, Esc) oturum nedeni "error" olmaz: iş
        zaten yapılmış olabilir, yalnız doğrulanamamıştır.
        """
        verdict: tuple[bool, str]
        observation = self._config.observation
        with_images = observation in ("screen", "both")
        if self._stop.is_set() or self._clock() >= limit:
            verdict = (False, "Doğrulama çalışamadı: süre doldu ya da durduruldu.")
        else:
            captured = await self._capture(limit, with_images=with_images)
            if isinstance(captured, SessionResult):
                verdict = (False, f"Doğrulama çalışamadı: {captured.summary}")
            else:
                elements_list: list[UiElement] | None = None
                re_captured: SessionResult | None = None
                if observation == "hybrid":
                    hwnd = self._layout.window[0] if self._layout else 0
                    elements_list = await self._read_elements(hwnd, limit=limit)
                    # OCR fallback kullanıldıysa (_last_used_vision) UIA yetersizdi; doğrulama
                    # _loop gibi taze ekran görüntüsünü de eklemelidir.
                    if not elements_list or self._last_used_vision:
                        with_images = True
                        recaptured = await self._recapture_hybrid(
                            elements_list,
                            self._layout.window if self._layout else None,
                            limit,
                        )
                        if isinstance(recaptured, SessionResult):
                            re_captured = recaptured
                        else:
                            captured, elements_list = recaptured
                if re_captured is not None:
                    verdict = (False, f"Doğrulama çalışamadı: {re_captured.summary}")
                elif with_images:
                    elements_block = ""
                    if observation in ("both", "hybrid"):
                        if elements_list is None:
                            hwnd = self._layout.window[0] if self._layout else 0
                            elements_list = await self._read_elements(hwnd, limit=limit)
                        elements_block = "\n" + render_elements(elements_list)
                    messages = build_verify_messages(
                        goal=goal,
                        done_text=done_text,
                        screens=captured,
                        elements_block=elements_block,
                        with_images=True,
                        elements=[],
                    )
                else:
                    if elements_list is None:
                        hwnd = self._layout.window[0] if self._layout else 0
                        elements = await self._read_elements(hwnd, limit=limit)
                    else:
                        elements = elements_list
                    messages = build_verify_messages(
                        goal=goal,
                        done_text=done_text,
                        screens=(),
                        elements_block="",
                        with_images=False,
                        elements=elements,
                    )
                try:
                    parsed = await self._ask(messages, limit, parse_verdict)
                except ProviderError as error:
                    verdict = (False, f"Doğrulama çalışamadı: model çağrısı başarısız ({error}).")
                else:
                    if self._stop.is_set() or self._clock() >= limit:
                        # Esc/süre sonrası gelen "evet" kullanıcının durdurduğu bir oturumu
                        # başarılı göstermesin.
                        verdict = (False, "Doğrulama çalışamadı: süre doldu ya da durduruldu.")
                    elif parsed is None:
                        verdict = (False, "Doğrulama çalışamadı: model geçerli yanıt vermedi.")
                    else:
                        verdict = (parsed.verified, parsed.evidence)
        os_ok, os_text = await self._os_check(limit, ask_ui=verdict[0])
        verdict = (
            verdict[0] and os_ok is not False,
            f"{verdict[1]} | OS: {os_text}",
        )
        self._audit.emit(
            "computer_verify", turn_id=turn_id, verified=verdict[0], evidence=verdict[1],
            os_check="unknown" if os_ok is None else "ok" if os_ok else "contradiction",
        )
        # "Bitti" notu da modelin ekrandan aktardığı metin olabilir; kanıtla aynı süzgeçten geçer.
        note = mask_screen_secrets(done_text)
        return SessionResult("done", note, self._steps, verdict[0], verdict[1])

    async def _recapture_hybrid(
        self, elements: list[UiElement], prev_window: Any, limit: float
    ) -> tuple[list[_Screen], list[UiElement]] | SessionResult:
        """Hybrid yeniden yakalamada pencere değişmişse UIA öğelerini yeni pencereye eşitler."""
        captured = await self._capture(limit, with_images=True)
        if isinstance(captured, SessionResult):
            return captured
        cur_window = self._layout.window if self._layout else None
        if prev_window is None or cur_window is None or cur_window != prev_window:
            elements = await self._read_elements(
                cur_window[0] if cur_window else 0, limit=limit
            )
        return captured, elements

    async def _os_check(self, limit: float, *, ask_ui: bool) -> tuple[bool | None, str]:
        """OS kanıtı (ADR 0070): girdi sonuçları ve yazımda odaktaki alan. Modelin "hayır"ını
        "evet" yapmaz; yalnız "evet"i geri çevirebilir. None: doğrulanamadı."""
        if self._os_issues:
            return False, f"çelişki: {next(iter(self._os_issues.values()))}"
        if not ask_ui or self._last_typed is None or self._stop.is_set():
            return None, "doğrulanamadı"
        text, target_id = self._last_typed
        remaining = limit - self._clock()
        if not text or target_id is None or remaining <= 0:
            return None, "doğrulanamadı"
        try:
            window = await asyncio.to_thread(self._backend.foreground_window)
            if window[0] != target_id:
                return None, "doğrulanamadı"
            found = await asyncio.to_thread(
                self._backend.focused_value_contains, text, min(3.0, remaining)
            )
        except (OSError, RuntimeError):
            return None, "doğrulanamadı"
        if found is None:
            return None, "doğrulanamadı"
        if not found:
            return False, "çelişki: odaktaki alanda yazılan metin bulunamadı"
        return True, "doğrulandı"

    async def _ask[T](
        self,
        messages: list[Message],
        limit: float,
        parse: Callable[[str], T],
        *,
        provider: Provider | None = None,
    ) -> T | None:
        """Modelden eylem/karar ister; bozuksa YENİ görüntü almadan bir kez yeniden dener."""
        target = provider or self._provider
        for attempt in range(2):
            timeout = min(self._provider_timeout, limit - self._clock())
            if timeout <= 0 or self._stop.is_set():
                return None
            completion = await target.complete(messages, [], timeout=timeout)
            try:
                return parse(completion.content)
            except ValueError as error:
                if attempt:
                    return None
                messages.append(Message(role="assistant", content=completion.content))
                # Neden geçersiz olduğunu söylemezsek model aynı hatayı tekrarlıyor.
                messages.append(Message(role="user", content=f"{error}\n{_RETRY_PROMPT}"))
        return None

    def _resolve_action_point(
        self, action: Action, screens: list[_Screen]
    ) -> tuple[tuple[int, int] | None, SessionResult | str | None]:
        """İşaretçi/öğe eyleminin hedef ekran noktasını ve varsa kip/koordinat engelini döner."""
        # Koordinat yalnız görüntülü adımda anlamlıdır ve görüntü koordinatıdır (ölçeklenir). Öğe
        # listesi sanal masaüstü koordinatı gösterir; o öğeye click_element ile gidilir. İki sistemi
        # öğe dikdörtgenine bakarak karıştırmak, pencereyi kaplayan Pane yüzünden her koordinatı
        # ölçeksiz bırakıyordu.
        if (
            self._config.observation == "uia"
            or (self._config.observation == "hybrid" and not self._step_has_image)
        ) and action.type in _POINTER_ACTIONS:
            return None, _NO_COORDS_IN_UIA
        if action.type in _POINTER_ACTIONS and action.screen > len(screens):
            return None, f"Ekran {action.screen} yok; {len(screens)} ekran görülüyor"
        if action.type == "click_element":
            resolved = self._element_point(action)
            return (None, resolved) if isinstance(resolved, str) else (resolved, None)
        if action.x is not None and action.y is not None:
            screen = screens[min(action.screen, len(screens)) - 1]
            try:
                scaled = scale_point(action.x, action.y, screen.shot, screen.size)
                return (scaled[0] + screen.origin[0], scaled[1] + screen.origin[1]), None
            except ValueError as error:
                # Görüntü dışındaki nokta başka bir monitörün sanal koordinatıysa (model listedeki
                # 2. monitör konumunu screen=1 ile verdi) oraya gidilir; değilse geçersizdir.
                if action.screen == 1 and any(
                    item is not screen
                    and item.origin[0] <= action.x < item.origin[0] + item.size[0]
                    and item.origin[1] <= action.y < item.origin[1] + item.size[1]
                    for item in screens
                ):
                    return (action.x, action.y), None
                return None, self._result("invalid", str(error))
        return None, None

    async def _act(
        self,
        action: Action,
        turn_id: str,
        screens: list[_Screen],
        layout: _Layout | None,
        limit: float,
    ) -> SessionResult | str | None:
        """Eylemi denetler ve uygular. Oturum bitecekse sonucu, adım engellendiyse nedeni
        (modele geri bildirilir), adım uygulandıysa None döner."""
        if self._clock() >= limit:
            return self._result("timeout", "Oturum süresi doldu.")
        point, point_error = self._resolve_action_point(action, screens)
        if point_error is not None:
            return point_error
        window: str | None = None
        target_id: int | None = None
        target_kind = "click" if action.type == "click_element" else action.type
        if action.type == "wait":
            await self._sleep(
                min(action.seconds or 0.0, max(0.0, limit - self._clock()))
            )  # arka uca dokunmaz
        else:
            if action.type == "key":
                key_blocked = key_block_reason(parse_keys(action.keys or ""))
                if key_blocked:
                    return key_blocked
            destructive = await self._destructive_target(action, point, turn_id)
            if destructive is not None:
                return destructive
            blocked, window, target_id = await self._target(target_kind, point)
            if blocked:
                if point is not None and layout is not None:
                    wl, wt, wr, wb = layout.window[1]
                    if not (wl <= point[0] < wr and wt <= point[1] < wb):
                        return (
                            f"{blocked} (nokta {point} ön plan penceresi dışında; "
                            "pencere içi koordinat ver veya click_element kullan)"
                        )
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
                    # Öğe tıklamasında kullanıcı neye tıklanacağını (tür + maskeli ad) görür.
                    **(
                        {"oge": self._element_label(action)}
                        if action.type == "click_element"
                        else {}
                    ),
                    "metin": action.text,
                    "tuslar": action.keys,
                    # Model gerekçesi ekrandaki sırları yankılayabilir; onayda maskelenir.
                    "gerekce": mask_screen_secrets(action.reason),
                    "pencere": window,
                },
            )
            if not approved:
                return self._result("rejected", "Hassas adım kullanıcı tarafından reddedildi.")
            # Onay, kullanıcının gördüğü pencereye verildi. Onay penceresi kapanınca odak bir süre
            # Artemis'te kalabilir (beklenir); başka bir pencere öne geçtiyse onay ona geçmez.
            changed = await self._await_approved_target(target_kind, point, target_id, limit)
            if changed:
                return changed
        # Onay beklerken Esc'ye basılmış olabilir: sıradaki eylem çalışmamalı.
        if self._stop.is_set():
            return self._result("stopped", "Kullanıcı oturumu durdurdu.")

        if self._clock() >= limit:
            return self._result("timeout", "Oturum süresi doldu.")
        if action.type != "wait":
            # Onay penceresi odağı kendisi değiştirir; onaylı adımda yakalama anındaki ön plan
            # karşılaştırması atlanır. Bunun yerine hedef, onaylanan pencere kimliğine ve
            # dikdörtgenine bağlıdır; monitör düzeni yine denetlenir.
            stale = await self._stale_reason(layout, check_window=approved is not True)
            if stale:
                return stale
        try:
            outcome, typed = await self._execute(action, point, limit, target_id)
        except (OSError, RuntimeError) as error:
            return self._result("error", f"Eylem uygulanamadı: {error}")
        issue_key = _signature(action, point) or (action.type,)
        if outcome != "sent":
            if outcome == "unsupported":
                self._os_issues[issue_key] = f"{action.type} eylemi arka uçta desteklenmiyor"
                return "Bu arka uç hover eylemini desteklemiyor; farklı bir eylem dene."
            self._os_issues[issue_key] = (
                f"{action.type} girdisi gönderilemedi ({outcome})"
                + (f", {typed} karakter yazıldı" if typed else "")
            )
            stopped: Reason = "stopped" if outcome == "stopped" else "timeout"
            if typed:
                # Yarım yazım başarı değildir ve gönderilen girdi geri alınmış sayılmaz.
                total = len(action.text or "")
                self._audit.emit(
                    "computer_input_partial", turn_id=turn_id, action=action.type,
                    sent=typed, total=total, reason=outcome,
                )
                return self._result(
                    "blocked" if outcome == "target" else stopped,
                    f"Yazım yarıda kesildi ({outcome}): {total} karakterin {typed} tanesi "
                    "gönderildi; gönderilen kısım geri alınmadı.",
                )
            if outcome == "target":
                return _Stale("Hedef pencere girdi gönderilmeden hemen önce değişti")
            return self._result(stopped, "İptal veya süre sınırı nedeniyle girdi gönderilmedi.")

        self._os_issues.pop(issue_key, None)
        if action.type == "type":
            self._last_typed = (action.text or "", target_id)
        elif action.type != "wait":
            self._last_typed = None  # UIA yalnız yazımın hemen ardından anlamlı
        self._steps += 1
        # Model gerekçesi ekrandan gelen sır içerebilir; bellek ve denetimde maskelenir.
        masked_reason = mask_screen_secrets(action.reason)
        self._memory.add(
            StepRecord(
                n=self._steps,
                action=action.type,
                target=describe_action(action),
                reason=masked_reason,
                window=window,
                signature=_signature(action, point),
            )
        )
        self._on_step(f"Adım {self._steps}: {action.reason}")
        self._audit.emit(
            "computer_step",
            turn_id=turn_id,
            step=self._steps,
            action=action.type,
            x=point[0] if point else None,
            y=point[1] if point else None,
            element=action.element,
            keys=action.keys,
            # Yazılan metin parola olabilir: yalnız uzunluk loglanır.
            text_length=len(action.text or "") if action.type == "type" else None,
            reason=masked_reason,
            sensitive=sensitive,
            approved=approved,
            auto_approved=auto_approved,
            screen=action.screen,
            # observed/progress modelin ekrandan türettiği serbest metindir ve ekran verisi
            # (ör. sayfa başlığı) taşıyabilir; sır kalıpları burada filtrelenip
            # kısaltılarak yazılır.
            observed=mask_screen_secrets(action.observed or "")[:200] or None,
            progress=mask_screen_secrets(action.progress or "")[:200] or None,
        )
        return None

    async def _stale_reason(self, layout: _Layout | None, *, check_window: bool) -> str | None:
        """Görüntü alındıktan beri ekran düzeni/ön plan değiştiyse engel nedeni döner.

        Model saniyelerce düşünür; bu sürede monitör çıkarılmış/taşınmış, DPI değişmiş, bir
        açılır pencere odağı almış ya da hedef pencere kaymış olabilir. Koordinatlar yakalama
        anındaki düzene göre ölçeklendiği için girdi modelin görmediği yere düşerdi; süreç
        denylist'i bunu yakalamaz (pencere hâlâ "izinli"). Okunamıyorsa güvenli sayılmaz.
        """
        if layout is None:
            return _Stale("Ekran düzeni doğrulanamadı")
        try:
            monitors = await asyncio.to_thread(self._backend.monitors)
            window = (
                await asyncio.to_thread(self._backend.foreground_window)
                if check_window
                else None
            )
            # Onay diyalogu odağı çaldığı için kimlik değişimi atlanır; ancak aynı pencere
            # onay sırasında taşınmış/boyutlanmışsa koordinatlar eskimiş demektir.
            current_rect = (
                None
                if check_window
                else await asyncio.to_thread(self._backend.window_rect, layout.window[0])
            )
        except (OSError, RuntimeError) as error:
            return _Stale(f"Ekran düzeni okunamadı ({error})")
        if monitors != layout.monitors:
            return _Stale("Ekran düzeni değişti")
        if check_window and window != layout.window:
            return _Stale("Ön plandaki pencere değişti veya taşındı")
        if not check_window and current_rect != layout.window[1]:
            return _Stale("Hedef pencere onay sırasında taşındı veya boyutu değişti")
        return None

    def _holder(self, action_type: str, point: tuple[int, int] | None) -> tuple[str, int]:
        """Girdiyi alacak pencerenin (exe, pid): tıklama/kaydırma noktaya, yazma/tuş ön plana."""
        if action_type in _POINTER_ACTIONS and point is not None:
            return self._backend.process_at(point[0], point[1])
        return self._backend.foreground_process()

    def _current_window(self, action_type: str, point: tuple[int, int] | None) -> int | None:
        """Girdiyi alacak pencerenin kimliği; okunamazsa None (güvenli sayılmaz)."""
        try:
            if action_type in _POINTER_ACTIONS and point is not None:
                return self._backend.window_at(point[0], point[1])
            return self._backend.foreground_window()[0]
        except (OSError, RuntimeError):
            return None

    async def _await_approved_target(
        self, action_type: str, point: tuple[int, int] | None, approved: int | None, limit: float
    ) -> str | None:
        """Onaylanan pencere yeniden girdi hedefi olana dek (kısa süre) bekler.

        Artemis'in kendi penceresi (onay penceresi/panel) önde kaldıkça beklenir; bütün pencere
        denetimini kaldırmak yerine yalnız bu geçici odak tanınır. Başka bir pencere öne geçtiyse
        ya da süre dolduysa adım `_Stale` ile yeniden planlanır: sonraki adım yine onaya gelir.
        """
        give_up = min(limit, self._clock() + _REFOCUS_SECONDS)
        while not self._stop.is_set():
            try:
                exe, pid = await asyncio.to_thread(self._holder, action_type, point)
            except (OSError, RuntimeError):
                return "Hedef pencere belirlenemedi."
            current = await asyncio.to_thread(self._current_window, action_type, point)
            if pid == self._own_pid:
                if self._clock() >= give_up:
                    return _Stale("Onaylanan pencere yeniden öne gelmedi; girdi gönderilmedi")
                await self._sleep(_REFOCUS_POLL_SECONDS)
                continue
            if current is None or current != approved:
                return _Stale(
                    "Onay verilen pencere değişti; girdi gönderilmedi, adım yeniden onaya gelir"
                )
            return process_block_reason(exe, pid, self._own_pid)
        return None  # durdurma çağıranda denetlenir

    async def _target(
        self, action_type: str, point: tuple[int, int] | None
    ) -> tuple[str | None, str | None, int | None]:
        """(engel nedeni, pencere adı, pencere kimliği): girdinin düşeceği pencere denetlenir.

        Tıklama/kaydırma noktadaki pencereye gider (ve onu öne getirir); yazma/tuş ön plana.
        Tıklamada ön plana bakmak yanlıştı: panelden komut verilince ön plan hep Artemis'ti ve
        ilk tıklama Brave'e olsa da oturum engelleniyordu.
        """
        # Seçim eylem türüne göredir, koordinatın varlığına göre değil: doğrulayıcı type/key
        # eyleminde x/y'yi reddetmiyor; modelin verdiği zararsız bir nokta yazmanın gittiği
        # ön planı (ör. terminal) denetimden kaçırmasın.
        if action_type in _POINTER_ACTIONS and point is None:
            return "İşaretçi eyleminde konum yok.", None, None
        exe, pid = await asyncio.to_thread(self._holder, action_type, point)
        if not exe:
            return "Hedef pencere belirlenemedi.", None, None
        blocked = process_block_reason(exe, pid, self._own_pid)
        if blocked:
            return blocked, None, None
        target_id = await asyncio.to_thread(self._current_window, action_type, point)
        if target_id is None:
            return "Hedef pencere belirlenemedi.", None, None
        return None, exe.replace("\\", "/").rsplit("/", 1)[-1], target_id

    async def _execute(
        self,
        action: Action,
        point: tuple[int, int] | None,
        limit: float,
        target_id: int | None,
    ) -> tuple[str, int]:
        """(sonuç, yazılan kod noktası): "sent", "stopped", "timeout" ya da "target".

        Son denetim OS girdisinden hemen önce, girdiyi gönderen thread'dedir (executor
        kuyruğunda beklenmiş olabilir): iptal, mutlak süre ve hedef pencere kimliği. Yazımda
        aynı denetim her karakterden önce yinelenir: Esc/süre sonrası ya da odak başka pencereye
        geçince kalan metin gönderilmez (ADR 0056).
        """

        target_kind = "click" if action.type == "click_element" else action.type

        def check() -> str | None:
            if self._stop.is_set():
                return "stopped"
            if self._clock() >= limit:
                return "timeout"
            if target_id is not None and self._current_window(target_kind, point) != target_id:
                return "target"
            return None

        def send() -> tuple[str, int]:
            early = check()
            if early:
                return early, 0
            backend = self._backend
            if action.type in ("click", "click_element") and point:
                backend.click(point[0], point[1], action.button, action.double)
            elif action.type == "hover" and point:
                hover_fn = getattr(backend, "hover", None)
                if not callable(hover_fn):
                    return "unsupported", 0
                hover_fn(point[0], point[1])
            elif action.type == "scroll" and point:
                backend.scroll(point[0], point[1], action.dy or 0)
            elif action.type == "type":
                why: list[str] = []

                def should_continue() -> bool:
                    reason = check()
                    if reason:
                        why.append(reason)
                    return reason is None

                typed = backend.type_text(action.text or "", should_continue)
                return (why[0] if why else "sent"), typed
            elif action.type == "key":
                parsed = parse_keys(action.keys or "")
                for index in range(action.repeat):
                    if index > 0:
                        reason = check()
                        if reason:
                            return reason, 0
                    backend.press_keys(parsed)
            return "sent", 0

        # İptalde de girdi thread'i bitene dek görev açık kalır (temizlik sürüyor sayılır).
        return await run_blocking(send)
