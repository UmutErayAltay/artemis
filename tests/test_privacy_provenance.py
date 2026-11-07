"""Gizlilik kökeni (ADR 0056): yerel modelin gördüğü, companion/not hafızası ve değişiklik
geçmişi yoluyla gizli içerik buluta maskesiz gitmemeli.

Hiçbir test ağa çıkmaz: sağlayıcılar senaryolu sahtelerdir; "buluta giden" mesajlar bulut
sağlayıcılarının HTTP sınırında kullandığı `_cloud_messages` maskesinden geçirilip incelenir.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from artemis.brain.agent_loop import AgentLoop
from artemis.brain.conversation import ConversationContext, TurnRecord
from artemis.brain.models import Completion, Message, ToolCall
from artemis.brain.providers import _cloud_messages
from artemis.brain.router import ProviderRouter
from artemis.config.settings import ModelConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate
from artemis.tools.base import ToolContext

MARKER = f"GIZLI-{uuid4().hex[:10]}"  # her koşuda benzersiz özel işaret


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", uuid4().hex)  # hiçbir sunucuya gitmez


class Scripted:
    """Router'ın çağırdığı sağlayıcı; gördüğü mesajları kaydeder, akışı da besler."""

    def __init__(self, *responses: Completion) -> None:
        self.responses = list(responses)
        self.seen: list[list[Message]] = []

    async def complete(
        self, messages: list[Message], tools: list[Any], *, timeout: float, stream: Any = None
    ) -> Completion:
        self.seen.append([message.model_copy(deep=True) for message in messages])
        completion = self.responses.pop(0)
        if stream is not None and completion.content:
            stream.text(completion.content)
        return completion


class Stream:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def text(self, delta: str) -> None:
        self.parts.append(delta)

    def tool_call(self) -> None:
        pass

    def step_start(self) -> None:
        pass

    def step_end(self, has_tool_calls: bool) -> None:
        pass


def _cloud(alias: str) -> ModelConfig:
    return ModelConfig(
        provider="openrouter", model=f"fixture-{alias}", input_per_million=Decimal("0"),
        output_per_million=Decimal("0"), context_window=4096,
    )


def _local(alias: str) -> ModelConfig:
    return ModelConfig(
        provider="ollama", model=f"fixture-{alias}", input_per_million=Decimal("0"),
        output_per_million=Decimal("0"),
    )


def _leaks(messages: list[Message]) -> bool:
    """Bulut sağlayıcısının HTTP sınırında maskelenmiş hâlde özel işaret hâlâ var mı?"""
    for message in _cloud_messages(messages):
        if MARKER in message.content:
            return True
        if any(MARKER in str(call.arguments) for call in message.tool_calls):
            return True
    return False


# --- Bulgu 2: yerel "zor işler" modelinin yanıtı ---------------------------------------


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("local_role", ["hard", "primary"])
async def test_local_model_answer_from_private_history_stays_private(
    tmp_path: Path, streamed: bool, local_role: str
) -> None:
    # local_role=hard: bulut ana + Ollama zor işler modeli (bildirilen yol).
    # local_role=primary: Ollama ana + bulut zor işler modeli (aynı kusurun ters yönü).
    if local_role == "hard":
        models = {"main": _cloud("main"), "hard": _local("hard")}
    else:
        models = {"main": _local("main"), "hard": _cloud("hard")}
    settings = Settings(
        workspace=tmp_path, state_dir=tmp_path / "state", audit_path=tmp_path / "audit.jsonl",
        models=models, primary_model="main", hard_model="hard", max_output_tokens=100,
    )
    echo = Completion(content=f"The secret is {MARKER}")
    plain = Completion(content="tamam")
    if local_role == "hard":
        local_first, cloud_next = "hard", "main"
        texts = ("bunu derin düşün", "merhaba")
    else:
        local_first, cloud_next = "main", "hard"
        texts = ("merhaba", "bunu derin düşün")
    providers = {local_first: Scripted(echo), cloud_next: Scripted(plain)}
    audit = AuditLog(settings.audit_path)
    router = ProviderRouter(settings, audit, providers=providers)
    context = ToolContext(workspace=tmp_path, settings=settings, audit=audit)
    loop = AgentLoop(router, SafetyGate(_empty_registry()), context, lambda *_: False)

    conversation = ConversationContext(max_turns=10, token_budget=50_000)
    conversation.record(
        TurnRecord(
            turn_id="t0", user_text="dosyayı oku", steps=(),
            answer=f"Dosyada {MARKER} yazıyor", answer_private=True, tainted=False,
        )
    )
    first = await loop.run(
        texts[0], conversation.render(), stream=Stream() if streamed else None
    )
    assert MARKER in first.text
    assert first.record is not None and first.record.answer_private is True
    conversation.record(first.record)

    second = await loop.run(texts[1], conversation.render())
    assert second.text == "tamam"
    cloud_request = providers[cloud_next].seen[-1]
    assert any(MARKER in message.content for message in cloud_request)  # geçmişte duruyor
    assert not _leaks(cloud_request)  # ama buluta maskeli gidiyor


async def test_local_tool_arguments_after_private_history_are_private(tmp_path: Path) -> None:
    # Yerel modelin gizli geçmişten türettiği araç argümanı da maskeli kalmalı.
    settings = Settings(
        workspace=tmp_path, state_dir=tmp_path / "state", audit_path=tmp_path / "audit.jsonl",
        models={"main": _cloud("main"), "hard": _local("hard")}, primary_model="main",
        hard_model="hard", max_output_tokens=100,
    )
    call = ToolCall(id="c1", name="olmayan_arac", arguments={"metin": MARKER})
    hard = Scripted(Completion(tool_calls=[call]), Completion(content="bitti"))
    main = Scripted(Completion(content="tamam"))
    audit = AuditLog(settings.audit_path)
    router = ProviderRouter(settings, audit, providers={"main": main, "hard": hard})
    context = ToolContext(workspace=tmp_path, settings=settings, audit=audit)
    loop = AgentLoop(router, SafetyGate(_empty_registry()), context, lambda *_: False)
    conversation = ConversationContext(max_turns=10, token_budget=50_000)
    conversation.record(
        TurnRecord(
            turn_id="t0", user_text="dosyayı oku", steps=(), answer=f"içerik: {MARKER}",
            answer_private=True, tainted=False,
        )
    )
    first = await loop.run("bunu derin düşün", conversation.render())
    assert first.record is not None
    conversation.record(first.record)
    await loop.run("merhaba", conversation.render())
    assert not _leaks(main.seen[-1])


def _empty_registry() -> Any:
    from artemis.tools.registry import ToolRegistry

    return ToolRegistry()
