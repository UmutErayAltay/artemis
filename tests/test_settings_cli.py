"""Config hataları ve onay varsayılanı sır veya yan etki üretmemelidir."""

import asyncio
import json
import signal
from pathlib import Path

import pytest
from pydantic import ValidationError

from artemis.brain.models import Completion
from artemis.channels import cli
from artemis.channels.cli import request_approval
from artemis.config.settings import ModelConfig, Settings, load_settings
from artemis.tools.registry import ToolRegistry


def test_secrets_are_not_config_fields():
    with pytest.raises(ValidationError):
        Settings.model_validate({"api_key": "yer-tutucu"})


def test_config_paths_are_relative_to_config_file(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("workspace: desktop\n", encoding="utf-8")
    assert load_settings(config).workspace == tmp_path / "desktop"


def test_example_has_no_guessed_model_or_price():
    settings = load_settings(Path("config/config.example.yaml"))
    assert not settings.models
    assert not settings.allow_paid
    assert not settings.allow_cloud_file_content
    assert settings.provider_timeout_seconds == 15


@pytest.mark.parametrize("timeout", [0, -1, 3600.1, float("inf"), float("nan")])
def test_provider_timeout_is_bounded(timeout):
    with pytest.raises(ValidationError):
        Settings(provider_timeout_seconds=timeout)


@pytest.mark.parametrize("wake_word", ["", "   ", "artemis\n", "a\x00b"])
def test_voice_wake_word_rejects_empty_and_control_text(wake_word):
    with pytest.raises(ValidationError):
        Settings.model_validate({"voice": {"wake_word": wake_word}})


@pytest.mark.parametrize("address", ["ftp://host", "http://public.example", "https://a:b@host"])
def test_invalid_provider_endpoints(address):
    with pytest.raises(ValidationError):
        Settings(openrouter_base_url=address)


def test_model_requires_explicit_prices():
    with pytest.raises(ValidationError):
        ModelConfig.model_validate({"provider": "openrouter", "model": "test"})


@pytest.mark.parametrize("answer,expected", [("", False), ("evet", True), ("e", False)])
def test_cli_confirmation_displays_tool_and_arguments(monkeypatch, capsys, answer, expected):
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    assert request_approval("delete_file", {"path": "önemli.txt"}) is expected
    out = capsys.readouterr().out
    assert "delete_file" in out and "önemli.txt" in out


def test_cli_approval_interrupt_signals_active_turn(monkeypatch, capsys):
    interrupted = []

    def interrupt(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    result = request_approval(
        "delete_file", {"path": "önemli.txt"}, on_interrupt=lambda: interrupted.append(True)
    )
    assert result is False
    assert interrupted == [True]
    assert "tur iptal ediliyor" in capsys.readouterr().out


async def test_cli_repeated_sigint_cancels_each_turn_and_restores_handler(tmp_path, monkeypatch):
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        primary_model="fake",
        models={
            "fake": ModelConfig(
                provider="ollama",
                model="fake",
                input_per_million=0,
                output_per_million=0,
            )
        },
    )
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings)
    monkeypatch.setattr(cli, "discover_tools", ToolRegistry)
    previous_handler = signal.getsignal(signal.SIGINT)
    handlers = []

    def set_handler(_signum, handler):
        handlers.append(handler)
        return previous_handler

    monkeypatch.setattr(cli.signal, "signal", set_handler)
    wakes = []
    original_wake = asyncio.BaseEventLoop.call_soon_threadsafe

    def track_wake(loop, callback, *args, **kwargs):
        if getattr(callback, "__name__", "") == "set":
            wakes.append(True)
        return original_wake(loop, callback, *args, **kwargs)

    monkeypatch.setattr(asyncio.BaseEventLoop, "call_soon_threadsafe", track_wake)

    class Provider:
        def __init__(self):
            self.calls = 0
            self.cleaned = 0

        async def complete(self, messages, tools, *, timeout):
            self.calls += 1
            handlers[-1](signal.SIGINT, None)
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cleaned += 1
                raise

    provider = Provider()
    monkeypatch.setattr(cli, "build_provider", lambda *_args: provider)
    prompts = iter(["birinci tur", "ikinci tur", "/çık"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(prompts))

    await cli.chat(tmp_path / "config.yaml")

    assert provider.calls == 2
    assert provider.cleaned == 2
    assert len(wakes) == 2
    assert handlers[0] is not previous_handler
    assert handlers[-1] is previous_handler


async def test_cli_yeni_resets_history_without_starting_a_turn(tmp_path, monkeypatch, capsys):
    settings = Settings(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        audit_path=tmp_path / "audit.jsonl",
        primary_model="fake",
        models={
            "fake": ModelConfig(
                provider="ollama",
                model="fake",
                input_per_million=0,
                output_per_million=0,
            )
        },
    )
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings)
    monkeypatch.setattr(cli, "discover_tools", ToolRegistry)
    previous_handler = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(cli.signal, "signal", lambda _signum, _handler: previous_handler)

    class Provider:
        def __init__(self):
            self.requests = []

        async def complete(self, messages, tools, *, timeout):
            self.requests.append([message.model_copy(deep=True) for message in messages])
            return Completion(content=f"yanıt {len(self.requests)}")

    provider = Provider()
    monkeypatch.setattr(cli, "build_provider", lambda *_args: provider)
    prompts = iter(["birinci", "/yeni", "ikinci", "/çık"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(prompts))

    await cli.chat(tmp_path / "config.yaml")

    # /yeni modele gitmez; yalnız sonraki turun geçmişini boşaltır.
    assert len(provider.requests) == 2
    assert [m.content for m in provider.requests[1] if m.role == "user"] == ["ikinci"]
    assert "Yeni sohbet başladı" in capsys.readouterr().out
    audit_lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in audit_lines]
    resets = [record for record in records if record["event"] == "context_reset"]
    assert len(resets) == 1 and resets[0]["removed_turns"] == 1
