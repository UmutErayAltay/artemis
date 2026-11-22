"""MCP ve claude_cli süreçleri Windows job'una bağlanır; çekirdek sahte kernel ile sınanır."""

import asyncio
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from artemis.brain import providers
from artemis.mcp import client as mcp_client
from artemis.tools import code_agent_process
from artemis.tools.code_agent_process import own_process_tree

windows_only = pytest.mark.skipif(os.name != "nt", reason="Job Object yalnız Windows'ta")


class FakeTree:
    def __init__(self, result: bool = True) -> None:
        self.kills = 0
        self.result = result
        self.proc: Any = None

    def kill(self) -> bool:
        self.kills += 1
        if self.result and self.proc is not None:
            self.proc.kill()
            self.proc.wait(timeout=2)
        return self.result


def fake_kernel() -> MagicMock:
    kernel = MagicMock()
    kernel.CreateJobObjectW.return_value = 77
    kernel.SetInformationJobObject.return_value = 1
    kernel.AssignProcessToJobObject.return_value = 1
    kernel.TerminateJobObject.return_value = 1
    kernel.CloseHandle.return_value = 1
    kernel.QueryInformationJobObject.return_value = 1  # active=0: boş
    return kernel


@windows_only
def test_helper_creates_and_assigns_job_for_popen_and_asyncio_wrapper() -> None:
    popen = SimpleNamespace(_handle=1234, pid=5)
    asyncio_proc = SimpleNamespace(_transport=SimpleNamespace(get_extra_info=lambda _k: popen))
    for proc in (popen, asyncio_proc):
        kernel = fake_kernel()
        tree = own_process_tree(proc, kernel=kernel)
        assert tree is not None
        kernel.CreateJobObjectW.assert_called_once()
        assert kernel.AssignProcessToJobObject.call_args.args[1] == 1234
        kernel.ResumeThread.assert_not_called()  # askıda başlamadı
        assert tree.kill() is True
        kernel.TerminateJobObject.assert_called_once()


@windows_only
def test_helper_assignment_failure_falls_back_to_none() -> None:
    kernel = fake_kernel()
    kernel.AssignProcessToJobObject.return_value = 0
    assert own_process_tree(SimpleNamespace(_handle=1, pid=5), kernel=kernel) is None
    kernel.CloseHandle.assert_called()  # yarım job sızdırılmadı


@windows_only
def test_helper_suspended_assignment_failure_kills_proc_and_raises() -> None:
    kernel = fake_kernel()
    kernel.AssignProcessToJobObject.return_value = 0
    proc = SimpleNamespace(_handle=1, pid=5, kill=MagicMock(), wait=MagicMock())
    with pytest.raises(OSError):
        own_process_tree(proc, kernel=kernel, suspended=True)
    kernel.CloseHandle.assert_called()
    proc.kill.assert_called_once()
    proc.wait.assert_called_once()


def test_helper_is_none_without_handle_or_off_windows() -> None:
    assert own_process_tree(SimpleNamespace(pid=1)) is None  # FakeProc benzeri


def test_helper_noop_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_agent_process.os, "name", "posix")
    assert own_process_tree(SimpleNamespace(_handle=1, pid=5)) is None


def _session(monkeypatch: pytest.MonkeyPatch, tree: Any) -> mcp_client._Session:
    seen: list[tuple[Any, bool]] = []

    def fake_own(proc: Any, *, suspended: bool = False) -> Any:
        seen.append((proc, suspended))
        if hasattr(tree, "proc"):
            tree.proc = proc
        return tree

    monkeypatch.setattr(mcp_client, "own_process_tree", fake_own)
    server = os.path.join(os.path.dirname(__file__), "mcp_fake_server.py")
    session = mcp_client._Session(
        [sys.executable, server, "ok"],
        {"PATH": os.environ.get("PATH", "")},
        None,
        time.monotonic() + 5,
        1 << 20,
    )
    assert isinstance(seen[0][0], subprocess.Popen)  # spawn sonrası bağlandı
    assert seen[0][1] is True
    return session


def test_mcp_job_failure_cleans_up_suspended_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_own(proc: Any, *, suspended: bool = False) -> Any:
        proc.kill()
        proc.wait(timeout=2)
        raise OSError("assign failed")

    monkeypatch.setattr(mcp_client, "own_process_tree", fail_own)
    server = os.path.join(os.path.dirname(__file__), "mcp_fake_server.py")
    with pytest.raises(mcp_client.MCPError) as info:
        mcp_client._Session(
            [sys.executable, server, "ok"],
            {"PATH": os.environ.get("PATH", "")},
            None,
            time.monotonic() + 5,
            1 << 20,
        )
    assert info.value.error_type == "process"


def test_mcp_job_assigned_on_spawn_and_terminated_on_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = FakeTree()
    _session(monkeypatch, tree).close()
    assert tree.kills >= 1


def test_mcp_failed_job_kill_falls_back_to_kill_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = FakeTree(result=False)
    called: list[int] = []
    real = mcp_client.kill_tree
    monkeypatch.setattr(mcp_client, "kill_tree", lambda p: called.append(p.pid) or real(p))
    session = _session(monkeypatch, tree)
    session.close(graceful=False)
    assert tree.kills >= 1 and called  # job başarısız -> kill_tree


async def test_claude_cli_job_terminated_after_call(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_claude_cli import FakeProc, ok, provider

    proc = FakeProc(ok(content="Tamam"))
    tree = FakeTree()

    async def fake_exec(*_a: str, **_k: Any) -> FakeProc:
        return proc

    monkeypatch.setattr(providers.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(providers, "own_process_tree", lambda p: tree)
    from artemis.brain.models import Message

    await provider(tmp_path).complete([Message(role="user", content="a")], [], timeout=5)
    assert tree.kills == 1


@pytest.mark.disruptive
@windows_only
def test_real_job_kills_grandchild_when_closed() -> None:
    code = (
        "import subprocess,sys,time;"
        "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(c.pid,flush=True);time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    tree = own_process_tree(proc)
    assert tree is not None
    try:
        grandchild = int(proc.stdout.readline())  # type: ignore[union-attr]
        assert tree.kill() is True
        proc.wait(timeout=5)
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {grandchild}"], capture_output=True, text=True
        ).stdout
        assert str(grandchild) not in out
    finally:
        proc.kill()
