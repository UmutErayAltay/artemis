"""Küçük, senkron MCP stdio istemcisi (ADR 0028).

Neden SDK değil: dört mesajlık sözleşme (initialize, initialized, tools/list,
tools/call) sabit ve testlidir; SDK sürüm kırılması köprüyü sessizce bozabilir.

Neden her çağrıda taze süreç: kalıcı oturum, yetim süreç ve yeniden bağlanma
durumunu v1'e sokmaz. Tüm adımlar TEK bir deadline altındadır; sınırsız
`read`/`wait`/`join` yoktur. Hata metinleri kullanıcıya gösterilebilir kısa
Türkçedir: komut satırı, ortam ve ham sunucu çıktısı içermez (sır taşıyabilir).
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.tools.code_agent_process import NO_WINDOW, kill_tree

SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_MESSAGE_BYTES = 1 << 20
MAX_LIST_PAGES = 20
ERROR_MESSAGE_LIMIT = 200
GRACEFUL_EXIT_SECONDS = 1.0
CLEANUP_SECONDS = 2.0


def _spawn_options() -> dict[str, Any]:
    """Süreç ağacı `kill_tree` ile kapatılabilsin diye yeni grup/oturum açılır.

    Kod ajanının `process_options()`'ı kullanılmaz: Windows'ta CREATE_SUSPENDED ekler,
    çünkü o süreç Job Object'e bağlandıktan sonra devam ettirilir. MCP sunucusu devam
    ettirilmediği için askıda kalır ve her çağrı zaman aşımına düşerdi (Windows CI'da
    ölçüldü).
    """
    if os.name == "nt":
        return {
            "creationflags": NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return {"start_new_session": True}


class MCPError(RuntimeError):
    """`error_type`: timeout | protocol | process | remote | too_large | version."""

    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class MCPToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]


@dataclass(frozen=True)
class MCPCallResult:
    is_error: bool
    text: str
    structured: dict[str, Any] | None
    unsupported_blocks: int


def _encode(message: Mapping[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"


class _Session:
    """Tek bir taze sürecin ömrü: başlat, konuş, her yolda temizle."""

    def __init__(
        self,
        command: Sequence[str],
        env: Mapping[str, str],
        cwd: Path | None,
        deadline: float,
        max_message_bytes: int,
    ) -> None:
        self._deadline = deadline
        self._max = max_message_bytes
        self._next_id = 0
        self._incoming: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self._outgoing: queue.Queue[bytes | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._proc = self._spawn(command, env, cwd)
        self._start_threads()

    @staticmethod
    def _spawn(
        command: Sequence[str], env: Mapping[str, str], cwd: Path | None
    ) -> subprocess.Popen[bytes]:
        if not command:
            raise MCPError("MCP sunucu komutu boş.", error_type="process")
        # os.environ'a bakmamak için PATH çağıranın verdiği ortamdan alınır.
        resolved = shutil.which(command[0], path=env.get("PATH", ""))
        if resolved is None:
            raise MCPError("MCP sunucu komutu bulunamadı.", error_type="process")
        try:
            return subprocess.Popen(
                [resolved, *command[1:]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
                cwd=cwd,
                shell=False,
                **_spawn_options(),
            )
        except (OSError, ValueError) as exc:
            raise MCPError("MCP sunucusu başlatılamadı.", error_type="process") from exc

    def _start_threads(self) -> None:
        targets = (self._read_stdout, self._drain_stderr, self._write_stdin)
        for target in targets:
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    # --- arka plan iş parçacıkları -------------------------------------

    def _read_stdout(self) -> None:
        stream = self._proc.stdout
        assert stream is not None
        try:
            while True:
                line = stream.readline(self._max + 1)
                if not line:
                    self._incoming.put(("eof", b""))
                    return
                if len(line) > self._max and not line.endswith(b"\n"):
                    # Kalanını okumak sınırı delmek olur; okuma burada biter.
                    self._incoming.put(("too_large", b""))
                    return
                self._incoming.put(("line", line))
        except (OSError, ValueError):
            self._incoming.put(("eof", b""))

    def _drain_stderr(self) -> None:
        stream = self._proc.stderr
        assert stream is not None
        try:
            # İçerik bilerek atılır: boru tıkanmasın, sır sızmasın.
            while stream.read(65536):
                pass
        except (OSError, ValueError):
            return

    def _write_stdin(self) -> None:
        stream = self._proc.stdin
        assert stream is not None
        # Yazıcı ayrı iş parçacığıdır: okumayan sunucu ana akışı deadline'sız kilitleyemez.
        while (data := self._outgoing.get()) is not None:
            try:
                stream.write(data)
                stream.flush()
            except (OSError, ValueError):
                return  # Ölüm eof olayıyla raporlanır.

    # --- JSON-RPC --------------------------------------------------------

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._outgoing.put(_encode(message))

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._outgoing.put(
            _encode({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        )
        while True:
            message = self._receive()
            if "method" in message:
                if "id" in message:
                    self._answer_server_request(message)
                continue  # id'siz olan bildirimdir; yok sayılır.
            if message.get("id") != request_id or isinstance(message.get("id"), bool):
                continue  # Eşleşmeyen yanıt yok sayılır.
            return self._unwrap(message)

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        if message.get("method") == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            # İstemci hiçbir sunucu özelliğini (roots, sampling) sunmaz.
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "Method not found"},
            }
        self._outgoing.put(_encode(reply))

    @staticmethod
    def _unwrap(message: dict[str, Any]) -> dict[str, Any]:
        error = message.get("error")
        if error is not None:
            detail = error.get("message") if isinstance(error, dict) else None
            text = detail[:ERROR_MESSAGE_LIMIT] if isinstance(detail, str) else ""
            prefix = "MCP sunucusu hata döndürdü"
            raise MCPError(f"{prefix}: {text}" if text else prefix, error_type="remote")
        result = message.get("result")
        if not isinstance(result, dict):
            raise MCPError("MCP yanıtı beklenen yapıda değil.", error_type="protocol")
        return result

    def _receive(self) -> dict[str, Any]:
        remaining = self._deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise queue.Empty
            kind, raw = self._incoming.get(timeout=remaining)
        except queue.Empty:
            raise MCPError("MCP sunucusu zamanında yanıt vermedi.", error_type="timeout") from None
        if kind == "eof":
            raise MCPError("MCP sunucusu yanıt vermeden kapandı.", error_type="process")
        if kind == "too_large":
            raise MCPError("MCP yanıtı izin verilen boyutu aştı.", error_type="too_large")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except ValueError as exc:  # UnicodeDecodeError ve JSONDecodeError ikisi de ValueError
            raise MCPError("MCP yanıtı geçerli JSON değil.", error_type="protocol") from exc
        if not isinstance(decoded, dict):
            raise MCPError("MCP yanıtı beklenen yapıda değil.", error_type="protocol")
        return decoded

    # --- temizlik --------------------------------------------------------

    def close(self, *, graceful: bool = True) -> None:
        """Hata yolunda (`graceful=False`) sunucunun kendiliğinden çıkması beklenmez.

        Takılmış ya da bozuk yanıt vermiş sunucu EOF'a uymaz; beklemek yalnız süre sınırını
        aşırır. Süreç ağacı doğrudan kapatılır.
        """
        proc = self._proc
        self._outgoing.put(None)
        writer = self._threads[2]
        writer.join(timeout=0.5)
        if not writer.is_alive():
            self._close_pipe(proc.stdin)  # EOF: uyumlu sunucu kendiliğinden çıkar.
        try:
            proc.wait(timeout=GRACEFUL_EXIT_SECONDS if graceful else 0)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            # Sınır dolarsa vazgeçilir; bekleme sınırsız olmaz.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=CLEANUP_SECONDS)
        for thread in self._threads:
            thread.join(timeout=1.0)
        # Okuyan iş parçacığı sürüyorsa kapatmak tampon kilidinde asılabilir; bırakılır.
        if not self._threads[0].is_alive():
            self._close_pipe(proc.stdout)
        if not self._threads[1].is_alive():
            self._close_pipe(proc.stderr)
        if not writer.is_alive():
            self._close_pipe(proc.stdin)

    @staticmethod
    def _close_pipe(pipe: Any) -> None:
        try:
            pipe.close()
        except (OSError, ValueError):
            return


class StdioMCPClient:
    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path | None = None,
        timeout: float,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
    ) -> None:
        self.command = tuple(command)
        self.env = dict(env)
        self.cwd = cwd
        self.timeout = timeout
        self.max_message_bytes = max_message_bytes

    def list_tools(self) -> list[MCPToolSpec]:
        deadline = time.monotonic() + self.timeout
        session = self._open(deadline)
        graceful = False
        try:
            self._handshake(session)
            tools = self._collect_tools(session)
            graceful = True
            return tools
        finally:
            session.close(graceful=graceful)

    def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, timeout: float | None = None
    ) -> MCPCallResult:
        limit = self.timeout if timeout is None else min(timeout, self.timeout)
        deadline = time.monotonic() + limit
        session = self._open(deadline)
        graceful = False
        try:
            self._handshake(session)
            result = session.request("tools/call", {"name": name, "arguments": dict(arguments)})
            parsed = self._parse_call_result(result)
            graceful = True
            return parsed
        finally:
            session.close(graceful=graceful)

    def _open(self, deadline: float) -> _Session:
        return _Session(self.command, self.env, self.cwd, deadline, self.max_message_bytes)

    @staticmethod
    def _handshake(session: _Session) -> None:
        result = session.request(
            "initialize",
            {
                "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
                "capabilities": {},
                "clientInfo": {"name": "jarvis", "version": "0.1"},
            },
        )
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version not in SUPPORTED_PROTOCOL_VERSIONS:
            # Bilinmeyen sürümle sessizce sürmek sözleşmeyi tahmin etmek olur.
            raise MCPError("MCP protokol sürümü desteklenmiyor.", error_type="version")
        session.notify("notifications/initialized")

    @staticmethod
    def _collect_tools(session: _Session) -> list[MCPToolSpec]:
        tools: list[MCPToolSpec] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = session.request("tools/list", params)
            items = result.get("tools")
            if not isinstance(items, list):
                raise MCPError("MCP araç listesi beklenen yapıda değil.", error_type="protocol")
            for item in items:
                spec = _parse_tool(item)
                if spec is not None:
                    tools.append(spec)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return tools

    @staticmethod
    def _parse_call_result(result: dict[str, Any]) -> MCPCallResult:
        content = result.get("content")
        if not isinstance(content, list):
            raise MCPError("MCP çağrı sonucu beklenen yapıda değil.", error_type="protocol")
        texts: list[str] = []
        unsupported = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
                    continue
            unsupported += 1
        structured = result.get("structuredContent")
        return MCPCallResult(
            is_error=result.get("isError") is True,
            text="\n".join(texts),
            structured=structured if isinstance(structured, dict) else None,
            unsupported_blocks=unsupported,
        )


def _parse_tool(item: Any) -> MCPToolSpec | None:
    """Geçersiz aracı atlar (hata değil): bir sunucunun tek kötü aracı hepsini düşürmesin."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    schema = item.get("inputSchema")
    if not isinstance(name, str) or not isinstance(schema, dict):
        return None
    if schema.get("type") != "object":
        return None
    description = item.get("description")
    annotations = item.get("annotations")
    return MCPToolSpec(
        name=name,
        description=description if isinstance(description, str) else "",
        input_schema=schema,
        annotations=annotations if isinstance(annotations, dict) else {},
    )
