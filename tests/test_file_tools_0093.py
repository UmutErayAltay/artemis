"""ADR 0093: copy_file, move_files, yol kuralı. Tüm dosyalar tmp_path altında."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from artemis.brain.models import ToolCall
from artemis.config.settings import Settings
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.base import ToolContext, ToolResult
from artemis.tools.plugins import filesystem
from artemis.tools.registry import discover_tools


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    workspace = tmp_path / "Masaüstü"
    workspace.mkdir()
    settings = Settings(
        workspace=workspace, state_dir=tmp_path / "state", audit_path=tmp_path / "audit.jsonl"
    )
    return ToolContext(workspace=workspace, settings=settings, audit=AuditLog(settings.audit_path))


async def run(ctx: ToolContext, name: str, **arguments: Any) -> ToolResult:
    tool = discover_tools().get(name)
    assert tool is not None
    return await tool.execute(tool.args_model.model_validate(arguments), ctx)


# --- D1 copy_file ---


async def test_copy_file_copies_and_keeps_source(ctx: ToolContext) -> None:
    root = ctx.workspace
    (root / "a.txt").write_bytes(b"abc\r\n\x00")
    (root / "yedek").mkdir()
    result = await run(ctx, "copy_file", source="a.txt", destination="yedek/a.txt")
    assert result.success
    assert (root / "yedek/a.txt").read_bytes() == b"abc\r\n\x00" and (root / "a.txt").exists()


async def test_copy_file_refuses_overwrite_and_missing_parent(ctx: ToolContext) -> None:
    root = ctx.workspace
    (root / "a.txt").write_text("yeni", encoding="utf-8")
    (root / "b.txt").write_text("eski", encoding="utf-8")
    exists = await run(ctx, "copy_file", source="a.txt", destination="b.txt")
    assert not exists.success and (root / "b.txt").read_text(encoding="utf-8") == "eski"
    nodir = await run(ctx, "copy_file", source="a.txt", destination="yok/a.txt")
    assert not nodir.success and "create_directory" in nodir.message
    assert not (root / "yok").exists()


async def test_copy_file_rejects_directory_escape_and_size(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ctx.workspace
    (root / "d").mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    assert not (await run(ctx, "copy_file", source="d", destination="e")).success
    assert not (await run(ctx, "copy_file", source="a.txt", destination="../o.txt")).success
    monkeypatch.setattr(filesystem, "MAX_COPY_BYTES", 0)
    big = await run(ctx, "copy_file", source="a.txt", destination="b.txt")
    assert not big.success and not (root / "b.txt").exists()


async def test_copy_file_fails_when_size_differs(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    (ctx.workspace / "a.txt").write_text("abc", encoding="utf-8")
    monkeypatch.setattr(filesystem.shutil, "copyfile", lambda s, d: Path(d).write_text("a"))
    assert not (await run(ctx, "copy_file", source="a.txt", destination="b.txt")).success


# --- D2 move_files ---


def _seed(root: Path) -> None:
    (root / "src").mkdir()
    (root / "dst").mkdir()
    for name in ("a.txt", "b.txt", "c.md"):
        (root / "src" / name).write_text(name, encoding="utf-8")


async def test_move_files_by_pattern(ctx: ToolContext) -> None:
    root = ctx.workspace
    _seed(root)
    result = await run(ctx, "move_files", source_dir="src", destination_dir="dst", pattern="*.txt")
    assert result.success and result.status == "ok"
    assert {row["name"]: row["status"] for row in result.data["files"]} == {
        "a.txt": "moved",
        "b.txt": "moved",
    }
    assert sorted(p.name for p in (root / "dst").iterdir()) == ["a.txt", "b.txt"]
    assert (root / "src" / "c.md").exists()


async def test_move_files_by_names_and_root_source(ctx: ToolContext) -> None:
    root = ctx.workspace
    (root / "dst").mkdir()
    (root / "x.txt").write_text("x", encoding="utf-8")
    result = await run(ctx, "move_files", destination_dir="dst", names=["x.txt"])
    assert result.success and (root / "dst/x.txt").exists() and not (root / "x.txt").exists()


async def test_move_files_no_match_and_missing_destination(ctx: ToolContext) -> None:
    root = ctx.workspace
    _seed(root)
    none = await run(ctx, "move_files", source_dir="src", destination_dir="dst", pattern="*.zip")
    assert not none.success and none.status == "error" and "taşınmadı" in none.message
    nodst = await run(ctx, "move_files", source_dir="src", destination_dir="yok", pattern="*")
    assert not nodst.success and "create_directory" in nodst.message
    assert len(list((root / "src").iterdir())) == 3


async def test_move_files_partial_is_honest_and_never_overwrites(ctx: ToolContext) -> None:
    root = ctx.workspace
    _seed(root)
    (root / "dst" / "a.txt").write_text("KEEP", encoding="utf-8")
    result = await run(
        ctx,
        "move_files",
        source_dir="src",
        destination_dir="dst",
        names=["a.txt", "b.txt", "z.txt"],
    )
    assert not result.success and result.status == "partial"
    status = {row["name"]: row["status"] for row in result.data["files"]}
    assert status == {"a.txt": "skipped_exists", "b.txt": "moved", "z.txt": "failed"}
    assert (root / "dst/a.txt").read_text(encoding="utf-8") == "KEEP"
    assert (root / "src/a.txt").exists()
    nothing = await run(ctx, "move_files", source_dir="src", destination_dir="dst", names=["a.txt"])
    assert not nothing.success and nothing.status == "error"


@pytest.mark.parametrize(
    "arguments",
    [
        {"names": ["../x.txt"]},
        {"names": ["a/b.txt"]},
        {"names": ["a\\b.txt"]},
        {"pattern": "../*"},
        {"pattern": "sub/*.txt"},
        {"pattern": "x" * 65},
        {"names": []},
        {"names": [f"{i}.txt" for i in range(51)]},
        {},
        {"names": ["a.txt"], "pattern": "*"},
    ],
)
def test_move_files_rejects_bad_selection(arguments: dict[str, Any]) -> None:
    tool = discover_tools().get("move_files")
    assert tool is not None
    with pytest.raises(ValidationError):
        tool.args_model.model_validate({"source_dir": "s", "destination_dir": "d", **arguments})


async def test_move_files_rejects_escaping_directories(ctx: ToolContext) -> None:
    _seed(ctx.workspace)
    for bad in ("../x", "C:\\x", "/etc"):
        r = await run(ctx, "move_files", source_dir=bad, destination_dir="dst", pattern="*")
        assert not r.success
        r = await run(ctx, "move_files", source_dir="src", destination_dir=bad, pattern="*")
        assert not r.success
    assert len(list((ctx.workspace / "src").iterdir())) == 3


async def test_move_files_symlink_escape_rejected(ctx: ToolContext, tmp_path: Path) -> None:
    root = ctx.workspace
    _seed(root)
    outside = tmp_path / "disarda.txt"
    outside.write_text("gizli", encoding="utf-8")
    try:
        (root / "src" / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlink oluşturulamıyor")
    result = await run(ctx, "move_files", source_dir="src", destination_dir="dst", pattern="link*")
    assert not result.success and result.data["files"][0]["status"] == "failed"
    assert outside.read_text(encoding="utf-8") == "gizli" and not (root / "dst/link.txt").exists()


async def test_move_files_tainted_turn_asks_approval(ctx: ToolContext) -> None:
    root = ctx.workspace
    _seed(root)
    gate = SafetyGate(discover_tools())
    state = TurnState()
    state.tainted = True
    asked: list[tuple[str, dict[str, Any]]] = []

    def deny(name: str, arguments: dict[str, Any]) -> bool:
        asked.append((name, arguments))
        return False

    call = ToolCall(
        id="m",
        name="move_files",
        arguments={"source_dir": "src", "destination_dir": "dst", "pattern": "*.txt"},
    )
    result = await gate.dispatch(call, ctx, state, deny)
    assert result.status == "denied" and not result.success
    assert asked and asked[0][1]["pattern"] == "*.txt"
    assert (root / "src/a.txt").exists() and not any((root / "dst").iterdir())


# --- D3 yol kuralı ---

PATH_TOOLS = (
    "list_files",
    "read_file",
    "create_file",
    "move_file",
    "rename_file",
    "delete_file",
    "create_directory",
    "copy_file",
    "move_files",
)


def test_path_rule_in_every_file_tool_description() -> None:
    tools = discover_tools()
    for name in PATH_TOOLS:
        tool = tools.get(name)
        assert tool is not None and tool.description.endswith(filesystem.PATH_RULE), name


async def test_absolute_path_error_teaches_correct_usage(ctx: ToolContext) -> None:
    result = await run(ctx, "list_files", path="C:\\Users\\x")
    assert not result.success and "göreli" in result.message and "yolsuz" in result.message
    assert (await run(ctx, "list_files", path="")).success  # boş yol = kök


def test_light_prompt_has_path_rule() -> None:
    from artemis.brain.light_mode import LIGHT_SYSTEM_PROMPT

    assert "mutlak" in LIGHT_SYSTEM_PROMPT and "boş" in LIGHT_SYSTEM_PROMPT
