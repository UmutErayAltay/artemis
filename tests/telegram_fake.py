"""Telegram için loopback sahte sunucu ve test kurulumu; gerçek token ve ağ yoktur.

Sunucu gerçek HTTP ve JSON kullanır, ama yalnız 127.0.0.1 üzerinde dinler. `getUpdates`
uzun bekleme yapmaz: kuyrukta güncelleme yoksa hemen `[]` döner. Kuyruk FIFO'dur ve her
çağrıda en çok bir güncelleme verir. Onay düğmesi basımı (`queue_press`) gerçek
düğme verisini, ancak Jarvis onay mesajını gönderdikten sonra üretilir.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jarvis.channels.runtime import Runtime, create_runtime
from jarvis.channels.telegram.api import TelegramClient
from jarvis.channels.telegram.channel import TelegramChannel
from jarvis.config.settings import Settings, TelegramConfig
from jarvis.safety.audit import AuditLog
from jarvis.tools.registry import ToolRegistry

# Sahte değer: yalnız bu sunucunun kabul ettiği biçimde, gerçek bir bota ait değildir.
FAKE_TOKEN = "123456:TEST-not-a-real-token-abcdefghijklmn"
CHAT_ID = 42
OUT_METHODS = ("sendMessage", "editMessageText", "answerCallbackQuery")
_LOOPBACK_NO_PROXY = "127.0.0.1,localhost"


@dataclass(frozen=True)
class Recorded:
    method: str
    body: dict[str, Any]
    token_ok: bool


@dataclass(frozen=True)
class _Failure:
    status: int
    payload: dict[str, Any]
    once: bool


@dataclass(frozen=True)
class _Press:
    """Düğme basımı; veri, en son gönderilen onay klavyesinden çözülür."""

    update_id: int
    choice: str
    from_id: int
    chat_id: int
    data: str | None


def make_message(
    update_id: int,
    text: str | None,
    *,
    from_id: Any = CHAT_ID,
    chat_id: Any = CHAT_ID,
    chat_type: str = "private",
    date: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Telegram `message` güncellemesi; `extra` iletilme ve medya alanlarını taşır."""
    message: dict[str, Any] = {
        "message_id": update_id,
        "date": int(time.time()) if date is None else date,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": from_id, "is_bot": False, "first_name": "Test"},
    }
    if text is not None:
        message["text"] = text
    message.update(extra)
    return {"update_id": update_id, "message": message}


def make_callback(
    update_id: int,
    data: str,
    *,
    from_id: Any = CHAT_ID,
    chat_id: Any = CHAT_ID,
    message_id: int = 1,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-test-{update_id}",
            "from": {"id": from_id, "is_bot": False, "first_name": "Test"},
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"}},
        },
    }


class FakeTelegram:
    def __init__(self, token: str = FAKE_TOKEN) -> None:
        self.token = token
        self.requests: list[Recorded] = []
        self._lock = threading.Lock()
        self._queue: deque[dict[str, Any] | _Press] = deque()
        self._failures: dict[str, _Failure] = {}
        self._next_update_id = 1000
        self._next_message_id = 100
        self._next_callback = 0
        self._markup: dict[str, Any] | None = None
        self._markup_message_id = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # Sunucu yaşam döngüsü ---------------------------------------------------------------

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                """Erişim günlüğü yolu yazar; yol token taşıdığından hiçbir şey yazılmaz."""

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                parts = self.path.split("/")
                if len(parts) != 3 or not parts[1].startswith("bot"):
                    self._reply(404, {"ok": False, "description": "Not Found"})
                    return
                try:
                    decoded = json.loads(raw)
                except ValueError:
                    decoded = {}
                body = decoded if isinstance(decoded, dict) else {}
                status, payload = fake.dispatch(parts[2], parts[1][3:], body)
                self._reply(status, payload)

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server = server
        self._thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None

    @property
    def api_base(self) -> str:
        assert self._server is not None, "Sunucu başlatılmadı"
        return f"http://127.0.0.1:{self._server.server_port}"

    # Kuyruk ve hata senaryoları ----------------------------------------------------------

    def _new_update_id(self) -> int:
        self._next_update_id += 1
        return self._next_update_id

    def queue_message(self, text: str | None, **options: Any) -> int:
        with self._lock:
            update_id = self._new_update_id()
            self._queue.append(make_message(update_id, text, **options))
        return update_id

    def queue_raw(self, update: dict[str, Any]) -> int:
        with self._lock:
            update_id = self._new_update_id()
            self._queue.append({**update, "update_id": update_id})
        return update_id

    def queue_press(
        self,
        choice: str = "ok",
        *,
        from_id: int = CHAT_ID,
        chat_id: int = CHAT_ID,
        data: str | None = None,
    ) -> int:
        """Onay düğmesine basışı kuyruğa koyar; veri son onay klavyesinden çözülür."""
        with self._lock:
            update_id = self._new_update_id()
            self._queue.append(
                _Press(update_id, choice, from_id=from_id, chat_id=chat_id, data=data)
            )
        return update_id

    def fail(
        self,
        method: str,
        *,
        status: int = 500,
        body: dict[str, Any] | None = None,
        once: bool = False,
    ) -> None:
        payload = body if body is not None else {"ok": False, "description": "Sahte hata"}
        with self._lock:
            self._failures[method] = _Failure(status, payload, once)

    def snapshot(self) -> list[Recorded]:
        with self._lock:
            return list(self.requests)

    # İstek işleyici (HTTP iş parçacığında çalışır) ---------------------------------------

    def dispatch(self, method: str, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self._lock:
            token_ok = token == self.token
            # Token kaydedilmez; yalnız doğru olup olmadığı tutulur.
            self.requests.append(Recorded(method, body, token_ok))
            if not token_ok:
                return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
            failure = self._failures.get(method)
            if failure is not None:
                if failure.once:
                    del self._failures[method]
                return failure.status, failure.payload
            if method == "getUpdates":
                return 200, {"ok": True, "result": self._poll(body.get("offset"))}
            if method == "sendMessage":
                return 200, {"ok": True, "result": self._send(body)}
            if method in {"editMessageText", "answerCallbackQuery"}:
                return 200, {"ok": True, "result": True}
            return 404, {"ok": False, "description": "Not Found"}

    def _send(self, body: dict[str, Any]) -> dict[str, Any]:
        self._next_message_id += 1
        message_id = self._next_message_id
        if body.get("reply_markup"):
            self._markup = body["reply_markup"]
            self._markup_message_id = message_id
        return {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": {"id": body.get("chat_id"), "type": "private"},
            "text": body.get("text", ""),
        }

    def _poll(self, offset: Any) -> list[dict[str, Any]]:
        if offset == -1:
            # Telegram'ın "son güncellemeyi ver, gerisini onayla" davranışı.
            updates = [item for item in self._queue if isinstance(item, dict)]
            self._queue.clear()
            return updates[-1:]
        if isinstance(offset, int) and not isinstance(offset, bool):
            self._queue = deque(item for item in self._queue if _id_of(item) >= offset)
        if not self._queue:
            return []
        head = self._queue[0]
        if isinstance(head, _Press):
            resolved = self._resolve(head)
            if resolved is None:
                return []
            self._queue.popleft()
            return [resolved]
        self._queue.popleft()
        return [head]

    def _resolve(self, press: _Press) -> dict[str, Any] | None:
        data = press.data
        if data is None:
            if self._markup is None:
                return None
            prefix = f"{press.choice}:"
            buttons = [
                button
                for row in self._markup.get("inline_keyboard", [])
                for button in row
                if str(button.get("callback_data", "")).startswith(prefix)
            ]
            if not buttons:
                return None
            data = str(buttons[0]["callback_data"])
        self._next_callback += 1
        update = make_callback(
            press.update_id,
            data,
            from_id=press.from_id,
            chat_id=press.chat_id,
            message_id=self._markup_message_id,
        )
        update["callback_query"]["id"] = f"cb-{self._next_callback}"
        return update


def _id_of(item: dict[str, Any] | _Press) -> int:
    return item["update_id"] if isinstance(item, dict) else item.update_id


def sent_bodies(fake: FakeTelegram) -> list[dict[str, Any]]:
    return [item.body for item in fake.snapshot() if item.method == "sendMessage"]


def method_bodies(fake: FakeTelegram, method: str) -> list[dict[str, Any]]:
    return [item.body for item in fake.snapshot() if item.method == method]


def everything_sent(fake: FakeTelegram) -> str:
    """Sunucunun gördüğü her gövdenin birleşik metni; içerik sızıntısı taraması için."""
    return json.dumps(
        [item.body for item in fake.snapshot()], ensure_ascii=False, sort_keys=True
    )


def closed_port() -> int:
    """Bağlantının reddedileceği, şu an dinlenmeyen loopback portu."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class Harness:
    root: Path
    fake: FakeTelegram
    settings: Settings
    audit: AuditLog
    _clients: list[TelegramClient] = field(default_factory=list)

    @property
    def workspace(self) -> Path:
        return self.settings.workspace

    @property
    def audit_path(self) -> Path:
        return self.settings.audit_path

    def client(
        self,
        token: str = FAKE_TOKEN,
        *,
        api_base: str | None = None,
        timeout: float = 10.0,
    ) -> TelegramClient:
        base = self.fake.api_base if api_base is None else api_base
        client = TelegramClient(token, base, audit=self.audit, timeout=timeout)
        self._clients.append(client)
        return client

    def audit_events(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def audit_text(self) -> str:
        return self.audit_path.read_text(encoding="utf-8") if self.audit_path.exists() else ""

    def channel(
        self,
        provider: Any,
        registry: ToolRegistry,
        *,
        approval_timeout: float | None = None,
    ) -> tuple[TelegramChannel, Runtime]:
        telegram = self.settings.telegram
        assert telegram.chat_id is not None
        client = self.client()
        channel = TelegramChannel(
            client,
            telegram.chat_id,
            audit=self.audit,
            approval_timeout=(
                telegram.approval_timeout_seconds if approval_timeout is None else approval_timeout
            ),
            max_message_age=telegram.max_message_age_seconds,
            poll_timeout=0,
        )
        runtime = create_runtime(
            self.root / "config.yaml",
            channel.approve,
            provider=provider,
            registry=registry,
            audit=self.audit,
            settings=self.settings,
        )
        channel.attach(runtime)
        return channel, runtime

    def close(self) -> None:
        for client in self._clients:
            client.close()
        self._clients.clear()
        self.fake.stop()


def build_harness(
    root: Path,
    monkeypatch: Any,
    *,
    chat_id: int | None = CHAT_ID,
    approval_timeout: float = 5.0,
    max_message_age: int = 120,
    **settings_fields: Any,
) -> Harness:
    """Loopback sunucu, sahte token ortamı ve gerçek AuditLog ile kurulum yapar."""
    monkeypatch.setenv("NO_PROXY", _LOOPBACK_NO_PROXY)
    monkeypatch.setenv("no_proxy", _LOOPBACK_NO_PROXY)
    monkeypatch.setenv("JARVIS_TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    root.mkdir(parents=True, exist_ok=True)
    fake = FakeTelegram()
    fake.start()
    workspace = root / "desktop"
    workspace.mkdir(parents=True, exist_ok=True)
    telegram = TelegramConfig(
        chat_id=chat_id,
        api_base=fake.api_base,
        poll_timeout_seconds=0,
        approval_timeout_seconds=approval_timeout,
        max_message_age_seconds=max_message_age,
    )
    settings = Settings(
        workspace=workspace,
        state_dir=root / "state",
        audit_path=root / "audit.jsonl",
        telegram=telegram,
        **settings_fields,
    )
    return Harness(root, fake, settings, AuditLog(settings.audit_path))
