"""İlk çalıştırma 'Beyni bağla' sihirbazı testleri; gerçek ağ, kasa veya tarayıcı kullanılmaz."""

from __future__ import annotations

import base64
import hashlib
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal

from artemis.channels.desktop.connect_wizard import (
    CLAUDE_DOC_URL,
    GEMINI_KEY_URL,
    GEMINI_MODEL_ALIAS,
    GEMINI_MODEL_ID,
    GEMINI_OPENAI_BASE_URL,
    ConnectWizard,
    OpenRouterPkce,
    free_tool_models,
    needs_wizard,
    pkce_challenge,
)
from artemis.config.settings import EndpointConfig, ModelConfig, Settings, load_settings


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _write_base_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "workspace": "desktop",
                "state_dir": "state",
                "audit_path": "audit.jsonl",
            }
        ),
        encoding="utf-8",
    )
    return path


class _UnexpectedNetworkHttp:
    """Beklenmeyen her ağ çağrısında hata fırlatan sahte HTTP istemcisi."""

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Beklenmeyen HTTP GET çağrısı")

    def post(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Beklenmeyen HTTP POST çağrısı")


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeClipboard(QObject):
    dataChanged = Signal()

    def __init__(self, initial: str = "") -> None:
        super().__init__()
        self._text = initial
        self.cleared = 0

    def text(self) -> str:
        return self._text

    def set_text(self, value: str) -> None:
        self._text = value
        self.dataChanged.emit()

    def clear(self) -> None:
        self._text = ""
        self.cleared += 1



def _settle(app, wizard, timeout: float = 3.0) -> None:
    """Gemini doğrulaması arka plan iş parçacığında: sonuç sinyali gelene dek olayları işlet."""
    import time

    end = time.monotonic() + timeout
    app.processEvents()
    while getattr(wizard, "_gemini_thread", None) is not None and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()

def test_four_main_buttons_have_equal_size_and_expected_labels(
    tmp_path: Path, qt_app
) -> None:
    path = _write_base_config(tmp_path)
    wizard = ConnectWizard(
        path,
        open_url=lambda _url: None,
        oauth=OpenRouterPkce(http=_UnexpectedNetworkHttp()),
        run_cli=lambda _name: None,
        store_key=lambda _ep, _key: None,
        clipboard=_FakeClipboard(),
        gemini_validator=lambda _key: 500,
    )
    buttons = wizard.choice_buttons
    assert len(buttons) == 4
    widths = {b.minimumWidth() for b in buttons}
    heights = {b.minimumHeight() for b in buttons}
    policies = {
        (b.sizePolicy().horizontalPolicy(), b.sizePolicy().verticalPolicy()) for b in buttons
    }
    assert len(widths) == 1 and next(iter(widths)) > 0
    assert len(heights) == 1 and next(iter(heights)) > 0
    assert len(policies) == 1

    texts = [b.text() for b in buttons]
    assert texts == [
        "Ücretsiz hesapla giriş\nOpenRouter · anahtar görmeden",
        "Google hesabımla\nGemini · ücretsiz kota",
        "Aboneliğimle bağlan\nClaude",
        "API anahtarı ya da yerel model\nOllama · LM Studio · Groq…",
    ]
    for button in buttons:
        assert len(button.text().splitlines()) == 2
    wizard.deleteLater()


def test_pkce_challenge_matches_rfc7636_appendix_b() -> None:
    verifier = "dBjftJeZ4CVP-mGBf4qIiQ5rW-XJ53qZBmHEpCPw-9I"
    expected_from_hashlib = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert pkce_challenge(verifier) == expected_from_hashlib

    rfc_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(rfc_verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    pkce = OpenRouterPkce(http=_UnexpectedNetworkHttp())
    expected_instance = (
        base64.urlsafe_b64encode(hashlib.sha256(pkce.code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert pkce.code_challenge == expected_instance


def test_callback_server_receives_code_on_loopback() -> None:
    pkce = OpenRouterPkce(http=_UnexpectedNetworkHttp())
    auth_url = pkce.start()
    parsed = urlsplit(auth_url)
    query = parse_qs(parsed.query)
    callback_url = query["callback_url"][0]
    assert callback_url.startswith("http://127.0.0.1:")
    assert urlsplit(callback_url).path == "/callback"
    assert parse_qs(urlsplit(callback_url).query)["state"] == [pkce.state]
    assert query["code_challenge"] == [pkce.code_challenge]
    assert query["code_challenge_method"] == ["S256"]

    with urllib.request.urlopen(f"{callback_url}&code=abc", timeout=5) as response:
        html_body = response.read().decode("utf-8")
    assert "Artemis'e dönebilirsin" in html_body
    assert pkce.wait_code(timeout=5) == "abc"


def test_exchange_and_wizard_openrouter_flow_stores_key_without_leaking(
    tmp_path: Path,
    qt_app,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_key = "sk-test"
    post_calls: list[tuple[str, dict[str, Any]]] = []
    stored: list[tuple[str, str]] = []
    opened_urls: list[str] = []

    class FakeHttp:
        def post(
            self, url: str, *, json: dict[str, Any], timeout: float, follow_redirects: bool
        ) -> _FakeResponse:
            assert follow_redirects is False
            assert timeout == 20
            post_calls.append((url, json))
            return _FakeResponse(200, {"key": secret_key})

        def get(self, url: str, *, timeout: float, follow_redirects: bool) -> _FakeResponse:
            assert follow_redirects is False
            return _FakeResponse(
                200,
                {
                    "data": [
                        {
                            "id": "meta-llama/llama-3.3-70b:free",
                            "supported_parameters": ["tools", "temperature"],
                        }
                    ]
                },
            )

    pkce = OpenRouterPkce(http=FakeHttp())

    def open_and_hit_callback(auth_url: str) -> None:
        opened_urls.append(auth_url)
        cb = parse_qs(urlsplit(auth_url).query)["callback_url"][0]
        sep = "&" if "?" in cb else "?"
        with urllib.request.urlopen(f"{cb}{sep}code=test-code", timeout=5) as resp:
            resp.read()

    path = _write_base_config(tmp_path)
    wizard = ConnectWizard(
        path,
        open_url=open_and_hit_callback,
        oauth=pkce,
        run_cli=lambda _name: None,
        store_key=lambda ep, k: (stored.append((ep, k)) or None),
    )
    wizard.openrouter_button.click()
    assert stored == [("openrouter", secret_key)]
    assert len(post_calls) == 1
    assert post_calls[0][1] == {
        "code": "test-code",
        "code_verifier": pkce.code_verifier,
        "code_challenge_method": "S256",
    }
    wizard.openrouter_bind_button.click()
    settings = load_settings(path)
    assert settings.endpoints["openrouter"].base_url == "https://openrouter.ai/api/v1"
    assert settings.endpoints["openrouter"].api_key is True
    assert settings.models[settings.primary_model].endpoint == "openrouter"

    captured = capsys.readouterr()
    assert secret_key not in captured.out
    assert secret_key not in captured.err
    assert secret_key not in caplog.text
    assert secret_key not in path.read_text(encoding="utf-8")
    wizard.deleteLater()


def test_api_key_preset_writes_config_and_stores_key(tmp_path: Path, qt_app) -> None:
    path = _write_base_config(tmp_path)
    stored: list[tuple[str, str]] = []
    opened: list[str] = []

    wizard = ConnectWizard(
        path,
        open_url=opened.append,
        oauth=OpenRouterPkce(http=_UnexpectedNetworkHttp()),
        run_cli=lambda _name: None,
        store_key=lambda ep, k: (stored.append((ep, k)) or None),
        clipboard=_FakeClipboard(),
    )
    wizard.apikey_button.click()
    # Gemini şablonu artık bu listede olmamalı (kendi düğmesi var)
    preset_texts = [wizard.preset_combo.itemText(i) for i in range(wizard.preset_combo.count())]
    assert all("Gemini" not in text and "Google AI Studio" not in text for text in preset_texts)

    groq_index = next(
        i for i in range(wizard.preset_combo.count()) if "Groq" in wizard.preset_combo.itemText(i)
    )
    wizard.preset_combo.setCurrentIndex(groq_index)
    assert not wizard.key_edit.isHidden()
    wizard.get_key_button.click()
    assert opened == ["https://console.groq.com/keys"]

    wizard.key_edit.setText("groq-secret-key")
    wizard.model_edit.setText("llama-3.3-70b-versatile")
    wizard.bind_button.click()

    assert stored == [("groq", "groq-secret-key")]
    settings = load_settings(path)
    assert settings.primary_model == "llama-3-3-70b-versatile"
    assert settings.endpoints["groq"].api_key is True
    model = settings.models[settings.primary_model]
    assert model.provider == "openai_compatible"
    assert model.endpoint == "groq"
    assert model.model == "llama-3.3-70b-versatile"
    assert "groq-secret-key" not in path.read_text(encoding="utf-8")
    wizard.deleteLater()


def test_gemini_guided_flow_clipboard_detection_and_disconnect(
    tmp_path: Path, qt_app
) -> None:
    fake_key = "AIza" + "A" * 35  # 39 karakter sahte anahtar
    path = _write_base_config(tmp_path)
    stored: list[tuple[str, str]] = []
    opened: list[str] = []
    validated: list[str] = []
    clipboard = _FakeClipboard()

    wizard = ConnectWizard(
        path,
        open_url=opened.append,
        oauth=OpenRouterPkce(http=_UnexpectedNetworkHttp()),
        run_cli=lambda _name: None,
        store_key=lambda ep, k: (stored.append((ep, k)) or None),
        clipboard=clipboard,
        gemini_validator=lambda k: (validated.append(k) or 200),
    )
    wizard.gemini_button.click()
    assert opened == [GEMINI_KEY_URL]

    # 1. Anahtar biçimine uymayan pano metni yok sayılır
    clipboard.set_text("merhaba dünya normal metin")
    _settle(qt_app, wizard)
    assert validated == []
    assert stored == []

    # 2. Adımdan çıkılınca pano izleyicisi ayrılır
    wizard.apikey_button.click()
    clipboard.set_text(fake_key)
    _settle(qt_app, wizard)
    assert validated == []
    assert stored == []

    # 3. Gemini adımına dönülür; geçerli anahtar kopyalanınca doğrulanır, saklanır, pano temizlenir
    clipboard.set_text("")
    wizard.gemini_button.click()
    clipboard.set_text(f"  {fake_key}\n")
    _settle(qt_app, wizard)

    # Pano yetki değildir: algılama yalnız alanı doldurur, hiçbir şey doğrulanmaz/saklanmaz.
    assert validated == []
    assert stored == []
    assert wizard.gemini_key_edit.text() == fake_key
    assert "Bağla" in wizard.status.text()
    assert fake_key[-4:] in wizard.status.text() and fake_key not in wizard.status.text()
    assert clipboard.cleared == 1  # anahtar alana alınır alınmaz panodan silinir

    wizard.gemini_bind_button.click()  # kullanıcının açık eylemi
    _settle(qt_app, wizard)

    assert validated == [fake_key]
    assert stored == [("gemini", fake_key)]
    assert clipboard.text() == ""
    assert clipboard.cleared == 1  # bağlamada ikinci kez silinmez (pano zaten boş)

    settings = load_settings(path)
    assert settings.primary_model == GEMINI_MODEL_ALIAS
    assert settings.endpoints["gemini"].base_url == GEMINI_OPENAI_BASE_URL
    assert settings.endpoints["gemini"].api_key is True
    model = settings.models[GEMINI_MODEL_ALIAS]
    assert model.provider == "openai_compatible"
    assert model.endpoint == "gemini"
    assert model.model == GEMINI_MODEL_ID
    assert fake_key not in path.read_text(encoding="utf-8")

    # Başarıdan sonra pano tekrar değişirse doğrulayıcı bir daha çağrılmaz
    clipboard.set_text(fake_key)
    _settle(qt_app, wizard)
    assert len(validated) == 1
    wizard.deleteLater()


def test_gemini_invalid_key_and_manual_paste(tmp_path: Path, qt_app) -> None:
    fake_key = "AIza" + "B" * 35
    path = _write_base_config(tmp_path)
    stored: list[tuple[str, str]] = []
    validated: list[str] = []
    clipboard = _FakeClipboard("başka bir metin")
    status_to_return = 401

    wizard = ConnectWizard(
        path,
        open_url=lambda _url: None,
        oauth=OpenRouterPkce(http=_UnexpectedNetworkHttp()),
        run_cli=lambda _name: None,
        store_key=lambda ep, k: (stored.append((ep, k)) or None),
        clipboard=clipboard,
        gemini_validator=lambda k: (validated.append(k) or status_to_return),
    )
    wizard.gemini_button.click()

    # 401 dönen doğrulama: hata mesajı gösterilir, anahtar saklanmaz, mesajda anahtar geçmez
    clipboard.set_text(fake_key)
    _settle(qt_app, wizard)
    assert validated == []  # pano yalnız alanı doldurur
    wizard.gemini_bind_button.click()
    _settle(qt_app, wizard)
    assert validated == [fake_key]
    assert stored == []
    assert wizard.status.text() == "Anahtar geçersiz görünüyor."
    assert fake_key not in wizard.status.text()

    # Elle yapıştırma yolu (pano başka bir metin tutarken panoyu silmez)
    status_to_return = 200
    clipboard._text = "kullanıcının başka panosu"
    wizard.gemini_key_edit.setText(fake_key)
    wizard.gemini_bind_button.click()
    _settle(qt_app, wizard)

    assert len(validated) == 2
    assert stored == [("gemini", fake_key)]
    assert clipboard.text() == "kullanıcının başka panosu"
    assert clipboard.cleared == 1  # yalnız algılamadaki silme; elle yapıştırma dokunmaz
    settings = load_settings(path)
    assert settings.primary_model == GEMINI_MODEL_ALIAS
    wizard.deleteLater()


def test_keyless_ollama_preset_never_calls_store_and_writes_valid_config(
    tmp_path: Path, qt_app
) -> None:
    path = _write_base_config(tmp_path)
    stored: list[tuple[str, str]] = []

    wizard = ConnectWizard(
        path,
        open_url=lambda _url: None,
        oauth=OpenRouterPkce(http=_UnexpectedNetworkHttp()),
        run_cli=lambda _name: None,
        store_key=lambda ep, k: (stored.append((ep, k)) or None),
    )
    wizard.apikey_button.click()
    ollama_index = next(
        i for i in range(wizard.preset_combo.count()) if "Ollama" in wizard.preset_combo.itemText(i)
    )
    wizard.preset_combo.setCurrentIndex(ollama_index)
    assert wizard.key_edit.isHidden()

    wizard.model_edit.setText("qwen2.5:7b")
    wizard.bind_button.click()

    assert stored == []
    settings = load_settings(path)
    assert settings.endpoints["ollama"].api_key is False
    assert settings.endpoints["ollama"].base_url == "http://127.0.0.1:11434/v1"
    assert settings.primary_model == "qwen2-5-7b"
    assert settings.models["qwen2-5-7b"].endpoint == "ollama"
    wizard.deleteLater()


def test_needs_wizard_conditions() -> None:
    empty = Settings()
    assert needs_wizard(empty, key_lookup=lambda _ep: "var") is True

    with_ep = Settings(
        endpoints={"groq": EndpointConfig(base_url="https://api.groq.com/openai/v1", api_key=True)},
        models={
            "m": ModelConfig(
                provider="openai_compatible",
                model="llama",
                input_per_million=0,
                output_per_million=0,
                endpoint="groq",
            )
        },
        primary_model="m",
    )
    assert needs_wizard(with_ep, key_lookup=lambda ep: "k" if ep == "groq" else None) is False
    assert needs_wizard(with_ep, key_lookup=lambda _ep: None) is True

    cli_settings = Settings(
        models={
            "claude": ModelConfig(
                provider="claude_cli",
                model="sonnet",
                input_per_million=0,
                output_per_million=0,
            )
        },
        primary_model="claude",
    )
    assert needs_wizard(cli_settings, key_lookup=lambda _ep: None) is False

    keyless = Settings(
        endpoints={"ollama": EndpointConfig(base_url="http://127.0.0.1:11434/v1", api_key=False)},
        models={
            "yerel": ModelConfig(
                provider="openai_compatible",
                model="llama3",
                input_per_million=0,
                output_per_million=0,
                endpoint="ollama",
            )
        },
        primary_model="yerel",
    )
    assert needs_wizard(keyless, key_lookup=lambda _ep: None) is False


def test_free_tool_models_filters_correctly() -> None:
    catalog = {
        "data": [
            {
                "id": "qwen/qwen-2.5-72b-instruct:free",
                "supported_parameters": ["temperature", "tools", "top_p"],
            },
            {
                "id": "openai/gpt-4o",
                "supported_parameters": ["tools", "temperature"],
            },
            {
                "id": "google/gemma-2-9b:free",
                "supported_parameters": ["temperature"],
            },
            {
                "id": "mistral/pixtral:free",
            },
        ]
    }
    assert free_tool_models(catalog) == ["qwen/qwen-2.5-72b-instruct:free"]


def test_callback_server_rejects_missing_or_wrong_state_and_accepts_correct_state() -> None:
    pkce = OpenRouterPkce(http=_UnexpectedNetworkHttp())
    auth_url = pkce.start()
    assert pkce.state is not None
    assert pkce._server is not None
    port = pkce._server.server_port

    # Auth URL encode edilmiş state içeren geri dönüş URL'sini taşır
    expected_cb = f"http://127.0.0.1:{port}/callback?state={pkce.state}"
    assert f"callback_url={quote(expected_cb, safe='')}" in auth_url

    # 1. State eksik: 400 döner, kod saklanmaz, sunucu beklemeyi sürdürür
    req_no_state = urllib.request.Request(f"http://127.0.0.1:{port}/callback?code=bad-code-1")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req_no_state, timeout=5)
    assert exc_info.value.code == 400
    assert pkce._server.code is None

    # 2. Yanlış state: 400 döner, kod saklanmaz
    req_wrong_state = urllib.request.Request(
        f"http://127.0.0.1:{port}/callback?state=wrong-state&code=bad-code-2"
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req_wrong_state, timeout=5)
    assert exc_info.value.code == 400
    assert pkce._server.code is None

    # 3. Origin başlığı olan istek (XHR/CORS): 400 döner
    req_origin = urllib.request.Request(
        f"http://127.0.0.1:{port}/callback?state={pkce.state}&code=bad-code-3",
        headers={"Origin": "http://evil.local"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req_origin, timeout=5)
    assert exc_info.value.code == 400
    assert pkce._server.code is None

    # 4. Sec-Fetch-Mode != navigate: 400 döner
    req_cors = urllib.request.Request(
        f"http://127.0.0.1:{port}/callback?state={pkce.state}&code=bad-code-4",
        headers={"Sec-Fetch-Mode": "cors"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req_cors, timeout=5)
    assert exc_info.value.code == 400
    assert pkce._server.code is None

    # 5. Doğru state ve navigate modu: 200 döner, kod saklanır
    req_ok = urllib.request.Request(
        f"http://127.0.0.1:{port}/callback?state={pkce.state}&code=valid-code",
        headers={"Sec-Fetch-Mode": "navigate"},
    )
    with urllib.request.urlopen(req_ok, timeout=5) as resp:
        assert resp.status == 200
        assert "Artemis'e dönebilirsin" in resp.read().decode("utf-8")
    assert pkce.wait_code(timeout=5) == "valid-code"


def test_claude_missing_shows_hint_and_leaves_config_unchanged(
    tmp_path: Path, qt_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    path = _write_base_config(tmp_path)
    before = path.read_bytes()

    wizard = ConnectWizard(
        path,
        open_url=lambda _url: None,
        oauth=OpenRouterPkce(http=_UnexpectedNetworkHttp()),
        store_key=lambda _ep, _k: None,
    )
    wizard.subscription_button.click()
    assert CLAUDE_DOC_URL in wizard.cli_hint.text()
    assert wizard.claude_add_button.isHidden()
    wizard._add_claude_cli_model()
    assert path.read_bytes() == before
    wizard.deleteLater()
