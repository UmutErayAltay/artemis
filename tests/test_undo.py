"""Geri alma günlüğü gerçek geçici dosya ve SQLite veritabanıyla sınanır."""

import sqlite3
from pathlib import Path
from typing import Any, Literal

import pytest

from jarvis.brain.models import ToolCall
from jarvis.config.settings import Settings
from jarvis.safety.audit import AuditLog
from jarvis.safety.gate import SafetyGate, TurnState
from jarvis.safety.undo import UndoJournal, UndoJournalError
from jarvis.tools.base import ToolContext, ToolResult
from jarvis.tools.registry import discover_tools


@pytest.fixture
async def context(tmp_path: Path) -> ToolContext:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        workspace=workspace,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
    )
    return ToolContext(workspace, settings, AuditLog(settings.audit_path), turn_id="turn-1")


async def delete(context: ToolContext, path: str = "sample.txt") -> ToolResult:
    tool = discover_tools().get("delete_file")
    assert tool is not None
    return await tool.execute(tool.args_model.model_validate({"path": path}), context)


async def test_delete_records_metadata_and_reopens_journal(context: ToolContext) -> None:
    source = context.workspace / "sample.txt"
    source.write_bytes(b"sensitive contents")
    trash_path = context.workspace.parent / "trashed.txt"
    context.services["trash"] = lambda path: path.rename(trash_path)

    result = await delete(context)

    assert result.success
    assert not source.exists()
    records = UndoJournal(context.settings.state_dir).list_records(root=context.workspace)
    assert len(records) == 1
    record = records[0]
    assert record.status == "completed"
    assert record.turn_id == "turn-1"
    assert record.relative_path == "sample.txt"
    assert record.size == len(b"sensitive contents")
    assert "sensitive contents" not in str(record.as_dict())
    assert result.data == {
        "undo_record_id": record.record_id,
        "undo_status": "completed",
    }


async def test_failed_preparation_aborts_delete(context: ToolContext) -> None:
    source = context.workspace / "sample.txt"
    source.write_text("keep", encoding="utf-8")
    calls: list[Path] = []

    class BrokenJournal:
        def prepare(self, **kwargs: Any) -> None:
            raise UndoJournalError("private details")

    context.services["undo_journal"] = BrokenJournal()
    context.services["trash"] = calls.append
    result = await delete(context)

    assert not result.success
    assert source.exists()
    assert not calls


async def test_noop_trash_is_recorded_as_failed(context: ToolContext) -> None:
    source = context.workspace / "sample.txt"
    source.write_text("keep", encoding="utf-8")
    context.services["trash"] = lambda path: None

    result = await delete(context)

    assert not result.success
    assert source.exists()
    records = UndoJournal(context.settings.state_dir).list_records(root=context.workspace)
    assert records[0].status == "failed"


async def test_trash_exception_after_removal_is_uncertain(context: ToolContext) -> None:
    source = context.workspace / "sample.txt"
    source.write_text("maybe moved", encoding="utf-8")

    def remove_then_fail(path: Path) -> None:
        path.unlink()
        raise OSError("private OS detail")

    context.services["trash"] = remove_then_fail
    result = await delete(context)

    assert not result.success
    assert result.data["undo_status"] == "uncertain"
    assert "private OS detail" not in result.message
    records = UndoJournal(context.settings.state_dir).list_records(root=context.workspace)
    assert records[0].status == "uncertain"


async def test_terminal_journal_write_failure_reports_uncertain(context: ToolContext) -> None:
    source = context.workspace / "sample.txt"
    source.write_text("gone", encoding="utf-8")
    journal = UndoJournal(context.settings.state_dir)
    context.services["undo_journal"] = journal
    context.services["trash"] = lambda path: path.unlink()
    original_set_status = journal.set_status

    def fail_completion(
        record_id: str, status: Literal["completed", "failed", "uncertain"]
    ) -> Any:
        if status == "completed":
            raise UndoJournalError("private details")
        return original_set_status(record_id, status)

    journal.set_status = fail_completion  # type: ignore[method-assign]
    result = await delete(context)

    assert not result.success
    assert result.data["undo_status"] == "prepared"
    assert not source.exists()
    reopened = UndoJournal(context.settings.state_dir)
    assert reopened.list_records(root=context.workspace)[0].status == "prepared"
    assert result.data["undo_status"] == "prepared"


async def test_denied_delete_does_not_create_journal(context: ToolContext) -> None:
    (context.workspace / "sample.txt").write_text("keep", encoding="utf-8")
    registry = discover_tools()
    gate = SafetyGate(registry)
    call = ToolCall(id="delete", name="delete_file", arguments={"path": "sample.txt"})
    result = await gate.dispatch(call, context, TurnState(), lambda name, args: False)

    assert result.status == "denied"
    assert (context.workspace / "sample.txt").exists()
    assert not (context.settings.state_dir / "undo.sqlite3").exists()


async def test_list_is_external_tainted_and_workspace_private(context: ToolContext) -> None:
    journal = UndoJournal(context.settings.state_dir)
    path = context.workspace / "one.txt"
    path.write_text("contents", encoding="utf-8")
    journal.prepare(turn_id="turn", root=context.workspace, relative_path="one.txt", size=8)
    other_root = context.workspace.parent / "other"
    other_root.mkdir()
    journal.prepare(turn_id="other", root=other_root, relative_path="private.txt", size=7)

    registry = discover_tools()
    gate = SafetyGate(registry)
    call = ToolCall(id="list-undo", name="list_undo_records", arguments={})
    state = TurnState()
    result = await gate.dispatch(call, context, state, lambda name, args: False)

    assert result.success and result.external
    assert result.content_kind == "file"
    assert state.tainted
    assert len(result.data["records"]) == 1
    assert result.data["records"][0]["relative_path"] == "one.txt"
    assert "private.txt" not in str(result.data)
    assert "geri yükleme" in result.message.lower()
    assert "prepared" in result.message


def test_metadata_is_redacted_and_workspace_filter_uses_original_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_TEST_SECRET", "sentinel")
    root = tmp_path / "workspace-sentinel"
    root.mkdir()
    journal = UndoJournal(tmp_path / "state")
    record = journal.prepare(
        turn_id="turn-sentinel", root=root, relative_path="notes-sentinel.txt", size=3
    )

    records = journal.list_records(root=root)
    database = journal.path.read_bytes()

    assert len(records) == 1
    assert records[0].relative_path == "notes-[GİZLENDİ].txt"
    assert "sentinel" not in repr(record)
    assert "sentinel" not in str(record.as_dict())
    assert b"sentinel" not in database
    assert "workspace_key" not in record.as_dict()
    assert "root" not in record.as_dict()


def test_terminal_status_is_idempotent_and_cannot_be_rewritten(
    context: ToolContext,
) -> None:
    journal = UndoJournal(context.settings.state_dir)
    record = journal.prepare(
        turn_id="turn-1", root=context.workspace, relative_path="a.txt", size=1
    )

    completed = journal.set_status(record.record_id, "completed")
    repeated = journal.set_status(record.record_id, "completed")
    assert repeated.status == completed.status == "completed"
    with pytest.raises(UndoJournalError):
        journal.set_status(record.record_id, "failed")


async def test_chmod_failure_aborts_delete(
    context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = context.workspace / "sample.txt"
    source.write_text("keep", encoding="utf-8")
    calls: list[Path] = []

    def fail_chmod(path: Path, mode: int) -> None:
        raise PermissionError("private path")

    monkeypatch.setattr(Path, "chmod", fail_chmod)
    context.services["trash"] = calls.append
    result = await delete(context)

    assert not result.success
    assert source.exists()
    assert calls == []
    assert "private path" not in result.message


def test_database_rejects_invalid_status(context: ToolContext) -> None:
    journal = UndoJournal(context.settings.state_dir)
    record = journal.prepare(
        turn_id="turn-1", root=context.workspace, relative_path="a.txt", size=1
    )

    with pytest.raises(sqlite3.IntegrityError), sqlite3.connect(journal.path) as connection:
        connection.execute(
            "UPDATE undo_records SET status = 'deleted' WHERE record_id = ?",
            (record.record_id,),
        )
