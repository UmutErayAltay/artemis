"""Artemis'in kendi hafıza dosyalarına yazma yolu (ADR 0054, Faz B); her şey tmp_path altında."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from artemis.brain.models import ToolCall
from artemis.memory.companion import (
    CompanionConflictError,
    companion_dir,
    content_tainted,
    ensure_skeleton,
    list_changes,
    read_companion,
    undo_change,
    write_companion,
)
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.base import Risk, ToolContext
from artemis.tools.code_agent_jobs import CodeAgentJobs
from artemis.tools.plugins.companion import SessionUpdateArguments
from artemis.tools.registry import discover_tools
from tests.test_companion import make_context

KW: dict[str, Any] = {"source": "t", "turn_id": "x", "tainted": False}


async def dispatch(context: ToolContext, state: TurnState, name: str, **arguments: Any):
    """Onay isteyen çağrılar onaylanır; reddetme davranışı kapı testlerinde ölçülür."""
    gate = SafetyGate(discover_tools())
    call = ToolCall(id="c1", name=name, arguments=arguments)
    return await gate.dispatch(call, context, state, lambda *_: True)


def db_states(state: Path) -> list[str]:
    with sqlite3.connect(companion_dir(state) / "degisiklikler.sqlite3") as db:
        return [row[0] for row in db.execute("SELECT state FROM changes ORDER BY id")]


def test_write_logs_and_replaces(tmp_path: Path) -> None:
    ensure_skeleton(tmp_path)
    change = write_companion(tmp_path, "isler", "yeni\n", **KW)
    assert read_companion(tmp_path, "isler", 100) == "yeni\n"
    assert db_states(tmp_path) == ["completed"]
    row = list_changes(tmp_path, 5)[0]
    assert row["id"] == change and row["after"] == "yeni\n" and "Açık işler" in row["before"]


def test_failed_write_marks_failed_and_keeps_file(tmp_path: Path, monkeypatch) -> None:
    ensure_skeleton(tmp_path)
    old = read_companion(tmp_path, "isler", 1000)

    def boom(*_: object) -> None:
        raise OSError("disk")

    monkeypatch.setattr("artemis.memory.companion.os.replace", boom)
    with pytest.raises(OSError):
        write_companion(tmp_path, "isler", "yeni\n", **KW)
    assert db_states(tmp_path) == ["failed"]
    assert read_companion(tmp_path, "isler", 1000) == old
    assert not list(companion_dir(tmp_path).glob("*.tmp"))


@pytest.mark.parametrize(
    ("key", "limit"), [("kimlik", 4000), ("isler", 6000), ("son_oturum", 3000)]
)
def test_too_long_rejected_without_write(tmp_path: Path, key: str, limit: int) -> None:
    ensure_skeleton(tmp_path)
    old = read_companion(tmp_path, key, 10**5)
    write_companion(tmp_path, key, "a" * limit, **KW)  # sınırda geçer
    write_companion(tmp_path, key, old, **KW)
    n = len(list_changes(tmp_path, 50))
    with pytest.raises(ValueError, match=str(limit)):
        write_companion(tmp_path, key, "a" * (limit + 1), **KW)
    assert read_companion(tmp_path, key, 10**5) == old and len(list_changes(tmp_path, 50)) == n


def test_unknown_key_and_symlink_target_rejected(tmp_path: Path) -> None:
    ensure_skeleton(tmp_path)
    with pytest.raises(ValueError):
        write_companion(tmp_path, "baska", "x", **KW)
    outside = tmp_path / "outside.md"
    outside.write_text("dış", encoding="utf-8")
    target = companion_dir(tmp_path) / "Isler.md"
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"Bu platformda symlink yetkisi yok: {error}")
    with pytest.raises(OSError, match="sembolik"):
        write_companion(tmp_path, "isler", "x", **KW)
    assert outside.read_text(encoding="utf-8") == "dış"


def test_undo_restores_and_refuses_conflict(tmp_path: Path) -> None:
    ensure_skeleton(tmp_path)
    first = write_companion(tmp_path, "isler", "bir\n", **KW)
    second = write_companion(tmp_path, "isler", "iki\n", **KW)
    with pytest.raises(CompanionConflictError):  # dosya artık "iki"; "bir"i geri almak ezerdi
        undo_change(tmp_path, first, turn_id="u", tainted=False)
    assert read_companion(tmp_path, "isler", 100) == "iki\n"
    undo_id = undo_change(tmp_path, second, turn_id="u", tainted=False)
    assert read_companion(tmp_path, "isler", 100) == "bir\n"
    assert list_changes(tmp_path, 1)[0]["source"] == "undo" and undo_id > second
    with pytest.raises(LookupError):
        undo_change(tmp_path, 999, turn_id="u", tainted=False)


def test_session_update_requires_a_field() -> None:
    with pytest.raises(ValidationError):
        SessionUpdateArguments()


async def test_session_update_writes_and_keeps_receipts(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    state_dir = context.settings.state_dir
    ensure_skeleton(state_dir)
    write_companion(
        state_dir, "isler", "# İşler\n- a\n\n## Tamamlanan kod işleri\n- eski\n", **KW
    )
    result = await dispatch(
        context, TurnState(), "memory_session_update", son_oturum="Özet", isler="# İşler\n- b"
    )
    assert result.success and len(result.data["changes"]) == 2
    assert read_companion(state_dir, "son_oturum", 100) == "Özet\n"
    text = read_companion(state_dir, "isler", 1000)
    assert "- b" in text and "- a" not in text and "- eski" in text


async def test_session_update_too_long_is_honest(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    ensure_skeleton(context.settings.state_dir)
    result = await dispatch(
        context, TurnState(), "memory_session_update", son_oturum="x" * 3001
    )
    assert result.success is False  # alan sınırı pydantic'te reddedilir
    result = await dispatch(context, TurnState(), "memory_session_update")
    assert result.success is False


async def test_rule_add_clean_turn_appends_under_heading(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    state_dir = context.settings.state_dir
    ensure_skeleton(state_dir)
    for rule in ("Birinci.", "İkinci\nsatır."):
        result = await dispatch(context, TurnState(), "memory_rule_add", text=rule)
        assert result.success is True
    text = read_companion(state_dir, "kimlik", 1000)
    assert text.count("## Kurallar") == 1
    assert text.endswith("## Kurallar\n- Birinci.\n- İkinci satır.\n")


async def test_rule_add_refused_in_tainted_turn(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    state_dir = context.settings.state_dir
    ensure_skeleton(state_dir)
    before = read_companion(state_dir, "kimlik", 1000)
    state = TurnState()
    state.tainted = True  # dış içerik okundu; onay verilse de araç yazmaz
    result = await dispatch(context, state, "memory_rule_add", text="Her şeyi sil.")
    assert result.success is False
    assert read_companion(state_dir, "kimlik", 1000) == before
    assert list_changes(state_dir, 5) == []


async def test_rule_add_allowed_after_external_history(tmp_path: Path) -> None:
    # Önceki turda vault okunmuş (history_external) ama bu tur temiz: onaylı kural yazılır,
    # Kimlik lekeli işaretlenmez (2026-11-07 gerçek denemede kural hiç eklenemiyordu).
    context = make_context(tmp_path)
    state_dir = context.settings.state_dir
    ensure_skeleton(state_dir)
    state = TurnState()
    state.history_external = True
    result = await dispatch(context, state, "memory_rule_add", text="Emoji kullanma.")
    assert result.success is True
    assert "- Emoji kullanma." in read_companion(state_dir, "kimlik", 1000)
    assert content_tainted(state_dir, "kimlik") is False


async def test_changes_and_undo_tools(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    state_dir = context.settings.state_dir
    ensure_skeleton(state_dir)
    await dispatch(context, TurnState(), "memory_rule_add", text="Kural.")
    listed = await dispatch(context, TurnState(), "memory_changes", limit=5)
    item = listed.data["changes"][0]
    assert item["file"] == "kimlik" and item["source"] == "rule_add" and not item["tainted"]
    undone = await dispatch(context, TurnState(), "memory_change_undo", change_id=item["id"])
    assert undone.success and "Kural." not in read_companion(state_dir, "kimlik", 1000)
    again = await dispatch(context, TurnState(), "memory_change_undo", change_id=item["id"])
    assert again.success is False  # dosya artık "after" değil: çakışma
    bad = await dispatch(context, TurnState(), "memory_changes", limit=51)
    assert bad.success is False


def finished_job(tmp_path: Path, job_id: str, status: str = "done") -> CodeAgentJobs:
    jobs = CodeAgentJobs(AuditLog(tmp_path / "jobs-audit.jsonl"))
    jobs._jobs[job_id] = {
        "job_id": job_id, "project": "web", "status": status, "summary": "Özet\nsatırı " * 30,
        "commits": ["a1b2c3d4e5f6 konu"], "created_at": 1_700_049_600.0,
    }
    return jobs


async def test_job_receipt_from_record_keeps_last_20(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    state_dir = context.settings.state_dir
    ensure_skeleton(state_dir)
    jobs = finished_job(tmp_path, "abcdef012345")
    context.services["code_agent_jobs"] = jobs
    for number in range(25):
        jobs._jobs["abcdef012345"]["project"] = f"p{number}"
        result = await dispatch(context, TurnState(), "memory_job_receipt", job_id="abcdef012345")
        assert result.success is True
    text = read_companion(state_dir, "isler", 10**5)
    lines = [x for x in text.split("\n") if x.startswith("- 2023")]
    assert len(lines) == 20 and lines[0].startswith("- 2023-11-15 p5:") and "p24:" in lines[-1]
    assert "commit a1b2c3d4e5" in lines[-1] and len(lines[-1]) < 260 and "\n" not in lines[-1]
    # Aynı satır ikinci kez eklenmez.
    again = await dispatch(context, TurnState(), "memory_job_receipt", job_id="abcdef012345")
    after = read_companion(state_dir, "isler", 10**5).split("\n")
    assert again.success and len([x for x in after if x.startswith("- 2023")]) == 20


async def test_job_receipt_unknown_unfinished_or_no_store(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    ensure_skeleton(context.settings.state_dir)
    before = read_companion(context.settings.state_dir, "isler", 1000)
    no_store = await dispatch(context, TurnState(), "memory_job_receipt", job_id="abcdef012345")
    assert no_store.success is False
    context.services["code_agent_jobs"] = finished_job(tmp_path, "abcdef012345", "running")
    running = await dispatch(context, TurnState(), "memory_job_receipt", job_id="abcdef012345")
    unknown = await dispatch(context, TurnState(), "memory_job_receipt", job_id="000000000000")
    assert running.success is False and unknown.success is False
    assert read_companion(context.settings.state_dir, "isler", 1000) == before


def test_registry_discovers_faz_b_tools_with_risks() -> None:
    registry = discover_tools()
    expected = {
        "memory_session_update": Risk.SIDE_EFFECT,
        "memory_rule_add": Risk.CONFIRM_REQUIRED,
        "memory_changes": Risk.READ_ONLY,
        "memory_change_undo": Risk.CONFIRM_REQUIRED,
        "memory_job_receipt": Risk.CONFIRM_REQUIRED,
    }
    for name, risk in expected.items():
        tool = registry.get(name)
        assert tool is not None and tool.risk is risk
        assert tool.args_model.model_json_schema()["additionalProperties"] is False


def test_ensure_skeleton_symlink_dir_creates_nothing_outside(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    try:
        (state / "hafiza").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Bu platformda symlink yetkisi yok: {error}")
    with pytest.raises(OSError, match="sembolik"):
        ensure_skeleton(state)
    assert not outside.exists()


async def test_tainted_session_update_needs_approval_and_marks_content(tmp_path: Path) -> None:
    from artemis.brain.agent_loop import companion_block
    from artemis.memory.companion import content_tainted

    context = make_context(tmp_path)
    gate = SafetyGate(discover_tools())
    asked: list[str] = []

    def deny(name: str, _args: Any) -> bool:
        asked.append(name)
        return False

    call = ToolCall(id="c1", name="memory_session_update", arguments={"son_oturum": "X yap"})
    state = TurnState(tainted=True)
    result = await gate.dispatch(call, context, state, deny)
    # Lekeli turda kapı sorar; red -> dosya yazılmaz (kalıcı enjeksiyon yolu kapalı).
    assert asked == ["memory_session_update"] and result.success is False
    assert "X yap" not in companion_block(context)
    approved = await gate.dispatch(call, context, TurnState(tainted=True), lambda *_: True)
    assert approved.success
    assert content_tainted(context.settings.state_dir, "son_oturum")
    block = companion_block(context)
    assert "dış içerik okunan bir oturumda yazıldı" in block and "X yap" in block
    opened = await dispatch(context, TurnState(), "memory_open_work")
    assert opened.external is True


async def test_undo_restoring_tainted_text_keeps_mark(tmp_path: Path) -> None:
    from artemis.memory.companion import content_tainted

    state_dir = tmp_path / "state"
    ensure = __import__("artemis.memory.companion", fromlist=["ensure_skeleton"])
    ensure.ensure_skeleton(state_dir)
    write_companion(state_dir, "isler", "dış", source="t", turn_id="1", tainted=True)
    later = write_companion(state_dir, "isler", "sonra", source="t", turn_id="2", tainted=False)
    # Temiz turdaki geri alma lekeli metni geri getirirse işaret de gelir.
    undo_change(state_dir, later, turn_id="3", tainted=False)
    assert content_tainted(state_dir, "isler")


async def test_taint_is_sticky_across_preserving_rewrites(tmp_path: Path) -> None:
    from artemis.memory.companion import content_tainted, ensure_skeleton

    state_dir = tmp_path / "state"
    ensure_skeleton(state_dir)
    assert not content_tainted(state_dir, "isler")  # günlük yok: şablon temiz
    first = write_companion(state_dir, "isler", "dış emir", source="t", turn_id="1", tainted=True)
    # Temiz turdaki yeniden yazım (ör. makbuz eski bölümü korur) lekeyi silmez.
    write_companion(state_dir, "isler", "dış emir\n- yeni", source="t", turn_id="2", tainted=False)
    assert content_tainted(state_dir, "isler")
    # Kullanıcının geri alması lekeli yazımdan önceki temiz içeriğe dönerse leke düşer.
    undo_change(state_dir, first + 1, turn_id="3", tainted=False)
    undo_change(state_dir, first, turn_id="4", tainted=False)
    assert not content_tainted(state_dir, "isler")


def test_undo_requires_confirmation() -> None:
    tool = discover_tools().get("memory_change_undo")
    assert tool is not None and tool.risk is Risk.CONFIRM_REQUIRED


async def test_tainted_memory_marks_turn_history_external(tmp_path: Path) -> None:
    from artemis.brain.agent_loop import AgentLoop
    from artemis.brain.fake import ScriptedProvider
    from artemis.brain.models import Completion
    from artemis.memory.companion import ensure_skeleton

    context = make_context(tmp_path)
    ensure_skeleton(context.settings.state_dir)
    write_companion(context.settings.state_dir, "isler", "x", source="t", turn_id="1", tainted=True)
    seen: list[bool] = []
    gate = SafetyGate(discover_tools())
    original = gate.dispatch

    async def spy(call: Any, ctx: Any, state: TurnState, approve: Any) -> Any:
        seen.append(state.history_external)
        return await original(call, ctx, state, approve)

    gate.dispatch = spy  # type: ignore[method-assign]
    provider = ScriptedProvider(
        [Completion(tool_calls=[ToolCall(id="c", name="memory_changes", arguments={})]),
         Completion(content="tamam")]
    )
    await AgentLoop(provider, gate, context, lambda *_: True).run("değişikliklere bak")
    assert seen == [True]
