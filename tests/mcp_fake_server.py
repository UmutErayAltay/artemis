"""Testler için sahte stdio MCP sunucusu (yalnız stdlib).

Kullanım: python tests/mcp_fake_server.py <mod> [pid_dosyası]
Gerçek bir MCP sunucusu yoktur; her mod istemcinin tek bir hata yolunu sınar.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}
EMPTY_SCHEMA = {"type": "object", "properties": {}}
PAGES: dict[str, list[dict[str, Any]]] = {
    "": [
        {
            "name": "echo",
            "description": "Metni geri döndürür.",
            "inputSchema": ECHO_SCHEMA,
            "annotations": {"readOnlyHint": True},
        },
        {"name": "env", "description": "Ortam anahtarları.", "inputSchema": EMPTY_SCHEMA},
        {"name": "broken", "description": "Geçersiz şema.", "inputSchema": {"type": "string"}},
    ],
    "p2": [
        {
            "name": "fail",
            "inputSchema": EMPTY_SCHEMA,
            "annotations": {"destructiveHint": True},
        },
        {"name": "mixed", "description": "Metin + resim.", "inputSchema": EMPTY_SCHEMA},
    ],
}


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(request_id: Any, result: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def read_message() -> dict[str, Any] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    parsed: dict[str, Any] = json.loads(line)
    return parsed


def sleep_forever() -> None:
    time.sleep(60)


def tool_result(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "echo":
        text = arguments["text"]
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"echo": text},
        }
    if name == "env":
        return {"content": [{"type": "text", "text": ",".join(sorted(os.environ))}]}
    if name == "fail":
        return {"content": [{"type": "text", "text": "başarısız"}], "isError": True}
    if name == "mixed":
        return {
            "content": [
                {"type": "text", "text": "metin"},
                {"type": "image", "data": "AAAA", "mimeType": "image/png"},
            ]
        }
    return {"content": [], "isError": True}


def handle_call(message: dict[str, Any]) -> None:
    request_id = message["id"]
    params = message.get("params", {})
    if MODE == "hang_call":
        sleep_forever()
    if MODE == "big":
        sys.stdout.write("x" * (2 << 20) + "\n")
        sys.stdout.flush()
        return
    if MODE == "remote_error":
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "kötü"}})
        return
    if MODE == "server_request":
        send({"jsonrpc": "2.0", "id": 900, "method": "ping"})
        first = read_message()
        send({"jsonrpc": "2.0", "id": 901, "method": "roots/list"})
        second = read_message()
        ok = (
            first is not None
            and first.get("id") == 900
            and first.get("result") == {}
            and second is not None
            and second.get("id") == 901
            and second.get("error", {}).get("code") == -32601
        )
        if not ok:
            sleep_forever()
    if MODE == "notify":
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "x"}})
        send({"jsonrpc": "2.0", "id": 777, "result": {"content": []}})
    reply(request_id, tool_result(params.get("name", ""), params.get("arguments", {})))


def handle_initialize(message: dict[str, Any]) -> bool:
    """False dönerse döngü sonlanır."""
    if MODE == "hang":
        sleep_forever()
    if MODE == "hang_child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(sys.argv[2], "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))
        sleep_forever()
    if MODE == "garbage":
        sys.stdout.write("bu json degil\n")
        sys.stdout.flush()
        return True
    if MODE == "die":
        sys.exit(3)
    version = "1999-01-01" if MODE == "bad_version" else message["params"]["protocolVersion"]
    reply(
        message["id"],
        {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0"},
        },
    )
    return True


def main() -> None:
    if MODE == "die":
        sys.exit(3)
    while (message := read_message()) is not None:
        method = message.get("method")
        if method == "initialize":
            handle_initialize(message)
        elif method == "tools/list":
            cursor = message.get("params", {}).get("cursor", "")
            result: dict[str, Any] = {"tools": PAGES[cursor]}
            if cursor == "":
                result["nextCursor"] = "p2"
            reply(message["id"], result)
        elif method == "tools/call":
            handle_call(message)


if __name__ == "__main__":
    main()
