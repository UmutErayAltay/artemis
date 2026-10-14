"""WinAPI sahiplik sırası sahte kernel ile; gerçek Windows kabulünün yerine geçmez."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.tools.code_agent_windows import WindowsProcessTree


class FakeCall:
    def __init__(self, handler: Any) -> None:
        self.handler = handler

    def __call__(self, *args: Any) -> Any:
        return self.handler(*args)


class FakeKernel:
    def __init__(self, *, active: list[int] | None = None, resume: int = 1) -> None:
        self.events: list[str] = []
        self.active = list(active or [0])
        self.CreateJobObjectW = FakeCall(lambda *args: self.event("create", 10))
        self.SetInformationJobObject = FakeCall(lambda *args: self.event("limits", 1))
        self.AssignProcessToJobObject = FakeCall(lambda *args: self.event("assign", 1))
        self.CreateToolhelp32Snapshot = FakeCall(lambda *args: self.event("snapshot", 11))
        self.Thread32First = FakeCall(self.first)
        self.Thread32Next = FakeCall(lambda *args: 0)
        self.OpenThread = FakeCall(lambda *args: self.event("open_thread", 12))
        self.ResumeThread = FakeCall(lambda *args: self.event("resume", resume))
        self.CloseHandle = FakeCall(lambda handle: self.event(f"close_{handle}", 1))
        self.TerminateJobObject = FakeCall(lambda *args: self.event("terminate", 1))
        self.QueryInformationJobObject = FakeCall(self.query)

    def event(self, name: str, result: int) -> int:
        self.events.append(name)
        return result

    def first(self, snapshot: Any, entry: Any) -> int:
        entry._obj.process_id = 7
        entry._obj.thread_id = 8
        return 1

    def query(self, job: Any, kind: Any, info: Any, size: Any, output: Any) -> int:
        self.events.append("query")
        info._obj.active = self.active.pop(0) if len(self.active) > 1 else self.active[0]
        return 1


def test_windows_suspended_creation_assign_before_resume_then_verify_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jarvis.tools.code_agent_process as module

    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    assert module.process_options()["creationflags"] & 0x4
    kernel = FakeKernel(active=[2, 1, 0])
    tree = WindowsProcessTree(SimpleNamespace(pid=7, _handle=123), kernel=kernel)
    assert kernel.events.index("assign") < kernel.events.index("resume")
    assert tree.kill()
    assert kernel.events.count("query") == 3
    assert kernel.events.index("terminate") < kernel.events.index("close_10")


def test_windows_active_processes_never_report_cleanup_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jarvis.tools.code_agent_windows.CLEANUP_SECONDS", 0.02)
    tree = WindowsProcessTree(SimpleNamespace(pid=7, _handle=123), kernel=FakeKernel(active=[1]))
    assert not tree.kill()


def test_windows_resume_failure_kills_owned_job() -> None:
    kernel = FakeKernel(resume=0xFFFFFFFF)
    with pytest.raises(OSError, match="devam ettirilemedi"):
        WindowsProcessTree(SimpleNamespace(pid=7, _handle=123), kernel=kernel)
    assert "terminate" in kernel.events and "close_10" in kernel.events
