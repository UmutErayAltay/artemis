"""Oyun modu: tam ekran uygulama öndeyken büyük STT modelini bırakıp hafif modele geçer.

Büyük yerel Whisper (large-v3) 2,5-4,3 GB GPU belleği tutar; oyun bu belleği isterken ses
kanalı onu boşuna işgal etmesin diye, tam ekran uygulama süresince hafif model devralır.
Varsayılan kapalıdır (``voice.game_mode``).
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from artemis.channels.voice import stt_catalog
from artemis.config.settings import VoiceConfig

logger = logging.getLogger(__name__)

# SHQueryUserNotificationState dönüş durumları (shellapi.h, QUERY_USER_NOTIFICATION_STATE).
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3


def fullscreen_app_active() -> bool:
    """Önde tam ekran bir uygulama (oyun) varsa True; Windows dışında her zaman False."""
    if sys.platform != "win32":
        return False
    import ctypes

    # Bu API seçildi çünkü ucuzdur (tek kabuk çağrısı, pencere taraması yok) ve Windows'un
    # bildirimleri bastırırken kullandığı kararın aynısını verir: kenarlıksız tam ekran oyunlar
    # QUNS_BUSY (2), özel (exclusive) D3D tam ekran ise QUNS_RUNNING_D3D_FULL_SCREEN (3) döner.
    state = ctypes.c_int(0)
    try:
        result = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
    except (OSError, AttributeError):
        return False
    if result != 0:
        # Sıfırdan farklı HRESULT: durum bilinmiyor; model değiştirmek yerine "oyun yok" sayılır.
        return False
    return state.value in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN)


class SwappableRecognizer(Protocol):
    """Yüklenip bırakılabilen tanıyıcı (FasterWhisperRecognizer bu sözleşmeye uyar)."""

    async def transcribe(self, pcm: bytes) -> str: ...

    async def transcribe_partial(self, pcm: bytes) -> str: ...

    def prepare(self) -> None: ...

    def unload(self) -> None: ...


class GameModeRecognizer:
    """Etkin tanıyıcıyı tam ekran durumuna göre büyük ve hafif model arasında değiştirir.

    Geçişte yeni model ÖNCE yüklenir, sonra etkin tanıyıcı değişir, en son eski model bırakılır:
    böylece hiçbir an tanıyıcısız kalınmaz ve yükleme başarısızsa eski model aynen sürer.
    """

    def __init__(
        self,
        primary: SwappableRecognizer,
        light: SwappableRecognizer,
        detector: Callable[[], bool],
        on_state: Callable[[str], None],
        *,
        poll_seconds: float = 3.0,
        confirm_polls: int = 2,
        primary_label: str = "",
        light_label: str = "",
    ) -> None:
        self._primary = primary
        self._light = light
        self._detector = detector
        self._on_state = on_state
        self._poll_seconds = poll_seconds
        self._confirm_polls = max(1, confirm_polls)
        self._primary_label = primary_label.strip()
        self._light_label = light_label.strip()
        self._lock = threading.Condition()
        # Tanıyıcıyı almış ama henüz bitirmemiş çağrı sayısı: eski model bunlar bitmeden
        # bırakılmaz, yoksa geç kalan çağrı bırakılan büyük modeli oyun sürerken geri yüklerdi.
        self._inflight: dict[int, int] = {}
        self._active: SwappableRecognizer = primary
        self._in_game = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> SwappableRecognizer:
        with self._lock:
            return self._active

    @property
    def in_game(self) -> bool:
        with self._lock:
            return self._in_game

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    async def transcribe(self, pcm: bytes) -> str:
        recognizer = self._acquire()
        try:
            return await recognizer.transcribe(pcm)
        finally:
            self._release(recognizer)

    async def transcribe_partial(self, pcm: bytes) -> str:
        recognizer = self._acquire()
        try:
            return await recognizer.transcribe_partial(pcm)
        finally:
            self._release(recognizer)

    def _acquire(self) -> SwappableRecognizer:
        with self._lock:
            recognizer = self._active
            self._inflight[id(recognizer)] = self._inflight.get(id(recognizer), 0) + 1
            return recognizer

    def _release(self, recognizer: SwappableRecognizer) -> None:
        with self._lock:
            self._inflight[id(recognizer)] -= 1
            self._lock.notify_all()

    def prepare(self) -> None:
        """Açılışta eskisi gibi büyük model yüklenir; hafif model ancak oyunda yüklenir."""
        self._primary.prepare()

    def start(self) -> None:
        """Tek izleyici iş parçacığını başlatır; ikinci çağrı ve kapanmış nesne etkisizdir."""
        if self._thread is not None or self._stop.is_set():
            return
        self._thread = threading.Thread(
            target=self._watch, name="artemis-game-mode", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        """İzleyiciyi durdurur; tekrar çağrılabilir. Süren model yüklemesi bekletmez (daemon)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=self._poll_seconds + 1)

    def _watch(self) -> None:
        candidate = self._in_game
        streak = 0
        # Event.wait uykusu close() çağrısında hemen uyanır; time.sleep kapanışı geciktirirdi.
        while not self._stop.wait(self._poll_seconds):
            try:
                reading = bool(self._detector())
            except (OSError, RuntimeError, ValueError, AttributeError) as error:
                logger.debug("Tam ekran denetimi başarısız (%s).", type(error).__name__)
                streak = 0
                continue
            if reading == self._in_game:
                streak = 0
                continue
            # Debounce: alt-tab titremesi her seferinde GB'lık model yüklemesine yol açmasın;
            # durum ancak art arda aynı okunursa uygulanır.
            streak = streak + 1 if reading == candidate else 1
            candidate = reading
            if streak < self._confirm_polls:
                continue
            streak = 0
            if self._stop.is_set():
                break
            self._switch(reading)

    def _switch(self, to_game: bool) -> None:
        if to_game:
            target, previous = self._light, self._primary
        else:
            target, previous = self._primary, self._light
        try:
            target.prepare()
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            # Etkin tanıyıcı korunur; sonraki onaylı okuma yeniden dener. Mesaj yol/ayrıntı
            # taşıyabileceği için yalnız tür adı yazılır.
            logger.warning(
                "Oyun modu model geçişi başarısız (%s); mevcut model korunuyor.",
                type(error).__name__,
            )
            self._on_state("game:error")
            return
        if self._stop.is_set():
            target.unload()
            return
        with self._lock:
            self._active = target
            self._in_game = to_game
            # Yeni çağrılar artık hedefe gider; eskisini almış olanların bitmesi beklenir.
            while self._inflight.get(id(previous), 0) and not self._stop.is_set():
                self._lock.wait(0.5)
            if self._stop.is_set() and self._inflight.get(id(previous), 0) > 0:
                return
        previous.unload()
        label = self._light_label if to_game else self._primary_label
        prefix = "game:on" if to_game else "game:off"
        self._on_state(f"{prefix}:{label}" if label else prefix)


def resolve_model_label(path: Path | None, fallback: str = "") -> str:
    """Model yolundan okunabilir kısa model adını çıkarır (ör. large-v3, small)."""
    if path is None:
        return fallback
    name = path.name.strip()
    for prefix in ("models--Systran--faster-whisper-", "faster-whisper-"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    # HF önbelleği: models--Systran--faster-whisper-small/snapshots/<hash> -> "small".
    if path.parent.name == "snapshots" and len(path.parents) >= 2:
        return resolve_model_label(path.parents[1], fallback)
    return name or fallback


def resolve_light_model(config: VoiceConfig) -> Path | None:
    """Kullanıcının seçtiği hafif model, yoksa yerelde kurulu faster-whisper `small`."""
    if config.game_mode_stt_model is not None:
        return config.game_mode_stt_model
    # İndirme yapılmaz: yalnız önbellekte tamamlanmış model kullanılır.
    return stt_catalog.installed_path("small")
