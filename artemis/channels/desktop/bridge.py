"""Qt'siz worker, kontrol mesajları ve senkron onay aracısı."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, TimeoutError
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from artemis.brain.agent_loop import RunResult
from artemis.brain.models import Completion, Message
from artemis.brain.router import ProviderRouter
from artemis.brain.streaming import ResponseStream
from artemis.channels.desktop import ptt_bridge
from artemis.channels.desktop.computer_host import ComputerHost
from artemis.channels.desktop.dictation import Dictation, windows_backend
from artemis.channels.desktop.selection import (
    SelectionActions,
    local_chat_alias,
    windows_selection_backend,
)
from artemis.channels.runtime import (
    Approval as ApprovalCallback,
)
from artemis.channels.runtime import (
    AuditObserver,
    Runtime,
    create_runtime,
)
from artemis.channels.telegram.api import TelegramClient, build_telegram_client
from artemis.channels.voice.adapters import SoundDeviceMicrophone
from artemis.channels.voice.core import VoiceController
from artemis.channels.voice.factory import build_dictation_recognizer, build_voice_controller
from artemis.channels.voice.metrics import EventCollector, collector_report, write_report
from artemis.config.settings import DictationConfig
from artemis.i18n import t
from artemis.proactive.delivery import Deliverer
from artemis.proactive.scheduler import ProactiveScheduler
from artemis.proactive.store import ProactiveStore
from artemis.safety.gate import remember_site, trusted_sites_path
from artemis.tools.base import ToolResult
from artemis.tools.code_agent_jobs import ACTIVE, CodeAgentJobs

RuntimeFactory = Callable[[Path, ApprovalCallback, AuditObserver], Runtime]
# İşçi döngüsünde çalışır: runtime, köprünün istek modelinden geçen komut çalıştırıcısı
# ve durum bildirimi alır. Testler gerçek mikrofon yerine sahte denetleyici verir.
VoiceFactory = Callable[
    [Runtime, Callable[[str], Awaitable[RunResult]], Callable[[str], None]],
    Awaitable[VoiceController],
]

_BUSY_TEXT = "Şu an başka bir işlem sürüyor; komut çalıştırılmadı."
# Kapanışta temizliği beklemenin üst sınırı. İptali yutan bir görev uygulamayı sonsuza dek
# "Temizleniyor…"da tutmasın; aşılırsa neyin beklendiği denetime yazılıp döngü durdurulur.
SHUTDOWN_CLEANUP_SECONDS = 15.0
SHUTDOWN_EXECUTOR_SECONDS = 5.0
_VOICE_ERROR_TEXT = "Ses açılamadı; ses paketini, model dosyalarını ve mikrofonu kontrol edin."


def _tool_labels(runtime: Runtime) -> dict[str, tuple[str, str]]:
    """Araç sınıflarındaki okunur adları bir kez toplar; işçi kaydı değiştirilmez.

    Kayıt listesi yerine ``definitions()`` ile ad alınır; böylece registry'nin iç
    sözlüğüne dokunulmaz. Etiketi olmayan araç tabloya girmez ve ham adla görünür.
    """
    labels: dict[str, tuple[str, str]] = {}
    for definition in runtime.registry.definitions():
        tool = runtime.registry.get(definition.name)
        if tool is not None and (tool.label or tool.done_label):
            labels[definition.name] = (tool.label, tool.done_label)
    return labels


def _ignore_reset_on_close(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Windows Proactor, karşı taraf zaten kopardığı TLS soketini kapatırken
    ConnectionResetError (WinError 10054) basar; bağlantı zaten bitmiştir, gürültüdür."""
    reset = isinstance(context.get("exception"), ConnectionResetError)
    if reset and "_call_connection_lost" in repr(context.get("handle")):
        return
    loop.default_exception_handler(context)


async def _complete_selection(
    provider: Any, messages: list[Message], model: str | None, timeout: float
) -> Completion:
    """Seçili metin için yerel model istendiyse yönlendirici olmadan buluta düşmez."""
    if model is None:
        return await provider.complete(messages, [], timeout=timeout)
    if isinstance(provider, ProviderRouter):
        return await provider.complete(messages, [], timeout=timeout, model=model)
    raise RuntimeError("Seçili yerel model yönlendirilemiyor.")


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


class DesktopBridge:
    """Tek işçi loop'u yönetir; GUI thread'i hiçbir asyncio nesnesine dokunmaz."""

    def __init__(
        self,
        config_path: Path,
        runtime_factory: RuntimeFactory | None = None,
        voice_factory: VoiceFactory | None = None,
    ) -> None:
        self._config_path = config_path
        self._runtime_factory = runtime_factory
        self._voice_factory: VoiceFactory = (
            voice_factory if voice_factory is not None else build_voice_controller
        )
        self._events: queue.SimpleQueue[BridgeEvent] = queue.SimpleQueue()
        self._audit_queue: queue.Queue[Activity] = queue.Queue(maxsize=256)
        self._audit_lock = threading.Lock()
        self._audit_dropped = 0
        self._audit_seq = 0
        self._turn_requests: dict[str, str] = {}
        self._lock = threading.RLock()
        self._broker = _ApprovalBroker(self)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runtime: Runtime | None = None
        # Araç etiketleri runtime kurulunca bir kez dolar; GUI yalnız kopya okur.
        self._labels: dict[str, tuple[str, str]] = {}
        self._active_request: str | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._cleanup_pending = False
        self._shutdown = False
        self._shutdown_deadline: float | None = None
        self._loop_stopping = False
        # Ses durumu _lock altında okunur/yazılır; mikrofon yalnız start_voice ile açılır.
        self._voice_starting = False
        self._voice_stop_requested = False
        self._voice_controller: VoiceController | None = None
        self._voice_task: asyncio.Task[None] | None = None
        self._ptt_holding = False
        self._ptt_auto_voice = False
        self._ptt_drain_task: asyncio.Task[None] | None = None
        # Dikte (ADR 0057) ilk kısayol basışında kurulur; ayar imzası değişince yenilenir.
        self._dictation: Dictation | None = None
        self._dictation_override: DictationConfig | None = None
        self._dictation_signature: tuple[float, int | str | None, str] | None = None
        # Seçili metin eylemleri (ADR 0062) de ilk kısayol basışında işçi döngüsünde kurulur.
        self._selection: SelectionActions | None = None
        # Proaktif zamanlayıcı (ADR 0027) yalnız işçi döngüsünde yaşar.
        self._proactive_stop: asyncio.Event | None = None
        self._proactive_task: asyncio.Task[None] | None = None
        self._proactive_telegram: TelegramClient | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main, name="artemis-desktop", daemon=True
        )
        self._thread.start()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def active_job_count(self) -> int:
        """Sürmekte olan kod ajanı işi sayısı; GUI listesi bayatken doğrudan kaynaktır.

        `status()` kilit altında kopya verir, GUI thread'inden çağırmak güvenlidir.
        """
        runtime = self._runtime
        jobs = runtime.context.services.get("code_agent_jobs") if runtime is not None else None
        if not isinstance(jobs, CodeAgentJobs):
            return 0
        return sum(
            1 for job in jobs.status() if job["status"] in ACTIVE or job.get("cleanup_pending")
        )

    def labels(self) -> dict[str, tuple[str, str]]:
        """Araç adından (onay başlığı, adım satırı) çiftine kopya; GUI bunu değiştiremez."""
        return dict(self._labels)

    @property
    def voice_active(self) -> bool:
        with self._lock:
            return self._voice_starting or self._voice_task is not None

    def start_voice(self) -> bool:
        """Sesi açar (GUI thread'i); mikrofonu yalnız kullanıcının bu çağrısı açar."""
        with self._lock:
            if (
                not self._ready.is_set()
                or self._stopped.is_set()
                or self._shutdown
                or self._voice_starting
                or self._voice_task is not None
            ):
                return False
            self._voice_starting = True
            self._voice_stop_requested = False
            # Önce kendi bayrağı, sonra dikteye bakış: dikte de tersini yapar (aynı anda
            # iki mikrofon açılmaz, dikte sesi asistan komutu sayılmaz).
            dictation = self._dictation
            if dictation is not None and dictation.busy:
                self._voice_starting = False
                self._events.put(State("dictation_message", t("Dikte sürerken ses açılamaz.")))
                return False
        loop = self._loop
        try:
            if loop is None:
                raise RuntimeError("İşçi döngüsü yok.")
            asyncio.run_coroutine_threadsafe(self._start_voice_on_worker(), loop)
        except RuntimeError:
            # Döngü kapanmışsa ses başlamaz; bayrak takılı kalıp yeni denemeyi engellemesin.
            with self._lock:
                self._voice_starting = False
            return False
        return True

    def stop_voice(self) -> bool:
        """Sesi kapatır; "off" olayını denetleyicinin kendi kapanışı yayımlar."""
        with self._lock:
            if not (self._voice_starting or self._voice_task is not None):
                return False
            self._voice_stop_requested = True
        loop = self._loop
        if loop is None:
            return False
        try:
            loop.call_soon_threadsafe(self._stop_voice_on_worker)
        except RuntimeError:
            return False
        return True

    def ptt_press(self) -> bool:
        """Bas-konuş başlangıcı (GUI thread'i): aktif sese bağlanır veya geçici ses başlatır."""
        return ptt_bridge.ptt_press(self, State)

    def ptt_release(self) -> bool:
        """Bas-konuş bitişi (GUI thread'i): biriken sesi komut olarak işler."""
        return ptt_bridge.ptt_release(self)

    def toggle_dictation(self) -> bool:
        """Dikte kısayolu (GUI thread'i): kayıt başlatır ya da durdurup yazdırır (ADR 0057)."""
        loop = self._loop
        if loop is None or not self._ready.is_set() or self._shutdown:
            return False
        try:
            loop.call_soon_threadsafe(self._toggle_dictation_on_worker)
        except RuntimeError:
            return False
        return True

    def reload_dictation_settings(self, dictation: DictationConfig) -> bool:
        """Ayarlar kaydedilince dikte ayarlarını sekme açıkken de canlıya alır."""
        loop = self._loop
        if loop is None or not self._ready.is_set() or self._shutdown:
            return False
        try:
            loop.call_soon_threadsafe(self._reload_dictation_settings_on_worker, dictation)
        except RuntimeError:
            return False
        return True

    def _reload_dictation_settings_on_worker(self, dictation: DictationConfig) -> None:
        runtime = self._runtime
        if runtime is None or self._shutdown:
            return
        self._dictation_override = dictation
        runtime.context.settings = runtime.context.settings.model_copy(
            update={"dictation": dictation}
        )
        if self._dictation is not None and not self._dictation.busy:
            self._dictation.shutdown()
            self._dictation = None
            self._dictation_signature = None

    def _toggle_dictation_on_worker(self) -> None:
        runtime = self._runtime
        if runtime is None or self._shutdown:
            return
        settings = runtime.settings
        dictation_cfg = self._dictation_override or settings.dictation
        if not dictation_cfg.enabled:
            return
        signature = (
            dictation_cfg.max_seconds,
            settings.voice.input_device,
            settings.voice.stt_mode,
        )
        if (
            self._dictation is not None
            and not self._dictation.busy
            and signature != self._dictation_signature
        ):
            self._dictation.shutdown()
            self._dictation = None
        if self._dictation is None:
            self._dictation_signature = signature
            self._dictation = Dictation(
                max_seconds=dictation_cfg.max_seconds,
                audit=runtime.audit,
                open_microphone=lambda: SoundDeviceMicrophone(
                    settings.voice, device=settings.voice.input_device
                ),
                get_recognizer=lambda: build_dictation_recognizer(runtime),
                backend=windows_backend(),
                emit=lambda kind, text: self._events.put(State(f"dictation_{kind}", text)),
                voice_active=lambda: self.voice_active,
                other_busy=lambda: self._selection is not None and self._selection.busy,
            )
        self._dictation.toggle()

    def selection_hotkey(self) -> bool:
        """Seçili metin kısayolu (GUI thread'i): seçimi yakalar (ADR 0062)."""
        return self._selection_call(lambda actions: actions.start_capture())

    def selection_run(self, action: str) -> bool:
        return self._selection_call(lambda actions: actions.start_run(action))

    def selection_write_back(self) -> bool:
        return self._selection_call(lambda actions: actions.start_write_back())

    def budget_target(self) -> tuple[Path, Decimal] | None:
        """Aktif çalışma zamanının bütçe veritabanı yolunu ve günlük sınırını döndürür."""
        runtime = self._runtime
        if runtime is None:
            return None
        return runtime.settings.state_dir / "budget.sqlite3", runtime.settings.daily_budget_usd

    def _selection_call(self, call: Callable[[SelectionActions], None]) -> bool:
        loop = self._loop
        if loop is None or not self._ready.is_set() or self._shutdown:
            return False
        try:
            loop.call_soon_threadsafe(self._selection_on_worker, call)
        except RuntimeError:
            return False
        return True

    def _selection_on_worker(self, call: Callable[[SelectionActions], None]) -> None:
        runtime = self._runtime
        if runtime is None or self._shutdown or not runtime.settings.selection_actions.enabled:
            return
        if self._selection is None:
            settings = runtime.settings

            async def complete(
                messages: list[Message], model: str | None, timeout: float
            ) -> Completion:
                # ARAÇSIZ: seçili metin dış veridir, hiçbir araç çağrısı açılmaz (ADR 0062).
                return await _complete_selection(runtime.provider, messages, model, timeout)

            self._selection = SelectionActions(
                audit=runtime.audit,
                backend=windows_selection_backend(windows_backend()),
                complete=complete,
                emit=lambda kind, text: self._events.put(State(f"selection_{kind}", text)),
                translate_to=settings.selection_actions.translate_to,
                cloud_allowed=settings.allow_cloud_clipboard,
                local_alias=local_chat_alias(settings),
                other_busy=lambda: self._dictation is not None and self._dictation.busy,
                timeout=min(settings.provider_timeout_seconds * 2, settings.max_seconds),
            )
        call(self._selection)

    def _post(self, callback: Callable[..., None], *args: Any) -> bool:
        loop = self._loop
        if loop is None:
            return False
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            return False
        return True

    def submit(self, text: str) -> str | None:
        request_id = str(uuid4())
        with self._lock:
            if (
                not self._ready.is_set()
                or self._stopped.is_set()
                or self._shutdown
                or self._active_request is not None
            ):
                return None
            if self._cleanup_pending:
                return None
            self._active_request = request_id
            self._broker.begin_request(request_id)
        if not self._post(self._start_turn, request_id, text):
            with self._lock:
                if self._active_request == request_id:
                    self._active_request = None
            return None
        return request_id

    def submit_tool(self, name: str, arguments: BaseModel, quiet: bool = False) -> str | None:
        """Tipli GUI isteği aynı worker ve onay aracısından geçer; model çağrılmaz.

        `quiet` arka plan yoklaması içindir: "Çalışıyor" durumu yayınlanmaz, böylece
        pet ve düğmeler her yoklamada meşgule dönmez."""
        if len(arguments.model_dump_json().encode("utf-8")) > 32 * 1024:
            return None
        request_id = str(uuid4())
        with self._lock:
            if (
                not self._ready.is_set()
                or self._stopped.is_set()
                or self._shutdown
                or self._active_request is not None
                or self._cleanup_pending
            ):
                return None
            self._active_request = request_id
            self._broker.begin_request(request_id)
        if not self._post(
            self._start_tool, request_id, name, arguments.model_copy(deep=True), quiet
        ):
            with self._lock:
                if self._active_request == request_id:
                    self._active_request = None
            return None
        return request_id

    def _start_tool(
        self, request_id: str, name: str, arguments: BaseModel, quiet: bool = False
    ) -> None:
        if not quiet:
            self._events.put(State("running", t("Araç isteği çalışıyor."), request_id))
        self._active_task = asyncio.create_task(self._run_tool(request_id, name, arguments))

    async def _run_tool(self, request_id: str, name: str, arguments: BaseModel) -> None:
        runtime = self._runtime
        delivered = False
        try:
            if runtime is None or self._shutdown or self._broker.is_cancelled():
                self._events.put(
                    Failed(request_id, "Cancelled", t("Araç başlamadan iptal edildi."))
                )
                delivered = True
                return
            result = await runtime.runner.dispatch_tool(name, arguments)
            self._enqueue_activity(request_id, "turn_end", {"stop_reason": "completed"})
            self._events.put(ToolFinished(request_id, name, result))
            delivered = True
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            self._events.put(Failed(request_id, type(error).__name__, t("Araç tamamlanamadı.")))
            delivered = True
        finally:
            # Beklenmeyen hata yukarı çıkarken GUI'nin takılı kalmaması için olay üretilir.
            if not delivered:
                self._events.put(Failed(request_id, "Interrupted", t("Araç tamamlanamadı.")))
            self._finish_request(request_id)

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            if self._active_request != request_id or self._shutdown:
                return False
            # İptal kilidi kilit içinde alınır; aksi halde araya giren yeni isteği iptal ederdi.
            self._broker.invalidate()
        return self._post(self._cancel_on_worker, request_id)

    def cancel_active(self) -> bool:
        """Kimliği bilmeyen çağıran (Esc kısayolu) için o an yürüyen isteği iptal eder."""
        with self._lock:
            request_id = self._active_request
        return request_id is not None and self.cancel(request_id)

    def reset_conversation(self) -> bool:
        """Yeni sohbeti başlatır; runner'a yalnız işçi loop'u dokunur (GUI thread'i değil)."""
        with self._lock:
            if (
                not self._ready.is_set()
                or self._stopped.is_set()
                or self._shutdown
                or self._active_request is not None
                or self._cleanup_pending
            ):
                return False
        return self._post(self._reset_on_worker)

    def decide(
        self,
        request_id: str,
        turn_id: str,
        approval_id: str,
        approved: bool,
        remember_site: bool = False,
    ) -> bool:
        return self._broker.decide(request_id, turn_id, approval_id, approved, remember_site)

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown or self._stopped.is_set():
                return
            self._shutdown = True
            request_id = self._active_request
        self._events.put(State("stopping", t("Temizleniyor.")))
        self._broker.invalidate()
        self._post(self._shutdown_on_worker, request_id)

    def poll_events(self, limit: int = 64) -> list[BridgeEvent]:
        result: list[BridgeEvent] = []
        for _ in range(max(0, limit)):
            try:
                result.append(self._events.get_nowait())
            except queue.Empty:
                break
        with self._audit_lock:
            dropped, self._audit_dropped = self._audit_dropped, 0
        if dropped:
            result.append(ActivityGap(dropped))
        while len(result) < limit:
            try:
                result.append(self._audit_queue.get_nowait())
            except queue.Empty:
                break
        return result

    def _enqueue_activity(
        self,
        request_id: str | None,
        event_name: str,
        fields: dict[str, Any],
        turn_id: str | None = None,
    ) -> None:
        with self._audit_lock:
            self._audit_seq += 1
            event = Activity(
                request_id=request_id,
                turn_id=turn_id,
                seq=self._audit_seq,
                event=event_name,
                fields=fields,
            )
            try:
                self._audit_queue.put_nowait(event)
            except queue.Full:
                with suppress(queue.Empty):
                    self._audit_queue.get_nowait()
                self._audit_queue.put_nowait(event)
                self._audit_dropped += 1

    def _on_record(self, record: dict[str, Any]) -> None:
        event_name = str(record.get("event", "unknown"))
        fields = {key: value for key, value in record.items() if key not in {"event", "timestamp"}}
        serialized = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 16 * 1024:
            summary: dict[str, Any] = {
                "summary": t("Denetim olayı görünüm için kısaltıldı."),
                "original_bytes": len(serialized.encode("utf-8")),
            }
            for key in ("turn_id", "tool"):
                if isinstance(fields.get(key), (str, int, float)):
                    summary[key] = fields[key]
            result = fields.get("result")
            if event_name == "tool_result" and isinstance(result, dict):
                summary["result"] = {
                    key: result[key] for key in ("status", "success", "message") if key in result
                }
            fields = summary
        with self._audit_lock:
            turn = record.get("turn_id")
            if record.get("event") == "turn_start" and turn is not None:
                # Gözetimsiz brifing turu, o sırada yürüyen kullanıcı isteğine yazılmaz.
                owner = "" if record.get("unattended") else self._broker.request_id or ""
                self._turn_requests[str(turn)] = owner
                if len(self._turn_requests) > 64:
                    self._turn_requests.pop(next(iter(self._turn_requests)))
        turn_id = str(record["turn_id"]) if record.get("turn_id") is not None else None
        self._enqueue_activity(
            self._request_for_turn(record.get("turn_id")),
            event_name,
            fields,
            turn_id=turn_id,
        )

    def _request_for_turn(self, turn_id: object) -> str | None:
        with self._audit_lock:
            if turn_id is not None:
                return self._turn_requests.get(str(turn_id))
            return self._broker.request_id

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        loop.set_exception_handler(_ignore_reset_on_close)
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            factory = self._runtime_factory
            if factory is None:
                self._runtime = create_runtime(
                    self._config_path, self._approve, on_record=self._on_record
                )
            else:
                self._runtime = factory(self._config_path, self._approve, self._on_record)
            self._labels = _tool_labels(self._runtime)
            # Esc ve hassas adım onayı yalnız masaüstünde var; computer_use başka yerde reddeder.
            self._runtime.context.services["computer_host"] = ComputerHost(
                self._approve,
                lambda text: self._events.put(State("computer", text)),
                on_escape=self.cancel_active,
            )
        except Exception as error:  # İşçi sınırında traceback/sır sızdırmadan arızayı GUI'ye taşır.
            self._events.put(
                Failed(
                    "",
                    type(error).__name__,
                    t("Runtime başlatılamadı; ayarları kontrol edin."),
                )
            )
            loop.close()
            self._stopped.set()
            self._events.put(State("stopped", t("İşçi durdu.")))
            return
        self._ready.set()
        configured = self._runtime.settings
        model = configured.models.get(configured.primary_model)
        display_model = model.model if model is not None else configured.primary_model
        self._events.put(State("ready", t("Hazır · {model}").format(model=display_model)))
        loop.call_soon(self._start_proactive)
        if self._shutdown:
            loop.call_soon(loop.stop)
        try:
            loop.run_forever()
        finally:
            # Kalan _stop_loop_when_clean çağrıları run_until_complete'i kesmesin.
            self._loop_stopping = True
            loop.run_until_complete(loop.shutdown_asyncgens())
            # Takılmış bir thread işçinin "durdu" demesini sonsuza dek engellemesin. Araç
            # thread'leri kendi zaman aşımlarıyla biter; süre aşılırsa join edilmeden geçilir.
            with suppress(TimeoutError):
                loop.run_until_complete(
                    asyncio.wait_for(loop.shutdown_default_executor(), SHUTDOWN_EXECUTOR_SECONDS)
                )
            loop.close()
            self._stopped.set()
            self._events.put(State("stopped", t("İşçi durdu.")))

    def _start_proactive(self) -> None:
        runtime = self._runtime
        if runtime is None or self._shutdown or not runtime.settings.proactive.enabled:
            return
        store = runtime.context.services.get("proactive_store")
        if not isinstance(store, ProactiveStore):
            return
        self._proactive_telegram = build_telegram_client(runtime.settings, runtime.audit)
        deliverer = Deliverer(
            runtime.settings,
            runtime.audit,
            panel=self._proactive_panel,
            routine_panel=self._proactive_routine_panel,
            telegram=self._proactive_telegram,
        )
        scheduler = ProactiveScheduler(runtime, store, deliverer)
        self._proactive_stop = asyncio.Event()
        self._proactive_task = asyncio.get_running_loop().create_task(
            scheduler.serve(self._proactive_stop)
        )

    def _proactive_panel(self, title: str, body: str) -> bool:
        if self._shutdown:
            return False
        self._events.put(Proactive(title, body))
        return True

    def _proactive_routine_panel(
        self, title: str, body: str, routine: str, suggest: str, summary_day: str
    ) -> bool:
        if self._shutdown:
            return False
        self._events.put(Proactive(title, body, routine, suggest, summary_day))
        return True

    def _stop_proactive(self) -> None:
        if self._proactive_stop is not None:
            self._proactive_stop.set()
        task = self._proactive_task
        if task is not None and not task.done():
            # Yürüyen brifing iptal edilir; talep edilmiş oluşum sonradan belirsiz raporlanır.
            task.cancel()

    def _close_proactive_client(self) -> None:
        client, self._proactive_telegram = self._proactive_telegram, None
        if client is not None:
            client.close()

    def _start_turn(self, request_id: str, text: str) -> None:
        if self._shutdown or self._runtime is None or self._runtime.agent.cleanup_pending:
            self._events.put(Failed(request_id, "CleanupPending", t("Önceki işlem temizleniyor.")))
            self._finish_request(request_id)
            return
        if self._broker.is_cancelled():
            self._events.put(State("cancelled", t("İstek başlamadan iptal edildi."), request_id))
            self._finish_request(request_id)
            return
        self._events.put(State("running", t("İstek çalışıyor."), request_id))
        self._active_task = asyncio.create_task(self._run_turn(request_id, text))

    async def _run_turn(self, request_id: str, text: str) -> None:
        # Yazılı yolda iptal, görev bitişi sayılır; ses yolu ise iptali yukarı taşır.
        with suppress(asyncio.CancelledError):
            await self._execute_request(request_id, text)

    async def _execute_request(
        self, request_id: str, text: str, stream: ResponseStream | None = None,
        audio_wav: bytes | None = None,
    ) -> RunResult | None:
        """Yazılı ve sesli isteğin ortak turu: aynı olaylar, aynı temizlik, aynı bitiş."""
        runtime = self._runtime
        if runtime is None:
            return None
        if self._broker.is_cancelled():
            self._events.put(State("cancelled", t("İstek başlamadan iptal edildi."), request_id))
            self._finish_request(request_id)
            return None
        result: RunResult | None = None
        try:
            kwargs: dict[str, Any] = {}
            if stream is not None:
                kwargs["stream"] = stream
            if audio_wav is not None:
                kwargs["audio_wav"] = audio_wav
            result = await runtime.runner.run(text, **kwargs)
            with self._audit_lock:
                self._turn_requests[result.turn_id] = request_id
            self._events.put(Finished(request_id, result))
        except Exception as error:  # İşçi sınırı: sır/traceback sızdırmadan GUI'ye bildirilir.
            self._events.put(Failed(request_id, type(error).__name__, t("İstek tamamlanamadı.")))
        except asyncio.CancelledError as error:
            self._events.put(Failed(request_id, type(error).__name__, t("İstek tamamlanamadı.")))
            raise
        finally:
            if runtime.agent.cleanup_pending:
                with self._lock:
                    self._cleanup_pending = True
                self._events.put(
                    State("cleanup_pending", t("Önceki işlem temizleniyor."), request_id)
                )
                self._cleanup_task = asyncio.create_task(self._finish_cleanup(request_id))
            else:
                self._finish_request(request_id)
        return result

    async def _run_voice_command(
        self, text: str, *, stream: ResponseStream | None = None,
        audio_wav: bytes | None = None,
    ) -> RunResult:
        """Sesli komut, yazılı istekle aynı anda yürümez; meşgulse runner'a hiç gitmez."""
        with self._lock:
            if self._active_request is not None or self._cleanup_pending or self._shutdown:
                return RunResult(text=t(_BUSY_TEXT), stop_reason="busy", turn_id="")
            request_id = str(uuid4())
            self._active_request = request_id
            self._broker.begin_request(request_id)
        self._events.put(VoiceCommand(request_id, text))
        self._events.put(State("running", t("Sesli istek çalışıyor."), request_id))
        # Söz kesme bu görevi iptal eder; CancelledError temizlikten sonra yukarı çıkar.
        result = await self._execute_request(request_id, text, stream, audio_wav=audio_wav)
        if result is not None:
            return result
        if self._broker.is_cancelled():
            return RunResult(text="", stop_reason="cancelled", turn_id="")
        return RunResult(text=t("İstek tamamlanamadı."), stop_reason="error", turn_id="")

    async def _finish_cleanup(self, request_id: str) -> None:
        runtime = self._runtime
        # Kapanışta bekleme `_stop_loop_when_clean`'e devredilir: o hâlâ temizliği bekler ama
        # üst süreyle sınırlıdır; burada sınırsız döngü uygulamayı kapanmaz bırakırdı.
        while runtime is not None and runtime.agent.cleanup_pending and not self._shutdown:
            await asyncio.sleep(0.05)
        with self._lock:
            self._cleanup_pending = False
        self._finish_request(request_id)

    def _finish_request(self, request_id: str) -> None:
        with self._lock:
            if self._active_request == request_id:
                self._active_request = None
        self._active_task = None
        if self._shutdown:
            asyncio.get_running_loop().call_soon(self._stop_loop_when_clean)
        else:
            self._events.put(State("ready", t("Hazır."), request_id))

    def _cancel_on_worker(self, request_id: str) -> None:
        with self._lock:
            active_request = self._active_request
        if active_request != request_id or self._runtime is None:
            return
        self._runtime.runner.cancel_current()
        self._events.put(
            State(
                "cancelling",
                t("İptal ediliyor; başlayan sistem işlemleri geri alınmaz."),
                request_id,
            )
        )

    def _reset_on_worker(self) -> None:
        runtime = self._runtime
        if runtime is None or not runtime.runner.reset_context():
            self._events.put(State("ready", t("Aktif işlem sürerken sohbet sıfırlanamaz.")))
        else:
            self._events.put(State("context_reset", t("Yeni sohbet başladı.")))

    def _approve(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Onay yalnız ekrandaki penceredir; ses açıksa onay boyunca mikrofon duraklar.

        İmza `(tool_name, arguments) -> bool` kalır. Duraklatma, onay bekleyen kullanıcının
        konuşması veya hoparlör sesi sonradan komut/"evet" olarak işlenmesin diye
        biriken sesi atar; sesli bir cevap hiçbir yoldan onay üretemez.
        """
        controller = self._voice_controller
        if controller is not None:
            controller.pause_for_approval()
        try:
            return self._broker.approve(tool_name, arguments)
        finally:
            if controller is not None:
                controller.resume_after_approval()

    def _emit_voice_state(self, state: str) -> None:
        self._events.put(State("voice", state))

    def _on_voice_state(self, state: str) -> None:
        if state.startswith("level:"):
            self._events.put(State("voice_level", state.removeprefix("level:")))
            return
        if state.startswith("mode:"):
            self._events.put(State("voice_mode", state.removeprefix("mode:")))
            return
        if state.startswith("denoise:"):
            self._events.put(State("voice_denoise", state.removeprefix("denoise:")))
            return
        if state.startswith("live:unavailable:"):
            self._events.put(
                State("voice_live_unavailable", state.removeprefix("live:unavailable:"))
            )
            return
        if state.startswith("live:"):
            self._events.put(State("voice_live", state.removeprefix("live:")))
            return
        if state.startswith("echo:"):
            self._events.put(State("voice_echo", state.removeprefix("echo:")))
            return
        if state.startswith("game:"):
            self._events.put(State("voice_game", state.removeprefix("game:")))
            return
        if state.startswith("heard:"):
            self._events.put(State("voice_heard", state.removeprefix("heard:")))
            return
        # "off" burada iletilmez: kapanışı _run_voice sonlandırır, böylece hata iletisi
        # her zaman "off"tan önce gelir ve referanslar temizlendikten sonra görünür.
        if state != "off":
            self._emit_voice_state(state)

    def _voice_failed(self, detail: str = "") -> None:
        # Ayar hatası (ValueError) kendi Türkçe nedenini taşır ve kullanıcının düzeltebileceği
        # şeyi söyler (ör. ücretli ses izni); aygıt/dosya hatasının ayrıntısı gösterilmez.
        text = (
            t("Ses açılamadı: {detail}").format(detail=detail[:300])
            if detail
            else t(_VOICE_ERROR_TEXT)
        )
        self._events.put(State("voice_error", text))

    async def _start_voice_on_worker(self) -> None:
        runtime = self._runtime
        if runtime is None or self._shutdown:
            with self._lock:
                self._voice_starting = False
            return
        handed_over = False
        self._emit_voice_state("preparing")
        try:
            try:
                controller = await self._voice_factory(
                    runtime, self._run_voice_command, self._on_voice_state
                )
            except ValueError as error:
                self._voice_failed(str(error))
                return
            except (OSError, RuntimeError, ImportError):
                self._voice_failed()
                return
            # Ölçüm isteğe bağlıdır; factory imzası değişmesin diye toplayıcı sonradan bağlanır.
            collector = EventCollector() if runtime.settings.voice.telemetry_report else None
            if collector is not None:
                controller.on_event = collector
            with self._lock:
                abandoned = (
                    self._voice_stop_requested
                    or self._shutdown
                    or (self._ptt_auto_voice and not self._ptt_holding)
                )
                if not abandoned:
                    self._voice_controller = controller
                    # Model `voice_continuous` aracıyla kipi değiştirebilsin;
                    # yalnız ses çalışırken var.
                    runtime.context.services["voice_controller"] = controller
                    self._voice_task = asyncio.create_task(self._run_voice(controller, collector))
                    handed_over = True
                    if self._ptt_holding:
                        controller.ptt_press()
                else:
                    self._ptt_auto_voice = False
            if abandoned:
                # Hazırlık sürerken kapatıldı: mikrofon hiç açılmadan kaynaklar bırakılır.
                await controller.close()
        finally:
            # Beklenmeyen hata veya close() arızasında başlangıç bayrağı takılı kalmasın.
            with self._lock:
                self._voice_starting = False
                if not handed_over:
                    self._ptt_auto_voice = False
                    self._ptt_holding = False
            if not handed_over:
                self._emit_voice_state("off")

    async def _run_voice(
        self, controller: VoiceController, collector: EventCollector | None = None,
    ) -> None:
        try:
            await controller.run()
        except (OSError, RuntimeError, ValueError, ImportError):
            self._voice_failed()
        finally:
            with self._lock:
                self._voice_task = None
                self._voice_controller = None
                if self._runtime is not None:
                    self._runtime.context.services.pop("voice_controller", None)
                self._voice_stop_requested = False
                self._ptt_auto_voice = False
                self._ptt_holding = False
                self._ptt_drain_task = None
            if collector is not None:
                await self._export_voice_telemetry(controller, collector)
            self._emit_voice_state("off")

    async def _export_voice_telemetry(
        self, controller: VoiceController, collector: EventCollector,
    ) -> None:
        """Ses kapanınca içeriksiz rapor yazılır; yazılamazsa ses kapanışı bozulmaz."""
        runtime = self._runtime
        if runtime is None:
            return
        report = collector_report(collector, "masaustu")
        # Yalnız gerçek sounddevice çıkışı bu bilgiyi taşır; başka sink için null kalır.
        output = getattr(getattr(controller, "sink", None), "output_info", None)
        report["hardware"] = {"output": output}
        try:
            path = await asyncio.to_thread(
                write_report, report, runtime.settings.state_dir / "voice-telemetry"
            )
        except (OSError, ValueError) as error:
            runtime.audit.emit("voice_telemetry", written=False, error_type=type(error).__name__)
            return
        runtime.audit.emit("voice_telemetry", written=True, path=str(path))

    def set_continuous(self, enabled: bool) -> bool:
        """Çalışan ses denetleyicisinin kesintisiz kipini değiştirir (GUI thread'i)."""
        loop = self._loop
        if loop is None or not self.voice_active:
            return False
        try:
            loop.call_soon_threadsafe(self._set_continuous_on_worker, enabled)
        except RuntimeError:
            return False
        return True

    def _set_continuous_on_worker(self, enabled: bool) -> None:
        controller = self._voice_controller
        if controller is not None:
            controller.set_continuous(enabled)

    def set_computer_mode(self, mode: str) -> bool:
        """Bilgisayar kullanımı kipini çalışan runtime'da değiştirir; Uygula beklemez.

        Kip yalnız araç bağlamının ayarında tutulur: sonraki her tur yeni bağlam kurar,
        dolayısıyla değişiklik config'e yazılana kadar da geçerlidir. Kabul edilen değerler
        `ComputerUseConfig.mode` ile aynıdır (accept | auto); başka değer reddedilir.
        """
        if mode not in {"accept", "auto"}:
            return False
        loop = self._loop
        if loop is None or self._runtime is None:
            return False
        try:
            loop.call_soon_threadsafe(self._set_computer_mode_on_worker, mode)
        except RuntimeError:
            return False
        return True

    def _set_computer_mode_on_worker(self, mode: str) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        # Tek gerçek kaynak araç bağlamıdır: araçlar ayarı oradan okur (kural 10).
        computer_use = runtime.context.settings.computer_use.model_copy(update={"mode": mode})
        runtime.context.settings = runtime.context.settings.model_copy(
            update={"computer_use": computer_use}
        )

    def _stop_voice_on_worker(self) -> None:
        if self._ptt_drain_task is not None and not self._ptt_drain_task.done():
            self._ptt_drain_task.cancel()
        controller = self._voice_controller
        if controller is not None:
            controller.request_stop()

    def _shutdown_on_worker(self, request_id: str | None) -> None:
        self._stop_voice_on_worker()
        if self._dictation is not None:
            self._dictation.shutdown()
        if self._selection is not None:
            self._selection.shutdown()
        self._stop_proactive()
        # Çalışan kod ajanı süreçleri panelle birlikte durur; yetim kalıp commit atmasın.
        if self._runtime is not None:
            self._runtime.shutdown()
        if request_id is not None:
            self._cancel_on_worker(request_id)
        # İptal yoksayılsa da kapanış üst süresi her durumda işlemelidir.
        asyncio.get_running_loop().call_soon(self._stop_loop_when_clean)

    def _stop_loop_when_clean(self) -> None:
        if self._loop_stopping:
            return
        runtime = self._runtime
        with self._lock:
            active_request = self._active_request
            voice_busy = self._voice_starting or self._voice_task is not None
        proactive = self._proactive_task
        proactive_busy = proactive is not None and not proactive.done()
        busy = {
            "turn_cleanup": runtime is not None and runtime.agent.cleanup_pending,
            "active_request": active_request is not None,
            "voice": voice_busy,
            "dictation": self._dictation is not None and self._dictation.busy,
            "selection": self._selection is not None and self._selection.busy,
            "proactive": proactive_busy,
        }
        if runtime is not None and any(busy.values()):
            if self._shutdown_deadline is None:
                self._shutdown_deadline = monotonic() + SHUTDOWN_CLEANUP_SECONDS
            if monotonic() < self._shutdown_deadline:
                asyncio.get_running_loop().call_later(0.05, self._stop_loop_when_clean)
                return
            # Temizlik doğrulanamadan çıkılıyor: "temiz kapandı" denmez, bekleyen parçalar
            # kayda geçer. Süreç çıkınca thread'ler de biter; OS'ye gitmiş işlem geri alınmaz.
            with suppress(OSError):
                runtime.audit.emit(
                    "shutdown_cleanup_timeout",
                    seconds=SHUTDOWN_CLEANUP_SECONDS,
                    pending=sorted(name for name, value in busy.items() if value),
                )
        self._loop_stopping = True
        self._close_proactive_client()
        asyncio.get_running_loop().stop()
