"""Model kademesi: tur başı seçim kuralları, yönlendiricide zor model ve ana modele düşüş."""

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from artemis.brain.agent_loop import AgentLoop
from artemis.brain.models import Completion, Message, ToolCall
from artemis.brain.router import ProviderRouter
from artemis.brain.tiering import choose_model
from artemis.config.settings import ModelConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate
from artemis.tools.base import ToolContext
from artemis.tools.registry import ToolRegistry
from tests.test_router import Responses


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", uuid4().hex)  # hiçbir sunucuya gitmez


def _model(price: str = "0") -> ModelConfig:
    return ModelConfig(
        provider="openrouter", model=f"fixture-{uuid4().hex[:6]}",
        input_per_million=Decimal(price), output_per_million=Decimal(price), context_window=1024,
    )


def _settings(tmp_path: Path, *, hard: str | None = "hard", hard_price: str = "0") -> Settings:
    return Settings(
        workspace=tmp_path, state_dir=tmp_path / "state", audit_path=tmp_path / "audit.jsonl",
        models={"main": _model(), "hard": _model(hard_price)}, primary_model="main",
        hard_model=hard, max_output_tokens=100,
    )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("merhaba", ""),
        ("notlar.txt'yi oku", ""),
        ("rapor.txt'yi sil", ""),  # tek eylem: ana model
        ("bunu derin düşün ve söyle", "deep"),
        ("txt dosyalarını metin klasörüne taşı, yoksa oluştur", "multi_step"),
        ("artemis projesinde bir iş yaptırmak istiyorum", "code_job"),
    ],
)
def test_choose_model_rules(tmp_path: Path, text: str, reason: str) -> None:
    model, why = choose_model(text, [], _settings(tmp_path))
    assert (model, why) == (("hard", reason) if reason else (None, ""))


def test_code_draft_continues_on_hard_model(tmp_path: Path) -> None:
    past = [
        Message(role="user", content="artemis'te bir iş"),
        Message(role="assistant", tool_calls=[
            ToolCall(id="c", name="code_agent_project_context", arguments={})
        ]),
        Message(role="tool", content="...", tool_call_id="c"),
        Message(role="assistant", content="Görev ne?"),
    ]
    assert choose_model("README'ye kurulum bölümü", past, _settings(tmp_path))[1] == "code_job"


def test_no_hard_model_means_no_choice(tmp_path: Path) -> None:
    assert choose_model("derin düşün", [], _settings(tmp_path, hard=None)) == (None, "")


async def test_router_uses_hard_model_then_falls_back_to_main(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    main, hard = Responses(Completion(content="ana")), Responses(Completion(content="zor"))
    router = ProviderRouter(
        settings, AuditLog(settings.audit_path), providers={"main": main, "hard": hard}
    )
    assert (await router.complete([], [], timeout=10, model="hard")).content == "zor"
    # Ücretli zor model izinsizken tur durmaz: ana modelle sürer ve denetime yazılır.
    paid = _settings(tmp_path, hard_price="5")
    main = Responses(Completion(content="ana"))
    router = ProviderRouter(
        paid, AuditLog(paid.audit_path), providers={"main": main, "hard": Responses()}
    )
    assert (await router.complete([], [], timeout=10, model="hard")).content == "ana"
    events = [json.loads(line)["event"] for line in paid.audit_path.read_text().splitlines()]
    assert "model_tier_fallback" in events


async def test_agent_loop_routes_multi_step_turn_and_audits(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    main, hard = Responses(Completion(content="ana")), Responses(Completion(content="zor"))
    audit = AuditLog(settings.audit_path)
    router = ProviderRouter(settings, audit, providers={"main": main, "hard": hard})
    context = ToolContext(workspace=tmp_path, settings=settings, audit=audit)
    loop = AgentLoop(router, SafetyGate(ToolRegistry()), context, lambda *_: False)
    assert (await loop.run("merhaba")).text == "ana"
    assert (await loop.run("dosyaları taşı ve özetle")).text == "zor"
    choices = [
        json.loads(line) for line in settings.audit_path.read_text().splitlines()
        if '"model_choice"' in line
    ]
    assert [(c["model"], c["reason"]) for c in choices] == [("hard", "multi_step")]


async def test_cloud_fallback_is_not_marked_local(tmp_path: Path) -> None:
    # Arayüzden eklenen (bulut) yedek: tek araç sınırı ve "gizli içerik gördü" işareti yok;
    # yalnız yerel ollama yedeği local_fallback'tir (ADR 0055).
    from artemis.brain.errors import RetryableProviderError

    settings = _settings(tmp_path, hard=None).model_copy(update={"fallback_model": "hard"})
    main = Responses(RetryableProviderError("kota"))
    backup = Responses(Completion(content="yedek"))
    router = ProviderRouter(
        settings, AuditLog(settings.audit_path), providers={"main": main, "hard": backup}
    )
    completion = await router.complete([], [], timeout=10)
    assert (completion.content, completion.local_fallback) == ("yedek", False)
