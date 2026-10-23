"""MCP stdio istemcisi: gerçek alt süreç + depo içi sahte sunucu, mock yok."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from artemis.mcp import client as mcp_client
from artemis.mcp.client import MCPError, StdioMCPClient

SERVER = Path(__file__).with_name("mcp_fake_server.py")


def make_client(
    mode: str,
    *extra: str,
    timeout: float = 3.0,
    env: dict[str, str] | None = None,
    max_message_bytes: int = 1 << 20,
) -> StdioMCPClient:
    base_env = env if env is not None else {"PATH": os.environ.get("PATH", "")}
    return StdioMCPClient(
        [sys.executable, str(SERVER), mode, *extra],
        env=base_env,
        timeout=timeout,
        max_message_bytes=max_message_bytes,
    )


def test_list_tools_paginates_skips_invalid_and_keeps_annotations() -> None:
    tools = {tool.name: tool for tool in make_client("ok").list_tools()}
    assert set(tools) == {"echo", "env", "fail", "mixed"}  # "broken" atlandı
    assert tools["echo"].annotations == {"readOnlyHint": True}
    assert tools["fail"].annotations == {"destructiveHint": True}
    assert tools["fail"].description == ""
    assert tools["env"].annotations == {}
    assert tools["echo"].input_schema["required"] == ["text"]


def test_echo_returns_text_and_structured() -> None:
    result = make_client("ok").call_tool("echo", {"text": "merhaba ğüş"})
    assert not result.is_error
    assert result.text == "merhaba ğüş"
    assert result.structured == {"echo": "merhaba ğüş"}
    assert result.unsupported_blocks == 0


def test_child_env_has_only_given_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTEMIS_TEST_SECRET_TOKEN", "gizli")
    env = {"PATH": os.environ.get("PATH", ""), "ARTEMIS_TEST_PASS": "1"}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    result = make_client("ok", env=env).call_tool("env", {})
    keys = set(result.text.split(","))
    assert "ARTEMIS_TEST_PASS" in keys
    assert "ARTEMIS_TEST_SECRET_TOKEN" not in keys
    assert "gizli" not in result.text


def test_is_error_flag() -> None:
    result = make_client("ok").call_tool("fail", {})
    assert result.is_error
    assert result.text == "başarısız"


def test_unsupported_blocks_are_counted() -> None:
    result = make_client("ok").call_tool("mixed", {})
    assert result.text == "metin"
    assert result.unsupported_blocks == 1


def test_hang_times_out_within_bound() -> None:
    started = time.monotonic()
    with pytest.raises(MCPError) as info:
        make_client("hang", timeout=1.0).list_tools()
    assert info.value.error_type == "timeout"
    assert time.monotonic() - started < 3.0


def test_hang_call_times_out() -> None:
    with pytest.raises(MCPError) as info:
        make_client("hang_call", timeout=1.0).call_tool("echo", {"text": "x"})
    assert info.value.error_type == "timeout"


def test_call_timeout_is_capped_by_client_timeout() -> None:
    started = time.monotonic()
    with pytest.raises(MCPError) as info:
        make_client("hang", timeout=1.0).call_tool("echo", {"text": "x"}, timeout=30.0)
    assert info.value.error_type == "timeout"
    assert time.monotonic() - started < 3.0


@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("garbage", "protocol"),
        ("die", "process"),
        ("bad_version", "version"),
        ("remote_error", "remote"),
    ],
)
def test_failure_modes_map_to_error_types(mode: str, error_type: str) -> None:
    with pytest.raises(MCPError) as info:
        make_client(mode).call_tool("echo", {"text": "x"})
    assert info.value.error_type == error_type


def test_remote_error_message_is_user_safe() -> None:
    with pytest.raises(MCPError) as info:
        make_client("remote_error").call_tool("echo", {"text": "x"})
    assert "kötü" not in str(info.value)  # sunucu metni mesaja girmez
    assert str(SERVER) not in str(info.value)


def test_oversized_message_is_rejected() -> None:
    started = time.monotonic()
    with pytest.raises(MCPError) as info:
        make_client("big").call_tool("echo", {"text": "x"})
    assert info.value.error_type == "too_large"
    assert time.monotonic() - started < 6.0


def test_server_requests_are_answered() -> None:
    result = make_client("server_request").call_tool("echo", {"text": "x"})
    assert result.text == "x"


def test_notifications_and_foreign_ids_are_ignored() -> None:
    result = make_client("notify").call_tool("echo", {"text": "x"})
    assert result.text == "x"


def test_missing_command_is_process_error() -> None:
    client = StdioMCPClient(
        ["artemis-olmayan-komut-xyz"], env={"PATH": os.environ.get("PATH", "")}, timeout=1.0
    )
    with pytest.raises(MCPError) as info:
        client.list_tools()
    assert info.value.error_type == "process"
    assert "artemis-olmayan" not in str(info.value)


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) Windows'ta süreci öldürebilir; tasklist yalnız okur.
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_dead(pid: int, seconds: float = 3.0) -> bool:
    # Öldürülen süreç reap edilene dek kısa süre görünebilir; sınırlı yoklanır.
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_hang_child_process_tree_is_killed(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    with pytest.raises(MCPError) as info:
        make_client("hang_child", str(pid_file), timeout=1.5).list_tools()
    assert info.value.error_type == "timeout"
    assert wait_dead(int(pid_file.read_text(encoding="utf-8")))


@pytest.mark.skipif(os.name == "nt", reason="Windows'ta lider çıkınca taskkill /T ağacı bulamaz")
def test_orphan_grandchild_is_killed_after_clean_exit(tmp_path: Path) -> None:
    pid_file = tmp_path / "orphan.pid"
    started = time.monotonic()
    tools = make_client("orphan", str(pid_file), timeout=3.0).list_tools()
    elapsed = time.monotonic() - started
    assert tools
    assert elapsed < 1.5
    assert wait_dead(int(pid_file.read_text(encoding="utf-8")))


def test_deeply_nested_json_is_protocol_error() -> None:
    with pytest.raises(MCPError) as info:
        make_client("deep", max_message_bytes=1 << 20).list_tools()
    assert info.value.error_type == "protocol"


def test_cancel_stops_call_and_kills_server(tmp_path: Path) -> None:
    pid_file = tmp_path / "server.pid"
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(MCPError) as info:
        make_client("hang_call", str(pid_file), timeout=5.0).call_tool(
            "echo", {"text": "x"}, cancel=cancel
        )
    assert info.value.error_type == "cancelled"
    assert time.monotonic() - started < 1.5
    assert wait_dead(int(pid_file.read_text(encoding="utf-8")))


def test_notification_flood_hits_session_budget() -> None:
    with pytest.raises(MCPError) as info:
        make_client("flood", timeout=5.0).call_tool("echo", {"text": "x"})
    assert info.value.error_type == "too_large"


def test_command_in_working_directory_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "fakecmd.exe" if os.name == "nt" else "fakecmd"
    exe = tmp_path / name
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    client = StdioMCPClient(["fakecmd"], env={"PATH": str(tmp_path)}, timeout=1.0)
    with pytest.raises(MCPError) as info:
        client.list_tools()
    assert info.value.error_type == "process"
    assert "çalışma dizini" in str(info.value).lower()


@pytest.mark.parametrize("bad", ['a"b', "a&b", "a|b", "a<b", "a>b", "a^b", "a%b", "a!b"])
def test_batch_files_reject_metacharacters(bad: str) -> None:
    with pytest.raises(MCPError) as info:
        mcp_client.validate_command_arguments("C:\\x\\npx.cmd", ["ok", bad])
    assert info.value.error_type == "process"


def test_batch_rule_ignores_executables_and_clean_arguments() -> None:
    mcp_client.validate_command_arguments("C:\\x\\tool.exe", ["a&b"])
    mcp_client.validate_command_arguments("C:\\x\\npx.CMD", ["-y", "paket@1.0"])
