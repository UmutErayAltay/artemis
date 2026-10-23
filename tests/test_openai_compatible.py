"""Özel OpenAI uyumlu uçlar (ADR 0033): ayar doğrulama, anahtar çözümü ve istek biçimi."""
# ruff: noqa: F811

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from artemis.brain import providers
from artemis.brain.errors import ProviderError
from artemis.brain.models import Message
from artemis.brain.providers import OpenAICompatibleProvider, OpenRouterProvider
from artemis.config.settings import EndpointConfig, ModelConfig, Settings
from tests.test_providers import HTTPState, http_server, response_for  # noqa: F401, F811


def _model(free: bool = True, endpoint: str | None = "yerel") -> ModelConfig:
    price = Decimal(0) if free else Decimal("0.5")
    return ModelConfig(
        provider="openai_compatible",
        model="m",
        input_per_million=price,
        output_per_million=price,
        endpoint=endpoint,
    )


def _settings(url: str, tmp_path: Path, *, api_key: bool = True, paid: bool = True) -> Settings:
    return Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "s",
        audit_path=tmp_path / "a.jsonl",
        allow_paid=paid,
        endpoints={"yerel-1": EndpointConfig(base_url=url, api_key=api_key)},
    )


def _provider(state: HTTPState, tmp_path: Path, **kw: object) -> OpenAICompatibleProvider:
    free = bool(kw.pop("free", True))
    return OpenAICompatibleProvider(
        _settings(state.url, tmp_path, **kw),  # type: ignore[arg-type]
        _model(free, "yerel-1"),
    )


@pytest.fixture(autouse=True)
def _no_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARTEMIS_YEREL_1_API_KEY", raising=False)
    monkeypatch.setattr(providers, "read_secret", lambda target: None)


def test_remote_http_endpoint_rejected() -> None:
    with pytest.raises(ValidationError):
        EndpointConfig(base_url="http://example.com/v1")
    with pytest.raises(ValidationError):
        EndpointConfig(base_url="https://x.com/v1?k=1")
    assert EndpointConfig(base_url="http://127.0.0.1:8317/v1/").base_url.endswith(":8317/v1")


def test_undefined_endpoint_and_bad_id_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(models={"a": _model(endpoint="yok")})
    with pytest.raises(ValidationError):
        Settings(endpoints={"kötü id": EndpointConfig(base_url="https://x.com/v1")})
    with pytest.raises(ValidationError):
        _model(endpoint=None)
    with pytest.raises(ValidationError):
        ModelConfig(
            provider="ollama",
            model="m",
            input_per_million=Decimal(0),
            output_per_million=Decimal(0),
            endpoint="yerel",
        )


def test_key_order_env_then_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings, model = _settings("https://x.com/v1", tmp_path), _model(endpoint="yerel-1")
    seen: list[str] = []

    def vault(target: str) -> str:
        seen.append(target)
        return "kasa"

    monkeypatch.setattr(providers, "read_secret", vault)
    assert providers._endpoint_key(settings, model) == "kasa"
    assert seen == ["Artemis/yerel-1"]
    monkeypatch.setenv("ARTEMIS_YEREL_1_API_KEY", "ortam")
    assert providers._endpoint_key(settings, model) == "ortam"
    assert seen == ["Artemis/yerel-1"]


def test_missing_key_message(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="ARTEMIS_YEREL_1_API_KEY"):
        providers._endpoint_key(_settings("https://x.com/v1", tmp_path), _model(endpoint="yerel-1"))


async def test_no_key_endpoint_sends_no_authorization(
    http_server: HTTPState,
    tmp_path: Path,
) -> None:
    http_server.body = response_for("openrouter")
    completion = await _provider(http_server, tmp_path, api_key=False).complete(
        [Message(role="user", content="x")], [], timeout=2
    )
    _, payload, headers = http_server.requests[0]
    assert not any(k.lower() == "authorization" for k in headers)
    assert completion.provider == "openai_compatible"
    assert "usage" not in payload and "provider" not in payload


async def test_key_sent_as_bearer(
    http_server: HTTPState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARTEMIS_YEREL_1_API_KEY", "gizli-test")
    http_server.body = response_for("openrouter")
    await _provider(http_server, tmp_path).complete([], [], timeout=2)
    assert http_server.requests[0][2]["Authorization"] == "Bearer gizli-test"


async def test_missing_usage_free_vs_paid(
    http_server: HTTPState,
    tmp_path: Path,
) -> None:
    http_server.body = {"choices": [{"message": {"content": "Tamam."}}]}
    done = await _provider(http_server, tmp_path, api_key=False).complete([], [], timeout=2)
    assert done.usage.input_tokens == 0 and done.usage.cost_usd == 0
    with pytest.raises(ProviderError):
        await _provider(http_server, tmp_path, api_key=False, free=False).complete(
            [], [], timeout=2
        )


async def test_cost_from_config_prices(http_server: HTTPState, tmp_path: Path) -> None:
    http_server.body = response_for("openrouter")
    http_server.body["usage"]["cost"] = 99  # type: ignore[index]  # OpenRouter alanı yok sayılır
    done = await _provider(http_server, tmp_path, api_key=False, free=False).complete(
        [], [], timeout=2
    )
    assert done.usage.cost_usd == Decimal("0.00006")


@pytest.mark.parametrize(
    "status,text", [(401, "anahtarını reddetti"), (403, "anahtarını reddetti"), (404, "bulunamadı")]
)
async def test_status_messages(
    status: int,
    text: str,
    http_server: HTTPState,
    tmp_path: Path,
) -> None:
    http_server.status = status
    with pytest.raises(ProviderError, match=text):
        await _provider(http_server, tmp_path, api_key=False).complete([], [], timeout=2)


async def test_openrouter_unchanged(
    http_server: HTTPState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    http_server.body = response_for("openrouter")
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "s",
        audit_path=tmp_path / "a.jsonl",
        openrouter_base_url=http_server.url,
    )
    model = ModelConfig(
        provider="openrouter",
        model="m",
        input_per_million=Decimal(0),
        output_per_million=Decimal(0),
    )
    done = await OpenRouterProvider(settings, model).complete([], [], timeout=2)
    _, payload, headers = http_server.requests[0]
    assert done.provider == "openrouter"
    assert payload["usage"] == {"include": True} and "provider" in payload
    assert headers["Authorization"] == "Bearer k"
