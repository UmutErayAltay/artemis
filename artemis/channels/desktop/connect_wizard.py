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
from artemis.channels.desktop.provider_layout import PRESETS
from artemis.channels.desktop.provider_section import _slug, store_endpoint_key
from artemis.channels.desktop.settings_io import _write_validated
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

_CHOICE_BUTTON_WIDTH = 220
_CHOICE_BUTTON_HEIGHT = 64


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


def _load_raw(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


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
        self._watching_clipboard = False
        self._gemini_thread: _GeminiValidateThread | None = None

        self.setWindowTitle("Artemis'i bir beyne bağla")
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        intro = QLabel("Başlamak için bir bağlantı yöntemi seçin:")
        intro.setWordWrap(True)
        root.addWidget(intro)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.openrouter_button = QPushButton(
            "Ücretsiz hesapla giriş\nOpenRouter · anahtar görmeden"
        )
        self.gemini_button = QPushButton(
            "Google hesabımla\nGemini · ücretsiz kota"
        )
        self.subscription_button = QPushButton(
            "Aboneliğimle bağlan\nClaude"
        )
        self.apikey_button = QPushButton(
            "API anahtarı ya da yerel model\nOllama · LM Studio · Groq…"
        )
        self.choice_buttons = (
            self.openrouter_button,
            self.gemini_button,
            self.subscription_button,
            self.apikey_button,
        )
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for button in self.choice_buttons:
            button.setMinimumWidth(_CHOICE_BUTTON_WIDTH)
            button.setMinimumHeight(_CHOICE_BUTTON_HEIGHT)
            button.setSizePolicy(policy)
            row.addWidget(button)
        root.addLayout(row)

        self.stack = QStackedWidget()
        self._openrouter_page = self._build_openrouter_page()
        self._gemini_page = self._build_gemini_page()
        self._subscription_page = self._build_subscription_page()
        self._apikey_page = self._build_apikey_page()
        self.stack.addWidget(self._openrouter_page)
        self.stack.addWidget(self._gemini_page)
        self.stack.addWidget(self._subscription_page)
        self.stack.addWidget(self._apikey_page)
        root.addWidget(self.stack)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.status)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.later_button = QPushButton("Sonra")
        self.later_button.clicked.connect(self.reject)
        bottom.addWidget(self.later_button)
        root.addLayout(bottom)

        self.openrouter_button.clicked.connect(self._start_openrouter_flow)
        self.gemini_button.clicked.connect(self._show_gemini_panel)
        self.subscription_button.clicked.connect(self._show_subscription_panel)
        self.apikey_button.clicked.connect(self._show_apikey_panel)
        self.finished.connect(self._on_dialog_finished)

        self._apply_preset_choice()

    def _build_openrouter_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)
        self.openrouter_info = QLabel(
            "Tarayıcıda OpenRouter ile giriş yapın; ardından ücretsiz bir model seçip bağlayın."
        )
        self.openrouter_info.setWordWrap(True)
        layout.addWidget(self.openrouter_info)

        self.openrouter_models = QComboBox()
        self.openrouter_models.setAccessibleName("OpenRouter ücretsiz modelleri")
        layout.addWidget(self.openrouter_models)

        self.openrouter_bind_button = QPushButton("Seçili modeli bağla")
        self.openrouter_bind_button.setEnabled(False)
        self.openrouter_bind_button.clicked.connect(self._bind_openrouter_model)
        layout.addWidget(self.openrouter_bind_button)
        return page

    def _build_subscription_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

        choice_row = QHBoxLayout()
        self.claude_choice_button = QPushButton("Claude")
        self.chatgpt_choice_button = QPushButton("ChatGPT")
        self.claude_choice_button.clicked.connect(self._select_claude_cli)
        self.chatgpt_choice_button.clicked.connect(self._select_chatgpt_cli)
        choice_row.addWidget(self.claude_choice_button)
        choice_row.addWidget(self.chatgpt_choice_button)
        choice_row.addStretch(1)
        layout.addLayout(choice_row)

        self.cli_hint = QLabel()
        self.cli_hint.setWordWrap(True)
        self.cli_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.cli_hint)

        self.claude_add_button = QPushButton("Claude modelini ekle")
        self.claude_add_button.setVisible(False)
        self.claude_add_button.clicked.connect(self._add_claude_cli_model)
        layout.addWidget(self.claude_add_button)

        self.codex_add_button = QPushButton("ChatGPT modelini ekle (yakında)")
        self.codex_add_button.setEnabled(False)
        self.codex_add_button.setVisible(False)
        layout.addWidget(self.codex_add_button)
        return page

    def _build_gemini_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

        self.gemini_steps = QLabel(
            "1) Açılan sayfada Google hesabınla gir. "
            "2) 'Create API key' / 'Anahtar oluştur' de. "
            "3) Anahtarı kopyala — Artemis kendisi algılar."
        )
        self.gemini_steps.setWordWrap(True)
        layout.addWidget(self.gemini_steps)

        self.gemini_key_edit = QLineEdit()
        self.gemini_key_edit.setAccessibleName("Gemini API anahtarı")
        self.gemini_key_edit.setPlaceholderText("Anahtarı buraya da yapıştırabilirsiniz")
        self.gemini_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.gemini_key_edit)

        self.gemini_bind_button = QPushButton("Bağla")
        self.gemini_bind_button.clicked.connect(self._bind_gemini_manual)
        layout.addWidget(self.gemini_bind_button)
        return page

    def _build_apikey_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

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
        self.get_key_button.clicked.connect(self._open_key_page)
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(self.get_key_button)
        layout.addLayout(key_row)

        self.model_edit = QLineEdit()
        self.model_edit.setAccessibleName("Model kimliği")
        self.model_edit.setPlaceholderText("Model kimliği (ör. gemini-2.5-flash, llama3.2)")
        layout.addWidget(self.model_edit)

        self.bind_button = QPushButton("Bağla")
        self.bind_button.clicked.connect(self._bind_preset_model)
        layout.addWidget(self.bind_button)
        return page

    # --- 1. OpenRouter PKCE ---------------------------------------------------------------

    def _start_openrouter_flow(self) -> None:
        self._stop_clipboard_watch()
        self.stack.setCurrentWidget(self._openrouter_page)
        self.openrouter_models.clear()
        self.openrouter_bind_button.setEnabled(False)
        try:
            auth_url = self._oauth.start()
            self._open_url(auth_url)
            code = self._oauth.wait_code()
            key = self._oauth.exchange(code)
        except (OSError, TimeoutError, ValueError) as error:
            self.status.setText(str(error))
            return
        failure = self._store_key("openrouter", key)
        if failure is not None:
            self.status.setText(failure)
            return
        try:
            models = self._oauth.fetch_free_models()
        except ValueError as error:
            self.status.setText(str(error))
            return
        if not models:
            self.status.setText("OpenRouter üzerinde uygun ücretsiz model bulunamadı.")
            return
        self.openrouter_models.addItems(models)
        self.openrouter_bind_button.setEnabled(True)
        self.status.setText("OpenRouter bağlandı; bir ücretsiz model seçin.")

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

    def closeEvent(self, event: Any) -> None:
        self._stop_clipboard_watch()
        super().closeEvent(event)

    def _show_gemini_panel(self) -> None:
        self.stack.setCurrentWidget(self._gemini_page)
        self.status.setText("")
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
        self._validate_and_bind_gemini(candidate)

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
        if thread is not None:
            thread.deleteLater()
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
        self.stack.setCurrentWidget(self._apikey_page)
        self._apply_preset_choice()

    def _current_preset(self) -> tuple[str, str, str, bool]:
        index = self.preset_combo.currentData()
        if isinstance(index, int) and 0 <= index < len(PRESETS):
            return PRESETS[index]
        return PRESETS[1]

    def _apply_preset_choice(self, *_args: object) -> None:
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
