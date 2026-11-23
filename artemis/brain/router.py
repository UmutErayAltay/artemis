"""Bulut arızası, gizlilik kararı ve harcama aynı sağlayıcı sınırında denetlenir."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from decimal import Decimal

from artemis.brain.budget import BudgetLedger
from artemis.brain.errors import (
    BudgetExceeded,
    ProviderError,
    RateLimitedError,
    RetryableProviderError,
)
from artemis.brain.models import Completion, Message
from artemis.brain.providers import (
    AnthropicProvider,
    ClaudeCliProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    OpenRouterProvider,
    Provider,
    _check_paid,
    _endpoint_key,
    _key,
)
from artemis.brain.streaming import EmissionGuard, TextStream
from artemis.config.settings import ModelConfig, Settings
from artemis.config.work_profile import is_local_only_profile
from artemis.i18n import t
from artemis.safety.audit import AuditLog
from artemis.tools.base import ToolDefinition

_AUDIO_CAPABLE_PROVIDERS = frozenset({"openai_compatible", "openrouter"})
_AUDIO_REJECT_CODES = ("HTTP 400", "HTTP 415", "HTTP 422")


def _strip_audio(messages: list[Message]) -> list[Message]:
    return [
        m.model_copy(update={"audio_wav": None}) if m.audio_wav is not None else m
        for m in messages
    ]


def _is_audio_rejection(error: ProviderError) -> bool:
    return not isinstance(error, RetryableProviderError) and any(
        code in str(error) for code in _AUDIO_REJECT_CODES
    )


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
        profile_getter: Callable[[], str] | None = None,
    ) -> None:
        if not settings.primary_model or settings.primary_model not in settings.models:
            raise ProviderError("Birincil model config içinde tanımlanmalı.")
        self.settings = settings
        self.audit = audit
        self.ledger = BudgetLedger(settings.state_dir / "budget.sqlite3", settings.daily_budget_usd)
        self._clock = clock
        self._profile_getter = profile_getter
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
                elif model.provider == "claude_cli":
                    self._providers[alias] = ClaudeCliProvider(settings, model)
                else:
                    self._providers[alias] = OllamaProvider(settings, model)

    def _local(self, alias: str) -> bool:
        """Yerel yedek: tam (maskesiz) içeriği alır, tur tek araçla sınırlanır (ADR 0055).
        Bulut yedeğe gizli içerik zaten maskeli gider; o tur normal sınırlarla sürer."""
        return self.settings.models[alias].provider == "ollama"

    def set_profile_getter(self, getter: Callable[[], str] | None) -> None:
        """Çalışma anında canlı çalışma profili okuyucusunu bağlar (ADR 0083)."""
        self._profile_getter = getter

    def _active_profile(self) -> str:
        if self._profile_getter is not None:
            return self._profile_getter()
        return self.settings.work_profile

    def _local_fallback_alias(self) -> str | None:
        for candidate in (
            self.settings.primary_model,
            self.settings.fallback_model,
            *self.settings.models,
        ):
            if not candidate or candidate not in self._providers:
                continue
            model = self.settings.models.get(candidate)
            if model is not None and model.provider == "ollama" and model.kind == "chat":
                return candidate
        return None

    def _guard_private_alias(self, alias: str) -> str:
        """Gizli profilde bulut model isteğini engeller; yerel (ollama) varsa ona düşer."""
        if not is_local_only_profile(self._active_profile()):
            return alias
        model = self.settings.models.get(alias)
        if model is not None and model.provider == "ollama":
            return alias
        local_alias = self._local_fallback_alias()
        self._emit_audit(
            "cloud_blocked_by_profile",
            profile="private",
            requested_alias=alias,
            provider=model.provider if model is not None else "unknown",
            model=model.model if model is not None else alias,
            fallback_alias=local_alias,
        )
        if local_alias is None:
            raise ProviderError(
                t(
                    "Gizli profil etkin: bulut modeline istek engellendi; "
                    "yerel (ollama) model gerekli."
                )
            )
        return local_alias

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
        stream: TextStream | None = None,
    ) -> Completion:
        alias = self._guard_private_alias(alias)
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
        has_audio = any(m.audio_wav is not None for m in messages)
        if has_audio and model.provider not in _AUDIO_CAPABLE_PROVIDERS:
            messages = _strip_audio(messages)
            self._emit_audit(
                "audio_input_fallback",
                provider=model.provider,
                model=model.model,
                reason="unsupported_provider",
            )
            has_audio = False
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
                    **({"has_audio": True} if has_audio else {}),
                )
            except ProviderError:
                # Kaydın yazılamadığı sırada istek henüz gönderilmedi; bu
                # rezervasyon güvenle kaldırılabilir.
                self.ledger.release(reservation)
                raise

        async def _invoke(
            msgs: list[Message], call_stream: TextStream | None, call_timeout: float
        ) -> Completion:
            if call_stream is None:
                return await self._providers[alias].complete(
                    msgs, tools, timeout=call_timeout
                )
            return await self._providers[alias].complete(
                msgs, tools, timeout=call_timeout, stream=call_stream
            )

        try:
            async with asyncio.timeout(timeout):
                if not has_audio:
                    completion = await _invoke(messages, stream, timeout)
                else:
                    audio_guard = EmissionGuard(stream) if stream is not None else None
                    try:
                        completion = await _invoke(messages, audio_guard, timeout)
                    except ProviderError as audio_error:
                        if not _is_audio_rejection(audio_error) or (
                            audio_guard is not None and audio_guard.emitted
                        ):
                            raise
                        self._emit_audit(
                            "audio_input_fallback",
                            provider=model.provider,
                            model=model.model,
                            reason="rejected_audio_input",
                        )
                        remaining = timeout - (self._clock() - started)
                        if remaining <= 0:
                            raise TimeoutError from None
                        completion = await _invoke(_strip_audio(messages), stream, remaining)
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
                cached_input_tokens=completion.usage.cached_input_tokens,
                cost_usd=str(completion.usage.cost_usd),
                local_fallback=local_fallback,
            )
        except OSError:
            raise ProviderError("Sağlayıcı tamamlandı ancak denetim kaydı yazılamadı.") from None
        # Gizlilik kökeni rolden ayrı: zor işler/ana model de yerel olabilir (ADR 0056).
        return completion.model_copy(
            update={"local_fallback": local_fallback, "local_model": self._local(alias)}
        )

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        timeout: float,
        stream: TextStream | None = None,
        model: str | None = None,
    ) -> Completion:
        started = self._clock()
        guard = EmissionGuard(stream) if stream is not None else None
        if is_local_only_profile(self._active_profile()):
            requested = (
                model
                if (
                    model
                    and model != self.settings.primary_model
                    and model in self.settings.models
                )
                else self.settings.primary_model
            )
            target = self._guard_private_alias(requested)
            return await self._call(
                target,
                messages,
                tools,
                timeout=min(timeout, self.settings.provider_timeout_seconds),
                local_fallback=False,
                stream=guard,
            )
        if model and model != self.settings.primary_model and model in self.settings.models:
            # Model kademesi: zor işler modeli denenir; ücret izni/bütçe/sağlayıcı hatası
            # kullanıcıyı durdurmaz, tur ana modelle (aşağıdaki normal yol) sürer.
            try:
                return await self._call(
                    model, messages, tools,
                    timeout=min(timeout, self.settings.provider_timeout_seconds),
                    local_fallback=False, stream=guard,
                )
            except (BudgetExceeded, ProviderError) as error:
                if guard is not None and guard.emitted:
                    raise  # yarım akış üstüne ikinci bir yanıt üretilmez (ADR 0043)
                if (
                    self._local(model)
                    and not self._local(self.settings.primary_model)
                    and any(message.private for message in messages)
                ):
                    raise
                self._emit_audit(
                    "model_tier_fallback", model=model, primary=self.settings.primary_model,
                    reason=type(error).__name__,
                )
                timeout -= self._clock() - started
                started = self._clock()
        fallback = self.settings.fallback_model
        attempt_timeout = min(timeout, self.settings.provider_timeout_seconds)
        if fallback and started < self._fallback_until:
            return await self._call(
                fallback, messages, tools, timeout=attempt_timeout,
                local_fallback=self._local(fallback),
                stream=guard,
            )
        try:
            return await self._call(
                self.settings.primary_model,
                messages,
                tools,
                timeout=attempt_timeout,
                local_fallback=False,
                stream=guard,
            )
        except RetryableProviderError as error:
            if fallback is None or fallback == self.settings.primary_model:
                raise
            # ADR 0043: metin kullanıcıya akmaya başladıysa yedek model ikinci, farklı bir
            # yanıt üretirdi; tur sağlayıcı hatasıyla biter, yarım yanıt dürüstçe kalır.
            if guard is not None and guard.emitted:
                self._emit_audit(
                    "provider_fallback_skipped",
                    primary=self.settings.primary_model,
                    reason="stream_started",
                )
                raise
            # Yalnızca hız sınırı (429) birincil modeli bir süre atlamayı haklı kılar;
            # bağlantı/5xx hataları sadece bu tur için yedeğe düşer (errors.py).
            if isinstance(error, RateLimitedError):
                self._fallback_until = self._clock() + self.settings.fallback_seconds
            self._emit_audit(
                "provider_fallback",
                primary=self.settings.primary_model,
                fallback=fallback,
                seconds=self.settings.fallback_seconds,
                reason=(
                    "rate_limit"
                    if isinstance(error, RateLimitedError)
                    else "connection_timeout_or_server_error"
                ),
            )
            remaining = min(
                timeout - (self._clock() - started), self.settings.provider_timeout_seconds
            )
            return await self._call(
                fallback, messages, tools, timeout=remaining,
                local_fallback=self._local(fallback), stream=guard,
            )
