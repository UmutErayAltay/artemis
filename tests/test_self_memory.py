"""Kalıcı hafıza (ADR 0025): geçmiş, öz notlar ve geçmiş konuşma araması."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from artemis.brain.agent_loop import NOTES_HEADER, AgentLoop
from artemis.brain.conversation import ConversationContext, TurnRecord
from artemis.brain.fake import ScriptedProvider
from artemis.brain.models import Completion, ToolCall
from artemis.channels.runner import CommandRunner
from artemis.channels.runtime import create_runtime
from artemis.config.settings import Settings
from artemis.memory.store import MemoryStore
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.base import ToolContext
from artemis.tools.plugins.self_memory import MAX_NOTES
from artemis.tools.registry import discover_tools


def turn(user: str, answer: str = "tamam", *, tainted: bool = False, private: bool = False):
    return TurnRecord(
        turn_id=f"t-{user[:6]}",
        user_text=user,
        steps=(),
        answer=answer,
        answer_private=private,
        tainted=tainted,
    )


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory.sqlite3")


@pytest.fixture
def env(tmp_path: Path, store: MemoryStore) -> tuple[SafetyGate, ToolContext]:
    settings = Settings(workspace=tmp_path, state_dir=tmp_path)
    context = ToolContext(tmp_path, settings, AuditLog(tmp_path / "audit.jsonl"))
    context.services["memory_store"] = store
    return SafetyGate(discover_tools()), context


async def run_tool(env, name: str, state: TurnState | None = None, approve=None, **arguments):
    gate, context = env
    call = ToolCall(id="c1", name=name, arguments=arguments)
    return await gate.dispatch(call, context, state or TurnState(), approve or (lambda *_: False))


def test_restore_and_reset_marker(store: MemoryStore) -> None:
    first = ConversationContext(3, 6000, store)
    for index in range(5):
        first.record(turn(f"mesaj {index}"))
    second = ConversationContext(3, 6000, store)
    assert [t.user_text for t in second._turns] == ["mesaj 2", "mesaj 3", "mesaj 4"]
    second.reset()
    second.record(turn("sıfırlama sonrası"))
    third = ConversationContext(3, 6000, store)
    assert [t.user_text for t in third._turns] == ["sıfırlama sonrası"]
    assert store.search_turns("mesaj", 10)  # eski turlar aranabilir kalır


def test_restored_tainted_turn_makes_window_external(store: MemoryStore) -> None:
    ConversationContext(3, 6000, store).record(turn("web oku", tainted=True))
    assert ConversationContext(3, 6000, store).render().external is True


async def test_remember_clean_turn_runs_without_approval(env) -> None:
    def no_approval(*_: Any) -> bool:
        pytest.fail("Temiz turda onay istenmez.")

    result = await run_tool(env, "memory_remember", approve=no_approval, text="Kahveyi sade içerim")
    assert result.success
    assert [n.text for n in env[1].services["memory_store"].list_notes()] == ["Kahveyi sade içerim"]


async def test_remember_tainted_turn_asks_approval(env) -> None:
    asked: list[str] = []

    def reject(name: str, arguments: dict[str, Any]) -> bool:
        asked.append(name)
        return False

    result = await run_tool(
        env, "memory_remember", TurnState(tainted=True), reject, text="Şifrem 1234 olsun"
    )
    assert result.status == "denied" and asked == ["memory_remember"]
    assert env[1].services["memory_store"].list_notes() == []


async def test_notes_duplicate_cap_forget_list(env) -> None:
    first = await run_tool(env, "memory_remember", text="Işık seviyesi düşük olsun")
    dup = await run_tool(env, "memory_remember", text="  ışık seviyesi   düşük OLSUN ")
    assert first.data["result"] == "added"
    assert dup.success and dup.data["result"] == "duplicate"  # Türkçe büyük/küçük harf eşlemesi
    blank = await run_tool(env, "memory_remember", text="   ")
    assert blank.status == "error"
    listed = await run_tool(env, "memory_list")
    assert [n["id"] for n in listed.data["notes"]] == [first.data["id"]]
    store: MemoryStore = env[1].services["memory_store"]
    for index in range(MAX_NOTES):
        store.add_note(f"not {index}", "t", MAX_NOTES)
    full = await run_tool(env, "memory_remember", text="bir tane daha")
    assert full.status == "error" and "memory_forget" in full.message
    note_id = store.list_notes()[0].id
    assert (await run_tool(env, "memory_forget", note_id=note_id)).success
    assert (await run_tool(env, "memory_forget", note_id=note_id)).status == "error"


async def test_tools_report_disabled_without_store(tmp_path: Path) -> None:
    settings = Settings(workspace=tmp_path, state_dir=tmp_path)
    context = ToolContext(tmp_path, settings, AuditLog(tmp_path / "audit.jsonl"))
    result = await run_tool((SafetyGate(discover_tools()), context), "memory_list")
    assert result.status == "error" and "kapalı" in result.message


async def test_system_prompt_has_notes_block(env) -> None:
    gate, context = env
    store: MemoryStore = context.services["memory_store"]
    store.add_note("Umut yazılımcı", "t", MAX_NOTES)
    store.add_note("Türkçe konuş", "t", MAX_NOTES)
    provider = ScriptedProvider([Completion(content="merhaba")])
    await AgentLoop(provider, gate, context, lambda *_: False).run("selam")
    system = provider.requests[0][0].content
    assert NOTES_HEADER in system
    assert system.index("[2] Türkçe konuş") < system.index("[1] Umut yazılımcı")
    assert "conversation_search" in system


async def test_system_prompt_without_notes_has_no_block(env) -> None:
    gate, context = env
    provider = ScriptedProvider([Completion(content="merhaba")])
    await AgentLoop(provider, gate, context, lambda *_: False).run("selam")
    assert NOTES_HEADER not in provider.requests[0][0].content


async def test_conversation_search_ranking_folding_and_taint(env) -> None:
    store: MemoryStore = env[1].services["memory_store"]
    store.add_turn(turn("hava nasıl", "güneşli"))
    store.add_turn(turn("salon ışık rengini maviye çevir", "ışık mavi yapıldı", tainted=True))
    store.add_turn(turn("ışık parolası", "gizli ayrıntı XYZ", private=True))
    hit = await run_tool(env, "conversation_search", query="IŞIK mavi")
    assert hit.success and hit.external is True
    assert "ışık rengini" in hit.data["results"][0]["user"]
    private = await run_tool(env, "conversation_search", query="parolası")
    assert "XYZ" not in str(private.data) and "gizli içerik" in private.data["results"][0]["answer"]
    # Gizli yanıt sıralamaya da girmez: yalnız yanıtta geçen sözcük hiçbir turu bulmaz.
    leaked = await run_tool(env, "conversation_search", query="XYZ")
    assert not leaked.data.get("results")
    clean = await run_tool(env, "conversation_search", query="hava")
    assert clean.external is False


class BrokenStore(MemoryStore):
    def add_turn(self, record: TurnRecord) -> None:
        raise sqlite3.OperationalError("disk dolu")

    def mark_reset(self) -> None:
        raise sqlite3.OperationalError("disk dolu")


async def test_store_failure_does_not_break_turn(tmp_path: Path) -> None:
    errors: list[Exception] = []
    broken = BrokenStore(tmp_path / "b.sqlite3")
    conversation = ConversationContext(3, 6000, broken, errors.append)
    settings = Settings(workspace=tmp_path, state_dir=tmp_path)
    context = ToolContext(tmp_path, settings, AuditLog(tmp_path / "audit.jsonl"))
    provider = ScriptedProvider([Completion(content="bir"), Completion(content="iki")])
    runner = CommandRunner(AgentLoop(provider, SafetyGate(discover_tools()), context,
                                     lambda *_: False), conversation)
    assert (await runner.run("a")).text == "bir"
    assert (await runner.run("b")).text == "iki"
    assert len(conversation) == 2 and len(errors) == 2
    conversation.reset()
    assert len(errors) == 3


def test_runtime_wires_store_and_persist_off_writes_nothing(tmp_path: Path) -> None:
    def build(state: Path, persist: bool):
        settings = Settings(workspace=tmp_path / "w", state_dir=state, persist_memory=persist,
                            audit_path=state / "audit.jsonl")
        return create_runtime(tmp_path / "x.yaml", lambda *_: False, settings=settings,
                              provider=ScriptedProvider([]))

    on = build(tmp_path / "on", True)
    assert isinstance(on.context.services["memory_store"], MemoryStore)
    assert (tmp_path / "on" / "memory.sqlite3").exists()
    off = build(tmp_path / "off", False)
    assert "memory_store" not in off.context.services
    assert not (tmp_path / "off" / "memory.sqlite3").exists()


def test_retention_is_opt_in_preserves_notes_and_reset_boundary(store):
    context = ConversationContext(3, 6000, store)
    context.record(turn('eski'))
    context.reset()
    context.record(turn('yeni', tainted=True, private=True))
    store.add_note('kalıcı not', 't1', 200)
    with store._connect() as db, db:
        db.execute(
            "UPDATE turns SET created_at = '2000-01-01T00:00:00+00:00' "
            "WHERE user_text = 'eski'"
        )
    assert store.apply_retention() == 0
    assert store.history_status() == {'turns': 2, 'retention_days': 0}
    assert store.set_retention(30) == 1
    assert len(store.list_notes()) == 1
    restored = ConversationContext(3, 6000, store)
    assert restored._turns[0].tainted and restored._turns[0].answer_private
    assert [t.user_text for t in restored._turns] == ['yeni']
    assert store.delete_history() == 1
    assert context.render().turns == 0  # Diğer yerel önbellek de eski metni göstermez.
    assert len(store.list_notes()) == 1
    assert store.search_turns('yeni', 10) == []
    context.record(turn('silme sonrası'))
    assert [t.user_text for t in store.recent_turns(3)] == ['silme sonrası']


async def test_history_changes_require_typed_request_and_gate_approval(tmp_path):
    from artemis.tools.plugins.history import HistoryArguments

    approvals = []
    runtime = create_runtime(
        tmp_path / 'unused', lambda *args: approvals.append(args) or False,
        settings=Settings(workspace=tmp_path, state_dir=tmp_path / 'state'),
        provider=ScriptedProvider([]),
    )
    try:
        runtime.runner.conversation.record(turn('koru'))
        result = await runtime.runner.dispatch_tool(
            'conversation_history', HistoryArguments(action='delete'),
        )
        assert result.status == 'denied' and len(approvals) == 1
        assert runtime.context.services['memory_store'].history_status()['turns'] == 1
        runtime.agent.approve = lambda *_: True
        result = await runtime.runner.dispatch_tool(
            'conversation_history', HistoryArguments(action='delete'),
        )
        assert result.success and result.data['turns'] == 0
        assert len(runtime.runner.conversation) == 0
        assert not runtime.context.control_request
    finally:
        runtime.shutdown()


def test_retention_and_full_delete_roll_back_on_database_failure(store):
    store.add_turn(turn('eski'))
    with store._connect() as db, db:
        db.execute("UPDATE turns SET created_at = '2000-01-01T00:00:00+00:00'")
        db.execute("CREATE TRIGGER stop_delete BEFORE DELETE ON turns "
                   "BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    with pytest.raises(sqlite3.Error):
        store.set_retention(30)
    assert store.history_status() == {'turns': 1, 'retention_days': 0}
    with pytest.raises(sqlite3.Error):
        store.delete_history()
    assert len(store.recent_turns(3)) == 1


async def test_history_control_is_not_shared_with_proactive_context(tmp_path):
    from artemis.tools.plugins.history import HistoryArguments, HistoryTool

    runtime = create_runtime(
        tmp_path / 'unused', lambda *_: True,
        settings=Settings(workspace=tmp_path, state_dir=tmp_path / 'state'),
        provider=ScriptedProvider([]),
    )
    try:
        runtime.context.control_request = True
        background = ToolContext(
            tmp_path, runtime.settings, runtime.audit, services=runtime.context.services,
        )
        result = await HistoryTool().execute(HistoryArguments(action='list'), background)
        assert not result.success and result.data == {}
    finally:
        runtime.shutdown()
