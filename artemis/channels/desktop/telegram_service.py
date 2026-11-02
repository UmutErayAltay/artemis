"""Masaüstü içinde çalışan iki yönlü Telegram kanalı (ADR 0052).

Kanal kendi iş parçacığında, kendi olay döngüsünde, kendi runtime'ı ve konuşma geçmişiyle
çalışır (ADR 0016: panel geçmişinden ayrı). Runtime iş parçacığının İÇİNDE kurulur: SQLite
bağlantıları ve döngü ilişkisi o iş parçacığına aittir. Onay yalnız Telegram düğmesiyle
sorulur (`channel.approve`); masaüstü onay penceresi hiç devreye girmez. Bu modül Qt'siz
kalır: durum metni geri çağrıyla gider, arayüz onu sinyalle taşır.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from pathlib import Path

from artemis.channels.runtime import Runtime
from artemis.channels.telegram.api import resolve_telegram_token
from artemis.channels.telegram.channel import (
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_STOPPED,
    serve_telegram,
)
from artemis.config.settings import TelegramConfig

JOIN_SECONDS = 10.0
_EXECUTOR_SECONDS = 5.0
MISSING_TEXT = "Önce token kaydet ve sohbeti eşleştir."
STOPPING_TEXT = "Kanal kapanıyor (sürmekte olan tur bitince); birkaç saniye sonra yeniden aç."


def channel_blocker(config: TelegramConfig) -> str | None:
    """Kanalın başlayamama nedeni (token ya da sohbet yok); None = başlayabilir."""
    if config.chat_id is None or resolve_telegram_token(config) is None:
        return MISSING_TEXT
    return None


def should_run(config: TelegramConfig) -> bool:
    return config.run_in_desktop and channel_blocker(config) is None


class TelegramService:
    def __init__(
        self,
        on_status: Callable[[str], None],
        *,
        runtime_factory: Callable[..., Runtime] | None = None,
        join_seconds: float = JOIN_SECONDS,
    ) -> None:
        self._on_status = on_status
        self._runtime_factory = runtime_factory
        self._join_seconds = join_seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = threading.Event()
        self._last = STATUS_STOPPED

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _report(self, text: str) -> None:
        self._last = text
        self._on_status(text)

    def start(self, config_path: Path) -> bool:
        """Çalışmıyorsa başlatır; zaten çalışıyorsa (ya da önceki durma bitmediyse) False."""
        if self.is_running:
            if self._stop_requested.is_set():
                # Önceki durma süresinde bitmedi (tur sürüyor): yeniden açma sessizce kaybolmasın.
                self._report(STOPPING_TEXT)
            return False
        self._stop_requested.clear()
        self._stop_event = asyncio.Event()
        self._report(STATUS_STARTING)
        thread = threading.Thread(
            target=self._main, args=(config_path,), name="artemis-telegram", daemon=True
        )
        self._thread = thread
        thread.start()
        return True

    def stop(self) -> None:
        """Idempotent ve iş parçacığı güvenli; sınırlı bekler (en çok `join_seconds`)."""
        self._stop_requested.set()
        with self._lock:
            loop, event = self._loop, self._stop_event
        if loop is not None and event is not None:
            # RuntimeError: döngü zaten kapandı, yani iş parçacığı bitmiş ya da bitiyor.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(event.set)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._join_seconds)

    def _main(self, config_path: Path) -> None:
        loop = asyncio.new_event_loop()
        event = self._stop_event
        assert event is not None
        with self._lock:
            self._loop = loop
        if self._stop_requested.is_set():
            event.set()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                serve_telegram(
                    config_path,
                    event,
                    embedded=True,
                    status=self._report,
                    runtime_factory=self._runtime_factory,
                )
            )
        except Exception as error:  # noqa: BLE001 - iş parçacığı sınırı: yutulmaz, panele bildirilir
            # Hangi arıza olursa olsun iş parçacığı sessizce düşmez ve arayüz "Durdu" demekle
            # yetinmez; yalnız tür adı raporlanır (mesaj/izleme URL ve token taşıyabilir).
            self._report(f"{STATUS_ERROR} ({type(error).__name__})")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
                # Terk edilmiş uzun yoklama istemci kapanınca biter; takılırsa süreyle sınırlı.
                with contextlib.suppress(TimeoutError):
                    loop.run_until_complete(
                        asyncio.wait_for(loop.shutdown_default_executor(), _EXECUTOR_SECONDS)
                    )
            finally:
                with self._lock:
                    self._loop = None
                loop.close()
                asyncio.set_event_loop(None)
            # Çakışma/hata durumu "durdu" ile ezilmez; panel nedeni görmeye devam eder.
            if self._last in {STATUS_RUNNING, STATUS_STARTING, STOPPING_TEXT}:
                self._report(STATUS_STOPPED)
