"""Qt'siz worker, kontrol mesajları ve senkron onay aracısı."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from jarvis.channels.runtime import (
    Approval as ApprovalCallback,
)
from jarvis.channels.runtime import (
    AuditObserver,
    Runtime,
    create_runtime,
)

RuntimeFactory = Callable[[Path, ApprovalCallback, AuditObserver], Runtime]


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


BridgeEvent = State | Finished | Failed | Approval | Activity | ActivityGap


@dataclass
class _PendingApproval:
    request_id: str
    turn_id: str
    approval_id: str
    future: Future[bool] = field(default_factory=Future, repr=False)


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
            approval_id = str(uuid4())
            item = _PendingApproval(request_id, runtime.context.turn_id, approval_id)
            self.pending = item
            notice = runtime.gate.pending_notice
            deadline = runtime.context.deadline
            bridge._events.put(
                Approval(
                    request_id,
                    item.turn_id,
                    approval_id,
                    tool_name,
                    arguments,
                    notice,
                    deadline,
                )
            )
        timeout = (
            max(0.0, deadline - monotonic())
            if deadline is not None
            else runtime.context.settings.max_seconds
        )
        try:
            approved = item.future.result(timeout=timeout) is True
            with self.lock:
                cancelled = self.cancel_requested.is_set()
                if cancelled:
                    # Callback senkron biçimde kapının içindedir; işçi eventi
                    # event loop'a sıraya koymadan önce iptal işaretlenmelidir.
                    runtime.runner.cancel_current()
            return approved and not cancelled
        except TimeoutError:
            return False
        finally:
            with self.lock:
                if self.pending is item:
                    self.pending = None

    def decide(
        self, request_id: str, turn_id: str, approval_id: str, approved: bool
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
            item.future.set_result(approved is True)
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
    ) -> None:
        self._config_path = config_path
        self._runtime_factory = runtime_factory
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
        self._active_request: str | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._cleanup_pending = False
        self._shutdown = False
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main, name="jarvis-desktop", daemon=True
        )
        self._thread.start()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

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
        loop = self._loop
        if loop is None:
            with self._lock:
                self._active_request = None
            return None
        self._events.put(State("running", "İstek çalışıyor.", request_id))
        loop.call_soon_threadsafe(self._start_turn, request_id, text)
        return request_id

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            if self._active_request != request_id or self._shutdown:
                return False
        self._broker.invalidate()
        loop = self._loop
        if loop is None:
            return False
        loop.call_soon_threadsafe(self._cancel_on_worker, request_id)
        return True

    def decide(
        self, request_id: str, turn_id: str, approval_id: str, approved: bool
    ) -> bool:
        return self._broker.decide(request_id, turn_id, approval_id, approved)

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown or self._stopped.is_set():
                return
            self._shutdown = True
            request_id = self._active_request
        self._events.put(State("stopping", "Temizleniyor."))
        self._broker.invalidate()
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._shutdown_on_worker, request_id)

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

    def _on_record(self, record: dict[str, Any]) -> None:
        fields = {key: value for key, value in record.items() if key not in {"event", "timestamp"}}
        serialized = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 16 * 1024:
            fields = {
                "summary": "Denetim olayı görünüm için kısaltıldı.",
                "original_bytes": len(serialized.encode("utf-8")),
            }
        with self._audit_lock:
            self._audit_seq += 1
            seq = self._audit_seq
            turn = fields.get("turn_id")
            if record.get("event") == "turn_start" and turn is not None:
                self._turn_requests[str(turn)] = self._broker.request_id or ""
                if len(self._turn_requests) > 64:
                    self._turn_requests.pop(next(iter(self._turn_requests)))
        event = Activity(
            request_id=self._request_for_turn(fields.get("turn_id")),
            turn_id=str(fields["turn_id"]) if fields.get("turn_id") is not None else None,
            seq=seq,
            event=str(record.get("event", "unknown")),
            fields=fields,
        )
        with self._audit_lock:
            try:
                self._audit_queue.put_nowait(event)
            except queue.Full:
                with suppress(queue.Empty):
                    self._audit_queue.get_nowait()
                self._audit_queue.put_nowait(event)
                self._audit_dropped += 1

    def _request_for_turn(self, turn_id: object) -> str | None:
        with self._audit_lock:
            if turn_id is not None:
                found = self._turn_requests.get(str(turn_id))
                if found is not None:
                    return found
            return self._broker.request_id

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            factory = self._runtime_factory
            if factory is None:
                self._runtime = create_runtime(
                    self._config_path, self._broker.approve, on_record=self._on_record
                )
            else:
                self._runtime = factory(self._config_path, self._broker.approve, self._on_record)
        except Exception as error:  # İşçi sınırında traceback/sır sızdırmadan arızayı GUI'ye taşır.
            self._events.put(
                Failed(
                    "",
                    type(error).__name__,
                    "Runtime başlatılamadı; ayarları kontrol edin.",
                )
            )
            loop.close()
            self._stopped.set()
            self._events.put(State("stopped", "İşçi durdu."))
            return
        self._ready.set()
        self._events.put(State("ready", f"Hazır · {self._runtime.settings.primary_model}"))
        if self._shutdown:
            loop.call_soon(loop.stop)
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()
            self._stopped.set()
            self._events.put(State("stopped", "İşçi durdu."))

    def _start_turn(self, request_id: str, text: str) -> None:
        if self._shutdown or self._runtime is None or self._runtime.agent.cleanup_pending:
            self._events.put(Failed(request_id, "CleanupPending", "Önceki işlem temizleniyor."))
            self._finish_request(request_id)
            return
        if self._broker.is_cancelled():
            self._events.put(State("cancelled", "İstek başlamadan iptal edildi.", request_id))
            self._finish_request(request_id)
            return
        self._active_task = asyncio.create_task(self._run_turn(request_id, text))

    async def _run_turn(self, request_id: str, text: str) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        if self._broker.is_cancelled():
            self._events.put(State("cancelled", "İstek başlamadan iptal edildi.", request_id))
            self._finish_request(request_id)
            return
        try:
            result = await runtime.runner.run(text)
            with self._audit_lock:
                self._turn_requests[result.turn_id] = request_id
            self._events.put(Finished(request_id, result))
        except Exception as error:
            self._events.put(Failed(request_id, type(error).__name__, "İstek tamamlanamadı."))
        except asyncio.CancelledError as error:
            self._events.put(Failed(request_id, type(error).__name__, "İstek tamamlanamadı."))
        finally:
            if runtime.agent.cleanup_pending:
                with self._lock:
                    self._cleanup_pending = True
                self._events.put(State("cleanup_pending", "Önceki işlem temizleniyor.", request_id))
                asyncio.create_task(self._finish_cleanup(request_id))
            else:
                self._finish_request(request_id)

    async def _finish_cleanup(self, request_id: str) -> None:
        runtime = self._runtime
        while runtime is not None and runtime.agent.cleanup_pending:
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
            self._events.put(State("ready", "Hazır.", request_id))

    def _cancel_on_worker(self, request_id: str) -> None:
        with self._lock:
            active_request = self._active_request
        if active_request != request_id or self._runtime is None:
            return
        self._runtime.runner.cancel_current()
        self._events.put(
            State(
                "cancelling",
                "İptal ediliyor; başlayan sistem işlemleri geri alınmaz.",
                request_id,
            )
        )

    def _shutdown_on_worker(self, request_id: str | None) -> None:
        if request_id is not None:
            self._cancel_on_worker(request_id)
        else:
            self._stop_loop_when_clean()

    def _stop_loop_when_clean(self) -> None:
        runtime = self._runtime
        with self._lock:
            active_request = self._active_request
        if runtime is not None and (
            runtime.agent.cleanup_pending or active_request is not None
        ):
            asyncio.get_running_loop().call_later(0.05, self._stop_loop_when_clean)
            return
        asyncio.get_running_loop().stop()
