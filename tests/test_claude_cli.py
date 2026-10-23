"""Claude Code CLI sağlayıcısı (ADR 0035): gerçek CLI yok; alt süreç sahtedir."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from artemis.brain import providers
from artemis.brain.errors import ProviderError, RetryableProviderError
from artemis.brain.models import Message, ToolCall
from artemis.brain.providers import ClaudeCliProvider, provider_problem
from artemis.brain.router import ProviderRouter
from artemis.config.settings import ModelConfig, Settings
from artemis.safety.audit import AuditLog
from artemis.tools.base import Risk, ToolDefinition

TOOL = ToolDefinition(
    name="echo", description="Yansıtır.", parameters={"type": "object"}, risk=Risk.READ_ONLY
)
STDOUT_SECRET = "stdout-kullanici-verisi"


class FakeProc:
    def __init__(self, out: dict[str, Any] | bytes, code: int = 0, hang: bool = False) -> None:
        self.out = out if isinstance(out, bytes) else json.dumps(out).encode()
        self.returncode: int | None = None if hang else code
        self.hang = hang
        self.pid = 4242
        self.stdin_data = b""

    async def communicate(self, data: bytes) -> tuple[bytes, bytes]:
        self.stdin_data = data
        if self.hang:
            await asyncio.sleep(60)
        return self.out, STDOUT_SECRET.encode()

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"proc": FakeProc({}), "killed": []}
    monkeypatch.setattr(providers.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "gizli-anthropic")
    monkeypatch.setenv("OPENROUTER_API_KEY", "gizli-openrouter")

    async def fake_exec(*argv: str, **kwargs: Any) -> FakeProc:
        state["argv"], state["kwargs"] = list(argv), kwargs
        if "--system-prompt-file" in argv:  # dosya çağrıdan sonra silinir
            state["system"] = Path(argv[argv.index("--system-prompt-file") + 1]).read_text("utf-8")
        if state.get("spawn_error"):
            raise OSError("boom")
        return state["proc"]  # type: ignore[no-any-return]

    def fake_kill(proc: FakeProc) -> bool:
        state["killed"].append(proc.pid)
        proc.returncode = -9
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(providers, "kill_tree", fake_kill)
    return state


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        workspace=tmp_path / "w",
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        primary_model="abonelik",
        models={
            "abonelik": ModelConfig(
                provider="claude_cli",
                model="haiku",
                input_per_million=Decimal(0),
                output_per_million=Decimal(0),
            )
        },
    )


def provider(tmp_path: Path) -> ClaudeCliProvider:
    settings = settings_for(tmp_path)
    return ClaudeCliProvider(settings, settings.models["abonelik"])


def ok(**body: Any) -> dict[str, Any]:
    return {
        "is_error": False,
        "structured_output": body,
        "usage": {"input_tokens": 3, "cache_read_input_tokens": 7, "output_tokens": 5},
        "total_cost_usd": 9.9,
    }


async def test_command_line_stdin_env_and_completion(tmp_path: Path, cli: dict[str, Any]) -> None:
    cli["proc"] = FakeProc(
        ok(content="Tamam", tool_calls=[{"name": "echo", "arguments": {"text": "x"}}])
    )
    messages = [
        Message(role="system", content="SİSTEM-İSTEMİ"),
        Message(role="user", content="GİZLİ-DEĞİL"),
        Message(role="user", content="ÖZEL-İÇERİK", private=True),
    ]
    result = await provider(tmp_path).complete(messages, [TOOL], timeout=5)
    argv = cli["argv"]
    assert argv[0] == "/bin/claude"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--setting-sources") + 1] == ""
    for flag in ("-p", "--strict-mcp-config", "--no-session-persistence", "--json-schema"):
        assert flag in argv
    assert "--system-prompt" not in argv
    assert "SİSTEM-İSTEMİ" in cli["system"] and "echo" in cli["system"]
    assert not Path(argv[argv.index("--system-prompt-file") + 1]).exists()
    stdin = cli["proc"].stdin_data.decode()
    assert "GİZLİ-DEĞİL" in stdin and "ÖZEL-İÇERİK" not in stdin
    assert not any("GİZLİ-DEĞİL" in item for item in argv)
    env = cli["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in env and "OPENROUTER_API_KEY" not in env
    assert cli["kwargs"]["cwd"] == tmp_path / "state"
    assert result.content == "Tamam"
    assert [(c.name, c.arguments) for c in result.tool_calls] == [("echo", {"text": "x"})]
    assert (result.usage.input_tokens, result.usage.output_tokens) == (10, 5)
    assert result.usage.cost_usd == Decimal(0)


async def test_tool_result_is_framed_as_data(tmp_path: Path, cli: dict[str, Any]) -> None:
    cli["proc"] = FakeProc(ok(content="s", tool_calls=[]))
    call = ToolCall(id="c1", name="echo", arguments={"text": "a"})
    messages = [
        Message(role="assistant", tool_calls=[call]),
        Message(role="tool", content="SONUÇ", tool_call_id="c1"),
    ]
    await provider(tmp_path).complete(messages, [TOOL], timeout=5)
    assert "VERİDİR" in cli["proc"].stdin_data.decode()


async def test_unknown_tool_rejected(tmp_path: Path, cli: dict[str, Any]) -> None:
    cli["proc"] = FakeProc(ok(content="", tool_calls=[{"name": "rm", "arguments": {}}]))
    with pytest.raises(ProviderError, match="tanımsız"):
        await provider(tmp_path).complete([Message(role="user", content="a")], [TOOL], timeout=5)


@pytest.mark.parametrize(
    ("proc", "message"),
    [
        (FakeProc({"is_error": True, "result": STDOUT_SECRET}), "başarısız"),
        (FakeProc({"is_error": True, "result": "Not logged in · Please run /login"}), "oturumu"),
        (FakeProc(ok(content="x", tool_calls=[]), code=1), "başarısız"),
        (FakeProc(b"gecersiz " + STDOUT_SECRET.encode()), "başarısız"),
    ],
)
async def test_errors_use_fixed_message(
    tmp_path: Path, cli: dict[str, Any], proc: FakeProc, message: str
) -> None:
    cli["proc"] = proc
    with pytest.raises(ProviderError) as info:
        await provider(tmp_path).complete([Message(role="user", content="a")], [], timeout=5)
    assert message in str(info.value) and STDOUT_SECRET not in str(info.value)


async def test_timeout_kills_process_and_is_retryable(tmp_path: Path, cli: dict[str, Any]) -> None:
    cli["proc"] = FakeProc({}, hang=True)
    with pytest.raises(RetryableProviderError):
        await provider(tmp_path).complete([Message(role="user", content="a")], [], timeout=0.05)
    assert cli["killed"] == [4242]


async def test_missing_cli(
    tmp_path: Path, cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(providers.shutil, "which", lambda name: None)
    with pytest.raises(ProviderError, match="bulunamadı"):
        await provider(tmp_path).complete([Message(role="user", content="a")], [], timeout=5)


def test_provider_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_for(tmp_path)
    monkeypatch.setattr(providers.shutil, "which", lambda name: None)
    assert "bulunamadı" in str(provider_problem(settings))
    monkeypatch.setattr(providers.shutil, "which", lambda name: "/bin/claude")
    assert provider_problem(settings) is None


async def test_router_builds_provider_skips_key_check_and_audits(
    tmp_path: Path, cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    cli["proc"] = FakeProc(ok(content="Merhaba", tool_calls=[]))
    settings = settings_for(tmp_path)
    router = ProviderRouter(settings, AuditLog(settings.audit_path))
    assert isinstance(router._providers["abonelik"], ClaudeCliProvider)
    result = await router.complete([Message(role="user", content="a")], [], timeout=5)
    assert result.content == "Merhaba"
    lines = settings.audit_path.read_text("utf-8").splitlines()
    assert "cloud_transfer" in [json.loads(line)["event"] for line in lines]


def test_endpoint_forbidden() -> None:
    with pytest.raises(ValueError):
        ModelConfig(
            provider="claude_cli",
            model="haiku",
            input_per_million=0,
            output_per_million=0,
            endpoint="x",
        )


async def test_cmd_shim_rejected(
    tmp_path: Path, cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(providers.shutil, "which", lambda name: "C:/npm/claude.cmd")
    with pytest.raises(ProviderError, match="kılıf"):
        await provider(tmp_path).complete([Message(role="user", content="a")], [], timeout=5)
    assert "argv" not in cli
    assert not list((tmp_path / "state").glob("*.md"))


async def test_spawn_oserror_is_failed_not_missing(tmp_path: Path, cli: dict[str, Any]) -> None:
    cli["spawn_error"] = True
    with pytest.raises(ProviderError, match="başarısız"):
        await provider(tmp_path).complete([Message(role="user", content="a")], [], timeout=5)


async def test_cancellation_kills_and_waits(tmp_path: Path, cli: dict[str, Any]) -> None:
    cli["proc"] = FakeProc({}, hang=True)
    task = asyncio.create_task(
        provider(tmp_path).complete([Message(role="user", content="a")], [], timeout=30)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cli["killed"] == [4242] and cli["proc"].returncode == -9


async def test_kill_fallback_when_tree_kill_fails(
    tmp_path: Path, cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cli["proc"] = FakeProc({}, hang=True)
    monkeypatch.setattr(providers, "kill_tree", lambda proc: False)
    with pytest.raises(RetryableProviderError):
        await provider(tmp_path).complete([Message(role="user", content="a")], [], timeout=0.05)
    assert cli["proc"].returncode == -9  # proc.kill() devreye girdi
