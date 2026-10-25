"""HTTP çeviricilerini yalnız loopback sunucuyla, gerçek anahtar olmadan sınar."""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from threading import Event, Thread
from typing import Any, Literal

import pytest

from artemis.brain.errors import BudgetExceeded, ProviderError, RetryableProviderError
from artemis.brain.models import Completion, Message, ToolCall
from artemis.brain.providers import (
    AnthropicProvider,
    OllamaProvider,
    OpenRouterProvider,
    Provider,
)
from artemis.brain.router import ProviderRouter
from artemis.config.settings import ModelConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.tools.base import Risk, ToolDefinition

ProviderName = Literal["openrouter", "anthropic", "ollama"]
PROVIDERS: tuple[ProviderName, ...] = ("openrouter", "anthropic", "ollama")


@dataclass
class HTTPState:
    url: str = ""
    status: int = 200
    body: dict[str, Any] | bytes = field(default_factory=dict, repr=False)
    response_headers: dict[str, str] = field(default_factory=dict, repr=False)
    requests: list[tuple[str, dict[str, Any], dict[str, str]]] = field(
        default_factory=list, repr=False
    )
    hold_response: bool = False
    release: Event = field(default_factory=Event, repr=False)


@pytest.fixture
def http_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[HTTPState]:
    # Oturum proxy ayarı loopback testini harici ağa yönlendirmemelidir.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    state = HTTPState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            """Test sunucusunun erişim günlüğü anahtar veya gövde yazmaz."""

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            state.requests.append(
                (self.path, json.loads(self.rfile.read(length)), dict(self.headers.items()))
            )
            if state.hold_response:
                # Zaman aşımından sonra kapanan sokete cevap yazmak testin amacı değildir.
                state.release.wait(timeout=5)
                return
            body = (
                state.body
                if isinstance(state.body, bytes)
                else json.dumps(state.body, ensure_ascii=False).encode("utf-8")
            )
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in state.response_headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def settings_for(http_server: HTTPState, tmp_path: Path, *, allow_paid: bool = True) -> Settings:
    return Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        allow_paid=allow_paid,
        openrouter_base_url=http_server.url,
        anthropic_base_url=http_server.url,
        ollama_base_url=http_server.url,
    )


def provider_for(
    name: ProviderName,
    http_server: HTTPState,
    tmp_path: Path,
    *,
    allow_paid: bool = True,
) -> Provider:
    model = ModelConfig(
        provider=name,
        model="fixture-model",
        input_per_million=Decimal("0.125") if name != "ollama" else Decimal(0),
        output_per_million=Decimal("0.5") if name != "ollama" else Decimal(0),
    )
    cls = {
        "openrouter": OpenRouterProvider,
        "anthropic": AnthropicProvider,
        "ollama": OllamaProvider,
    }[name]
    return cls(settings_for(http_server, tmp_path, allow_paid=allow_paid), model)


def response_for(name: ProviderName, *, tool: bool = False) -> dict[str, Any]:
    function = {"name": "file-read_1", "arguments": {"path": "örnek.txt"}}
    calls = [{"id": "call_1", "type": "function", "function": function}] if tool else []
    if name == "openrouter":
        if tool:
            function["arguments"] = json.dumps(function["arguments"], ensure_ascii=False)
        return {
            "choices": [{"message": {"content": "Tamam.", "tool_calls": calls}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
    if name == "anthropic":
        content: list[dict[str, Any]] = [{"type": "text", "text": "Tamam."}]
        if tool:
            content.append({
                "type": "tool_use", "id": "call_1", "name": "file-read_1",
                "input": {"path": "örnek.txt"},
            })
        return {"content": content, "usage": {"input_tokens": 100, "output_tokens": 20}}
    return {
        "message": {"content": "Tamam.", "tool_calls": calls},
        "done": True,
        "prompt_eval_count": 100,
        "eval_count": 20,
    }


@pytest.fixture(autouse=True)
def ephemeral_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", token_urlsafe())
    monkeypatch.setenv("ANTHROPIC_API_KEY", token_urlsafe())


@pytest.mark.parametrize("name", ("openrouter", "ollama"))
@pytest.mark.parametrize("calls", (None, False, 0, {}, ""))
async def test_optional_tool_calls_null_only(
    name: ProviderName, calls: Any, http_server: HTTPState, tmp_path: Path
) -> None:
    http_server.body = response_for(name)
    message = (
        http_server.body["choices"][0]["message"]
        if name == "openrouter"
        else http_server.body["message"]
    )
    message["tool_calls"] = calls
    provider = provider_for(name, http_server, tmp_path)
    if calls is None:
        result = await provider.complete([Message(role="user", content="Merhaba")], [], timeout=2)
        assert result.content == "Tamam."
        assert result.tool_calls == []
        assert result.usage.input_tokens == 100
    else:
        with pytest.raises(ProviderError):
            await provider.complete([Message(role="user", content="Merhaba")], [], timeout=2)


@pytest.mark.parametrize("name", PROVIDERS)
async def test_tool_schema_and_calls_roundtrip(
    name: ProviderName, http_server: HTTPState, tmp_path: Path
) -> None:
    http_server.body = response_for(name, tool=True)
    provider = provider_for(name, http_server, tmp_path)
    definition = ToolDefinition(
        name="file-read_1",
        description="Dosyanın içeriğini okur.",
        parameters={
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"], "additionalProperties": False,
        },
        risk=Risk.READ_ONLY,
    )
    prior_call = ToolCall(id="prior", name=definition.name, arguments={"path": "önce.txt"})
    result = await provider.complete([
        Message(role="system", content="Türkçe yanıtla."),
        Message(role="user", content="Dosyayı oku."),
        Message(role="assistant", tool_calls=[prior_call]),
        Message(role="tool", tool_call_id="prior", content="Önceki veri"),
        Message(role="user", content="Şimdi diğer dosyayı oku."),
    ], [definition], timeout=2)
    assert result.tool_calls == [
        ToolCall(id="call_1", name=definition.name, arguments={"path": "örnek.txt"})
    ]
    assert result.content == "Tamam."
    assert result.provider == name
    assert result.model == "fixture-model"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (100, 20)
    expected_cost = Decimal(0) if name == "ollama" else Decimal("0.0000225")
    assert result.usage.cost_usd == expected_cost
    path, payload, headers = http_server.requests[0]
    assert payload["stream"] is False
    if name == "anthropic":
        assert path == "/messages"
        assert payload["system"] == "Türkçe yanıtla."
        assert payload["tools"][0] == {
            "name": definition.name, "description": definition.description,
            "input_schema": definition.parameters,
        }
        assert payload["messages"][1]["content"][0]["input"] == prior_call.arguments
        assert payload["messages"][2]["content"][0] == {
            "type": "tool_result", "tool_use_id": "prior", "content": "Önceki veri",
        }
        assert "x-api-key" in {key.lower() for key in headers}
    else:
        assert path == ("/api/chat" if name == "ollama" else "/chat/completions")
        schema = payload["tools"][0]["function"]
        assert schema["name"] == definition.name
        assert schema["parameters"] == definition.parameters
        arguments = payload["messages"][2]["tool_calls"][0]["function"]["arguments"]
        assert (json.loads(arguments) if name == "openrouter" else arguments) == {
            "path": "önce.txt"
        }
        if name == "ollama":
            assert payload["messages"][3]["tool_name"] == definition.name
            assert "tool_call_id" not in payload["messages"][3]
        else:
            assert payload["messages"][3]["tool_call_id"] == "prior"
    if name == "openrouter":
        max_price = payload["provider"]["max_price"]
        assert Decimal(str(max_price["prompt"])) == Decimal("0.125")
        assert Decimal(str(max_price["completion"])) == Decimal("0.5")


@pytest.mark.parametrize("name", PROVIDERS)
async def test_private_content_and_arguments_only_reach_local_provider(
    name: ProviderName, http_server: HTTPState, tmp_path: Path
) -> None:
    http_server.body = response_for(name)
    private_content = token_urlsafe()
    private_argument = token_urlsafe()
    messages = [
        Message(role="user", content=private_content, private=True),
        Message(role="assistant", private=True, tool_calls=[
            ToolCall(id="private", name="create_file", arguments={"content": private_argument})
        ]),
    ]
    await provider_for(name, http_server, tmp_path).complete(messages, [], timeout=2)
    serialized = json.dumps(http_server.requests[0][1])
    assert (private_content in serialized) == (name == "ollama")
    assert (private_argument in serialized) == (name == "ollama")
    assert messages[0].content == private_content
    assert messages[1].tool_calls[0].arguments["content"] == private_argument


@pytest.mark.parametrize("name", PROVIDERS)
@pytest.mark.parametrize("status", [400, 401, 503])
async def test_http_errors_do_not_expose_body_headers_or_chained_exception(
    name: ProviderName, status: int, http_server: HTTPState, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = token_urlsafe()
    monkeypatch.setenv("OPENROUTER_API_KEY", sentinel)
    monkeypatch.setenv("ANTHROPIC_API_KEY", sentinel)
    http_server.status = status
    http_server.body = {"error": sentinel}
    http_server.response_headers["x-debug-token"] = sentinel
    provider = provider_for(name, http_server, tmp_path)
    expected_error = RetryableProviderError if status >= 500 else ProviderError
    with pytest.raises(expected_error) as failure:
        await provider.complete([Message(role="user", content="Merhaba")], [], timeout=2)
    assert sentinel not in str(failure.value)
    assert sentinel not in repr(failure.value)
    assert sentinel not in repr(provider)
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None


@pytest.mark.parametrize("name", PROVIDERS)
async def test_rate_limit_is_retryable_so_fallback_model_runs(
    name: ProviderName, http_server: HTTPState, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 429 geçicidir: kalıcı hata sayılınca bilgisayar kullanımı oturumu ilk 429'da bitiyordu.
    monkeypatch.setenv("OPENROUTER_API_KEY", token_urlsafe())
    monkeypatch.setenv("ANTHROPIC_API_KEY", token_urlsafe())
    http_server.status = 429
    http_server.body = {"error": "hız sınırı"}
    with pytest.raises(RetryableProviderError) as failure:
        await provider_for(name, http_server, tmp_path).complete(
            [Message(role="user", content="Merhaba")], [], timeout=2
        )
    assert "429" in str(failure.value)
    assert failure.value.__cause__ is None


@pytest.mark.parametrize("name", PROVIDERS)
async def test_redirect_is_not_followed(
    name: ProviderName, http_server: HTTPState, tmp_path: Path
) -> None:
    http_server.status = 307
    http_server.response_headers["Location"] = f"{http_server.url}/leak-target"
    with pytest.raises(ProviderError):
        await provider_for(name, http_server, tmp_path).complete([], [], timeout=2)
    assert len(http_server.requests) == 1
    assert http_server.requests[0][0] != "/leak-target"


@pytest.mark.parametrize("name", PROVIDERS)
async def test_timeout_has_safe_error(
    name: ProviderName, http_server: HTTPState, tmp_path: Path
) -> None:
    http_server.hold_response = True
    with pytest.raises(RetryableProviderError) as failure:
        await provider_for(name, http_server, tmp_path).complete([], [], timeout=0.05)
    assert failure.value.__context__ is None
    assert "127.0.0.1" not in str(failure.value)


@pytest.mark.parametrize("name,variable", [
    ("openrouter", "OPENROUTER_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY"),
])
async def test_missing_key_fails_before_http(
    name: ProviderName, variable: str, http_server: HTTPState, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(variable)
    with pytest.raises(ProviderError, match="ortam değişkeni"):
        await provider_for(name, http_server, tmp_path).complete([], [], timeout=2)
    assert http_server.requests == []


@pytest.mark.parametrize("name", ["openrouter", "anthropic"])
async def test_paid_model_is_blocked_before_http_without_explicit_setting(
    name: ProviderName, http_server: HTTPState, tmp_path: Path
) -> None:
    with pytest.raises(BudgetExceeded):
        await provider_for(name, http_server, tmp_path, allow_paid=False).complete(
            [], [], timeout=2
        )
    assert http_server.requests == []


@pytest.mark.parametrize("name", PROVIDERS)
@pytest.mark.parametrize("body", [b"not-json", b"[]", b"{}"])
async def test_malformed_responses_are_errors(
    name: ProviderName, body: bytes, http_server: HTTPState, tmp_path: Path
) -> None:
    http_server.body = body
    with pytest.raises(ProviderError):
        await provider_for(name, http_server, tmp_path).complete([], [], timeout=2)


@pytest.mark.parametrize("name", PROVIDERS)
async def test_missing_usage_does_not_invent_zero_cost(
    name: ProviderName, http_server: HTTPState, tmp_path: Path
) -> None:
    response = response_for(name)
    if name == "ollama":
        del response["prompt_eval_count"]
    else:
        del response["usage"]
    http_server.body = response
    with pytest.raises(ProviderError):
        await provider_for(name, http_server, tmp_path).complete([], [], timeout=2)


async def test_openrouter_uses_verified_reported_cost(
    http_server: HTTPState, tmp_path: Path
) -> None:
    response = response_for("openrouter")
    response["usage"]["cost"] = "0.0000037"
    http_server.body = response
    result = await provider_for("openrouter", http_server, tmp_path).complete(
        [Message(role="user", content="Merhaba")], [], timeout=2
    )
    assert result.usage.cost_usd == Decimal("0.0000037")


@pytest.mark.parametrize("cost", [-1, "NaN", "Infinity", "not-a-cost", None])
async def test_openrouter_rejects_invalid_reported_cost(
    cost: Any, http_server: HTTPState, tmp_path: Path
) -> None:
    response = response_for("openrouter")
    response["usage"]["cost"] = cost
    http_server.body = response
    with pytest.raises(ProviderError):
        await provider_for("openrouter", http_server, tmp_path).complete(
            [Message(role="user", content="Merhaba")], [], timeout=2
        )


async def test_ollama_tool_result_requires_prior_call_name(
    http_server: HTTPState, tmp_path: Path
) -> None:
    with pytest.raises(ProviderError, match="önceki araç çağrısı"):
        await provider_for("ollama", http_server, tmp_path).complete(
            [Message(role="tool", tool_call_id="missing", content="sonuç")], [], timeout=2
        )


async def test_ollama_missing_call_ids_do_not_alias_across_turns(
    http_server: HTTPState, tmp_path: Path
) -> None:
    response = response_for("ollama", tool=True)
    del response["message"]["tool_calls"][0]["id"]
    http_server.body = response
    provider = provider_for("ollama", http_server, tmp_path)
    first = await provider.complete([], [], timeout=2)
    first_call = first.tool_calls[0]

    second = await provider.complete(
        [
            Message(role="assistant", tool_calls=[first_call]),
            Message(role="tool", tool_call_id=first_call.id, content="sonuç 1"),
        ],
        [],
        timeout=2,
    )
    second_call = second.tool_calls[0]
    assert first_call.id != second_call.id
    sent_tool_result = http_server.requests[1][1]["messages"][1]
    assert sent_tool_result["tool_name"] == first_call.name


async def test_ollama_incomplete_response_is_not_success(
    http_server: HTTPState, tmp_path: Path
) -> None:
    response = response_for("ollama")
    response["done"] = False
    http_server.body = response
    with pytest.raises(ProviderError):
        await provider_for("ollama", http_server, tmp_path).complete([], [], timeout=2)


@pytest.mark.parametrize("name", PROVIDERS)
async def test_length_cut_is_marked_not_silent(
    name: ProviderName, http_server: HTTPState, tmp_path: Path
) -> None:
    response = response_for(name)
    if name == "openrouter":
        response["choices"][0]["finish_reason"] = "length"
    elif name == "anthropic":
        response["stop_reason"] = "max_tokens"
    else:
        response["done_reason"] = "length"
    http_server.body = response
    result = await provider_for(name, http_server, tmp_path).complete(
        [Message(role="user", content="Merhaba")], [], timeout=2
    )
    assert result.content.endswith("devam et\" de)")
    http_server.body = response_for(name)
    normal = await provider_for(name, http_server, tmp_path).complete(
        [Message(role="user", content="Merhaba")], [], timeout=2
    )
    assert "kesildi" not in normal.content


# --- ADR 0043: akış yalnız metin taşır; araç çağrısı tam ve doğrulanmış biçimde döner ---

STREAM_TOOLS = [ToolDefinition(
    name="file-read_1", description="Okur.", risk=Risk.READ_ONLY,
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)]
STREAM_MESSAGES = [Message(role="user", content="selam")]


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def text(self, delta: str) -> None:
        self.events.append(("text", delta))

    def tool_call(self) -> None:
        self.events.append(("tool", ""))

    @property
    def joined(self) -> str:
        return "".join(value for kind, value in self.events if kind == "text")


def _sse(*chunks: Any, done: bool = True) -> bytes:
    lines = [": OPENROUTER PROCESSING"]
    lines += [f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks]
    if done:
        lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


def _ndjson(*items: Any) -> bytes:
    return ("\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n").encode()


def _delta(**delta: Any) -> dict[str, Any]:
    return {"choices": [{"delta": delta, "finish_reason": None}]}


async def test_openrouter_streams_text_and_assembles_tool_call_silently(http_server, tmp_path):
    http_server.body = _sse(
        _delta(content="Bakıyorum, "),
        _delta(content="hemen."),
        _delta(tool_calls=[{"index": 0, "id": "call_1",
                            "function": {"name": "file-read_1", "arguments": "{\"pa"}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": "th\": \"örnek.txt\"}"}}]),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.001}},
    )
    recorder = Recorder()
    completion = await provider_for("openrouter", http_server, tmp_path).complete(
        STREAM_MESSAGES, STREAM_TOOLS, timeout=5, stream=recorder,
    )
    assert http_server.requests[0][1]["stream"] is True
    assert recorder.joined == "Bakıyorum, hemen."
    # Araç argüman parçaları akışa hiç girmez; yalnız "araç başladı" işareti gelir.
    assert ("tool", "") in recorder.events
    assert "örnek" not in recorder.joined and "path" not in recorder.joined
    assert completion.content == "Bakıyorum, hemen."
    assert [(call.name, call.arguments) for call in completion.tool_calls] == [
        ("file-read_1", {"path": "örnek.txt"})
    ]
    assert completion.usage.cost_usd == Decimal("0.001")


async def test_stream_cut_before_finish_is_retryable_not_success(http_server, tmp_path):
    http_server.body = _sse(_delta(content="Yarım"), done=False)
    with pytest.raises(RetryableProviderError):
        await provider_for("openrouter", http_server, tmp_path).complete(
            STREAM_MESSAGES, [], timeout=5, stream=Recorder(),
        )


async def test_streamed_tool_call_is_validated_like_full_response(http_server, tmp_path):
    # Bozuk argüman JSON'u akışta da aynı ayrıştırıcıda reddedilir; kısmi argüman çalışmaz.
    http_server.body = _sse(
        _delta(tool_calls=[{"index": 0, "id": "c", "function": {"name": "file-read_1",
                                                                 "arguments": "{\"path\": "}}]),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    )
    with pytest.raises(ProviderError):
        await provider_for("openrouter", http_server, tmp_path).complete(
            STREAM_MESSAGES, STREAM_TOOLS, timeout=5, stream=Recorder(),
        )


async def test_non_streaming_call_is_unchanged(http_server, tmp_path):
    http_server.body = {"choices": [{"message": {"content": "Tamam."}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    completion = await provider_for("openrouter", http_server, tmp_path).complete(
        STREAM_MESSAGES, [], timeout=5,
    )
    assert http_server.requests[0][1]["stream"] is False
    assert completion.content == "Tamam."


async def test_ollama_streams_ndjson_and_keeps_final_counters(http_server, tmp_path):
    http_server.body = _ndjson(
        {"message": {"content": "Merhaba, "}, "done": False},
        {"message": {"content": "nasılsın?"}, "done": False},
        {"message": {"content": "", "tool_calls": [
            {"function": {"name": "file-read_1", "arguments": {"path": "a.txt"}}}]},
         "done": False},
        {"message": {"content": ""}, "done": True, "done_reason": "stop",
         "prompt_eval_count": 12, "eval_count": 7},
    )
    recorder = Recorder()
    completion = await provider_for("ollama", http_server, tmp_path).complete(
        STREAM_MESSAGES, STREAM_TOOLS, timeout=5, stream=recorder,
    )
    assert recorder.joined == "Merhaba, nasılsın?" and ("tool", "") in recorder.events
    assert completion.tool_calls[0].arguments == {"path": "a.txt"}
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (12, 7)


async def test_ollama_stream_without_done_is_not_success(http_server, tmp_path):
    http_server.body = _ndjson({"message": {"content": "Yarım"}, "done": False})
    with pytest.raises(RetryableProviderError):
        await provider_for("ollama", http_server, tmp_path).complete(
            STREAM_MESSAGES, [], timeout=5, stream=Recorder(),
        )


class StreamingFake:
    def __init__(self, text: str, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0

    async def complete(self, messages, tools, *, timeout, stream=None):
        self.calls += 1
        if stream is not None and self.text:
            stream.text(self.text)
        if self.error is not None:
            raise self.error
        return Completion(content=self.text)


def _router(tmp_path, primary, fallback) -> ProviderRouter:
    model = ModelConfig(provider="ollama", model="m",
                        input_per_million=Decimal(0), output_per_million=Decimal(0))
    settings = Settings(
        workspace=tmp_path, state_dir=tmp_path / "state", audit_path=tmp_path / "audit.jsonl",
        primary_model="primary", fallback_model="fallback",
        models={"primary": model, "fallback": model},
    )
    return ProviderRouter(settings, AuditLog(settings.audit_path),
                          providers={"primary": primary, "fallback": fallback})


async def test_router_does_not_fall_back_after_text_reached_the_user(tmp_path):
    fallback = StreamingFake("İkinci yanıt.")
    router = _router(tmp_path, StreamingFake("Birinci ", RetryableProviderError("koptu")),
                     fallback)
    recorder = Recorder()
    with pytest.raises(RetryableProviderError):
        await router.complete(STREAM_MESSAGES, [], timeout=5, stream=recorder)
    assert fallback.calls == 0 and recorder.joined == "Birinci "
    rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(row["event"] == "provider_fallback_skipped" for row in rows)


async def test_router_still_falls_back_when_nothing_was_streamed(tmp_path):
    fallback = StreamingFake("Yedek yanıt.")
    router = _router(tmp_path, StreamingFake("", RetryableProviderError("bağlanamadı")),
                     fallback)
    recorder = Recorder()
    completion = await router.complete(STREAM_MESSAGES, [], timeout=5, stream=recorder)
    assert completion.content == "Yedek yanıt." and fallback.calls == 1
    assert recorder.joined == "Yedek yanıt."


async def test_router_without_stream_keeps_old_provider_contract(tmp_path):
    class Legacy:
        async def complete(self, messages, tools, *, timeout):
            return Completion(content="eski")

    router = _router(tmp_path, Legacy(), Legacy())
    assert (await router.complete(STREAM_MESSAGES, [], timeout=5)).content == "eski"



def _anthropic_sse(*events: dict[str, Any]) -> bytes:
    lines = []
    for event in events:
        lines.append(f"event: {event['type']}")
        lines.append(f"data: {json.dumps(event, ensure_ascii=False)}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _anthropic_events(*, stop: bool = True) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 100, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Bakıyorum. "}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Hemen."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "call_1", "name": "file-read_1", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"ör"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": "nek.txt\"}"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 20}},
    ]
    if stop:
        events.append({"type": "message_stop"})
    return events


async def test_anthropic_streams_text_and_assembles_tool_input_silently(http_server, tmp_path):
    http_server.body = _anthropic_sse(*_anthropic_events())
    recorder = Recorder()
    completion = await provider_for("anthropic", http_server, tmp_path).complete(
        STREAM_MESSAGES, STREAM_TOOLS, timeout=5, stream=recorder,
    )
    assert http_server.requests[0][1]["stream"] is True
    assert recorder.joined == "Bakıyorum. Hemen."
    assert ("tool", "") in recorder.events and "örnek" not in recorder.joined
    assert completion.tool_calls[0].arguments == {"path": "örnek.txt"}
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (100, 20)


async def test_anthropic_stream_without_message_stop_is_not_success(http_server, tmp_path):
    http_server.body = _anthropic_sse(*_anthropic_events(stop=False))
    with pytest.raises(RetryableProviderError):
        await provider_for("anthropic", http_server, tmp_path).complete(
            STREAM_MESSAGES, STREAM_TOOLS, timeout=5, stream=Recorder(),
        )


@pytest.mark.parametrize(("error", "expected"), [
    ("overloaded_error", RetryableProviderError), ("invalid_request_error", ProviderError),
])
async def test_anthropic_stream_error_event_is_an_error(http_server, tmp_path, error, expected):
    http_server.body = _anthropic_sse(
        _anthropic_events()[0], {"type": "error", "error": {"type": error, "message": "x"}},
    )
    with pytest.raises(expected):
        await provider_for("anthropic", http_server, tmp_path).complete(
            STREAM_MESSAGES, [], timeout=5, stream=Recorder(),
        )


async def test_anthropic_broken_streamed_tool_input_is_rejected(http_server, tmp_path):
    events = _anthropic_events()
    events[7] = {"type": "content_block_delta", "index": 1,
                 "delta": {"type": "input_json_delta", "partial_json": ""}}
    http_server.body = _anthropic_sse(*events)
    with pytest.raises(ProviderError):
        await provider_for("anthropic", http_server, tmp_path).complete(
            STREAM_MESSAGES, STREAM_TOOLS, timeout=5, stream=Recorder(),
        )
