"""MCP stdio istemcisi: gerçek alt süreç + depo içi sahte sunucu, mock yok."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from jarvis.mcp.client import MCPError, StdioMCPClient

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
    monkeypatch.setenv("JARVIS_TEST_SECRET_TOKEN", "gizli")
    env = {"PATH": os.environ.get("PATH", ""), "JARVIS_TEST_PASS": "1"}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    result = make_client("ok", env=env).call_tool("env", {})
    keys = set(result.text.split(","))
    assert "JARVIS_TEST_PASS" in keys
    assert "JARVIS_TEST_SECRET_TOKEN" not in keys
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
    assert "kötü" in str(info.value)
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
        ["jarvis-olmayan-komut-xyz"], env={"PATH": os.environ.get("PATH", "")}, timeout=1.0
    )
    with pytest.raises(MCPError) as info:
        client.list_tools()
    assert info.value.error_type == "process"
    assert "jarvis-olmayan" not in str(info.value)


@pytest.mark.skipif(os.name == "nt", reason="PID yaşam kontrolü POSIX os.kill(pid, 0) ile")
def test_hang_child_process_tree_is_killed(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    with pytest.raises(MCPError) as info:
        make_client("hang_child", str(pid_file), timeout=1.5).list_tools()
    assert info.value.error_type == "timeout"
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    # Öldürülen alt süreç init'e devredilir; reap edilene dek kısa süre zombi görünebilir.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("alt süreç temizlenmedi")
