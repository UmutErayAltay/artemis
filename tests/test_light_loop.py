"""Hafif kipte AgentLoop (ADR 0092): daraltılmış araç seti, kısa istem, onarım, dürtü."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from artemis.brain.agent_loop import AgentLoop
from artemis.brain.errors import ProviderError
from artemis.brain.models import Completion, Message, ToolCall
from artemis.config.settings import ModelConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate
from artemis.tools.base import ToolContext, ToolDefinition
from artemis.tools.registry import discover_tools


class Recorder:
    """Her isteğin mesaj ve araç tanımlarını saklayan, senaryolu sahte sağlayıcı."""

    def __init__(self, responses: list[Completion]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[list[Message], list[ToolDefinition]]] = []

    async def complete(
        self, messages: list[Message], tools: list[ToolDefinition], *, timeout: float,
        stream: Any = None,
    ) -> Completion:
        self.requests.append(([m.model_copy(deep=True) for m in messages], list(tools)))
        if not self.responses:
            raise ProviderError("senaryo bitti")
        return self.responses.pop(0)


def local_model() -> ModelConfig:
    return ModelConfig(
        provider="ollama", model="fixture-local", input_per_million=Decimal(0),
        output_per_million=Decimal(0),
    )


def cloud_model() -> ModelConfig:
    return ModelConfig(
        provider="anthropic", model="claude-fixture", input_per_million=Decimal(0),
        output_per_million=Decimal(0),
    )


def make(
    tmp_path: Path, responses: list[Completion], *, model: ModelConfig | None = None,
) -> tuple[AgentLoop, Recorder, list[tuple[str, dict[str, Any]]]]:
    settings = Settings(
        workspace=tmp_path, state_dir=tmp_path / "state", audit_path=tmp_path / "audit.jsonl",
        models={"m": model or local_model()}, primary_model="m",
    )
    context = ToolContext(
        workspace=tmp_path, settings=settings, audit=AuditLog(settings.audit_path)
    )
    approvals: list[tuple[str, dict[str, Any]]] = []

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        approvals.append((name, arguments))
        return False  # Hiçbir test gerçek yan etki üretmez.

    provider = Recorder(responses)
    loop = AgentLoop(provider, SafetyGate(discover_tools()), context, approve)
    return loop, provider, approvals


def tool_call(name: str, call_id: str = "c1", **arguments: Any) -> Completion:
    return Completion(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def audit_events(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]


async def test_light_mode_narrows_tools_and_expands_once(tmp_path: Path) -> None:
    loop, provider, _ = make(tmp_path, [
        tool_call("need_more_tools", "a"),
        tool_call("need_more_tools", "b"),
        Completion(content="bitti"),
    ])
    result = await loop.run("notlar.txt dosyasını oku")
    assert result.text == "bitti"
    first, second, third = (names for _m, names in provider.requests)
    first_names = {d.name for d in first}
    assert len(first_names) <= 17  # en çok 16 araç + need_more_tools
    assert {"read_file", "need_more_tools", "current_time"} <= first_names
    assert "power" not in first_names
    # Genişletme sonrası tam kayıt gider, yerleşik çağrı artık sunulmaz.
    assert len(second) == len(discover_tools().definitions())
    assert "need_more_tools" not in {d.name for d in second}
    assert len(third) == len(second)
    events = audit_events(tmp_path)
    assert [e["event"] for e in events].count("light_mode_expand") == 1
    started = next(e for e in events if e["event"] == "light_mode")
    assert "files" in started["groups"]
    # İkinci çağrıya kısa, zararsız yanıt döner.
    last_tool = [m for m in provider.requests[-1][0] if m.role == "tool"][-1]
    assert "zaten" in last_tool.content


async def test_out_of_set_call_is_rejected_before_the_gate(tmp_path: Path) -> None:
    loop, provider, approvals = make(tmp_path, [
        tool_call("mail_send", to="a@b.co", subject="s", body="b"),
        Completion(content="tamam"),
    ])
    await loop.run("notlar.txt dosyasını oku")
    assert approvals == []  # kapıya hiç gitmedi
    tool_msg = next(m for m in provider.requests[1][0] if m.role == "tool")
    assert "need_more_tools" in tool_msg.content
    assert "light_mode_reject" in [e["event"] for e in audit_events(tmp_path)]


async def test_no_match_sends_full_set_without_pseudo_tool(tmp_path: Path) -> None:
    loop, provider, _ = make(tmp_path, [Completion(content="merhaba")])
    await loop.run("merhaba nasılsın")
    names = {d.name for d in provider.requests[0][1]}
    assert len(names) == len(discover_tools().definitions())
    assert "need_more_tools" not in names


@pytest.mark.parametrize(
    "model", [cloud_model(), local_model().model_copy(update={"light_mode": False})]
)
async def test_light_mode_off_is_todays_request(tmp_path: Path, model: ModelConfig) -> None:
    loop, provider, _ = make(tmp_path, [Completion(content="x")], model=model)
    await loop.run("notlar.txt dosyasını oku")
    messages, tools = provider.requests[0]
    assert len(tools) == len(discover_tools().definitions())
    assert "need_more_tools" not in {d.name for d in tools}
    assert "Kodlama işi için önce proje" in messages[0].content
    assert "KURALLAR:" not in messages[0].content
    assert "light_mode" not in [e["event"] for e in audit_events(tmp_path)]


async def test_light_prompt_is_short_and_apps_only_for_app_group(tmp_path: Path) -> None:
    from artemis.brain.agent_loop import SYSTEM_PROMPT
    from artemis.brain.light_mode import LIGHT_SYSTEM_PROMPT

    loop, provider, _ = make(tmp_path, [Completion(content="a"), Completion(content="b"),
                                        Completion(content="c")])
    await loop.run("notlar.txt dosyasını oku")
    await loop.run("Spotify'ı aç")
    await loop.run("merhaba nasılsın")
    files, apps, nothing = (req[0][0].content for req in provider.requests)
    assert files.startswith(LIGHT_SYSTEM_PROMPT) or LIGHT_SYSTEM_PROMPT in files
    assert "Kodlama işi için önce proje" not in files
    assert len(files) < len(SYSTEM_PROMPT) // 2
    assert "SIKI KURALLAR" not in files
    assert "İzinli uygulama takma adları" not in files
    assert "İzinli uygulama takma adları" in apps  # uygulama grubu seçildi
    assert "İzinli uygulama takma adları" in nothing  # yönlendirme belirsiz: tam set
    assert "need_more_tools çağır" in files  # daraltılmış turda yerleşik çağrı anlatılır
    assert "need_more_tools" not in nothing
    # Zaman cümlesi korunur.
    assert "[tarih saat]" in files


async def test_light_mode_keeps_only_two_past_turns(tmp_path: Path) -> None:
    from artemis.brain.conversation import ContextWindow

    past: list[Message] = []
    for i in range(4):
        past += [Message(role="user", content=f"soru{i}"),
                 Message(role="assistant", content=f"yanıt{i}")]
    window = ContextWindow(messages=past, turns=4, shortened_outputs=0, dropped_turns=0,
                           estimated_tokens=1)
    loop, provider, _ = make(tmp_path, [Completion(content="a"), Completion(content="b")])
    await loop.run("notlar.txt dosyasını oku", window)
    sent = [m.content for m in provider.requests[0][0] if m.role == "user"]
    assert [c for c in sent if c.startswith("soru")] == ["soru2", "soru3"]

    off, provider_off, _ = make(tmp_path, [Completion(content="a")], model=cloud_model())
    await off.run("notlar.txt dosyasını oku", window)
    assert len([m for m in provider_off.requests[0][0] if m.content.startswith("soru")]) == 4


async def test_json_in_content_is_repaired_and_still_hits_the_gate(tmp_path: Path) -> None:
    content = '```json\n{"name": "delete_file", "arguments": {"path": "x.txt"}}\n```'
    loop, provider, approvals = make(tmp_path, [Completion(content=content),
                                                Completion(content="reddedildi")])
    (tmp_path / "x.txt").write_text("veri")
    result = await loop.run("x.txt dosyasını sil")
    # CONFIRM_REQUIRED araç yine onay ister; onay verilmedi, dosya yerinde.
    assert approvals == [("delete_file", {"path": "x.txt"})]
    assert (tmp_path / "x.txt").read_text() == "veri"
    assert result.tool_calls == 1
    assert "light_mode_repair" in [e["event"] for e in audit_events(tmp_path)]


async def test_bare_arguments_json_matching_one_tool_is_repaired(tmp_path: Path) -> None:
    loop, _provider, _ = make(tmp_path, [
        Completion(content='{"path": "a.txt", "content": "merhaba"}'),
        Completion(content="oluşturdum"),
    ])
    result = await loop.run("a.txt dosyası oluştur")
    assert (tmp_path / "a.txt").read_text() == "merhaba"
    assert result.text == "oluşturdum"


@pytest.mark.parametrize("content", [
    '{"path": "kayit"}',  # path alan adı birden çok araca uyar: belirsiz
    'Tamam: {"path": "a.txt", "content": "x"}',  # yalnız JSON değil
    '{"nonsense": 1}',
    "[1, 2]",
])
async def test_ambiguous_or_non_json_content_is_not_repaired(
    tmp_path: Path, content: str
) -> None:
    loop, _provider, _ = make(tmp_path, [Completion(content=content)])
    result = await loop.run("a.txt dosyası oluştur")
    assert result.text == content
    assert result.tool_calls == 0


async def test_repair_is_off_when_light_mode_is_off(tmp_path: Path) -> None:
    content = '{"path": "a.txt", "content": "x"}'
    loop, _provider, _ = make(tmp_path, [Completion(content=content)], model=cloud_model())
    result = await loop.run("a.txt dosyası oluştur")
    assert result.text == content and not (tmp_path / "a.txt").exists()


async def test_validation_hint_lists_fields_at_most_twice(tmp_path: Path) -> None:
    bad = [tool_call("read_file", f"c{i}", file=f"a{i}") for i in range(3)]
    loop, provider, _ = make(tmp_path, [*bad, Completion(content="bitti")])
    await loop.run("a.txt dosyasını oku")
    tools = [m.content for m in provider.requests[-1][0] if m.role == "tool"]
    assert len(tools) == 3
    assert "path (string, zorunlu)" in tools[0] and "path (string, zorunlu)" in tools[1]
    assert "Beklenen alanlar" not in tools[2]  # üçüncüde bugünkü kapı hatası


async def test_nudge_fires_once_for_multi_step_question(tmp_path: Path) -> None:
    from artemis.brain.light_mode import NUDGE_MESSAGE

    loop, provider, _ = make(tmp_path, [
        Completion(content="Hangi klasöre taşıyayım?"),
        Completion(content="Yine soruyorum: emin misin?"),
    ])
    result = await loop.run("txt dosyalarını metin klasörüne taşı, yoksa oluştur")
    assert len(provider.requests) == 2
    last = provider.requests[1][0][-1]
    assert (last.role, last.content) == ("user", NUDGE_MESSAGE)
    assert result.text == "Yine soruyorum: emin misin?"  # ikinci kez dürtülmez
    assert [e["event"] for e in audit_events(tmp_path)].count("light_mode_nudge") == 1


async def test_nudge_fires_for_concrete_target_with_single_action(tmp_path: Path) -> None:
    loop, provider, _ = make(tmp_path, [Completion(content="Emin misin?"),
                                        Completion(content="tamam")])
    await loop.run("rapor.txt dosyasını sil")
    assert len(provider.requests) == 2


@pytest.mark.parametrize("text", ["şunu sil", "bunu hallet"])
async def test_no_nudge_for_single_action_without_target(tmp_path: Path, text: str) -> None:
    loop, provider, _ = make(tmp_path, [Completion(content="Neyi silmemi istersin?")])
    result = await loop.run(text)
    assert len(provider.requests) == 1
    assert result.text == "Neyi silmemi istersin?"


async def test_no_nudge_when_light_mode_is_off(tmp_path: Path) -> None:
    loop, provider, _ = make(tmp_path, [Completion(content="Hangi klasör?")],
                             model=cloud_model())
    await loop.run("txt dosyalarını metin klasörüne taşı, yoksa oluştur")
    assert len(provider.requests) == 1
