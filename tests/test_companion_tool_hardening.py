"""Hafıza yazma araçlarının sertleştirme testleri (makbuz bölümü ve leke yayılımı)."""

from pathlib import Path
from typing import Any

from artemis.brain.models import ToolCall
from artemis.memory.companion import (
    ensure_skeleton,
    read_companion,
    write_companion,
)
from artemis.safety.audit import AuditLog
from artemis.safety.gate import SafetyGate, TurnState
from artemis.tools.base import ToolContext
from artemis.tools.code_agent_jobs import CodeAgentJobs
from artemis.tools.registry import discover_tools
from tests.test_companion import make_context


async def dispatch(context: ToolContext, state: TurnState, name: str, **arguments: Any):
    """Onay isteyen çağrılar onaylanır; reddetme davranışı kapı testlerinde ölçülür."""
    gate = SafetyGate(discover_tools())
    call = ToolCall(id="c1", name=name, arguments=arguments)
    return await gate.dispatch(call, context, state, lambda *_: True)


def finished_job(tmp_path: Path, job_id: str, status: str = "done") -> CodeAgentJobs:
    jobs = CodeAgentJobs(AuditLog(tmp_path / "jobs-audit.jsonl"))
    jobs._jobs[job_id] = {
        "job_id": job_id, "project": "web", "status": status, "summary": "Özet\nsatırı " * 30,
        "commits": ["a1b2c3d4e5f6 konu"], "created_at": 1_700_049_600.0,
    }
    return jobs


async def test_session_update_strips_model_job_receipt_section(tmp_path: Path) -> None:
    # Mevcut kodda başlık model metninde varsa gerçek makbuz ezilir (SAHTE kalır, GERÇEK yok).
    context = make_context(tmp_path)
    state_dir = context.settings.state_dir
    ensure_skeleton(state_dir)
    write_companion(
        state_dir,
        "isler",
        "# İşler\n- a\n\n## Tamamlanan kod işleri\n- GERÇEK\n",
        source="test",
        turn_id="t0",
        tainted=False,
    )
    result = await dispatch(
        context,
        TurnState(),
        "memory_session_update",
        isler="# İşler\n- b\n\n## Tamamlanan kod işleri\n- SAHTE\n",
    )
    assert result.success is True
    text = read_companion(state_dir, "isler", 1000)
    assert "- GERÇEK" in text
    assert "- SAHTE" not in text
    assert text.count("## Tamamlanan kod işleri") == 1
    assert "- b" in text
