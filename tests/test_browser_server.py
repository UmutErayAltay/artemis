"""Tarayıcı MCP sunucusu (ADR 0032): saf URL politikası + gerçek istemciyle uçtan uca.

Uçtan uca testler sahte arka ucu (`ARTEMIS_BROWSER_FAKE`) kullanır; gerçek ağa veya
Chromium'a çıkılmaz. Gerçek Chromium testi yalnız Playwright kuruluysa ve elle çalışır.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from artemis.mcp.client import MCPCallResult, StdioMCPClient
from artemis.mcp_servers import browser
from artemis.mcp_servers.browser import check_url
from artemis.tools.platform import child_environment

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = "93.184.216.34"


def resolver(*addresses: str):
    return lambda host: list(addresses)


def unresolvable(host: str) -> list[str]:
    raise socket.gaierror("çözülemedi")


# --- check_url --------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["http://example.com/a", "https://example.com:8443/x?y=1"])
def test_public_http_urls_are_allowed(url: str) -> None:
    assert check_url(url, resolver(PUBLIC)) is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,<b>x</b>",
        "javascript:alert(1)",
        "chrome://settings",
        "ftp://example.com/x",
        "about:blank",
        "example.com/yol",
    ],
)
def test_non_http_schemes_are_rejected(url: str) -> None:
    assert check_url(url, resolver(PUBLIC)) is not None


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "[::1]",
        "10.1.2.3",
        "172.16.0.5",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",
        "[fe80::1]",
        "[::ffff:127.0.0.1]",
        "0.0.0.0",
        "224.0.0.1",
        "100.64.0.1",
    ],
)
def test_local_ip_literals_are_rejected(host: str) -> None:
    def must_not_resolve(name: str) -> list[str]:
        raise AssertionError("IP literal çözümlenmemeli")

    assert check_url(f"http://{host}/", must_not_resolve) is not None


def test_localhost_name_is_rejected() -> None:
    assert check_url("http://localhost:8080/", resolver(PUBLIC)) is not None
    assert check_url("http://app.localhost/", resolver(PUBLIC)) is not None


@pytest.mark.parametrize("address", ["10.0.0.1", "127.0.0.1", "::1", "::ffff:10.0.0.1"])
def test_name_resolving_to_private_is_rejected(address: str) -> None:
    assert check_url("http://evil.example/", resolver(address)) is not None


def test_mixed_public_and_private_resolution_is_rejected() -> None:
    assert check_url("http://evil.example/", resolver(PUBLIC, "192.168.0.7")) is not None


def test_unresolvable_or_empty_resolution_is_rejected() -> None:
    assert check_url("http://yok.example/", unresolvable) is not None
    assert check_url("http://yok.example/", resolver()) is not None


@pytest.mark.parametrize(
    "url",
    ["http://user@example.com/", "http://user:pw@example.com/", "https://a.com@127.0.0.1/"],
)
def test_userinfo_is_rejected(url: str) -> None:
    assert check_url(url, resolver(PUBLIC)) is not None


def test_invalid_port_is_rejected() -> None:
    assert check_url("http://example.com:99999/", resolver(PUBLIC)) is not None


def test_allow_local_relaxes_host_but_not_scheme() -> None:
    assert check_url("http://127.0.0.1:8000/", resolver(PUBLIC), allow_local=True) is None
    assert check_url("http://localhost/", resolver(PUBLIC), allow_local=True) is None
    assert check_url("file:///etc/passwd", resolver(PUBLIC), allow_local=True) is not None
    assert check_url("http://u@127.0.0.1/", resolver(PUBLIC), allow_local=True) is not None


def test_default_resolver_caches_per_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolve = browser.make_resolver()
    assert resolve("a.example") == [PUBLIC]
    assert resolve("a.example") == [PUBLIC]
    assert calls == ["a.example"]


# --- run_browse (süreç içi, sahte arka uç) ---------------------------------------------


def test_run_browse_rejects_bad_url_before_backend_is_created() -> None:
    def factory() -> browser.Backend:
        raise AssertionError("tarayıcı başlatılmamalı")

    result = browser.run_browse({"url": "file:///etc/passwd"}, factory, resolver(PUBLIC))
    assert result["isError"] is True


def test_text_is_truncated_to_budget() -> None:
    backend = browser.FakeBackend({"title": "T", "body_text": "x" * 50000})
    result = browser.run_browse({"url": "https://example.com/"}, lambda: backend, resolver(PUBLIC))
    data = result["structuredContent"]
    assert data["truncated"] is True
    assert len(data["title"]) + len(data["text"]) <= browser.MAX_TEXT_CHARS


# --- Gerçek istemciyle uçtan uca --------------------------------------------------------


def make_client(tmp_path: Path, spec: dict[str, Any]) -> StdioMCPClient:
    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps(spec), encoding="utf-8")
    env = {**child_environment(), "ARTEMIS_BROWSER_FAKE": str(fake), "PYTHONPATH": str(ROOT)}
    return StdioMCPClient(
        [sys.executable, "-m", "artemis.mcp_servers.browser"], env=env, cwd=ROOT, timeout=20
    )


def call(client: StdioMCPClient, arguments: dict[str, Any]) -> MCPCallResult:
    return client.call_tool("browse", arguments)


def test_list_tools_shows_browse_with_strict_schema(tmp_path: Path) -> None:
    tools = make_client(tmp_path, {}).list_tools()
    assert [tool.name for tool in tools] == ["browse"]
    schema = tools[0].input_schema
    assert schema["additionalProperties"] is False
    assert schema["properties"]["steps"]["maxItems"] == 20
    item = schema["properties"]["steps"]["items"]
    assert item["properties"]["action"]["enum"] == ["click", "fill", "read_text", "wait_for"]
    assert item["additionalProperties"] is False


def test_call_returns_title_url_and_body_text(tmp_path: Path) -> None:
    spec = {
        "title": "Başlık ğüş",
        "body_text": "Merhaba dünya",
        "final_url": "https://example.com/z",
    }
    result = call(make_client(tmp_path, spec), {"url": "https://example.com/"})
    assert not result.is_error
    assert result.structured is not None
    assert result.structured["title"] == "Başlık ğüş"
    assert result.structured["text"] == "Merhaba dünya"
    assert result.structured["url"] == "https://example.com/z"
    assert result.structured["blocked_requests"] == 0


def test_read_text_steps_return_selected_texts(tmp_path: Path) -> None:
    spec = {"title": "T", "elements": {"#a": "birinci", "#b": "ikinci"}}
    steps = [
        {"action": "wait_for", "selector": "#a"},
        {"action": "read_text", "selector": "#a"},
        {"action": "read_text", "selector": "#b", "timeout_ms": 500},
    ]
    result = call(make_client(tmp_path, spec), {"url": "https://example.com/", "steps": steps})
    assert not result.is_error
    assert result.structured is not None
    assert result.structured["read_texts"] == ["birinci", "ikinci"]


def test_failed_step_reports_index_and_partial_texts(tmp_path: Path) -> None:
    spec = {"elements": {"#a": "birinci"}}
    steps = [
        {"action": "read_text", "selector": "#a"},
        {"action": "click", "selector": "#yok"},
        {"action": "read_text", "selector": "#a"},
    ]
    result = call(make_client(tmp_path, spec), {"url": "https://example.com/", "steps": steps})
    assert result.is_error
    assert result.structured is not None
    assert result.structured["failed_step"] == 1
    assert result.structured["read_texts"] == ["birinci"]


def test_redirect_to_loopback_is_blocked_and_reported(tmp_path: Path) -> None:
    spec = {"redirects": ["http://127.0.0.1:9/gizli"], "body_text": "sızdı"}
    result = call(make_client(tmp_path, spec), {"url": "https://example.com/"})
    assert result.is_error
    assert "sızdı" not in result.text
    assert result.structured is not None
    assert result.structured["blocked_requests"] == 1


def test_blocked_subrequest_is_counted_but_page_still_returns(tmp_path: Path) -> None:
    spec = {
        "body_text": "sayfa",
        "subrequests": [
            "http://169.254.169.254/meta",
            "file:///etc/passwd",
            "https://cdn.example/x",
        ],
    }
    result = call(make_client(tmp_path, spec), {"url": "https://example.com/"})
    assert not result.is_error
    assert result.structured is not None
    # example.com / cdn.example gerçek DNS'e çıkmaz: çözülemeyen ad da engellenir (güvenli taraf).
    assert result.structured["blocked_requests"] >= 2


@pytest.mark.parametrize(
    "arguments",
    [
        {"url": "https://example.com/", "steps": [{"action": "click", "selector": "a"}] * 21},
        {"url": "https://example.com/", "steps": [{"action": "hover", "selector": "a"}]},
        {"url": "https://example.com/", "steps": [{"action": "fill", "selector": "a"}]},
        {"url": "https://example.com/", "steps": [{"action": "click", "selector": ""}]},
        {"url": "https://example.com/", "extra": 1},
        {"steps": []},
    ],
)
def test_invalid_arguments_are_rejected_without_launching(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    # Sahte dosya yok: arka uç oluşturulsaydı "okunamadı" hatası çıkardı; doğrulama ondan önce.
    client = make_client(tmp_path, {})
    (tmp_path / "fake.json").unlink()
    result = call(client, arguments)
    assert result.is_error
    assert "okunamadı" not in result.text
    assert "Geçersiz argümanlar" in result.text


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "data:text/html,x", "javascript:1", "http://127.0.0.1/"]
)
def test_forbidden_urls_are_rejected_end_to_end(tmp_path: Path, url: str) -> None:
    result = call(make_client(tmp_path, {"body_text": "sızdı"}), {"url": url})
    assert result.is_error
    assert "sızdı" not in result.text
    assert "reddedildi" in result.text


def test_missing_playwright_gives_honest_install_hint(tmp_path: Path) -> None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("Playwright kurulu")
    env = {**child_environment(), "PYTHONPATH": str(ROOT)}
    client = StdioMCPClient(
        [sys.executable, "-m", "artemis.mcp_servers.browser"], env=env, cwd=ROOT, timeout=20
    )
    # Kamusal ad gerçek DNS gerektirir; IP literal çözümleme istemez.
    result = call(client, {"url": "http://93.184.216.34/"})
    assert result.is_error
    assert "playwright install chromium" in result.text


def test_protocol_edge_cases() -> None:
    import subprocess

    lines = [
        "bozuk json",
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        ),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            }
        ),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "bilinmeyen/yontem"}),
    ]
    env = {**child_environment(), "PYTHONPATH": str(ROOT)}
    done = subprocess.run(
        [sys.executable, "-m", "artemis.mcp_servers.browser"],
        input=("\n".join(lines) + "\n").encode(),
        capture_output=True,
        cwd=ROOT,
        env=env,
        timeout=20,
        check=False,
    )
    replies = [json.loads(line) for line in done.stdout.decode("utf-8").splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"
    assert replies[0]["result"]["serverInfo"]["name"] == "artemis-browser"
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[2]["error"]["code"] == -32601


# --- Gerçek Chromium (elle) -------------------------------------------------------------


class _Page(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = (
            "<html><head><title>Deneme</title></head><body>"
            "<input id='i'><button id='b' onclick=\"document.getElementById('o')"
            ".textContent=document.getElementById('i').value\">tamam</button>"
            "<p id='o'>boş</p></body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


@pytest.mark.disruptive
def test_real_chromium_fill_click_read(tmp_path: Path) -> None:
    pytest.importorskip("playwright")
    server = HTTPServer(("127.0.0.1", 0), partial(_Page))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            **child_environment(),
            "PYTHONPATH": str(ROOT),
            "ARTEMIS_BROWSER_ALLOW_LOCAL": "1",
        }
        env.pop("ARTEMIS_BROWSER_FAKE", None)
        client = StdioMCPClient(
            [sys.executable, "-m", "artemis.mcp_servers.browser"], env=env, cwd=ROOT, timeout=60
        )
        steps = [
            {"action": "fill", "selector": "#i", "value": "merhaba"},
            {"action": "click", "selector": "#b"},
            {"action": "read_text", "selector": "#o"},
        ]
        result = call(client, {"url": f"http://127.0.0.1:{server.server_port}/", "steps": steps})
        assert not result.is_error, result.text
        assert result.structured is not None
        assert result.structured["read_texts"] == ["merhaba"]
    finally:
        server.shutdown()
        server.server_close()


# --- İnceleme bulguları: geçersiz IDNA, gerçek arka ucun bağlam düzeyi korumaları ----------


def test_invalid_idna_host_is_rejected_not_raised() -> None:
    def broken(host: str) -> list[str]:
        raise UnicodeError("label too long")

    assert check_url("http://a..com/", broken) == "Host çözülemedi."


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []


class _FakePage:
    url = "https://example.org/son"

    def __init__(self, recorder: _Recorder) -> None:
        self.recorder = recorder

    def goto(self, url: str, timeout: int) -> None:
        self.recorder.events.append(("goto", url))

    def title(self) -> str:
        return "Başlık"

    def inner_text(self, selector: str, timeout: int) -> str:
        return "metin"


class _FakeContext:
    def __init__(self, recorder: _Recorder) -> None:
        self.recorder = recorder
        self.guard: Any = None
        self.socket_guard: Any = None

    def route(self, pattern: str, handler: Any) -> None:
        self.recorder.events.append(("context.route", pattern))
        self.guard = handler

    def route_web_socket(self, pattern: str, handler: Any) -> None:
        self.recorder.events.append(("context.route_web_socket", pattern))
        self.socket_guard = handler

    def new_page(self) -> _FakePage:
        self.recorder.events.append(("new_page", None))
        return _FakePage(self.recorder)

    def close(self) -> None:
        self.recorder.events.append(("context.close", None))


class _FakeBrowser:
    def __init__(self, recorder: _Recorder) -> None:
        self.recorder = recorder
        self.context: _FakeContext | None = None

    def new_context(self, **kwargs: Any) -> _FakeContext:
        self.recorder.events.append(("new_context", kwargs))
        self.context = _FakeContext(self.recorder)
        return self.context


class _Route:
    def __init__(self, url: str) -> None:
        self.request = type("R", (), {"url": url})()
        self.outcome = ""

    def continue_(self) -> None:
        self.outcome = "continue"

    def abort(self) -> None:
        self.outcome = "abort"


class _Socket:
    def __init__(self, url: str) -> None:
        self.url = url
        self.outcome = ""

    def connect_to_server(self) -> None:
        self.outcome = "connect"

    def close(self) -> None:
        self.outcome = "close"


def test_real_backend_guards_whole_context_and_websockets_before_any_page() -> None:
    recorder = _Recorder()
    fake = _FakeBrowser(recorder)
    request = browser.BrowseRequest.model_validate({"url": "https://example.org/"})

    def public_only(url: str) -> bool:
        return check_url(url, lambda host: ["93.184.216.34"]) is None

    state = browser.PlaywrightBackend._drive(fake, request, public_only, True, RuntimeError)
    names = [name for name, _ in recorder.events]
    assert names.index("context.route") < names.index("new_page")
    assert names.index("context.route_web_socket") < names.index("new_page")
    assert names[-1] == "context.close"
    assert recorder.events[0] == (
        "new_context",
        {"accept_downloads": False, "service_workers": "block"},
    )
    assert state.body_text == "metin"
    context = fake.context
    assert context is not None
    for url, expected in [
        ("https://example.org/a.js", "continue"),
        ("http://127.0.0.1:8080/", "abort"),
        ("file:///etc/passwd", "abort"),
    ]:
        route = _Route(url)
        context.guard(route)
        assert route.outcome == expected, url
    for url, expected in [("wss://example.org/s", "connect"), ("ws://127.0.0.1:9222/", "close")]:
        sock = _Socket(url)
        context.socket_guard(sock)
        assert sock.outcome == expected, url


def test_chromium_disables_non_http_local_channels() -> None:
    assert any(
        "webrtc-ip-handling-policy=disable_non_proxied_udp" in a for a in browser.CHROMIUM_ARGS
    )
    assert "--dns-prefetch-disable" in browser.CHROMIUM_ARGS
