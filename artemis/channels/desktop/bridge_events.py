"""Masaüstü köprüsünün olay tipleri ve senkron onay aracısı (bridge.py'den ayrıldı).

GUI'ye akan olay veri sınıfları ile işçi iş parçacığındaki onay bekleyişini yöneten
`_ApprovalBroker` burada durur. `artemis.channels.desktop.bridge` olayları yeniden dışa verir.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, TimeoutError
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from artemis.safety.gate import remember_site, trusted_sites_path
from artemis.tools.base import ToolResult

if TYPE_CHECKING:
    from artemis.channels.desktop.bridge import DesktopBridge


@dataclass(frozen=True)
class State:
    state: str
    message: str = ""
    request_id: str | None = None


@dataclass(frozen=True)
class Finished:
    request_id: str
    result: Any = field(repr=False)


@dataclass(frozen=True)
class ToolFinished:
    request_id: str
    tool_name: str
    result: ToolResult = field(repr=False)


@dataclass(frozen=True)
class Failed:
    request_id: str
    error_type: str
    message: str


@dataclass(frozen=True)
class Approval:
    request_id: str
    turn_id: str
    approval_id: str
    tool_name: str
    arguments: dict[str, Any] = field(repr=False)
    notice: str = ""
    deadline: float | None = None
    # Yalnız gösterim: okunur Türkçe ad. Boşsa arayüz ham tool_name'i başlık yapar.
    tool_label: str = ""
    # Doluysa pencere "bu site için bir daha sorma" seçeneğini de sunar (ADR 0036).
    site: str = ""


@dataclass(frozen=True)
class VoiceCommand:
    """Sesten çıkan komut; yazılı istekle aynı request_id modelinden geçer."""

    request_id: str
    text: str


@dataclass(frozen=True)
class Activity:
    request_id: str | None
    turn_id: str | None
    seq: int
    event: str
    fields: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ActivityGap:
    dropped_count: int


@dataclass(frozen=True)
class Proactive:
    """Artemis'in kendiliğinden mesajı (ADR 0027); hiçbir isteğe ve geçmişe bağlı değildir."""

    title: str
    body: str
    # Doluysa panel bu rutin için "Çalıştır" düğmesi gösterir (ADR 0058).
    routine: str = ""
    # Doluysa düğme bu metni sohbet kutusuna yazar, göndermez (ADR 0061).
    suggest: str = ""
    # Doluysa düğmeler "Aç" ve (vault açıksa) "Vault'a ekle" olur (ADR 0066); değer `YYYY-MM-DD`.
    summary_day: str = ""
    # Dış kaynaklı yer tutucu (dosya adı/yolu, takvim başlığı/konumu) taşıyan öneri.
    suggest_tainted: bool = False


BridgeEvent = (
    State
    | Finished
    | ToolFinished
    | Failed
    | Approval
    | VoiceCommand
    | Activity
    | ActivityGap
    | Proactive
)


@dataclass
class _PendingApproval:
    request_id: str
    turn_id: str
    approval_id: str
    # True/False ya da "site": onayla ve bu site için bir daha sorma (ADR 0036).
    future: Future[bool | str] = field(default_factory=Future, repr=False)


class _ApprovalBroker:
    def __init__(self, bridge: DesktopBridge) -> None:
        self.bridge = bridge
        self.lock = threading.RLock()
        self.pending: _PendingApproval | None = None
        self.request_id: str | None = None
        self.cancel_requested = threading.Event()

    def begin_request(self, request_id: str) -> None:
        with self.lock:
            self.request_id = request_id
            self.cancel_requested.clear()

    def is_cancelled(self) -> bool:
        return self.cancel_requested.is_set()

    def approve(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        bridge = self.bridge
        with self.lock:
            request_id = self.request_id
            runtime = bridge._runtime
            if request_id is None or runtime is None or self.cancel_requested.is_set():
                return False
            # Aynı anda tek onay beklenebilir; ikinci istek ilkinin yerine geçmeden reddedilir.
            if self.pending is not None:
                return False
            approval_id = str(uuid4())
            item = _PendingApproval(request_id, runtime.context.turn_id, approval_id)
            self.pending = item
            notice = runtime.gate.pending_notice
            site = runtime.gate.pending_site
            deadline = runtime.context.deadline
            tool = runtime.registry.get(tool_name)
            tool_label = tool.label if tool is not None else ""
            bridge._events.put(
                Approval(
                    request_id,
                    item.turn_id,
                    approval_id,
                    tool_name,
                    arguments,
                    notice,
                    deadline,
                    tool_label,
                    site,
                )
            )
        timeout = (
            max(0.0, deadline - monotonic())
            if deadline is not None
            else runtime.context.settings.max_seconds
        )
        try:
            decision = item.future.result(timeout=timeout)
            approved = decision is True or decision == "site"
            with self.lock:
                cancelled = self.cancel_requested.is_set()
                if cancelled:
                    # Callback senkron biçimde kapının içindedir; işçi eventi
                    # event loop'a sıraya koymadan önce iptal işaretlenmelidir.
                    runtime.runner.cancel_current()
            if decision == "site" and site and not cancelled:
                try:
                    remember_site(trusted_sites_path(runtime.context.settings.state_dir), site)
                except OSError as error:
                    # Site dosyası yazılamasa da kullanıcının verdiği onay turu bozmasın.
                    with suppress(OSError):
                        runtime.context.audit.emit(
                            "trusted_site_not_saved", error_type=type(error).__name__
                        )
            return approved and not cancelled
        except TimeoutError:
            return False
        finally:
            with self.lock:
                if self.pending is item:
                    self.pending = None

    def decide(
        self,
        request_id: str,
        turn_id: str,
        approval_id: str,
        approved: bool,
        remember_site: bool = False,
    ) -> bool:
        with self.lock:
            item = self.pending
            if (
                item is None
                or item.request_id != request_id
                or item.turn_id != turn_id
                or item.approval_id != approval_id
                or item.future.done()
            ):
                return False
            approved = approved is True
            item.future.set_result("site" if approved and remember_site else approved)
            return True

    def invalidate(self) -> None:
        with self.lock:
            self.cancel_requested.set()
            item = self.pending
            if item is not None and not item.future.done():
                item.future.set_result(False)
