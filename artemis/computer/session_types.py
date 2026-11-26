"""Bilgisayar kullanımı oturumunun tipleri, sabitleri ve döngü imzası (session.py'den ayrıldı).

Oturum sınıfı dışındaki saf tanımlar burada durur: sonuç/ekran/yerleşim veri sınıfları, arka uç
protokolü, ekran kilidi ve tekrar imzası. `artemis.computer.session` hepsini yeniden dışa verir.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from artemis.computer.actions import Action, parse_keys

if TYPE_CHECKING:
    from artemis.computer.windows import UiElement

Reason = Literal[
    "done", "ask_user", "limit", "timeout", "rejected", "stopped", "blocked", "invalid", "error"
]

_RETRY_PROMPT = "Yalnız tek bir geçerli JSON eylem döndür."
_REPEATABLE_KEYS = frozenset(
    (name,) for name in ("up", "down", "left", "right", "tab", "pageup", "pagedown")
)
_REPEAT_BLOCK = (
    "TEKRAR ENGELLENDİ: aynı eylem 3 kez denendi; farklı yöntem dene "
    "(klavye kısayolu/URL) ya da ask_user kullan."
)
# Engellenen adım oturumu hemen bitirmez, modele söylenir (ör. panelden komut verilince
# Artemis penceresi görüntüde durur); bu kadar engelden sonra oturum biter.
_MAX_BLOCKS = 3


class _Stale(str):
    """Görüntü alındıktan sonra ekran değişti: engel hedefin yasaklığından değil, eskilikten."""
# Girdisi noktadaki pencereye düşen eylemler; geri kalan her girdi (type, key) ön plana gider.
_POINTER_ACTIONS = frozenset({"click", "scroll", "hover"})
_MIN_INTERACTIVE_UIA = 5
_NO_COORDS_IN_UIA = "Bu kipte koordinat yok; click_element kullan"
# Deadline'a yaklaşırken son modelin ve ana döngünün yanıt verebilmesi için bırakılan pay.
_DEADLINE_MARGIN = 2.0
# Onay penceresi kapanınca odak bir süre Artemis'te kalabilir; onaylanan pencerenin yeniden
# öne gelmesi en çok bu kadar beklenir, sonra adım yeniden planlanır (ADR 0056).
_REFOCUS_SECONDS = 1.5
_REFOCUS_POLL_SECONDS = 0.1

# Tek fiziksel ekran, tek süreç: masaüstü ve Telegram oturumları aynı fare/klavyeyi sürer
# (ADR 0053). Kilit süreç genelindedir çünkü iki host ayrı iş parçacıklarında, birbirinden
# habersiz kurulur; kilidin sahibi `ComputerHost.session()`'dır.
SCREEN_LOCK = threading.Lock()


class ScreenBusyError(RuntimeError):
    """Başka bir bilgisayar kullanımı oturumu (masaüstü ya da Telegram) ekranı kullanıyor."""


class ComputerBackend(Protocol):
    """`artemis.computer.windows` modülünün kullanılan yüzeyi; testler sahtesini verir."""

    def monitors(self) -> list[tuple[int, int, int, int]]: ...
    def capture(self, left: int, top: int, width: int, height: int) -> tuple[bytes, int, int]: ...
    def foreground_process(self) -> tuple[str, int]: ...
    def foreground_window(self) -> tuple[int, tuple[int, int, int, int]]: ...
    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]: ...
    def process_at(self, x: int, y: int) -> tuple[str, int]: ...
    def window_at(self, x: int, y: int) -> int: ...
    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None: ...
    def hover(self, x: int, y: int) -> None: ...
    def scroll(self, x: int, y: int, notches: int) -> None: ...
    # Her kod noktasından önce `should_continue` sorulur; gönderilen kod noktası sayısı döner.
    def type_text(self, text: str, should_continue: Callable[[], bool]) -> int: ...
    def press_keys(self, keys: tuple[str, ...]) -> None: ...
    # Odaktaki denetimin değeri `expected` metnini içeriyor mu; okunamazsa None (ADR 0070).
    def focused_value_contains(self, expected: str, timeout: float) -> bool | None: ...
    # Odaktaki denetimin UI Automation adı; okunamazsa veya boşsa None (ADR 0078).
    def focused_name(self, timeout: float) -> str | None: ...
    # Ön plan penceresinin UI Automation öğeleri; okunamazsa boş liste (ADR 0073).
    def ui_elements(self, hwnd: int) -> list[UiElement]: ...


class _Audit(Protocol):
    def emit(self, event: str, **fields: Any) -> None: ...


@dataclass(frozen=True)
class SessionResult:
    reason: Reason
    summary: str
    steps: int
    # None: doğrulama denenmedi (done dışı nedenler). False: denendi ya da çalışamadı.
    verified: bool | None = None
    evidence: str = ""


@dataclass(frozen=True)
class _Screen:
    """Bir monitör: sanal masaüstü köşesi, gerçek boyut, küçültülmüş görüntü boyutu ve PNG."""

    origin: tuple[int, int]
    size: tuple[int, int]
    shot: tuple[int, int]
    png: bytes


@dataclass(frozen=True)
class _Layout:
    """Yakalama anındaki monitörler ve ön plan penceresi (hwnd, sol-üst-sağ-alt)."""

    monitors: list[tuple[int, int, int, int]]
    window: tuple[int, tuple[int, int, int, int]]



def _signature(
    action: Action, point: tuple[int, int] | None = None
) -> tuple[Any, ...] | None:
    """click/hover/type/key için döngü imzası; scroll ve wait yasal olarak tekrarlanır, muaftır.

    click_element kimliği her adımda yeniden verilir; imza çözülmüş noktanın 40 px ızgarasıdır,
    yoksa aynı düğmeye tekrar tekrar tıklama koruması hiç tetiklenmezdi."""
    if action.type in ("click", "hover"):
        # 40 px ızgara: bir piksel kayan aynı konum aynı sayılsın.
        return (
            action.type,
            action.screen,
            round((action.x or 0) / 40),
            round((action.y or 0) / 40),
        )
    if action.type == "click_element":
        if point is None:
            return None
        return ("click_element", round(point[0] / 40), round(point[1] / 40))
    if action.type == "type":
        return ("type", action.screen, hash(action.text))
    if action.type == "key":
        keys = parse_keys(action.keys or "")
        # Ok/Tab gibi yürüyüş tuşları (video sarma, form gezinme) yasal olarak art arda basılır.
        if keys in _REPEATABLE_KEYS:
            return None
        return ("key", action.screen, ",".join(keys))
    return None
