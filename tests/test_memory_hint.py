"""Ucuz model güvenceleri: kalıcı talimat ipucu ve kayıt yokken "kaydettim" düzeltmesi."""

from pathlib import Path
from typing import Any

import pytest

from artemis.brain.agent_loop import UNSAVED_NOTE, AgentLoop, memory_hint
from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import Completion, ToolCall
from artemis.memory.companion import ensure_skeleton
from artemis.safety.gate import SafetyGate
from artemis.tools.registry import discover_tools
from tests.test_companion import make_context


def _loop(tmp_path: Path, responses: list[Completion]) -> tuple[AgentLoop, ScriptedProvider]:
    context = make_context(tmp_path)
    ensure_skeleton(context.settings.state_dir)
    provider = ScriptedProvider(responses)
    loop = AgentLoop(provider, SafetyGate(discover_tools()), context, lambda *_: True)
    return loop, provider


@pytest.mark.parametrize(
    ("text", "tool"),
    [
        ("bundan sonra emoji kullanma", "memory_rule_add"),
        ("Artık madde işareti kullanma", "memory_rule_add"),
        ("ben Ankara'da yaşıyorum", "memory_remember"),
        ("merhaba", None),
        ("benzin fiyatları ne durumda", None),
    ],
)
def test_memory_hint_patterns(text: str, tool: str | None) -> None:
    hint = memory_hint(text)
    assert (tool in hint) if tool else hint == ""


async def test_hint_rides_on_user_message_not_system_prompt(tmp_path: Path) -> None:
    # İpucu tura özeldir; sistem istemine girerse her turda önbellek bozulur.
    loop, provider = _loop(tmp_path, [Completion(content="Tamam.")])
    await loop.run("bundan sonra kısa yaz")
    system, user = provider.requests[0][0].content, provider.requests[0][-1].content
    assert "kalıcı bir talimat olabilir" in user
    assert "kalıcı bir talimat olabilir" not in system


async def test_unsaved_claim_gets_honest_note(tmp_path: Path) -> None:
    loop, _ = _loop(tmp_path, [Completion(content="Ankara'da yaşadığını not ettim.")])
    result = await loop.run("ben Ankara'da yaşıyorum")
    assert result.text.endswith(UNSAVED_NOTE)


async def test_saved_claim_is_left_alone(tmp_path: Path) -> None:
    write = ToolCall(
        id="c1", name="memory_session_update", arguments={"isler": "# Açık işler\n- README"}
    )
    loop, _ = _loop(
        tmp_path,
        [Completion(tool_calls=[write]), Completion(content="Açık işlere kaydettim.")],
    )
    result = await loop.run("README işini açık işlere ekle")
    assert result.text == "Açık işlere kaydettim."


async def test_failed_write_still_gets_note(tmp_path: Path) -> None:
    # memory_store servisi yok: memory_remember başarısız döner, iddia düzeltilmeli.
    remember = ToolCall(id="c1", name="memory_remember", arguments={"text": "Ankara'da yaşıyor"})
    loop, _ = _loop(
        tmp_path,
        [Completion(tool_calls=[remember]), Completion(content="Kaydettim.")],
    )
    result: Any = await loop.run("ben Ankara'da yaşıyorum")
    assert result.text.endswith(UNSAVED_NOTE)


@pytest.mark.parametrize(
    ("user", "answer", "noted"),
    [
        ("bundan sonra emoji kullanma", "Tamam, bundan sonra emoji kullanmam.", True),
        ("artık madde işareti kullanma", "Tamam, madde işareti kullanmayacağım.", True),
        ("artık yeter, teşekkürler", "Tamam, görüşmek üzere. İyi çalışmalar!", False),
        ("merhaba", "Bundan sonra ne yapalım?", False),
    ],
)
def test_unsaved_rule_promise_gets_note(user: str, answer: str, noted: bool) -> None:
    from artemis.brain.agent_loop import UNSAVED_RULE_NOTE, honest_memory_claim

    assert honest_memory_claim(answer, False, user).endswith(UNSAVED_RULE_NOTE) is noted
    assert honest_memory_claim(answer, True, user) == answer


def test_questions_are_not_directives() -> None:
    from artemis.brain.agent_loop import UNSAVED_RULE_NOTE, honest_memory_claim, is_directive

    assert not is_directive("her zaman bu kadar yavaş mısın?")
    assert is_directive("bundan sonra emoji kullanma") and is_directive("artık mimari çiz")
    answer = 'Ölçemiyorum, "her zaman yavaşım" diyemem.'
    assert not honest_memory_claim(answer, False, "her zaman bu kadar yavaş mısın?").endswith(
        UNSAVED_RULE_NOTE
    )
