"""İlk çalıştırma 'Beyni bağla' sihirbazı testleri; gerçek ağ, kasa veya tarayıcı kullanılmaz."""

from __future__ import annotations

import base64
import hashlib
import os
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from artemis.channels.desktop.connect_wizard import (
    CLAUDE_DOC_URL,
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


def test_three_main_buttons_have_equal_size_and_expected_labels(
    tmp_path: Path, qt_app
) -> None:
    path = _write_base_config(tmp_path)
    wizard = ConnectWizard(
        path,
        open_url=lambda _url: None,
        oauth=OpenRouterPkce(http=_UnexpectedNetworkHttp()),
        run_cli=lambda _name: None,
        store_key=lambda _ep, _key: None,
    )
    buttons = wizard.choice_buttons
    assert len(buttons) == 3
    widths = {b.minimumWidth() for b in buttons}
    heights = {b.minimumHeight() for b in buttons}
    policies = {
        (b.sizePolicy().horizontalPolicy(), b.sizePolicy().verticalPolicy()) for b in buttons
    }
    assert len(widths) == 1 and next(iter(widths)) > 0
    assert len(heights) == 1 and next(iter(heights)) > 0
    assert len(policies) == 1

    first_lines = [b.text().splitlines()[0] for b in buttons]
    assert first_lines == [
        "Ücretsiz hesapla giriş",
        "Aboneliğimle bağlan",
        "API anahtarı ya da yerel model",
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
    assert callback_url.startswith("http://127.0.0.1:") and callback_url.endswith("/callback")
    assert query["code_challenge"] == [pkce.code_challenge]
    assert query["code_challenge_method"] == ["S256"]

    with urllib.request.urlopen(f"{callback_url}?code=abc", timeout=5) as response:
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
        with urllib.request.urlopen(f"{cb}?code=test-code", timeout=5) as resp:
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
    )
    wizard.apikey_button.click()
    # Gemini şablonunu seç
    gemini_index = next(
        i for i in range(wizard.preset_combo.count()) if "Gemini" in wizard.preset_combo.itemText(i)
    )
    wizard.preset_combo.setCurrentIndex(gemini_index)
    assert not wizard.key_edit.isHidden()
    wizard.get_key_button.click()
    assert opened == ["https://aistudio.google.com/apikey"]

    wizard.key_edit.setText("gemini-secret-key")
    wizard.model_edit.setText("gemini-2.5-flash")
    wizard.bind_button.click()

    assert stored == [("gemini", "gemini-secret-key")]
    settings = load_settings(path)
    assert settings.primary_model == "gemini-2-5-flash"
    assert settings.endpoints["gemini"].api_key is True
    model = settings.models[settings.primary_model]
    assert model.provider == "openai_compatible"
    assert model.endpoint == "gemini"
    assert model.model == "gemini-2.5-flash"
    assert "gemini-secret-key" not in path.read_text(encoding="utf-8")
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
