"""HTTP biçimleri bu sınırda çevrilir; araç yürütme yetkisi sağlayıcılara verilmez."""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from typing import Any, Protocol, cast
from uuid import uuid4

import httpx

from jarvis.brain.errors import (
    BudgetExceeded as BudgetExceeded,
)
from jarvis.brain.errors import (
    ProviderError as ProviderError,
)
from jarvis.brain.errors import RetryableProviderError
from jarvis.brain.models import Completion, Message, ToolCall, Usage
from jarvis.config.settings import ModelConfig, Settings
from jarvis.safety.audit import AuditLog
from jarvis.tools.base import ToolDefinition

_PRIVATE_PLACEHOLDER = "[Gizlilik ayarı nedeniyle içerik buluta gönderilmedi.]"
_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class Provider(Protocol):
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        timeout: float,
    ) -> Completion: ...


def _cloud_messages(messages: list[Message]) -> list[Message]:
    """Maskeyi son HTTP sınırında uygulamak, yeni kanalların gizliliği atlamasını önler."""
    return [
        message.model_copy(
            update={
                "content": _PRIVATE_PLACEHOLDER,
                "tool_calls": [
                    call.model_copy(update={"arguments": {}}) for call in message.tool_calls
                ],
            }
        )
        if message.private
        else message
        for message in messages
    ]


def _tool_schemas(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for tool in tools:
        if not _TOOL_NAME.fullmatch(tool.name):
            raise ProviderError("Araç adı sağlayıcı biçimine uygun değil.")
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
        )
    return definitions


def _chat_messages(
    messages: list[Message], *, native_arguments: bool = False, ollama: bool = False
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    tool_names = {
        call.id: call.name
        for message in messages
        if message.role == "assistant"
        for call in message.tool_calls
    }
    for message in messages:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_call_id:
            if ollama:
                # Ollama'nın chat message sözleşmesi tool yanıtını adla eşler.
                # Önceki çağrı bulunmazsa tool_name göndermek doğru bir eşleme uydurur.
                tool_name = tool_names.get(message.tool_call_id)
                if tool_name is None:
                    raise ProviderError("Araç yanıtı için önceki araç çağrısı bulunamadı.")
                item["tool_name"] = tool_name
            else:
                item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments
                        if native_arguments
                        else json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        result.append(item)
    return result


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderError("Sağlayıcı yanıtındaki nesne biçimi geçersiz.")
    return cast(dict[str, Any], value)


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ProviderError("Sağlayıcı yanıtındaki liste biçimi geçersiz.")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ProviderError("Sağlayıcı yanıtındaki metin biçimi geçersiz.")
    return value


def _tokens(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderError("Sağlayıcı güvenilir token kullanımı döndürmedi.")
    return cast(int, value)


def _usage(model: ModelConfig, input_tokens: Any, output_tokens: Any) -> Usage:
    prompt = _tokens(input_tokens)
    generated = _tokens(output_tokens)
    cost = (
        Decimal(prompt) * model.input_per_million + Decimal(generated) * model.output_per_million
    ) / Decimal(1_000_000)
    return Usage(input_tokens=prompt, output_tokens=generated, cost_usd=cost)


def _reported_cost(value: Any) -> Decimal:
    """Sağlayıcının gerçek maliyetini Decimal'e çevirip güvenliğini doğrular."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ProviderError("Sağlayıcının gerçek maliyeti geçersiz.")
    try:
        cost = Decimal(str(value))
    except (ValueError, ArithmeticError):
        raise ProviderError("Sağlayıcının gerçek maliyeti geçersiz.") from None
    if not cost.is_finite() or cost < 0:
        raise ProviderError("Sağlayıcının gerçek maliyeti geçersiz.")
    return cost


def _openrouter_max_price(model: ModelConfig) -> dict[str, float]:
    """OpenRouter'ın milyon-token birimli yönlendirme tavanlarını üretir."""
    return {
        "prompt": float(model.input_per_million),
        "completion": float(model.output_per_million),
    }


_TRUNCATED = "…(yanıt uzunluk sınırında kesildi; devam için \"devam et\" de)"


def _mark_truncated(content: str, truncated: bool) -> str:
    """Sağlayıcı çıktı sınırında durduysa bunu sessizce yutma; kullanıcı yarım cevabı bilsin."""
    return f"{content.rstrip()}\n\n{_TRUNCATED}" if truncated and content.strip() else content


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            decoded = None
        return _object(decoded)
    return _object(value)


def _chat_calls(value: Any) -> list[ToolCall]:
    # OpenAI uyumlu yanıtlar araç çağrısı olmadığında null döndürebilir.
    if value is None:
        return []
    calls: list[ToolCall] = []
    for raw in _array(value):
        item = _object(raw)
        function = _object(item.get("function"))
        calls.append(
            ToolCall(
                id=_string(item["id"]) if "id" in item else uuid4().hex,
                name=_string(function.get("name")),
                arguments=_arguments(function.get("arguments")),
            )
        )
    return calls


async def _post(
    url: str, payload: dict[str, Any], headers: dict[str, str], *, timeout: float
) -> dict[str, Any]:
    if timeout <= 0:
        raise RetryableProviderError("Sağlayıcı için ayrılan süre doldu.")
    # Hata metnine URL, yanıt veya HTTP nesnesi koymak sır sızdırabileceğinden
    # istisna zinciri de dışarı taşınmaz. Redirect yeni bir sunucuya anahtar taşıyamaz.
    network_failed = False
    response: httpx.Response | None = None
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.RequestError:
        network_failed = True
    if network_failed or response is None:
        raise RetryableProviderError("Sağlayıcıya bağlanılamadı veya yanıt süresi doldu.")
    if response.status_code >= 500:
        raise RetryableProviderError(
            f"Sağlayıcı sunucu hatası verdi (HTTP {response.status_code})."
        )
    if response.status_code >= 300:
        raise ProviderError(f"Sağlayıcı isteği kabul etmedi (HTTP {response.status_code}).")
    try:
        value = response.json()
    except (json.JSONDecodeError, ValueError):
        value = None
    return _object(value)


def _key(name: str) -> str:
    key = os.environ.get(name, "")
    if not key:
        raise ProviderError(f"{name} ortam değişkeni tanımlı değil.")
    return key


def _check_paid(settings: Settings, model: ModelConfig) -> None:
    if not settings.allow_paid and (model.input_per_million or model.output_per_million):
        raise BudgetExceeded("Ücretli model çağrısı ayarlarda açıkça izinli değil.")


class OpenRouterProvider:
    def __init__(self, settings: Settings, model: ModelConfig) -> None:
        self.settings = settings
        self.model = model

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        timeout: float,
    ) -> Completion:
        _check_paid(self.settings, self.model)
        payload: dict[str, Any] = {
            "model": self.model.model,
            "messages": _chat_messages(_cloud_messages(messages)),
            "max_tokens": self.settings.max_output_tokens,
            "stream": False,
            "usage": {"include": True},
            "provider": {"max_price": _openrouter_max_price(self.model)},
        }
        if tools:
            payload["tools"] = _tool_schemas(tools)
        data = await _post(
            f"{str(self.settings.openrouter_base_url).rstrip('/')}/chat/completions",
            payload,
            {"Authorization": f"Bearer {_key('OPENROUTER_API_KEY')}"},
            timeout=timeout,
        )
        choices = _array(data.get("choices"))
        if not choices:
            raise ProviderError("Sağlayıcı yanıtında tamamlanmış bir seçenek yok.")
        message = _object(_object(choices[0]).get("message"))
        usage = _object(data.get("usage"))
        calculated_usage = _usage(
            self.model, usage.get("prompt_tokens"), usage.get("completion_tokens")
        )
        actual_cost = (
            _reported_cost(usage["cost"]) if "cost" in usage else calculated_usage.cost_usd
        )
        return Completion(
            content=_mark_truncated(
                _string(message.get("content") or ""),
                _object(choices[0]).get("finish_reason") == "length",
            ),
            tool_calls=_chat_calls(message.get("tool_calls", [])),
            usage=Usage(
                input_tokens=calculated_usage.input_tokens,
                output_tokens=calculated_usage.output_tokens,
                cost_usd=actual_cost,
            ),
            provider="openrouter",
            model=self.model.model,
        )


class AnthropicProvider:
    def __init__(self, settings: Settings, model: ModelConfig) -> None:
        self.settings = settings
        self.model = model

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        timeout: float,
    ) -> Completion:
        _check_paid(self.settings, self.model)
        system: list[str] = []
        converted: list[dict[str, Any]] = []
        for message in _cloud_messages(messages):
            if message.role == "system":
                system.append(message.content)
                continue
            blocks: list[dict[str, Any]] = []
            role = "user" if message.role == "tool" else message.role
            if message.role == "tool":
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
            else:
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                    for call in message.tool_calls
                )
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": role, "content": blocks})
        payload: dict[str, Any] = {
            "model": self.model.model,
            "system": "\n".join(system),
            "messages": converted,
            "max_tokens": self.settings.max_output_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = [
                {
                    "name": item["function"]["name"],
                    "description": item["function"]["description"],
                    "input_schema": item["function"]["parameters"],
                }
                for item in _tool_schemas(tools)
            ]
        data = await _post(
            f"{str(self.settings.anthropic_base_url).rstrip('/')}/messages",
            payload,
            {"x-api-key": _key("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"},
            timeout=timeout,
        )
        content: list[str] = []
        calls: list[ToolCall] = []
        for raw in _array(data.get("content")):
            block = _object(raw)
            if block.get("type") == "text":
                content.append(_string(block.get("text")))
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        id=_string(block.get("id")),
                        name=_string(block.get("name")),
                        arguments=_object(block.get("input")),
                    )
                )
        usage = _object(data.get("usage"))
        return Completion(
            content=_mark_truncated("\n".join(content), data.get("stop_reason") == "max_tokens"),
            tool_calls=calls,
            usage=_usage(self.model, usage.get("input_tokens"), usage.get("output_tokens")),
            provider="anthropic",
            model=self.model.model,
        )


class OllamaProvider:
    def __init__(self, settings: Settings, model: ModelConfig) -> None:
        self.settings = settings
        self.model = model

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        timeout: float,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model.model,
            "messages": _chat_messages(messages, native_arguments=True, ollama=True),
            "stream": False,
            "options": {
                "num_predict": self.settings.max_output_tokens,
                "num_ctx": self.model.context_window,
            },
        }
        if tools:
            payload["tools"] = _tool_schemas(tools)
        data = await _post(
            f"{str(self.settings.ollama_base_url).rstrip('/')}/api/chat",
            payload,
            {},
            timeout=timeout,
        )
        if data.get("done") is not True:
            raise ProviderError("Yerel sağlayıcı tamamlanmış bir yanıt döndürmedi.")
        message = _object(data.get("message"))
        return Completion(
            content=_mark_truncated(
                _string(message.get("content") or ""), data.get("done_reason") == "length"
            ),
            tool_calls=_chat_calls(message.get("tool_calls", [])),
            usage=_usage(self.model, data.get("prompt_eval_count"), data.get("eval_count")),
            provider="ollama",
            model=self.model.model,
        )


def build_provider(settings: Settings, audit: AuditLog) -> Provider:
    # Yerel import yönlendiricinin aynı sağlayıcı arayüzünü kullanmasına izin verir.
    from jarvis.brain.router import ProviderRouter

    return ProviderRouter(settings, audit)
