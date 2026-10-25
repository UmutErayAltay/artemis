"""Sağlayıcıdan kanala metin akışı sözleşmesi (ADR 0043).

Akışa yalnız kullanıcıya gösterilecek metin girer. Araç çağrısı parçaları burada hiç
taşınmaz: sağlayıcı yalnız "bu adımda araç çağrısı başladı" der, çağrının kendisi
yine tam ``Completion`` olarak ayrıştırılıp doğrulanır ve SafetyGate'ten geçer.
Callback'ler senkron ve aynı event loop'tadır; uzun iş yapmamalı, fırlatmamalıdır.
"""

from __future__ import annotations

from typing import Protocol


class TextStream(Protocol):
    """Tek bir model çağrısının (adımın) akışı; sağlayıcı bunu görür."""

    def text(self, delta: str) -> None: ...

    def tool_call(self) -> None: ...


class ResponseStream(TextStream, Protocol):
    """Bütün turun akışı; AgentLoop adım sınırlarını da bildirir."""

    def step_start(self) -> None: ...

    def step_end(self, has_tool_calls: bool) -> None: ...


class EmissionGuard:
    """Router için: ilk metin dışarı çıktı mı? Çıktıysa yedek modele geçilmez."""

    def __init__(self, target: TextStream) -> None:
        self.target = target
        self.emitted = False

    def text(self, delta: str) -> None:
        if delta:
            self.emitted = True
            self.target.text(delta)

    def tool_call(self) -> None:
        self.target.tool_call()
