"""Artemis'in kendi hafıza dosyaları (ADR 0054); her şey tmp_path altında."""

from pathlib import Path
from typing import Any

import pytest

from artemis.brain.agent_loop import COMPANION_CHARS, COMPANION_HEADER, companion_block
from artemis.brain.models import ToolCall
from artemis.config.settings import CodeAgentConfig, MemoryConfig, Settings
from artemis.memory.companion import (
    COMPANION_FILES,
    companion_dir,
    ensure_skeleton,
    read_companion,
)
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.base import ToolContext
from artemis.tools.registry import discover_tools


def make_context(tmp_path: Path, **overrides: Any) -> ToolContext:
    workspace = tmp_path / "desktop"
    workspace.mkdir(exist_ok=True)
    settings = Settings(
        workspace=workspace,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        **overrides,
    )
    return ToolContext(workspace, settings, AuditLog(settings.audit_path))


async def dispatch(context: ToolContext, state: TurnState, name: str, **arguments: Any):
    gate = SafetyGate(discover_tools())
    call = ToolCall(id="c1", name=name, arguments=arguments)
    return await gate.dispatch(call, context, state, lambda *_: False)


def test_skeleton_creates_three_files_and_never_overwrites(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert sorted(ensure_skeleton(state)) == sorted(COMPANION_FILES.values())
    own = companion_dir(state) / "Isler.md"
    own.write_text("benim işlerim", encoding="utf-8")
    assert ensure_skeleton(state) == []
    assert own.read_text(encoding="utf-8") == "benim işlerim"


def test_skeleton_refuses_symlink_dir_and_file(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (state / "hafiza").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Bu platformda symlink yetkisi yok: {error}")
    with pytest.raises(OSError, match="sembolik"):
        ensure_skeleton(state)
    assert list(outside.iterdir()) == []
    (state / "hafiza").unlink()
    ensure_skeleton(state)
    (companion_dir(state) / "Kimlik.md").unlink()
    (companion_dir(state) / "Kimlik.md").symlink_to(outside / "x.md")
    with pytest.raises(OSError, match="sembolik"):
        ensure_skeleton(state)
    assert read_companion(state, "kimlik", 100) == ""


def test_read_companion_missing_is_empty(tmp_path: Path) -> None:
    assert read_companion(tmp_path, "isler", 100) == ""


def test_companion_block_budget_and_truncation(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    ensure_skeleton(context.settings.state_dir)
    folder = companion_dir(context.settings.state_dir)
    for name in COMPANION_FILES.values():
        (folder / name).write_text("x" * 5000, encoding="utf-8")
    block = companion_block(context)
    assert COMPANION_HEADER in block
    assert block.count("…[kısaltıldı]") == 3
    assert len(block) <= COMPANION_CHARS + len(COMPANION_HEADER) + 4


def test_companion_block_empty_without_files_or_on_read_error(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    assert companion_block(context) == ""
    ensure_skeleton(context.settings.state_dir)
    (companion_dir(context.settings.state_dir) / "Kimlik.md").write_bytes(b"\xff\xfe\x00bad")
    assert "Kimlik" not in companion_block(context)  # çözülemeyen dosya sessizce atlanır


async def test_open_work_own_files_only_is_not_external(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    ensure_skeleton(context.settings.state_dir)
    state = TurnState()
    result = await dispatch(context, state, "memory_open_work")
    assert result.success and result.external is False
    assert result.data["vault_sources"] == []
    assert set(result.data["sources"]) == {"isler", "son_oturum"}
    assert state.tainted is False


async def test_open_work_with_vault_file_taints_turn(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "850-Companion").mkdir(parents=True)
    (vault / "850-Companion" / "Threads.md").write_text("Açık: ses testi", encoding="utf-8")
    context = make_context(
        tmp_path,
        memory=MemoryConfig(
            vault_path=vault,
            companion_vault_files=("850-Companion/Threads.md", "yok.md"),
        ),
    )
    state = TurnState()
    result = await dispatch(context, state, "memory_open_work")
    assert result.success and result.external is True and result.content_kind == "file"
    assert result.data["vault_sources"] == ["850-Companion/Threads.md"]
    assert "ses testi" in result.data["content"]["850-Companion/Threads.md"]
    assert state.tainted is True


@pytest.mark.parametrize("rel", ["../x.md", "/etc/passwd", "C:/x.md", "a/../../x.md"])
async def test_open_work_rejects_path_escape(tmp_path: Path, rel: str) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "x.md").write_text("gizli", encoding="utf-8")
    context = make_context(
        tmp_path, memory=MemoryConfig(vault_path=vault, companion_vault_files=(rel,))
    )
    result = await dispatch(context, TurnState(), "memory_open_work")
    assert result.success is False and result.external is False
    assert "gizli" not in result.model_dump_json()


async def test_open_work_honest_when_nothing_to_read(tmp_path: Path) -> None:
    result = await dispatch(make_context(tmp_path), TurnState(), "memory_open_work")
    assert result.success is False


async def test_project_context_heads_and_external(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("K" * 3000, encoding="utf-8")
    (repo / "README.md").write_text("okuBeni", encoding="utf-8")
    context = make_context(
        tmp_path, code_agent=CodeAgentConfig(projects={"demo": repo})
    )
    state = TurnState()
    result = await dispatch(context, state, "code_agent_project_context", project="demo")
    assert result.success and result.external is True
    assert len(result.data["files"]["CLAUDE.md"]) == 1000
    assert result.data["files"]["README.md"] == "okuBeni"
    assert state.tainted is True


async def test_project_context_unknown_project_and_no_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    context = make_context(
        tmp_path, code_agent=CodeAgentConfig(projects={"demo": repo})
    )
    unknown = await dispatch(context, TurnState(), "code_agent_project_context", project="yok")
    assert unknown.success is False
    empty = await dispatch(context, TurnState(), "code_agent_project_context", project="demo")
    assert empty.success is False
