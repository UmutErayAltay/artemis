"""Tarayıcı otomasyonu MCP sunucusu (ADR 0032): `python -m jarvis.mcp_servers.browser`.

Neden ayrı süreç ve SDK'sız: istemci (ADR 0028) her çağrıda taze süreç başlatır; Playwright
ve Chromium ağır bağımlılıklardır ve ana sürece hiç yüklenmemelidir. Dört mesajlık sözleşme
(initialize, initialized, tools/list, tools/call) küçüktür, SDK sürüm kırılması riski almaz.

Güvenlik modeli: sayfanın yaptığı HER istek (ilk adres, yönlendirme, iframe, alt kaynak)
`check_url` kuralından geçer; yalnız ilk adresi doğrulamak, genel bir sayfanın yerel ağa
yönlendirmesine izin verirdi. Kalan risk: Chromium'un doğrulamadan sonra adresi yeniden
çözmesi (DNS yeniden bağlama), ADR 0032'de kayıtlıdır.

TEST KANCASI (yalnız test): `JARVIS_BROWSER_FAKE=<json yolu>` Playwright yerine belirleyici
sahte arka ucu seçer. Dosya biçimi `FakeBackend` docstring'indedir. Sahte arka uç aynı
`check_url` korumasından geçer; kontrolü gevşetmez, yalnız Chromium'u taklit eder.
`JARVIS_BROWSER_ALLOW_LOCAL=1` yalnız HOST denetimini gevşetir, şema denetimini asla.
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
MAX_LINE_BYTES = 1 << 20
MAX_STEPS = 20
MAX_TEXT_CHARS = 12000
DEFAULT_STEP_TIMEOUT_MS = 10000
NAVIGATION_TIMEOUT_MS = 20000
ALLOW_LOCAL_ENV = "JARVIS_BROWSER_ALLOW_LOCAL"
FAKE_BACKEND_ENV = "JARVIS_BROWSER_FAKE"
INSTALL_HINT = (
    'Tarayıcı otomasyonu kurulu değil. Şunu çalıştırın: pip install -e ".[browser]" '
    "ve ardından bir kez: python -m playwright install chromium"
)

Resolver = Callable[[str], list[str]]


# --- URL politikası ---------------------------------------------------------------------


def make_resolver() -> Resolver:
    """`socket.getaddrinfo` sarmalayıcısı; aynı host aynı çağrıda bir kez çözülür.

    Önbellek, bir sayfanın yüzlerce alt isteğinde DNS'i tekrarlamamak içindir.
    """
    cache: dict[str, list[str]] = {}

    def resolve(host: str) -> list[str]:
        if host not in cache:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            cache[host] = sorted({str(info[4][0]) for info in infos})
        return cache[host]

    return resolve


def _address_rejection(raw: str) -> str | None:
    try:
        address = ipaddress.ip_address(raw.split("%", 1)[0])  # IPv6 kapsam kimliği (%eth0) atılır
    except ValueError:
        return "geçersiz adres"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 IPv6 görünümlü loopback'tir; gömülü IPv4 denetlenir.
        address = address.ipv4_mapped
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global  # örn. CGNAT 100.64/10 yukarıdakilerin hiçbirine girmez
    ):
        return "yerel veya özel ağ adresi"
    return None


def check_url(url: str, resolve: Resolver, *, allow_local: bool = False) -> str | None:
    """Reddedilme nedenini (Türkçe) döndürür; izinliyse None.

    `allow_local` yalnız host/adres denetimini kapatır; şema ve userinfo kuralı her zaman
    geçerlidir (file:// ile yerel dosya okuma ölçülmüş bir açıktı).
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - geçersiz portu ValueError ile ortaya çıkarır
    except ValueError:
        return "Adres ayrıştırılamadı."
    if parts.scheme.lower() not in {"http", "https"}:
        return "Yalnız http ve https adreslerine izin verilir."
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "Adreste kullanıcı bilgisi (user@host) bulunamaz."
    if not host:
        return "Adreste host yok."
    if allow_local:
        return None
    if host == "localhost" or host.endswith(".localhost"):
        return "localhost adreslerine izin verilmez."
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = resolve(host)
        except (OSError, UnicodeError):
            # Geçersiz IDNA etiketi (`a..com`, 64+ karakter) UnicodeError verir.
            return "Host çözülemedi."
        if not addresses:
            return "Host çözülemedi."
    else:
        addresses = [host]
    for raw in addresses:
        if _address_rejection(raw) is not None:
            return "Adres yerel veya özel ağa çözülüyor."
    return None


# --- Girdi doğrulama --------------------------------------------------------------------


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["click", "fill", "read_text", "wait_for"]
    selector: str = Field(min_length=1, max_length=500)
    value: str | None = Field(default=None, max_length=2000)
    timeout_ms: int | None = Field(default=None, ge=100, le=30000)

    @model_validator(mode="after")
    def _fill_needs_value(self) -> Step:
        if self.action == "fill" and self.value is None:
            raise ValueError("fill adımı için value gerekir")
        return self


class BrowseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2000)
    steps: list[Step] = Field(default_factory=list, max_length=MAX_STEPS)


TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "minLength": 1, "maxLength": 2000},
        "steps": {
            "type": "array",
            "maxItems": MAX_STEPS,
            "items": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["click", "fill", "read_text", "wait_for"],
                    },
                    "selector": {"type": "string", "minLength": 1, "maxLength": 500},
                    "value": {"type": "string", "maxLength": 2000},
                    "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 30000},
                },
                "required": ["action", "selector"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}
TOOL_SPEC: dict[str, Any] = {
    "name": "browse",
    "description": (
        "Bir web sayfasını temiz bir Chromium oturumunda açar, adımları sırayla uygular "
        "(click, fill, read_text, wait_for) ve metni döndürür. Yerel ağ adresleri ve "
        "http/https dışındaki şemalar reddedilir. Giriş yapılmış oturum kullanılmaz."
    ),
    "inputSchema": TOOL_SCHEMA,
}


# --- Arka uç soyutlaması ----------------------------------------------------------------


class BackendUnavailable(RuntimeError):
    """Tarayıcı arka ucu başlatılamadı (Playwright veya Chromium yok)."""


class NavigationFailed(RuntimeError):
    """İlk sayfa açılamadı (ağ hatası, engellenen yönlendirme, zaman aşımı)."""


class StepFailed(RuntimeError):
    """Bir adım başarısız oldu; o ana kadar okunan metinler kısmi sonuç olarak taşınır."""

    def __init__(self, index: int, message: str, read_texts: list[str]) -> None:
        super().__init__(message)
        self.index = index
        self.read_texts = read_texts


@dataclass
class PageState:
    title: str = ""
    url: str = ""
    body_text: str = ""
    read_texts: list[str] = field(default_factory=list)


class Backend(Protocol):
    def run(
        self, request: BrowseRequest, allow: Callable[[str], bool], *, collect_body: bool
    ) -> PageState:
        """Sayfayı açar, adımları uygular. `allow` her istek için çağrılır; False ise iptal.

        Hatada `NavigationFailed` veya (kısmi okumalarla) `StepFailed` fırlatır.
        """
        ...


# `route` yalnız HTTP isteklerini görür. WebRTC (yerel UDP/ICE) ve DNS ön getirme yerel ağa
# istek dışı yoldan ulaşabilir; bu kanallar tarayıcı düzeyinde kapatılır (ADR 0032).
CHROMIUM_ARGS = (
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--dns-prefetch-disable",
    "--disable-features=NetworkPrediction",
)
URL_LIMIT = 2000


class PlaywrightBackend:
    """Gerçek Chromium. Playwright burada tembel yüklenir: kurulu olmayabilir."""

    def run(
        self, request: BrowseRequest, allow: Callable[[str], bool], *, collect_body: bool
    ) -> PageState:
        try:
            from playwright.sync_api import (  # type: ignore[import-not-found]
                Error as PlaywrightError,
            )
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BackendUnavailable(INSTALL_HINT) from exc

        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True, args=CHROMIUM_ARGS)
                except (PlaywrightError, OSError) as exc:
                    raise BackendUnavailable(INSTALL_HINT) from exc
                try:
                    return self._drive(browser, request, allow, collect_body, PlaywrightError)
                finally:
                    browser.close()
        except (PlaywrightError, OSError) as exc:
            # sync_playwright() başlangıcı veya kapanışı (sürücü süreci) başarısız oldu.
            raise BackendUnavailable(INSTALL_HINT) from exc

    @staticmethod
    def _drive(
        browser: Any,
        request: BrowseRequest,
        allow: Callable[[str], bool],
        collect_body: bool,
        playwright_error: type[Exception],
    ) -> PageState:
        context = browser.new_context(accept_downloads=False, service_workers="block")
        try:
            def guard(route: Any) -> None:
                if allow(route.request.url):
                    route.continue_()
                else:
                    route.abort()

            def guard_socket(socket: Any) -> None:
                # `route` WebSocket'leri yakalamaz: ws://127.0.0.1 ayrıca denetlenmeli.
                url = socket.url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
                if allow(url):
                    socket.connect_to_server()
                else:
                    socket.close()

            # Bağlam düzeyinde: tıklamayla açılan yeni sekme/pencere de aynı kuraldan geçer.
            context.route("**/*", guard)
            context.route_web_socket("**/*", guard_socket)
            page = context.new_page()
            try:
                page.goto(request.url, timeout=NAVIGATION_TIMEOUT_MS)
            except (playwright_error, ValueError) as exc:
                raise NavigationFailed("Sayfa açılamadı veya yönlendirme engellendi.") from exc
            state = PageState()
            for index, step in enumerate(request.steps):
                timeout = step.timeout_ms or DEFAULT_STEP_TIMEOUT_MS
                try:
                    if step.action == "click":
                        page.click(step.selector, timeout=timeout)
                    elif step.action == "fill":
                        page.fill(step.selector, step.value or "", timeout=timeout)
                    elif step.action == "wait_for":
                        page.wait_for_selector(step.selector, timeout=timeout)
                    else:
                        state.read_texts.append(page.inner_text(step.selector, timeout=timeout))
                except (playwright_error, ValueError, OSError) as exc:
                    raise StepFailed(
                        index, f"{index}. adım ({step.action}) başarısız.", state.read_texts
                    ) from exc
            try:
                state.title = page.title()
                state.url = page.url
                if collect_body:
                    state.body_text = page.inner_text("body", timeout=DEFAULT_STEP_TIMEOUT_MS)
            except (playwright_error, ValueError) as exc:
                raise NavigationFailed("Sayfa içeriği okunamadı.") from exc
            return state
        finally:
            context.close()


class FakeBackend:
    """Test kancası (`JARVIS_BROWSER_FAKE`): Chromium'u taklit eden belirleyici arka uç.

    JSON biçimi (hepsi isteğe bağlı):
      title, final_url, body_text: sayfa bilgisi.
      redirects: ilk gezinmede izlenen yönlendirme adresleri; biri engellenirse gezinme
        başarısız olur (gerçek Chromium'daki iptal edilen yönlendirme gibi).
      subrequests: sayfanın yüklediği alt kaynak adresleri; engellenenler yalnız sayılır.
      elements: {seçici: metin}. Bu sözlükte olmayan seçici adımı başarısız eder.
    Her adres gerçek sunucuyla aynı `allow` (check_url) işlevinden geçer.
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec

    def run(
        self, request: BrowseRequest, allow: Callable[[str], bool], *, collect_body: bool
    ) -> PageState:
        spec = self._spec
        if not allow(request.url):
            raise NavigationFailed("Sayfa açılamadı veya yönlendirme engellendi.")
        for hop in spec.get("redirects", []):
            if not allow(str(hop)):
                raise NavigationFailed("Sayfa açılamadı veya yönlendirme engellendi.")
        for resource in spec.get("subrequests", []):
            allow(str(resource))  # engellenirse yalnız sayaç artar
        elements: dict[str, str] = spec.get("elements", {})
        state = PageState()
        for index, step in enumerate(request.steps):
            if step.selector not in elements:
                raise StepFailed(
                    index, f"{index}. adım ({step.action}) başarısız.", state.read_texts
                )
            if step.action == "read_text":
                state.read_texts.append(elements[step.selector])
        state.title = str(spec.get("title", ""))
        state.url = str(spec.get("final_url", request.url))
        if collect_body:
            state.body_text = str(spec.get("body_text", ""))
        return state


def select_backend() -> Backend:
    fake_path = os.environ.get(FAKE_BACKEND_ENV)
    if not fake_path:
        return PlaywrightBackend()
    try:
        spec = json.loads(Path(fake_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackendUnavailable("Sahte arka uç dosyası okunamadı.") from exc
    if not isinstance(spec, dict):
        raise BackendUnavailable("Sahte arka uç dosyası bir nesne olmalı.")
    return FakeBackend(spec)


# --- Araç yürütme -----------------------------------------------------------------------


def _error(message: str, **extra: Any) -> dict[str, Any]:
    structured = {"error": message, **extra}
    return {
        "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
        "structuredContent": structured,
        "isError": True,
    }


def _fit(texts: list[str]) -> tuple[list[str], bool]:
    """Toplam uzunluğu MAX_TEXT_CHARS'ı aşmayacak şekilde sırayla kırpar."""
    budget = MAX_TEXT_CHARS
    fitted: list[str] = []
    truncated = False
    for text in texts:
        if len(text) > budget:
            text = text[:budget]
            truncated = True
        budget -= len(text)
        fitted.append(text)
    return fitted, truncated


def run_browse(
    arguments: Any,
    backend_factory: Callable[[], Backend] = select_backend,
    resolve: Resolver | None = None,
    *,
    allow_local: bool | None = None,
) -> dict[str, Any]:
    """Doğrular, URL'yi tarayıcıdan ÖNCE denetler, sonucu MCP biçiminde döndürür."""
    try:
        request = BrowseRequest.model_validate(arguments)
    except ValidationError:
        # Pydantic'in ham hata metni girdi değerlerini içerir; kısa sabit mesaj döner.
        return _error("Geçersiz argümanlar: url ve en çok 20 geçerli adım bekleniyor.")
    local = allow_local if allow_local is not None else os.environ.get(ALLOW_LOCAL_ENV) == "1"
    resolver = resolve if resolve is not None else make_resolver()
    reason = check_url(request.url, resolver, allow_local=local)
    if reason is not None:
        return _error(f"Adres reddedildi: {reason}")

    blocked = 0

    def allow(candidate: str) -> bool:
        nonlocal blocked
        if check_url(candidate, resolver, allow_local=local) is None:
            return True
        blocked += 1
        return False

    collect_body = not any(step.action == "read_text" for step in request.steps)
    try:
        state = backend_factory().run(request, allow, collect_body=collect_body)
    except BackendUnavailable as exc:
        return _error(str(exc))
    except NavigationFailed as exc:
        return _error(str(exc), blocked_requests=blocked)
    except StepFailed as exc:
        fitted, truncated = _fit(exc.read_texts)
        return _error(
            str(exc),
            failed_step=exc.index,
            read_texts=fitted,
            truncated=truncated,
            blocked_requests=blocked,
        )

    if collect_body:
        (title, body), truncated = _fit([state.title, state.body_text])
        payload: dict[str, Any] = {"title": title, "url": state.url[:URL_LIMIT], "text": body}
    else:
        texts, truncated = _fit(state.read_texts)
        payload = {"title": state.title[:200], "url": state.url[:URL_LIMIT], "read_texts": texts}
    payload["truncated"] = truncated
    payload["blocked_requests"] = blocked
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": False,
    }


# --- JSON-RPC döngüsü -------------------------------------------------------------------


def _send(message: dict[str, Any]) -> None:
    line = json.dumps(message, ensure_ascii=False)
    if len(line.encode("utf-8")) >= MAX_LINE_BYTES:
        # Metinler zaten sınırlıdır; yine de istemcinin 1 MiB tavanı bir sözleşme güvencesidir.
        line = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32603, "message": "Yanıt çok büyük."},
            }
        )
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def handle_message(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if not isinstance(method, str) or "id" not in message:
        return  # bildirim (notifications/initialized dahil) veya yanıt: cevap verilmez
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        )
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "jarvis-browser", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL_SPEC]}
    elif method == "tools/call":
        if params.get("name") != "browse":
            result = _error("Bilinmeyen araç.")
        else:
            result = run_browse(params.get("arguments", {}))
    elif method == "ping":
        result = {}
    else:
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Yöntem bulunamadı."},
            }
        )
        return
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def main() -> None:
    # MCP stdio UTF-8'dir; Windows'ta metin akışları kod sayfasıyla açılır, açıkça çevrilir.
    for stream in (sys.stdin, sys.stdout):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        if len(line) >= MAX_LINE_BYTES:
            continue
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            continue  # bozuk satır sunucuyu düşürmez
        if not isinstance(message, dict):
            continue
        try:
            handle_message(message)
        except (ValueError, TypeError, KeyError, RecursionError):
            # Beklenmeyen girdi süreci düşürmez; istek kimliği varsa iç hata yanıtı döner.
            if "id" in message:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {"code": -32603, "message": "İç hata."},
                    }
                )


if __name__ == "__main__":
    main()
