"""Bulut arızası, gizlilik kararı ve harcama aynı sağlayıcı sınırında denetlenir."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from decimal import Decimal

from jarvis.brain.budget import BudgetLedger
from jarvis.brain.errors import BudgetExceeded, ProviderError, RetryableProviderError
from jarvis.brain.models import Completion, Message
from jarvis.brain.providers import (
    AnthropicProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    OpenRouterProvider,
    Provider,
    _check_paid,
    _endpoint_key,
    _key,
)
from jarvis.config.settings import ModelConfig, Settings
from jarvis.safety.audit import AuditLog
from jarvis.tools.base import ToolDefinition


def _reservation(model: ModelConfig, max_output_tokens: int) -> Decimal:
    # Token tahmini dil ve tokenizer ile değişir; bağlam üst sınırı güvenli kalır.
    return (
        Decimal(model.context_window) * model.input_per_million
        + Decimal(max_output_tokens) * model.output_per_million
    ) / Decimal(1_000_000)


class ProviderRouter:
    def __init__(
        self,
        settings: Settings,
        audit: AuditLog,
        *,
        providers: dict[str, Provider] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not settings.primary_model or settings.primary_model not in settings.models:
            raise ProviderError("Birincil model config içinde tanımlanmalı.")
        self.settings = settings
        self.audit = audit
        self.ledger = BudgetLedger(settings.state_dir / "budget.sqlite3", settings.daily_budget_usd)
        self._clock = clock
        self._fallback_until = 0.0
        self._providers: dict[str, Provider]
        if providers is not None:
            self._providers = providers
        else:
            self._providers = {}
            for alias, model in settings.models.items():
                if model.provider == "openrouter":
                    self._providers[alias] = OpenRouterProvider(settings, model)
                elif model.provider == "anthropic":
                    self._providers[alias] = AnthropicProvider(settings, model)
                elif model.provider == "openai_compatible":
                    self._providers[alias] = OpenAICompatibleProvider(settings, model)
                else:
                    self._providers[alias] = OllamaProvider(settings, model)

    def _emit_audit(self, event: str, **fields: object) -> None:
        try:
            self.audit.emit(event, **fields)
        except OSError:
            raise ProviderError("Sağlayıcı denetim kaydı güvenli biçimde yazılamadı.") from None

    async def _call(
        self,
        alias: str,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        timeout: float,
        local_fallback: bool,
    ) -> Completion:
        model = self.settings.models[alias]
        _check_paid(self.settings, model)
        if timeout <= 0:
            raise RetryableProviderError("Sağlayıcı için ayrılan süre doldu.")
        # Henüz bir istek çıkmadığı bilinen hatalar günlük rezervasyonu tüketmez.
        if model.provider == "openrouter":
            _key("OPENROUTER_API_KEY")
        elif model.provider == "anthropic":
            _key("ANTHROPIC_API_KEY")
        elif model.provider == "openai_compatible":
            _endpoint_key(self.settings, model)
        amount = _reservation(model, self.settings.max_output_tokens)
        try:
            reservation = self.ledger.reserve(amount)
        except BudgetExceeded:
            self._emit_audit(
                "provider_budget_denied",
                provider=model.provider,
                model=model.model,
                reserved_usd=str(amount),
            )
            raise
        started = self._clock()
        # Yerel uç dahil openai_compatible kaydı yazılır: uç adresi config'te değişebilir.
        if model.provider != "ollama":
            try:
                self._emit_audit(
                    "cloud_transfer",
                    provider=model.provider,
                    model=model.model,
                    message_count=len(messages),
                    masked_messages=sum(message.private for message in messages),
                    allow_cloud_file_content=self.settings.allow_cloud_file_content,
                    allow_cloud_clipboard=self.settings.allow_cloud_clipboard,
                )
            except ProviderError:
                # Kaydın yazılamadığı sırada istek henüz gönderilmedi; bu
                # rezervasyon güvenle kaldırılabilir.
                self.ledger.release(reservation)
                raise
        try:
            async with asyncio.timeout(timeout):
                completion = await self._providers[alias].complete(
                    messages, tools, timeout=timeout
                )
        except TimeoutError:
            error = RetryableProviderError("Sağlayıcı denemesi zaman aşımına uğradı.")
            self._emit_audit(
                "provider_call",
                provider=model.provider,
                model=model.model,
                success=False,
                error_type=type(error).__name__,
                elapsed_seconds=self._clock() - started,
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                retained_reservation_usd=str(amount),
            )
            raise error from None
        except (ProviderError, asyncio.CancelledError) as error:
            # Sunucu isteği işlemiş olabilir. Bilinmeyen harcamayı sıfır saymak
            # tekrar denemelerin günlük tavanı aşmasına yol açar.
            try:
                self._emit_audit(
                    "provider_call",
                    provider=model.provider,
                    model=model.model,
                    success=False,
                    error_type=type(error).__name__,
                    elapsed_seconds=self._clock() - started,
                    input_tokens=None,
                    output_tokens=None,
                    cost_usd=None,
                    retained_reservation_usd=str(amount),
                )
            except ProviderError:
                if isinstance(error, asyncio.CancelledError):
                    error.add_note("Denetim kaydı yazılamadı (OSError).")
                    raise error from None
                raise
            raise
        # Tamamlanan çağrının maliyeti kayıt yazılamasa bile rezervasyondan çıkarılır.
        try:
            self.ledger.settle(reservation, completion.usage.cost_usd)
        except ProviderError:
            raise
        try:
            self._emit_audit(
                "provider_call",
                provider=model.provider,
                model=model.model,
                success=True,
                elapsed_seconds=self._clock() - started,
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
                cost_usd=str(completion.usage.cost_usd),
                local_fallback=local_fallback,
            )
        except OSError:
            raise ProviderError("Sağlayıcı tamamlandı ancak denetim kaydı yazılamadı.") from None
        return completion.model_copy(update={"local_fallback": local_fallback})

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        timeout: float,
    ) -> Completion:
        started = self._clock()
        fallback = self.settings.fallback_model
        attempt_timeout = min(timeout, self.settings.provider_timeout_seconds)
        if fallback and started < self._fallback_until:
            return await self._call(
                fallback, messages, tools, timeout=attempt_timeout, local_fallback=True
            )
        try:
            return await self._call(
                self.settings.primary_model,
                messages,
                tools,
                timeout=attempt_timeout,
                local_fallback=False,
            )
        except RetryableProviderError:
            if fallback is None or fallback == self.settings.primary_model:
                raise
            self._fallback_until = self._clock() + self.settings.fallback_seconds
            self._emit_audit(
                "provider_fallback",
                primary=self.settings.primary_model,
                fallback=fallback,
                seconds=self.settings.fallback_seconds,
                reason="connection_timeout_or_server_error",
            )
            remaining = min(
                timeout - (self._clock() - started), self.settings.provider_timeout_seconds
            )
            return await self._call(
                fallback, messages, tools, timeout=remaining, local_fallback=True
            )
