"""Sabit masaüstü eylemleri için stdin/stdout işçisi."""

from __future__ import annotations

import json
import sys
from typing import Any
from urllib.parse import urlsplit

from jarvis.tools.platform import UnsupportedPlatformError, WindowsDesktop

MAX_REQUEST_BYTES = 1_100_000
MAX_RESPONSE_BYTES = 1_100_000
MAX_TEXT_BYTES = 1_000_000
_ACTIONS = {
    "open_application",
    "close_application",
    "read_clipboard",
    "write_clipboard",
    "open_browser",
}


def _invalid() -> dict[str, Any]:
    return {"status": "error", "message": "Masaüstü isteği geçersiz.", "data": {}}


def _request() -> tuple[str, dict[str, Any]] | None:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        return None
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(request, dict) or set(request) != {"action", "arguments"}:
        return None
    action = request["action"]
    arguments = request["arguments"]
    if not isinstance(action, str) or action not in _ACTIONS or not isinstance(arguments, dict):
        return None
    return action, arguments


def _string_argument(
    arguments: dict[str, Any], name: str, maximum: int, *, minimum: int = 0
) -> str | None:
    if set(arguments) != {name}:
        return None
    value = arguments[name]
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        return None
    if len(value.encode("utf-8")) > maximum:
        return None
    return value


def _dispatch(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    desktop = WindowsDesktop()
    if action == "open_application":
        if set(arguments) not in ({"executable"}, {"executable", "file_path"}):
            return _invalid()
        executable = _string_argument(
            {"executable": arguments["executable"]}, "executable", 4096, minimum=1
        )
        if executable is None:
            return _invalid()
        if arguments.get("file_path") is not None:
            document = _string_argument(
                {"file_path": arguments["file_path"]}, "file_path", 4096, minimum=1
            )
            if document is None:
                return _invalid()
            desktop.open_document(executable, document)
        else:
            desktop.open_application(executable)
        try:
            verified = desktop.wait_for_application_window(executable)
        except (OSError, ValueError):
            # Başlatma gerçekleşti; doğrulama arızasını açma başarısızlığı saymayız.
            verified = False
        if verified:
            return {
                "status": "ok",
                "message": "Uygulama açıldı; görünür penceresi doğrulandı.",
                "data": {"window_verified": True},
            }
        return {
            "status": "sent",
            "message": "Uygulama açma isteği işletim sistemine gönderildi.",
            "data": {},
        }
    if action == "close_application":
        executable = _string_argument(arguments, "executable", 4096, minimum=1)
        if executable is None:
            return _invalid()
        if not desktop.close_application(executable):
            if not desktop.application_windows_open(executable):
                return {
                    "status": "ok",
                    "message": "Uygulama pencereleri kapandı; arka plan süreçleri açık kalabilir.",
                    "data": {"windows_closed": True, "processes_closed": False},
                }
            return {
                "status": "error",
                "message": "Uygulamanın kapandığı doğrulanamadı.",
                "data": {},
            }
        return {"status": "ok", "message": "Uygulama kapandı.", "data": {}}
    if action == "read_clipboard":
        if arguments:
            return _invalid()
        clipboard_text = desktop.read_clipboard()
        if len(clipboard_text.encode("utf-8")) > MAX_TEXT_BYTES:
            return {"status": "error", "message": "Pano metni yanıt sınırını aşıyor.", "data": {}}
        return {"status": "ok", "message": "Pano okundu.", "data": {"text": clipboard_text}}
    if action == "write_clipboard":
        write_text = _string_argument(arguments, "text", MAX_TEXT_BYTES)
        if write_text is None:
            return _invalid()
        if not desktop.write_clipboard(write_text):
            return {"status": "error", "message": "Panoya yazma doğrulanamadı.", "data": {}}
        return {"status": "ok", "message": "Pano metni yazıldı.", "data": {}}
    if action == "open_browser":
        url = _string_argument(arguments, "url", 8192, minimum=1)
        if url is None:
            return _invalid()
        try:
            parsed = urlsplit(url)
        except ValueError:
            return _invalid()
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
        ):
            return _invalid()
        desktop.open_browser(url)
        return {
            "status": "sent",
            "message": "Tarayıcı açma isteği işletim sistemine gönderildi.",
            "data": {},
        }
    return _invalid()


def main() -> int:
    request = _request()
    if request is None:
        response = _invalid()
    else:
        try:
            response = _dispatch(*request)
        except UnsupportedPlatformError:
            response = {
                "status": "error",
                "message": "Bu masaüstü işlemi yalnız Windows'ta destekleniyor.",
                "data": {},
            }
        except (OSError, RuntimeError, ValueError):
            response = {"status": "error", "message": "Masaüstü işlemi yapılamadı.", "data": {}}
        # Beklenmeyen hatalar sabit hata metni uydurmak yerine işçiyi sıfırdan
        # farklı kodla bitirir. Üst süreç stderr'i atar ve genel hata döndürür.
    try:
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        encoded = json.dumps(_invalid(), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        encoded = json.dumps(
            {"status": "error", "message": "Masaüstü yanıtı çok büyük.", "data": {}},
            ensure_ascii=False,
        ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
