"""İlk çalıştırma 'Beyni bağla' sihirbazı: OpenRouter PKCE, abonelik CLI, API anahtarı/yerel model.

Anahtarlar asla YAML'a, loga veya hata mesajlarına yazılmaz; yalnız `store_endpoint_key` üzerinden
Kimlik Bilgisi Yöneticisi'ne kaydedilir.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import http.server
import re
import secrets
import shutil
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import yaml
from pydantic import ValidationError
from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from artemis.brain.providers import stored_endpoint_key
from artemis.channels.desktop import themes
from artemis.channels.desktop.provider_helpers import _slug
from artemis.channels.desktop.provider_layout import PRESETS
from artemis.channels.desktop.provider_section import store_endpoint_key
from artemis.channels.desktop.settings_io import _write_validated
from artemis.channels.desktop.thread_shutdown import shutdown_thread
from artemis.config.settings import Settings, load_settings

KEY_PAGES: dict[str, str] = {
    "openrouter": "https://openrouter.ai/keys",
    "gemini": "https://aistudio.google.com/apikey",
    "groq": "https://console.groq.com/keys",
    "openai": "https://platform.openai.com/api-keys",
    "claude": "https://console.anthropic.com/settings/keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "xai": "https://console.x.ai",
    "mistral": "https://console.mistral.ai/api-keys",
}

CLAUDE_DOC_URL = "https://docs.claude.com/claude-code"
CODEX_DOC_URL = "https://github.com/openai/codex"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
GEMINI_KEY_URL = "https://aistudio.google.com/apikey"
GEMINI_VALIDATE_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_MODEL_ALIAS = "gemini-3-flash"
GEMINI_MODEL_ID = "gemini-3-flash-preview"
GEMINI_KEY_RE = re.compile(r"^(AIza[0-9A-Za-z_-]{35}|AQ\.[0-9A-Za-z_-]{30,})$")

_CALLBACK_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Artemis</title></head>"
    "<body><p>Artemis'e dönebilirsin</p></body></html>"
).encode()

_CHOICE_BUTTON_WIDTH = 280
_CHOICE_BUTTON_HEIGHT = 84
_DIALOG_WIDTH = 660


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256: BASE64URL-ENCODE(SHA256(ASCII(code_verifier))) '=' dolgusuz."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def free_tool_models(catalog_json: object) -> list[str]:
    """OpenRouter model kataloğundan `:free` ile biten ve `tools` destekleyen kimlikleri süzer."""
    if not isinstance(catalog_json, Mapping):
        return []
    data = catalog_json.get("data")
    if not isinstance(data, list):
        return []
    found: set[str] = set()
    for item in data:
        if not isinstance(item, Mapping):
            continue
        model_id = item.get("id")
        params = item.get("supported_parameters")
        if (
            isinstance(model_id, str)
            and model_id.endswith(":free")
            and isinstance(params, list)
            and "tools" in params
        ):
            found.add(model_id)
    return sorted(found)


class _CallbackServer(http.server.HTTPServer):
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], handler: type[http.server.BaseHTTPRequestHandler]):
        super().__init__(address, handler)
        self.code: str | None = None
        self.expected_state: str = ""
        self.got_request = threading.Event()


def _default_server_factory() -> _CallbackServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            server: _CallbackServer = self.server  # type: ignore[assignment]
            # Tarayıcı dışı/XHR isteklerini ve CSRF girişimlerini engelle:
            # Gerçek üst düzey yönlendirmede Origin başlığı olmaz ve Sec-Fetch-Mode 'navigate' olur.
            if "Origin" in self.headers:
                self.send_response(400)
                self.end_headers()
                return
            fetch_mode = self.headers.get("Sec-Fetch-Mode")
            if fetch_mode is not None and fetch_mode != "navigate":
                self.send_response(400)
                self.end_headers()
                return

            parsed = urlsplit(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = parse_qs(parsed.query)
            received_state = params.get("state", [""])[0]
            if not received_state or not hmac.compare_digest(
                received_state, server.expected_state
            ):
                self.send_response(400)
                self.end_headers()
                return
            values = params.get("code")
            if values and values[0]:
                server.code = values[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_CALLBACK_HTML)))
            self.end_headers()
            self.wfile.write(_CALLBACK_HTML)
            server.got_request.set()

        def log_message(self, format: str, *args: object) -> None:
            # Sır (code parametresi) standart hataya veya loga yazılmaz.
            return

    return _CallbackServer(("127.0.0.1", 0), Handler)


class OpenRouterPkce:
    """OpenRouter OAuth PKCE akışı; ağ ve yerel geri dönüş sunucusu enjekte edilebilir."""

    def __init__(
        self,
        *,
        http: Any = None,
        server_factory: Callable[[], _CallbackServer] | None = None,
    ) -> None:
        self._http = http if http is not None else httpx
        self._server_factory = server_factory or _default_server_factory
        self.code_verifier: str = secrets.token_urlsafe(64)
        self.code_challenge: str = pkce_challenge(self.code_verifier)
        self.state: str | None = None
        self._server: _CallbackServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        """127.0.0.1 üzerinde tek kullanımlık geri dönüş sunucusu açar, auth adresini döner."""
        self.close()
        self.state = secrets.token_urlsafe(32)
        server = self._server_factory()
        server.expected_state = self.state
        server.timeout = 0.5
        self._server = server
        port = server.server_port

        def serve() -> None:
            try:
                while not server.got_request.is_set():
                    server.handle_request()
            except OSError:
                server.got_request.set()
            finally:
                with contextlib.suppress(OSError):
                    server.server_close()

        thread = threading.Thread(target=serve, daemon=True)
        self._thread = thread
        thread.start()
        callback_url = f"http://127.0.0.1:{port}/callback?state={self.state}"
        quoted_callback = quote(callback_url, safe="")
        return (
            f"{OPENROUTER_AUTH_URL}?callback_url={quoted_callback}"
            f"&code_challenge={self.code_challenge}&code_challenge_method=S256"
        )

    def wait_code(self, timeout: float = 300) -> str:
        """Tarayıcı geri dönüşünden gelen yetkilendirme kodunu bekler."""
        server = self._server
        if server is None:
            raise ValueError("Giriş başlatılmadı.")
        finished = server.got_request.wait(timeout=timeout)
        self.close()
        if not finished:
            raise TimeoutError("OpenRouter giriş süresi doldu.")
        if not server.code:
            raise ValueError("OpenRouter yetkilendirme kodu alınamadı.")
        return server.code

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.got_request.set()
            with contextlib.suppress(OSError):
                server.server_close()

    def exchange(self, code: str) -> str:
        """Yetkilendirme kodunu OpenRouter API anahtarıyla değiştirir; gövde/sır hataya girmez."""
        payload = {
            "code": code,
            "code_verifier": self.code_verifier,
            "code_challenge_method": "S256",
        }
        try:
            response = self._http.post(
                OPENROUTER_KEYS_URL,
                json=payload,
                timeout=20,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise ValueError("OpenRouter sunucusuna ulaşılamadı.") from None
        if not (200 <= response.status_code < 300):
            raise ValueError(
                f"OpenRouter anahtar değişimi başarısız oldu (HTTP {response.status_code})."
            )
        try:
            data = response.json()
        except ValueError:
            data = None
        key = data.get("key") if isinstance(data, Mapping) else None
        if not isinstance(key, str) or not key.strip():
            raise ValueError("OpenRouter yanıtında API anahtarı bulunamadı.")
        return key.strip()

    def fetch_free_models(self) -> list[str]:
        """Ücretsiz ve araç destekli OpenRouter modellerini getirir."""
        try:
            response = self._http.get(
                OPENROUTER_MODELS_URL,
                timeout=20,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise ValueError("OpenRouter model listesi alınamadı.") from None
        if not (200 <= response.status_code < 300):
            raise ValueError(
                f"OpenRouter model listesi alınamadı (HTTP {response.status_code})."
            )
        try:
            catalog = response.json()
        except ValueError:
            raise ValueError("OpenRouter model listesi okunamadı.") from None
        return free_tool_models(catalog)


def needs_wizard(
    settings: Settings,
    *,
    key_lookup: Callable[[str], str | None] | None = None,
) -> bool:
    """Model yoksa ya da birincil modelin ucu anahtar isteyip anahtar bulunamıyorsa True döner."""
    lookup = key_lookup or stored_endpoint_key
    if not settings.models:
        return True
    primary = settings.models.get(settings.primary_model)
    if primary is None:
        return True
    if primary.endpoint is None:
        return False
    endpoint = settings.endpoints.get(primary.endpoint)
    if endpoint is None:
        return True
    if not endpoint.api_key:
        return False
    return not bool(lookup(primary.endpoint))


def _default_open_url(url: str) -> object:
    return QDesktopServices.openUrl(QUrl(url))


def validate_gemini_key(key: str) -> int:
    """Google AI Studio anahtarını tek bir GET isteğiyle doğrular; sır mesaja/loga girmez."""
    try:
        response = httpx.get(
            GEMINI_VALIDATE_URL,
            headers={"x-goog-api-key": key},
            timeout=15,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        return 0
    return response.status_code


class _GeminiValidateThread(QThread):
    finished_with = Signal(str, int)

    def __init__(
        self,
        key: str,
        validator: Callable[[str], int],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._key = key
        self._validator = validator

    def run(self) -> None:
        status = self._validator(self._key)
        self.finished_with.emit(self._key, status)


LOCAL_SERVICE_PORTS: tuple[tuple[int, str], ...] = (
    (8317, "CLIProxyAPI / EasyProxy"),
    (8787, "Yerel proxy"),
    (4000, "LiteLLM"),
    (1234, "LM Studio"),
    (11434, "Ollama"),
)
_MAX_PROBED_MODELS = 200
LocalService = tuple[str, str, bool, list[str]]  # ad, base_url, anahtar ister mi, modeller


def probe_local_services(get: Callable[..., Any] | None = None) -> list[LocalService]:
    """Bu bilgisayarda OpenAI uyumlu model servisi arar.

    Yalnız 127.0.0.1'e kısa GET atılır; yanıt dış veridir, yalnız model kimlikleri listeye girer.
    Artemis proxy kurmaz, çalıştırmaz, kimseye giriş yapmaz: kullanıcının açtığı servise bağlanır.
    """
    fetch = get or httpx.get
    found: list[LocalService] = []
    for port, name in LOCAL_SERVICE_PORTS:
        base_url = f"http://127.0.0.1:{port}/v1"
        try:
            response = fetch(f"{base_url}/models", timeout=0.8, follow_redirects=False)
        except httpx.HTTPError:
            continue
        if response.status_code in {401, 403}:
            found.append((name, base_url, True, []))
            continue
        if response.status_code != 200:
            continue
        try:
            body = response.json()
        except ValueError:
            continue
        data = body.get("data") if isinstance(body, Mapping) else None
        if not isinstance(data, list):
            continue
        ids = {
            item["id"] for item in data
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            and 0 < len(item["id"]) <= 200
        }
        found.append((name, base_url, False, sorted(ids)[:_MAX_PROBED_MODELS]))
    return found


class _LocalProbeThread(QThread):
    finished_with = Signal(list)

    def __init__(self, probe: Callable[[], list[Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._probe = probe

    def run(self) -> None:
        self.finished_with.emit(self._probe())


class _OpenRouterLoginThread(QThread):
    """Tarayıcı geri dönüşünü bekler (en çok 5 dk); arayüz bu sırada donmaz."""

    finished_with = Signal(str, list, str)  # anahtar, modeller, hata

    def __init__(self, oauth: OpenRouterPkce, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._oauth = oauth

    def run(self) -> None:
        try:
            key = self._oauth.exchange(self._oauth.wait_code())
        except (OSError, TimeoutError, ValueError) as error:
            self.finished_with.emit("", [], str(error))
            return
        try:
            models = self._oauth.fetch_free_models()
        except ValueError as error:
            self.finished_with.emit(key, [], str(error))
            return
        self.finished_with.emit(key, models, "")


def _retire_thread(
    thread: _LocalProbeThread | _OpenRouterLoginThread | _GeminiValidateThread | None,
    slot: Any,
) -> None:
    """İş parçacığı bitmeden deleteLater çağırmamak için önce güvenli durdurur."""
    if thread is None:
        return
    if shutdown_thread(thread, ("finished_with", slot), wait_ms=2000):
        # Süre dolduysa thread DETACHED içinde kalır; bitince silinir.
        finished = getattr(thread, "finished", None)
        if finished is not None:
            finished.connect(thread.deleteLater)
    else:
        thread.deleteLater()


def _load_raw(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


class ChoiceCard(QPushButton):
    """Başlık, açıklama ve isteğe bağlı rozet taşıyan seçilebilir kart."""

    def __init__(self, title: str, detail: str, badge: str = "") -> None:
        super().__init__()
        self.title, self.detail = title, detail
        self.setObjectName("choiceCard")
        self.setCheckable(True)
        self.setAutoDefault(False)
        self.setAccessibleName(f"{title}. {detail}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        labels = [QLabel(title), QLabel(detail)]
        labels[0].setObjectName("choiceTitle")
        labels[1].setObjectName("choiceDetail")
        labels[1].setWordWrap(True)
        top.addWidget(labels[0])
        top.addStretch(1)
        if badge:
            labels.append(QLabel(badge))
            labels[-1].setObjectName("choiceBadge")
            top.addWidget(labels[-1])
        layout.addLayout(top)
        layout.addWidget(labels[1])
        layout.addStretch(1)
        for label in labels:
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


def _page(title: str, text: str) -> tuple[QFrame, QVBoxLayout, QLabel]:
    page = QFrame()
    page.setObjectName("wizardPage")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(10)
    heading = QLabel(title)
    heading.setObjectName("sectionTitle")
    info = QLabel(text)
    info.setObjectName("muted")
    info.setWordWrap(True)
    layout.addWidget(heading)
    layout.addWidget(info)
    return page, layout, info


class ConnectWizard(QDialog):
    """İlk kurulumda kullanıcıya 4 yoldan biriyle beyin modeli bağlatan pencere."""

    def __init__(
        self,
        config_path: Path,
        *,
        open_url: Callable[[str], object] | None = None,
        oauth: OpenRouterPkce | None = None,
        run_cli: Callable[[str], str | None] | None = None,
        store_key: Callable[[str, str], str | None] | None = None,
        clipboard: Any = None,
        gemini_validator: Callable[[str], int] | None = None,
        local_probe: Callable[[], list[Any]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config_path = config_path
        self._open_url: Callable[[str], object] = open_url or _default_open_url
        self._oauth: OpenRouterPkce = oauth or OpenRouterPkce()
        self._which: Callable[[str], str | None] = run_cli or shutil.which
        self._store_key: Callable[[str, str], str | None] = store_key or store_endpoint_key
        self._clipboard: Any = clipboard
        self._gemini_validator: Callable[[str], int] = gemini_validator or validate_gemini_key
        self._local_probe = local_probe or probe_local_services
        self._watching_clipboard = False
        self._gemini_thread: _GeminiValidateThread | None = None
        self._login_thread: _OpenRouterLoginThread | None = None
        self._probe_thread: _LocalProbeThread | None = None
        self._found: dict[str, tuple[str, bool, list[str]]] = {}
        self._local: tuple[str, str, bool] | None = None  # seçilen yerel servis: ad, adres, anahtar

        self.setWindowTitle("Artemis'i bir beyne bağla")
        self.setMinimumWidth(_DIALOG_WIDTH)
        # Kullanıcının seçtiği tema (ana pencere); ilk açılışta pencere yoksa varsayılan (koyu).
        self.setStyleSheet(themes.stylesheet(str(getattr(parent, "_theme", themes.DEFAULT_THEME))))
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(14)

        title = QLabel("Artemis'i bir beyne bağla")
        title.setObjectName("pageTitle")
        intro = QLabel(
            "Artemis düşünmek için bir dil modeline bağlanır. Sana uyan yolu seç; "
            "sonra Ayarlar > Modeller'den istediğin kadar model ekleyebilirsin."
        )
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(intro)

        self.openrouter_button = ChoiceCard(
            "Ücretsiz hesapla giriş",
            "OpenRouter · anahtar kopyalamadan, tarayıcıda tek giriş", "Önerilen",
        )
        self.gemini_button = ChoiceCard(
            "Google hesabımla", "Gemini · AI Studio'dan ücretsiz anahtar, Artemis panodan alır"
        )
        self.subscription_button = ChoiceCard(
            "Aboneliğimle bağlan", "Claude · bilgisayarındaki Claude Code girişini kullanır"
        )
        self.apikey_button = ChoiceCard(
            "API, yerel model ya da proxy", "Ollama · LM Studio · Groq · kendi proxy'n…"
        )
        self.choice_buttons = (
            self.openrouter_button, self.gemini_button, self.subscription_button,
            self.apikey_button,
        )
        grid = QGridLayout()
        grid.setSpacing(12)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for index, button in enumerate(self.choice_buttons):
            button.setMinimumWidth(_CHOICE_BUTTON_WIDTH)
            button.setMinimumHeight(_CHOICE_BUTTON_HEIGHT)
            button.setSizePolicy(policy)
            grid.addWidget(button, index // 2, index % 2)
        root.addLayout(grid)

        self.stack = QStackedWidget()
        self._empty_page, empty_layout, _info = _page(
            "Bir yol seç",
            "Kararsızsan 'Ücretsiz hesapla giriş' en kısa yol: kart, fatura ya da anahtar "
            "kopyalama yok.",
        )
        empty_layout.addStretch(1)
        self._openrouter_page = self._build_openrouter_page()
        self._gemini_page = self._build_gemini_page()
        self._subscription_page = self._build_subscription_page()
        self._apikey_page = self._build_apikey_page()
        for page in (self._empty_page, self._openrouter_page, self._gemini_page,
                     self._subscription_page, self._apikey_page):
            self.stack.addWidget(page)
        root.addWidget(self.stack, 1)

        bottom = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bottom.addWidget(self.status, 1)
        self.later_button = QPushButton("Sonra")
        self.later_button.setAutoDefault(False)
        self.later_button.clicked.connect(self.reject)
        bottom.addWidget(self.later_button)
        root.addLayout(bottom)

        self.openrouter_button.clicked.connect(self._show_openrouter_panel)
        self.gemini_button.clicked.connect(self._show_gemini_panel)
        self.subscription_button.clicked.connect(self._show_subscription_panel)
        self.apikey_button.clicked.connect(self._show_apikey_panel)
        self.finished.connect(self._on_dialog_finished)

        self._probed = False
        self._apply_preset_choice()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._start_local_probe()

    def _select_card(self, card: ChoiceCard) -> None:
        for button in self.choice_buttons:
            button.setChecked(button is card)

    def _build_openrouter_page(self) -> QWidget:
        page, layout, self.openrouter_info = _page(
            "OpenRouter ile ücretsiz giriş",
            "Tarayıcıda OpenRouter hesabınla giriş yap (Google ile de olur). Artemis anahtarı "
            "kendisi alır ve Windows Kimlik Bilgisi Yöneticisi'ne kaydeder. Ücretsiz modeller "
            "günlük kotayla çalışır, kart istemez.",
        )
        self.openrouter_login_button = QPushButton("Tarayıcıda giriş yap")
        self.openrouter_login_button.setObjectName("primary")
        self.openrouter_login_button.setAutoDefault(False)
        self.openrouter_login_button.clicked.connect(self._start_openrouter_flow)
        layout.addWidget(self.openrouter_login_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.openrouter_models = QComboBox()
        self.openrouter_models.setAccessibleName("OpenRouter ücretsiz modelleri")
        self.openrouter_models.setVisible(False)
        layout.addWidget(self.openrouter_models)

        self.openrouter_bind_button = QPushButton("Seçili modeli bağla")
        self.openrouter_bind_button.setObjectName("primary")
        self.openrouter_bind_button.setAutoDefault(False)
        self.openrouter_bind_button.setEnabled(False)
        self.openrouter_bind_button.setVisible(False)
        self.openrouter_bind_button.clicked.connect(self._bind_openrouter_model)
        layout.addWidget(self.openrouter_bind_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def _build_subscription_page(self) -> QWidget:
        page, layout, _info = _page(
            "Abonelikle bağlan",
            "Bilgisayarında Claude Code kuruluysa Artemis onu çalıştırır; giriş ve kota senin "
            "aboneliğinde kalır, Artemis kimlik bilgilerine dokunmaz.",
        )
        choice_row = QHBoxLayout()
        self.claude_choice_button = QPushButton("Claude")
        self.chatgpt_choice_button = QPushButton("ChatGPT")
        for button in (self.claude_choice_button, self.chatgpt_choice_button):
            button.setObjectName("chip")
            button.setAutoDefault(False)
            choice_row.addWidget(button)
        self.claude_choice_button.clicked.connect(self._select_claude_cli)
        self.chatgpt_choice_button.clicked.connect(self._select_chatgpt_cli)
        choice_row.addStretch(1)
        layout.addLayout(choice_row)

        self.cli_hint = QLabel()
        self.cli_hint.setWordWrap(True)
        self.cli_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.cli_hint)

        self.claude_add_button = QPushButton("Claude modelini ekle")
        self.claude_add_button.setObjectName("primary")
        self.claude_add_button.setAutoDefault(False)
        self.claude_add_button.setVisible(False)
        self.claude_add_button.clicked.connect(self._add_claude_cli_model)
        layout.addWidget(self.claude_add_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.codex_add_button = QPushButton("ChatGPT modelini ekle (yakında)")
        self.codex_add_button.setEnabled(False)
        self.codex_add_button.setVisible(False)
        layout.addWidget(self.codex_add_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def _build_gemini_page(self) -> QWidget:
        page, layout, self.gemini_steps = _page(
            "Google hesabınla Gemini",
            "1) Açılan AI Studio sayfasında Google hesabınla gir.  2) 'Create API key' de.  "
            "3) Anahtarı kopyala; Artemis panodan kendisi alır.",
        )
        key_row = QHBoxLayout()
        self.gemini_key_edit = QLineEdit()
        self.gemini_key_edit.setAccessibleName("Gemini API anahtarı")
        self.gemini_key_edit.setPlaceholderText("Anahtarı buraya da yapıştırabilirsin")
        self.gemini_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_row.addWidget(self.gemini_key_edit, 1)
        self.gemini_bind_button = QPushButton("Bağla")
        self.gemini_bind_button.setObjectName("primary")
        # Varsayılan düğme değil: Enter tuşu bağlamayı tetiklemesin, yalnız açık tıklama.
        self.gemini_bind_button.setAutoDefault(False)
        self.gemini_bind_button.setDefault(False)
        self.gemini_bind_button.clicked.connect(self._bind_gemini_manual)
        key_row.addWidget(self.gemini_bind_button)
        layout.addLayout(key_row)
        reopen = QPushButton("AI Studio sayfasını yeniden aç")
        reopen.setObjectName("ghost")
        reopen.setAutoDefault(False)
        reopen.clicked.connect(self._open_gemini_page)
        layout.addWidget(reopen, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def _build_apikey_page(self) -> QWidget:
        page, layout, _info = _page(
            "API anahtarı, yerel model ya da proxy",
            "Sağlayıcıyı seç, anahtarını ve model kimliğini yaz. Yerel servisler anahtar istemez.",
        )
        self.local_box = QFrame()
        self.local_box.setObjectName("localFound")
        local_layout = QVBoxLayout(self.local_box)
        local_layout.setContentsMargins(12, 10, 12, 10)
        local_title = QLabel("Bu bilgisayarda çalışan servis bulundu:")
        local_title.setObjectName("choiceTitle")
        local_layout.addWidget(local_title)
        self.local_row = QHBoxLayout()
        local_layout.addLayout(self.local_row)
        self.local_box.setVisible(False)
        layout.addWidget(self.local_box)
        self.local_buttons: list[QPushButton] = []

        self.preset_combo = QComboBox()
        self.preset_combo.setAccessibleName("Sağlayıcı")
        for index, (label, slug, _url, _needs_key) in enumerate(PRESETS):
            if not slug or slug == "gemini":
                continue
            self.preset_combo.addItem(label, index)
        self.preset_combo.currentIndexChanged.connect(self._apply_preset_choice)
        layout.addWidget(self.preset_combo)

        key_row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setAccessibleName("API anahtarı")
        self.key_edit.setPlaceholderText("API anahtarı")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.get_key_button = QPushButton("Anahtar al")
        self.get_key_button.setAutoDefault(False)
        self.get_key_button.clicked.connect(self._open_key_page)
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(self.get_key_button)
        layout.addLayout(key_row)

        model_row = QHBoxLayout()
        self.model_edit = QLineEdit()
        self.model_edit.setAccessibleName("Model kimliği")
        self.model_edit.setPlaceholderText("Model kimliği (ör. llama-3.3-70b, qwen2.5:7b)")
        self.model_pick = QComboBox()
        self.model_pick.setAccessibleName("Servisteki modeller")
        self.model_pick.setVisible(False)
        self.model_pick.currentTextChanged.connect(self.model_edit.setText)
        model_row.addWidget(self.model_edit, 1)
        model_row.addWidget(self.model_pick, 1)
        layout.addLayout(model_row)

        self.bind_button = QPushButton("Bağla")
        self.bind_button.setObjectName("primary")
        self.bind_button.setAutoDefault(False)
        self.bind_button.clicked.connect(self._bind_preset_model)
        layout.addWidget(self.bind_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    # --- Yerel servis taraması ------------------------------------------------------------

    def _start_local_probe(self) -> None:
        if self._probed:
            return
        self._probed = True
        thread = _LocalProbeThread(self._local_probe, self)
        self._probe_thread = thread
        thread.finished_with.connect(self._on_local_found)
        thread.start()

    def _on_local_found(self, found: list[Any]) -> None:
        thread, self._probe_thread = self._probe_thread, None
        _retire_thread(thread, self._on_local_found)
        self._found = {base: (name, needs_key, models) for name, base, needs_key, models in found}
        for base_url, (name, needs_key, models) in self._found.items():
            note = "anahtar ister" if needs_key else f"{len(models)} model"
            button = QPushButton(f"{name} · :{urlsplit(base_url).port} · {note}")
            button.setObjectName("chip")
            button.setAutoDefault(False)
            button.setProperty("base_url", base_url)
            button.clicked.connect(self._use_local_from_sender)
            self.local_row.addWidget(button)
            self.local_buttons.append(button)
        self.local_row.addStretch(1)
        self.local_box.setVisible(bool(self._found))

    def _use_local_from_sender(self) -> None:
        sender = self.sender()
        if isinstance(sender, QPushButton):
            self.use_local_service(str(sender.property("base_url")))

    def use_local_service(self, base_url: str) -> None:
        name, needs_key, models = self._found[base_url]
        self._local = (name, base_url, needs_key)
        self.preset_combo.setVisible(False)
        self.key_edit.setVisible(needs_key)
        self.key_edit.clear()
        self.get_key_button.setVisible(False)
        self.model_pick.clear()
        self.model_pick.addItems(models)
        self.model_pick.setVisible(bool(models))
        self.model_edit.setVisible(not models)
        self.status.setText(
            f"{name} ({base_url}) seçildi"
            + ("; proxy'nin API anahtarını yaz." if needs_key else "; bir model seç ve bağla.")
        )

    # --- 1. OpenRouter PKCE ---------------------------------------------------------------

    def _show_openrouter_panel(self) -> None:
        self._stop_clipboard_watch()
        self._select_card(self.openrouter_button)
        self.stack.setCurrentWidget(self._openrouter_page)
        self.status.setText("")

    def _start_openrouter_flow(self) -> None:
        if self._login_thread is not None:
            return
        self._show_openrouter_panel()
        self.openrouter_models.clear()
        self.openrouter_bind_button.setEnabled(False)
        try:
            self._open_url(self._oauth.start())
        except OSError as error:
            self.status.setText(str(error))
            return
        thread = _OpenRouterLoginThread(self._oauth, self)
        self._login_thread = thread
        thread.finished_with.connect(self._on_openrouter_done)
        self.openrouter_login_button.setEnabled(False)
        self.status.setText("Tarayıcıda girişi tamamla; Artemis bekliyor…")
        thread.start()

    def _on_openrouter_done(self, key: str, models: list[str], error: str) -> None:
        thread, self._login_thread = self._login_thread, None
        _retire_thread(thread, self._on_openrouter_done)
        self.openrouter_login_button.setEnabled(True)
        if key:
            failure = self._store_key("openrouter", key)
            if failure is not None:
                self.status.setText(failure)
                return
        if error or not key:
            self.status.setText(error or "OpenRouter girişi tamamlanamadı.")
            return
        if not models:
            self.status.setText("OpenRouter üzerinde uygun ücretsiz model bulunamadı.")
            return
        self.openrouter_models.addItems(models)
        self.openrouter_models.setVisible(True)
        self.openrouter_bind_button.setVisible(True)
        self.openrouter_bind_button.setEnabled(True)
        self.openrouter_login_button.setVisible(False)
        self.status.setText("OpenRouter bağlandı; bir ücretsiz model seç.")

    def _bind_openrouter_model(self) -> None:
        model_id = self.openrouter_models.currentText().strip()
        if not model_id:
            self.status.setText("Önce bir model seçin.")
            return
        if self._write_endpoint_and_model(
            endpoint_id="openrouter",
            base_url=OPENROUTER_BASE_URL,
            api_key=True,
            model_id=model_id,
        ):
            self.accept()

    # --- 2. Google (Gemini) rehberli anahtar ---------------------------------------------

    def _get_clipboard(self) -> Any:
        if self._clipboard is not None:
            return self._clipboard
        return QGuiApplication.clipboard()

    def _start_clipboard_watch(self) -> None:
        if self._watching_clipboard:
            return
        clip = self._get_clipboard()
        if clip is not None:
            clip.dataChanged.connect(self._on_clipboard_changed)
            self._watching_clipboard = True

    def _stop_clipboard_watch(self) -> None:
        if not self._watching_clipboard:
            return
        clip = self._get_clipboard()
        self._watching_clipboard = False
        if clip is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                clip.dataChanged.disconnect(self._on_clipboard_changed)

    def _on_dialog_finished(self, _result: int) -> None:
        self._stop_clipboard_watch()
        self._oauth.close()  # bekleyen tarayıcı girişi hemen biter
        for thread, slot in (
            (self._login_thread, self._on_openrouter_done),
            (self._probe_thread, self._on_local_found),
            (self._gemini_thread, self._on_gemini_validated),
        ):
            shutdown_thread(thread, ("finished_with", slot), wait_ms=500)
        self._login_thread = self._probe_thread = self._gemini_thread = None

    def closeEvent(self, event: Any) -> None:
        self._stop_clipboard_watch()
        super().closeEvent(event)

    def _show_gemini_panel(self) -> None:
        self._select_card(self.gemini_button)
        self.stack.setCurrentWidget(self._gemini_page)
        self.status.setText("")
        self._open_gemini_page()

    def _open_gemini_page(self) -> None:
        self._open_url(GEMINI_KEY_URL)
        self._start_clipboard_watch()

    def _on_clipboard_changed(self) -> None:
        if not self._watching_clipboard or self.stack.currentWidget() is not self._gemini_page:
            return
        clip = self._get_clipboard()
        if clip is None:
            return
        candidate = (clip.text() or "").strip()
        if not GEMINI_KEY_RE.fullmatch(candidate):
            return
        # Pano yetki değildir: başka bir yerel süreç de panoya anahtar koyabilir. Algılama yalnız
        # alanı doldurur; doğrulama, kayıt ve pano temizliği kullanıcının "Bağla"sıyla olur.
        if self.gemini_key_edit.text():
            return  # kullanıcının yazdığının üzerine yazılmaz
        # İlk algılamada izleme biter: ikinci bir pano yazımı alanı değiştiremez. Odak taşınmaz
        # (yanlışlıkla Enter onay sayılmasın); anahtar panoda beklemesin diye hemen temizlenir.
        self._stop_clipboard_watch()
        self.gemini_key_edit.setText(candidate)
        if clip.text().strip() == candidate:
            clip.clear()
        self.status.setText(
            f"Panoda bir anahtar algılandı (…{candidate[-4:]}); bağlamak için 'Bağla'ya bas."
        )

    def _bind_gemini_manual(self) -> None:
        candidate = self.gemini_key_edit.text().strip()
        self.gemini_key_edit.clear()
        if not GEMINI_KEY_RE.fullmatch(candidate):
            self.status.setText("Anahtar geçersiz görünüyor.")
            return
        self._validate_and_bind_gemini(candidate)

    def _validate_and_bind_gemini(self, key: str) -> None:
        if self._gemini_thread is not None and self._gemini_thread.isRunning():
            return
        thread = _GeminiValidateThread(key, self._gemini_validator, self)
        self._gemini_thread = thread
        thread.finished_with.connect(self._on_gemini_validated)
        # Bekleme yok: doğrulama arka planda, sonuç sinyalle gelir (arayüz donmaz).
        self.status.setText("Anahtar doğrulanıyor…")
        thread.start()

    def _on_gemini_validated(self, key: str, status_code: int) -> None:
        thread, self._gemini_thread = self._gemini_thread, None
        _retire_thread(thread, self._on_gemini_validated)
        if status_code == 200:
            self._stop_clipboard_watch()
            failure = self._store_key("gemini", key)
            if failure is not None:
                self.status.setText(failure)
                return
            if not self._write_endpoint_and_model(
                endpoint_id="gemini",
                base_url=GEMINI_OPENAI_BASE_URL,
                api_key=True,
                model_id=GEMINI_MODEL_ID,
                alias=GEMINI_MODEL_ALIAS,
            ):
                return
            clip = self._get_clipboard()
            if clip is not None and clip.text() == key:
                clip.clear()
            self.status.setText("Gemini bağlandı.")
            self.accept()
            return
        if status_code in {400, 401, 403}:
            self.status.setText("Anahtar geçersiz görünüyor.")
            return
        self.status.setText(f"HTTP {status_code}")

    # --- 3. Abonelik (Claude / ChatGPT CLI) ----------------------------------------------

    def _show_subscription_panel(self) -> None:
        self._stop_clipboard_watch()
        self._select_card(self.subscription_button)
        self.stack.setCurrentWidget(self._subscription_page)
        self._select_claude_cli()

    def _select_claude_cli(self) -> None:
        self.stack.setCurrentWidget(self._subscription_page)
        self.codex_add_button.setVisible(False)
        found = self._which("claude")
        if not found:
            self.claude_add_button.setVisible(False)
            self.cli_hint.setText(
                f"Claude Code CLI bulunamadı. Kurulum için resmi sayfa: {CLAUDE_DOC_URL}"
            )
            self.status.setText(self.cli_hint.text())
            return
        self.claude_add_button.setVisible(True)
        self.cli_hint.setText(
            "Giriş için terminalde `claude` çalıştırın, sonra `/login` yazın. "
            "Artemis bu kimlik bilgilerine dokunmaz."
        )
        self.status.setText("")

    def _select_chatgpt_cli(self) -> None:
        self.stack.setCurrentWidget(self._subscription_page)
        self.claude_add_button.setVisible(False)
        self.codex_add_button.setVisible(True)
        self.codex_add_button.setEnabled(False)
        found = self._which("codex")
        if not found:
            self.cli_hint.setText(
                f"Codex CLI bulunamadı ({CODEX_DOC_URL}). Giriş komutu: `codex login` — yakında."
            )
        else:
            self.cli_hint.setText(
                "Giriş için terminalde `codex login` çalıştırın. ChatGPT abonelik desteği yakında."
            )
        self.status.setText("yakında")

    def _add_claude_cli_model(self) -> None:
        if not self._which("claude"):
            self._select_claude_cli()
            return
        try:
            raw = _load_raw(self.config_path)
            models: dict[str, Any] = raw.setdefault("models", {})
            models["claude"] = {
                "provider": "claude_cli",
                "model": "sonnet",
                "input_per_million": "0",
                "output_per_million": "0",
            }
            raw["primary_model"] = "claude"
            _write_validated(self.config_path, raw)
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            self.status.setText("Ayarlar kaydedilemedi; mevcut ayarlar korunuyor.")
            return
        self.accept()

    # --- 4. API anahtarı ya da yerel model ------------------------------------------------

    def _show_apikey_panel(self) -> None:
        self._stop_clipboard_watch()
        self._select_card(self.apikey_button)
        self.stack.setCurrentWidget(self._apikey_page)
        self.status.setText("")
        self._apply_preset_choice()
        self._start_local_probe()

    def _current_preset(self) -> tuple[str, str, str, bool]:
        index = self.preset_combo.currentData()
        if isinstance(index, int) and 0 <= index < len(PRESETS):
            return PRESETS[index]
        return PRESETS[1]

    def _apply_preset_choice(self, *_args: object) -> None:
        self._local = None
        self.preset_combo.setVisible(True)
        self.model_pick.setVisible(False)
        self.model_edit.setVisible(True)
        _label, slug, _url, key_required = self._current_preset()
        self.key_edit.setVisible(key_required)
        self.key_edit.clear()
        has_key_page = key_required and slug in KEY_PAGES
        self.get_key_button.setVisible(has_key_page)

    def _open_key_page(self) -> None:
        _label, slug, _url, _needs_key = self._current_preset()
        page = KEY_PAGES.get(slug)
        if page:
            self._open_url(page)

    def _bind_preset_model(self) -> None:
        _label, endpoint_id, base_url, key_required = self._current_preset()
        if self._local is not None:
            _name, base_url, key_required = self._local
            endpoint_id = f"yerel-{urlsplit(base_url).port}"
        model_id = self.model_edit.text().strip()
        if not model_id:
            self.status.setText("Model kimliği yazın.")
            return
        if key_required:
            typed_key = self.key_edit.text().strip()
            if not typed_key:
                self.status.setText("API anahtarı gerekli.")
                return
            failure = self._store_key(endpoint_id, typed_key)
            self.key_edit.clear()
            if failure is not None:
                self.status.setText(failure)
                return
        if self._write_endpoint_and_model(
            endpoint_id=endpoint_id,
            base_url=base_url,
            api_key=key_required,
            model_id=model_id,
        ):
            self.accept()

    def _write_endpoint_and_model(
        self,
        *,
        endpoint_id: str,
        base_url: str,
        api_key: bool,
        model_id: str,
        alias: str | None = None,
    ) -> bool:
        alias = alias or _slug(model_id) or endpoint_id
        try:
            raw = _load_raw(self.config_path)
            endpoints: dict[str, Any] = raw.setdefault("endpoints", {})
            endpoints[endpoint_id] = {"base_url": base_url, "api_key": api_key}
            models: dict[str, Any] = raw.setdefault("models", {})
            models[alias] = {
                "provider": "openai_compatible",
                "model": model_id,
                "input_per_million": "0",
                "output_per_million": "0",
                "endpoint": endpoint_id,
            }
            raw["primary_model"] = alias
            _write_validated(self.config_path, raw)
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            self.status.setText("Ayarlar kaydedilemedi; mevcut ayarlar korunuyor.")
            return False
        return True


def maybe_connect(config_path: Path, parent: QWidget | None = None) -> None:
    """Ayarlar geçerliyse ve beyin modeli eksikse bağlama sihirbazını açar."""
    try:
        settings = load_settings(config_path)
    except (OSError, ValueError, ValidationError, yaml.YAMLError):
        return
    if needs_wizard(settings):
        wizard = ConnectWizard(config_path, parent=parent)
        wizard.exec()
