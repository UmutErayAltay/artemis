"""Tek masaüstü örneği (ADR 0030): kullanıcıya özel yerel soket, tek sabit ileti.

Soket komut kanalı DEĞİLDİR: yalnız `show` iletisi tanınır, en çok `MAX_READ` bayt okunur,
başka her şey yok sayılır. Bu yüzden aynı makinedeki başka bir süreç bağlansa bile en çok
paneli öne getirebilir.
"""

from __future__ import annotations

import getpass
import hashlib
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SHOW_MESSAGE = b"show\n"
MAX_READ = 16
CONNECT_TIMEOUT_MS = 500
# Bağlanıp ileti göndermeyen istemci sunucuda asılı kalmasın.
CLIENT_TIMEOUT_MS = 1000
SWEEP_INTERVAL_MS = 250
# Aynı kullanıcının bir süreci bağlantı açıp tutarak nesne biriktiremesin.
MAX_PENDING = 8
CLAIM_ATTEMPTS = 3


def _current_user() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        # Kullanıcı adı çözülemezse ad yine kurulum başına tekildir (state_dir'den gelir).
        return "unknown"


def instance_name(state_dir: Path, *, user: str | None = None) -> str:
    """Ad, kullanıcı ve çözülmüş durum dizininden türer: iki ayrı kurulum birbirini engellemez."""
    owner = user if user is not None else _current_user()
    key = f"{owner}\0{state_dir.resolve()}".encode()
    return "jarvis-" + hashlib.sha256(key).hexdigest()[:16]


def notify_running(name: str, *, show: bool, timeout_ms: int = CONNECT_TIMEOUT_MS) -> bool:
    """Çalışan örnek varsa True. `show` ise ona paneli öne getirmesini söyler."""
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(timeout_ms):
        return False
    if show:
        socket.write(SHOW_MESSAGE)
        socket.waitForBytesWritten(timeout_ms)
        # Bağlantıyı okuyan taraf kapatır: istemci hemen kopsaydı Windows'ta (adlandırılmış
        # boru) kopma, iletiden önce işlenip ileti kaybolabiliyordu.
        if socket.waitForDisconnected(timeout_ms):
            return True
    socket.abort()
    return True


class InstanceServer(QObject):
    """Birincil örneğin dinleyicisi; geçerli `show` iletisinde `show_requested` yayar.

    Bağlantılar tek sözlükte izlenir ve tek zamanlayıcıyla süpürülür: bağlantı başına
    lambda/zamanlayıcı, silinmiş Qt nesnesine geri çağrı riskini taşır.
    """

    show_requested = Signal()

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self._pending: dict[QLocalSocket, float] = {}
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept)
        self._sweeper = QTimer(self)
        self._sweeper.setInterval(SWEEP_INTERVAL_MS)
        self._sweeper.timeout.connect(self._sweep)

    def listen(self) -> bool:
        return bool(self._server.listen(self.name))

    def is_listening(self) -> bool:
        return bool(self._server.isListening())

    def close(self) -> None:
        self._sweeper.stop()
        for connection in list(self._pending):
            self._drop(connection)
        self._server.close()

    def _accept(self) -> None:
        while (connection := self._server.nextPendingConnection()) is not None:
            if len(self._pending) >= MAX_PENDING:
                connection.abort()
                connection.deleteLater()
                continue
            self._pending[connection] = time.monotonic()
            connection.readyRead.connect(self._on_ready)
            connection.disconnected.connect(self._on_disconnected)
        if self._pending and not self._sweeper.isActive():
            self._sweeper.start()

    def _on_ready(self) -> None:
        connection = self.sender()
        if not isinstance(connection, QLocalSocket) or connection not in self._pending:
            return
        if connection.bytesAvailable() < len(SHOW_MESSAGE):
            return
        self._consume(connection)

    def _consume(self, connection: QLocalSocket) -> None:
        data = bytes(connection.read(MAX_READ).data())
        self._drop(connection)
        if data == SHOW_MESSAGE:
            self.show_requested.emit()

    def _on_disconnected(self) -> None:
        connection = self.sender()
        if not isinstance(connection, QLocalSocket) or connection not in self._pending:
            return
        # Kopma, okunmamış iletiden önce gelebilir: önce tamponda kalanı değerlendir.
        if connection.bytesAvailable() >= len(SHOW_MESSAGE):
            self._consume(connection)
            return
        self._pending.pop(connection, None)
        connection.deleteLater()

    def _drop(self, connection: QLocalSocket) -> None:
        if self._pending.pop(connection, None) is None:
            return
        connection.readyRead.disconnect(self._on_ready)
        connection.disconnected.disconnect(self._on_disconnected)
        connection.abort()
        connection.deleteLater()

    def _sweep(self) -> None:
        limit = time.monotonic() - CLIENT_TIMEOUT_MS / 1000
        for connection, opened in list(self._pending.items()):
            if opened < limit:
                self._drop(connection)
        if not self._pending:
            self._sweeper.stop()


def claim_instance(name: str, *, show: bool) -> InstanceServer | None:
    """Pencere kurulmadan ÖNCE çağrılır. None: başka örnek çalışıyor (gerekirse uyarıldı).

    Aynı anda başlayan iki örnekten biri dinlemeyi kazanır; kaybeden bir sonraki turda ona
    bağlanıp çıkar. Bayat soket yalnız hiçbir sahip yanıt vermiyorsa kaldırılır, böylece
    canlı bir örneğin soketi silinmez. Dönen sunucu `is_listening()` False ise koruma
    kurulamamıştır; çağıran bunu kullanıcıya söyler.
    """
    for _ in range(CLAIM_ATTEMPTS):
        if notify_running(name, show=show):
            return None
        server = InstanceServer(name)
        if server.listen():
            return server
        server.deleteLater()
    QLocalServer.removeServer(name)
    server = InstanceServer(name)
    server.listen()
    return server
